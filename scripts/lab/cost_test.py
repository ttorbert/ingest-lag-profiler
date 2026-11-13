#!/usr/bin/env python3
"""Measure the miss-rate query's cost in the lab.

Loads N synthetic events into logs-lagcost.events-default, spread over one past clock
hour by event.ingested, with realistic lag. Then runs the pack's miss-rate query
(esql/miss-rate.esql) against that hour while K rules search the stream, and reports
the query time. The rule pairs go in a separate lookup index, lagprof-cost-rule-streams,
so the hourly workflow's cleanup can't touch them.

    cost_test.py load --events 1000000      add events (repeat to grow the hour)
    cost_test.py run --rules 1 5 20         time the query for each rule count
    cost_test.py clean                      delete the test data stream and lookup index

Lab only. Run it when nothing else is being measured: the load is heavy.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lagprof  # noqa: E402

STREAM = "logs-lagcost.events-default"
LOOKUP = "lagprof-cost-rule-streams"
# The measured hour: two hours before the first load, truncated, so it's a complete past
# hour. Saved on first load so later runs measure the same hour.
HOUR_FILE = lagprof.PACK_ROOT / "build" / "cost-hour"
if HOUR_FILE.exists():
    HOUR = datetime.fromisoformat(HOUR_FILE.read_text().strip())
else:
    HOUR = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)


def client() -> lagprof.Client:
    cfg = lagprof.load_config("targets/docker.env")
    if cfg.target != "docker":
        raise SystemExit("Lab only.")
    return lagprof.Client(cfg)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cmd_load(c: lagprof.Client, events: int, batch: int, seed: int) -> int:
    HOUR_FILE.parent.mkdir(exist_ok=True)
    HOUR_FILE.write_text(HOUR.isoformat())
    rng = random.Random(seed)
    hosts = [f"cost-host-{i:03d}" for i in range(200)]
    sent, t0 = 0, time.time()
    while sent < events:
        n = min(batch, events - sent)
        lines = []
        for _ in range(n):
            ingested = HOUR + timedelta(seconds=rng.random() * 3600)
            lag = rng.expovariate(1 / 20.0) if rng.random() < 0.97 else rng.uniform(300, 900)
            lines.append('{"create":{}}')
            lines.append(json.dumps({
                "@timestamp": iso(ingested - timedelta(seconds=lag)),
                "event": {"ingested": iso(ingested), "category": "process"},
                "data_stream": {"type": "logs", "dataset": "lagcost.events", "namespace": "default"},
                "host": {"name": rng.choice(hosts)}}))
        _, out = c._send(c.cfg.es_url, "POST", f"/{STREAM}/_bulk", raw=("\n".join(lines) + "\n").encode(),
                         headers={"Content-Type": "application/x-ndjson"})
        if out.get("errors"):
            raise SystemExit(f"bulk errors: {[i for i in out['items'] if i['create'].get('error')][:1]}")
        sent += n
        if sent % (batch * 20) == 0 or sent == events:
            print(f"  {sent:,} loaded ({sent / (time.time() - t0):,.0f} events/s)")
    c.es("POST", f"/{STREAM}/_refresh")
    _, cnt = c.es("POST", f"/{STREAM}/_count", {"query": {"range": {"event.ingested": {"gte": iso(HOUR), "lt": iso(HOUR + timedelta(hours=1))}}}})
    print(f"events in the measured hour ({iso(HOUR)}): {cnt['count']:,}")
    return 0


def set_rules(c: lagprof.Client, k: int) -> None:
    code, _ = c.es("GET", f"/{LOOKUP}", allow=(404,))
    if code == 404:
        c.es("PUT", f"/{LOOKUP}", {"settings": {"index": {"mode": "lookup"}},
                                    "mappings": lagprof.load_json(lagprof.LOOKUP_TEMPLATE)["template"]["mappings"]})
    c.es("POST", f"/{LOOKUP}/_delete_by_query?refresh=true", {"query": {"match_all": {}}})
    lines = []
    for i in range(k):
        lines.append(json.dumps({"index": {"_id": f"cost-rule-{i}"}}))
        lines.append(json.dumps({"data_stream": {"name": STREAM, "type": "logs", "dataset": "lagcost.events", "namespace": "default"},
                                 "lagprof": {"space": {"id": "default"}, "run": {"id": "cost"},
                                             "rule": {"id": f"cost-rule-{i}", "name": f"cost rule {i}", "interval_s": 300,
                                                      "lookback_s": 360 + 60 * (i % 5), "timestamp_field": "@timestamp"}}}))
    c._send(c.cfg.es_url, "POST", f"/{LOOKUP}/_bulk?refresh=true", raw=("\n".join(lines) + "\n").encode(),
            headers={"Content-Type": "application/x-ndjson"})


def query_text(path: str | None = None) -> str:
    q = Path(path).read_text() if path else (lagprof.PACK_ROOT / "esql" / "miss-rate.esql").read_text()
    end = HOUR + timedelta(hours=1)
    q = q.replace("FROM logs-*", f"FROM {STREAM}", 1)
    q = q.replace("lagprof-rule-streams", LOOKUP)
    q = q.replace("DATE_TRUNC(1 hour, NOW()) - 25 hours", f'TO_DATETIME("{iso(end - timedelta(hours=25))}")')
    q = q.replace("DATE_TRUNC(1 hour, NOW()) - 1 hour", f'TO_DATETIME("{iso(HOUR)}")')
    q = q.replace("DATE_TRUNC(1 hour, NOW())", f'TO_DATETIME("{iso(end)}")')
    return q


def cmd_run(c: lagprof.Client, rule_counts: list[int], repeats: int, query_file: str | None = None) -> int:
    _, cnt = c.es("POST", f"/{STREAM}/_count", {"query": {"range": {"event.ingested": {"gte": iso(HOUR), "lt": iso(HOUR + timedelta(hours=1))}}}})
    print(f"measured hour {iso(HOUR)}: {cnt['count']:,} events\n")
    print(f"{'rules':>5} {'rows joined':>12} {'took (ms)':>10} {'best (ms)':>10} {'result rows':>11}")
    q = query_text(query_file)
    for k in rule_counts:
        set_rules(c, k)
        times = []
        out = {}
        for _ in range(repeats):
            t0 = time.time()
            _, out = c.es("POST", "/_query", {"query": q})
            times.append(out.get("took", int((time.time() - t0) * 1000)))
        rows = sum(v[[x["name"] for x in out["columns"]].index("events")] for v in out["values"])
        print(f"{k:>5} {rows:>12,} {sum(times) / len(times):>10.0f} {min(times):>10} {len(out['values']):>11}")
    _, stats = c.es("GET", "/_nodes/stats/jvm,breaker")
    node = next(iter(stats["nodes"].values()))
    print(f"\nheap used now: {node['jvm']['mem']['heap_used_percent']}%; request breaker peak trips: "
          f"{node['breakers']['request']['tripped']}")
    return 0


def cmd_clean(c: lagprof.Client) -> int:
    c.es("DELETE", f"/_data_stream/{STREAM}", allow=(404,))
    c.es("DELETE", f"/{LOOKUP}", allow=(404,))
    HOUR_FILE.unlink(missing_ok=True)
    print("removed test data stream and lookup index")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("--events", type=int, default=1_000_000)
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("run")
    p.add_argument("--rules", type=int, nargs="+", default=[1, 5, 20])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--query-file", help="miss-rate query to time (default esql/miss-rate.esql)")
    sub.add_parser("clean")
    args = ap.parse_args()
    c = client()
    c.timeout = 900
    if args.cmd == "load":
        return cmd_load(c, args.events, args.batch, args.seed)
    if args.cmd == "run":
        return cmd_run(c, args.rules, args.repeats, args.query_file)
    return cmd_clean(c)


if __name__ == "__main__":
    sys.exit(main())

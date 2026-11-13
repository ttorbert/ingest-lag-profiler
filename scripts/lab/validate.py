#!/usr/bin/env python3
"""Lab validation: do real detection rules miss late events at the rate the profiler predicts?

    validate.py setup                      create the two validation rules
    validate.py send  --run R --per-lag N --minutes M   send events with known lag
    validate.py score --run R              match events to alerts, compare with the formula
                                           and with the pack's miss-rate query

Rules (Default space, tagged lagprof-validation):
    lagprof-val-timestamp   every 5m, look back 9m, searches @timestamp
    lagprof-val-ingested    same, timestamp_override event.ingested (should miss nothing)

Each event gets a unique event.id ("<run>-<n>") and is stamped `lag` seconds before it's
sent, at a random moment in the send window, so arrivals land at random points in the
rule's schedule. Sent events are logged to build/validation/<run>.jsonl.

Pass criteria (design doc): for each lag, observed miss rate within 5 points of the
formula; the profiler's estimate within 5 points of the observed overall rate; the
event.ingested rule misses nothing. A 95% confidence interval is shown for each lag.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lagprof  # noqa: E402

INTERVAL, LOOKBACK = 300, 540
LAGS = [180, 300, 420, 540, 660]
DATASET = "lagtest.synthetic"
STREAM = f"logs-{DATASET}-default"
PIPELINE = "lagprof-lab-ingested"
OUT_DIR = lagprof.PACK_ROOT / "build" / "validation"
RULES = {
    "lagprof-val-timestamp": {},
    "lagprof-val-ingested": {"timestamp_override": "event.ingested"},
}


def client() -> lagprof.Client:
    cfg = lagprof.load_config("targets/docker.env")
    if cfg.target != "docker":
        raise SystemExit("Lab only: use the docker target.")
    return lagprof.Client(cfg)


def expected(lag: float) -> float:
    return min(1.0, max(0.0, (lag - (LOOKBACK - INTERVAL)) / INTERVAL))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------

def cmd_setup(c: lagprof.Client) -> int:
    c.es("PUT", f"/_ingest/pipeline/{PIPELINE}", {
        "description": "Ingest Lag Profiler lab: set event.ingested to arrival time",
        "processors": [{"set": {"field": "event.ingested", "value": "{{{_ingest.timestamp}}}"}}]})
    for rule_id, extra in RULES.items():
        code, _ = c.kb("GET", f"/api/detection_engine/rules?rule_id={rule_id}", allow=(404,))
        if code == 200:
            print(f"rule {rule_id}: exists")
            continue
        c.kb("POST", "/api/detection_engine/rules", {
            "rule_id": rule_id, "name": f"validation: {rule_id.split('-')[-1]}",
            "description": "Ingest Lag Profiler validation rule", "risk_score": 21, "severity": "low",
            "type": "query", "language": "kuery", "query": "event.provider:lagprof-validation",
            "index": [f"logs-{DATASET}-*"], "interval": "5m", "from": "now-9m",
            "max_signals": 1000, "enabled": True, "tags": ["lagprof-validation"], **extra})
        print(f"rule {rule_id}: created")
    return 0


def cmd_send(c: lagprof.Client, run: str, per_lag: int, minutes: float, seed: int | None) -> int:
    rng = random.Random(seed)
    duration = minutes * 60
    plan = sorted((rng.random() * duration, lag) for lag in LAGS for _ in range(per_lag))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = (OUT_DIR / f"{run}.jsonl").open("w")
    start = time.time()
    print(f"run {run}: {len(plan)} events over {minutes:g} minutes "
          f"({per_lag} per lag, lags {LAGS}s). Log: {log.name}")
    i, n = 0, 0
    while i < len(plan):
        now_rel = time.time() - start
        batch = []
        while i < len(plan) and plan[i][0] <= now_rel:
            batch.append(plan[i][1])
            i += 1
        if batch:
            now = datetime.now(timezone.utc)
            lines, rows = [], []
            for lag in batch:
                eid = f"{run}-{n}"
                n += 1
                ts = now - timedelta(seconds=lag)
                lines.append(json.dumps({"create": {"_index": STREAM, "pipeline": PIPELINE}}))
                lines.append(json.dumps({
                    "@timestamp": iso(ts),
                    "data_stream": {"type": "logs", "dataset": DATASET, "namespace": "default"},
                    "event": {"id": eid, "provider": "lagprof-validation", "category": "process"},
                    "host": {"name": "lagprof-validation"},
                    "message": f"validation event {eid}, lag {lag}s"}))
                rows.append({"id": eid, "lag": lag, "sent": iso(now), "timestamp": iso(ts)})
            _, out = c._send(c.cfg.es_url, "POST", "/_bulk", raw=("\n".join(lines) + "\n").encode(),
                             headers={"Content-Type": "application/x-ndjson"})
            if out.get("errors"):
                bad = [it["create"].get("error") for it in out["items"] if it["create"].get("error")]
                raise SystemExit(f"bulk errors: {bad[:2]}")
            for r in rows:
                log.write(json.dumps(r) + "\n")
            log.flush()
            if n % 250 < len(batch):
                print(f"  {n}/{len(plan)} sent")
        time.sleep(0.2)
    log.close()
    wait = LOOKBACK + INTERVAL + 120
    print(f"All sent. Score after {wait // 60} minutes, once every rule run that could catch them has run:")
    print(f"  validate.py score --run {run}")
    return 0


def alert_ids(c: lagprof.Client, rule_id: str, run: str) -> set[str]:
    ids: set[str] = set()
    body = {"size": 10000, "_source": ["event.id"],
            "query": {"bool": {"filter": [{"term": {"kibana.alert.rule.rule_id": rule_id}},
                                          {"prefix": {"event.id": f"{run}-"}}]}}}
    code, out = c.es("POST", "/.alerts-security.alerts-default/_search", body, allow=(404,))
    if code == 404:
        return ids
    for h in out["hits"]["hits"]:
        eid = (h["_source"].get("event") or {}).get("id") or h["_source"].get("event.id")
        if isinstance(eid, list):
            eid = eid[0]
        if eid:
            ids.add(eid)
    return ids


def profiler_estimate(c: lagprof.Client, run: str, start: str, end: str) -> tuple[int, float] | None:
    """Run the pack's miss-rate query over the validation window, for this run's events only."""
    q = (lagprof.PACK_ROOT / "esql" / "miss-rate.esql").read_text()
    q = q.replace("FROM logs-*\n", f'FROM logs-*\n| WHERE STARTS_WITH(event.id, "{run}-")\n', 1)
    q = q.replace("DATE_TRUNC(1 hour, NOW()) - 25 hours", f'TO_DATETIME("{start}") - 1 hour')
    q = q.replace("DATE_TRUNC(1 hour, NOW()) - 1 hour", f'TO_DATETIME("{start}")')
    q = q.replace("DATE_TRUNC(1 hour, NOW())", f'TO_DATETIME("{end}")')
    _, out = c.es("POST", "/_query", {"query": q})
    cols = [x["name"] for x in out["columns"]]
    events = missed = 0
    for v in out["values"]:
        row = dict(zip(cols, v))
        if row["data_stream.dataset"] == DATASET and row["lagprof.rule.name"] == "validation: timestamp":
            events += row["events"]
            missed += row["est_missed"]
    return (events, missed) if events else None


def mechanism_check(c: lagprof.Client, sent: list[dict], caught: set[str]) -> tuple[int, int, int, float]:
    """Predict each event's outcome from the rule's actual run times (Kibana event log).

    The model: only the first run that starts after the event is searchable can catch it,
    and it does if the event's timestamp is within [run start - look-back, run start].
    Events sent within EDGE_S seconds of a run start are too close to call: the search
    happens a moment after the logged start, and searchability follows a ~1 s refresh.
    Returns (events predicted, mismatches, events too close to call, mean schedule delay s).
    """
    EDGE_S = 2.0
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    _, r = c.kb("GET", "/api/detection_engine/rules?rule_id=lagprof-val-timestamp")
    body = {"size": 10000, "sort": [{"@timestamp": "asc"}],
            "query": {"bool": {"filter": [{"term": {"event.provider": "alerting"}},
                                          {"term": {"event.action": "execute"}}, {"term": {"rule.id": r["id"]}}]}},
            "_source": ["event.start", "kibana.task.schedule_delay"]}
    _, out = c.es("POST", "/.kibana-event-log-*/_search", body)
    runs = [parse(h["_source"]["event"]["start"]) for h in out["hits"]["hits"]]
    delays = [int((h["_source"].get("kibana") or {}).get("task", {}).get("schedule_delay", 0)) / 1e9
              for h in out["hits"]["hits"]]
    predicted = mismatches = edge = 0
    for ev in sent:
        ts, sent_at = parse(ev["timestamp"]), parse(ev["sent"])
        searchable = sent_at + timedelta(seconds=1)  # refresh
        if any(abs((s - sent_at).total_seconds()) < EDGE_S for s in runs):
            edge += 1
            continue
        first = next((s for s in runs if s >= searchable), None)
        if first is None:
            continue
        predicted += 1
        would_catch = first - timedelta(seconds=LOOKBACK) <= ts <= first
        mismatches += would_catch != (ev["id"] in caught)
    return predicted, mismatches, edge, (sum(delays) / len(delays) if delays else 0.0)


def ensure_profiled(c: lagprof.Client) -> None:
    """The estimate needs the validation rule in lagprof-rule-streams; run the workflow if it isn't."""
    _, n = c.es("POST", "/lagprof-rule-streams/_count",
                {"query": {"term": {"lagprof.rule.name": "validation: timestamp"}}}, allow=(404,))
    if n.get("count"):
        return
    _, wl = c.kb("GET", "/api/workflows?size=1000")
    ids = [w["id"] for w in wl.get("results", []) if w.get("name") == "lagprof-hourly-all-spaces"]
    if not ids:
        print("No all-spaces workflow in the Default space; can't refresh the rule pairs.")
        return
    _, r = c.kb("POST", f"/api/workflows/workflow/{ids[0]}/run", {"inputs": {}})
    for _ in range(60):
        _, e = c.kb("GET", f"/api/workflows/executions/{r['workflowExecutionId']}")
        if e["status"] not in ("pending", "running", "waiting"):
            break
        time.sleep(3)
    print(f"Ran the workflow to pick up the validation rules: {e['status']}")


def cmd_score(c: lagprof.Client, run: str, tolerance: float) -> int:
    ensure_profiled(c)
    path = OUT_DIR / f"{run}.jsonl"
    sent = [json.loads(l) for l in path.read_text().splitlines()]
    caught = {rid: alert_ids(c, rid, run) for rid in RULES}
    print(f"run {run}: {len(sent)} events sent; alerts: "
          + ", ".join(f"{rid} {len(ids)}" for rid, ids in caught.items()))

    ok = True
    print(f"\n{'lag':>5} {'n':>5} {'expected':>9} {'observed':>9} {'95% interval':>17} {'diff':>6}  pass")
    total_missed = 0
    for lag in LAGS:
        rows = [s for s in sent if s["lag"] == lag]
        missed = sum(1 for s in rows if s["id"] not in caught["lagprof-val-timestamp"])
        total_missed += missed
        obs, exp = missed / len(rows), expected(lag)
        lo, hi = wilson(missed, len(rows))
        diff = (obs - exp) * 100
        passed = abs(diff) <= tolerance
        ok &= passed
        print(f"{lag:>4}s {len(rows):>5} {exp*100:>8.1f}% {obs*100:>8.1f}% {lo*100:>7.1f}% – {hi*100:>5.1f}% "
              f"{diff:>+5.1f}  {'yes' if passed else 'NO'}")

    predicted, mismatches, edge, delay = mechanism_check(c, sent, caught["lagprof-val-timestamp"])
    print(f"\nmechanism check: {mismatches} of {predicted} events differ from the prediction made from the "
          f"rule's actual run times; should be 0 ({edge} sent within 2s of a run start not judged; "
          f"mean schedule delay {delay:.1f}s)")
    ok &= mismatches == 0

    ingested_missed = sum(1 for s in sent if s["id"] not in caught["lagprof-val-ingested"])
    print(f"\nevent.ingested rule missed {ingested_missed} of {len(sent)} (should be 0)")
    ok &= ingested_missed == 0

    observed_overall = total_missed / len(sent) * 100
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    first = min(parse(s["sent"]) for s in sent) - timedelta(minutes=1)
    last = max(parse(s["sent"]) for s in sent) + timedelta(minutes=1)
    est = profiler_estimate(c, run, iso(first), iso(last))
    if est:
        events, missed = est
        est_pct = missed / events * 100
        diff = est_pct - observed_overall
        passed = abs(diff) <= tolerance
        ok &= passed
        print(f"profiler estimate {est_pct:.1f}% over {events} events; observed {observed_overall:.1f}%; "
              f"diff {diff:+.1f}  {'yes' if passed else 'NO'}")
    else:
        print("profiler estimate: no rows (has the workflow run since the validation rules were created?)")
        ok = False
    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    p = sub.add_parser("send")
    p.add_argument("--run", required=True)
    p.add_argument("--per-lag", type=int, default=600)
    p.add_argument("--minutes", type=float, default=90)
    p.add_argument("--seed", type=int)
    p = sub.add_parser("score")
    p.add_argument("--run", required=True)
    p.add_argument("--tolerance", type=float, default=5.0, help="points (default 5)")
    args = ap.parse_args()
    c = client()
    if args.cmd == "setup":
        return cmd_setup(c)
    if args.cmd == "send":
        return cmd_send(c, args.run, args.per_lag, args.minutes, args.seed)
    return cmd_score(c, args.run, args.tolerance)


if __name__ == "__main__":
    sys.exit(main())

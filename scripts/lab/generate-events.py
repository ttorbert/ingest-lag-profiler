#!/usr/bin/env python3
"""Steady lab traffic for the Ingest Lag Profiler.

Writes events into several logs-lagtest.* data streams. Each simulated host has a
lag profile, so every dashboard panel has something to show without Fleet:

    normal       a few seconds of lag
    slow         about 4 minutes of lag (tops the slowest hosts panel)
    bursty       usually fast, 10% of events 10 minutes late
    clock-ahead  stamped 5 minutes in the future (clock problems panel)

Lag is simulated by backdating @timestamp. Arrival time is set by Elasticsearch
through the lagprof-lab-ingested pipeline, except for the "legacy" dataset, which
gets no event.ingested at all (measurement gaps panel).

    generate-events.py --target-file targets/docker.env --eps 20 --minutes 60
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

PIPELINE = "lagprof-lab-ingested"

DATASETS = {
    "lagtest.auth": {"event.category": "authentication", "event.action": "logon"},
    "lagtest.process": {"event.category": "process", "event.action": "start"},
    "lagtest.network": {"event.category": "network", "event.action": "connection"},
    "lagtest.dns": {"event.category": "network", "event.action": "dns_query"},
    "lagtest.legacy": {"event.category": "host", "event.action": "heartbeat"},  # no event.ingested
}

HOSTS = {
    "lab-host-01": "normal",
    "lab-host-02": "normal",
    "lab-host-03": "normal",
    "lab-host-slow": "slow",
    "lab-host-bursty": "bursty",
    "lab-host-skewed": "clock-ahead",
}


def lag_seconds(profile: str, rng: random.Random) -> float:
    if profile == "normal":
        return rng.expovariate(1 / 4.0)
    if profile == "slow":
        return max(30.0, rng.gauss(240.0, 40.0))
    if profile == "bursty":
        return 600.0 + rng.random() * 60 if rng.random() < 0.10 else rng.expovariate(1 / 3.0)
    if profile == "clock-ahead":
        return -300.0 + rng.expovariate(1 / 3.0)
    raise ValueError(profile)


def ensure_pipeline(client: lagprof.Client) -> None:
    client.es("PUT", f"/_ingest/pipeline/{PIPELINE}", {
        "description": "Ingest Lag Profiler lab: set event.ingested to arrival time",
        "processors": [{"set": {"field": "event.ingested", "value": "{{{_ingest.timestamp}}}"}}],
        "_meta": {"managed_by": "lagprof-lab"},
    })


def build_bulk(n: int, namespace: str, rng: random.Random) -> bytes:
    now = datetime.now(timezone.utc)
    lines = []
    for _ in range(n):
        dataset = rng.choice(list(DATASETS))
        host = rng.choice(list(HOSTS))
        ts = now - timedelta(seconds=lag_seconds(HOSTS[host], rng))
        action = {"create": {"_index": f"logs-{dataset}-{namespace}"}}
        if dataset != "lagtest.legacy":
            action["create"]["pipeline"] = PIPELINE
        doc = {
            "@timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            # Fleet integrations include these; the profiler joins on dataset and namespace.
            "data_stream": {"type": "logs", "dataset": dataset, "namespace": namespace},
            "host": {"name": host},
            "event": {k.split(".", 1)[1]: v for k, v in DATASETS[dataset].items()},
            "message": f"lab event from {host}",
            "lagprof_lab": {"profile": HOSTS[host]},
        }
        lines.append(json.dumps(action))
        lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target-file", default="targets/docker.env")
    ap.add_argument("--eps", type=float, default=20.0, help="events per second (default 20)")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (default: run until Ctrl-C)")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    cfg = lagprof.load_config(args.target_file)
    if cfg.target != "docker":
        print("Refusing to write lab events to a non-lab target. Use a docker target file.", file=sys.stderr)
        return 2
    client = lagprof.Client(cfg)
    ensure_pipeline(client)
    rng = random.Random(args.seed)

    deadline = time.time() + args.minutes * 60 if args.minutes else None
    sent = errors = 0
    print(f"Sending about {args.eps:g} events/s to logs-lagtest.*-{args.namespace}. Ctrl-C to stop.")
    try:
        while deadline is None or time.time() < deadline:
            started = time.time()
            n = max(1, int(rng.gauss(args.eps, args.eps ** 0.5)))
            _, out = client._send(cfg.es_url, "POST", "/_bulk", raw=build_bulk(n, args.namespace, rng),
                                  headers={"Content-Type": "application/x-ndjson"})
            if out.get("errors"):
                errors += sum(1 for item in out["items"] if item["create"].get("error"))
            sent += n
            if sent // 1000 != (sent - n) // 1000:
                print(f"  {sent} sent, {errors} rejected")
            time.sleep(max(0.0, 1.0 - (time.time() - started)))
    except KeyboardInterrupt:
        pass
    print(f"Done: {sent} sent, {errors} rejected")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Create lab spaces and detection rules that cover the profiler's cases.

Spaces: default, soc, empty (no rules). Rules (all enabled, all tagged lagprof-lab):

  default  lab: timestamp rule         query, every 5m, look back 9m, @timestamp
  default  lab: "ingested" \\ rule     same, timestamp_override event.ingested; quotes and a
                                       backslash in the name test escaping (never at risk, so
                                       it stays out of the Rules at risk panel)
  default  lab: auth rule (mixed index patterns)
                                       remote and missing patterns mixed with a real one
  soc      lab: one-minute rule        query, every 1m, look back 6m
  soc      lab: esql rule              ES|QL (unsupported)

Safe to run again: existing lab rules are matched by rule_id and left alone.

    seed_rules.py --target-file targets/docker.env
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402

BASE = {"description": "Ingest Lag Profiler lab rule", "risk_score": 21, "severity": "low",
        "enabled": True, "tags": ["lagprof-lab"], "max_signals": 1000}

RULES = [
    ("default", {"rule_id": "lagprof-lab-timestamp", "name": "lab: timestamp rule", "type": "query",
                 "language": "kuery", "query": "event.category:*", "index": ["logs-lagtest.*"],
                 "interval": "5m", "from": "now-9m"}),
    ("default", {"rule_id": "lagprof-lab-ingested", "name": 'lab: "ingested" \\ rule', "type": "query",
                 "language": "kuery", "query": "event.category:*", "index": ["logs-lagtest.*"],
                 "interval": "5m", "from": "now-9m", "timestamp_override": "event.ingested"}),
    ("default", {"rule_id": "lagprof-lab-quoted", "name": "lab: auth rule (mixed index patterns)", "type": "query",
                 "language": "kuery", "query": "event.category:authentication",
                 "index": ["logs-lagtest.auth-*", "remote:logs-*", "missing-index-name"],
                 "interval": "5m", "from": "now-6m"}),
    ("soc", {"rule_id": "lagprof-lab-one-minute", "name": "lab: one-minute rule", "type": "query",
             "language": "kuery", "query": "event.category:process", "index": ["logs-lagtest.process-*"],
             "interval": "1m", "from": "now-6m"}),
    ("soc", {"rule_id": "lagprof-lab-esql", "name": "lab: esql rule", "type": "esql", "language": "esql",
             "query": "FROM logs-lagtest.* | STATS c = COUNT(*) BY host.name", "interval": "5m",
             "from": "now-6m"}),
]

SPACES = [("soc", "SOC"), ("empty", "Empty")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-file", default="targets/docker.env")
    args = ap.parse_args()
    cfg = lagprof.load_config(args.target_file)
    if cfg.target != "docker":
        print("Lab only: use a docker target file.", file=sys.stderr)
        return 2
    c = lagprof.Client(cfg)

    for sid, name in SPACES:
        code, _ = c.kb("POST", "/api/spaces/space", {"id": sid, "name": name}, allow=(409,))
        print(f"space {sid}: {'exists' if code == 409 else 'created'}")

    for space, rule in RULES:
        prefix = lagprof.space_prefix(space)
        code, _ = c.kb("GET", f"{prefix}/api/detection_engine/rules?rule_id={rule['rule_id']}", allow=(404,))
        if code == 200:
            print(f"rule {rule['rule_id']} ({space}): exists")
            continue
        c.kb("POST", f"{prefix}/api/detection_engine/rules", {**BASE, **rule})
        print(f"rule {rule['rule_id']} ({space}): created")
    return 0


if __name__ == "__main__":
    sys.exit(main())

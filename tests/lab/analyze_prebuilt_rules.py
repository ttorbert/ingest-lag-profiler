#!/usr/bin/env python3
"""Analyze the installed prebuilt detection rules from the profiler's point of view.

Reports, for Elastic's prebuilt rules:
  - how many search on event.ingested (timestamp override) vs @timestamp
  - rule types, and what share lagprof-rules-parse marks supported (and why not)
  - whether the rules' index patterns reach logs-* data streams
  - schedules: interval, look-back and the safe margin (look-back minus interval)

The rules are sent through the real pipeline with the _simulate API, so the
support numbers are what the workflow would record.

    analyze_prebuilt_rules.py --target-file targets/docker.env [--markdown]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402

def pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-file", default="targets/docker.env")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    c = lagprof.Client(lagprof.load_config(args.target_file))

    # The fields= filter makes 9.5.6 return a 500, so fetch whole rules.
    _, out = c.kb("GET", "/api/detection_engine/rules/_find?per_page=10000&page=1")
    rules = [r for r in out["data"] if r.get("immutable") or (r.get("rule_source") or {}).get("type") == "external"]
    total = len(rules)

    # Run every rule through the real parse pipeline.
    pipeline = lagprof.strip_notes(json.loads(
        (ROOT / "elasticsearch/ingest-pipelines/lagprof-rules-parse.json").read_text()))
    parsed = []
    for i in range(0, total, 500):
        batch = rules[i:i + 500]
        docs = [{"_source": {"lagprof": {"rule": {
            "id": r["id"], "type": r.get("type") or "", "interval_raw": r.get("interval") or "",
            "from_raw": r.get("from") or "", "index_patterns": r.get("index") or "",
            "data_view_id": r.get("data_view_id") or "", "timestamp_override": r.get("timestamp_override") or "",
            "timestamp_override_fallback_disabled": r.get("timestamp_override_fallback_disabled", "")}}}}
            for r in batch]
        _, sim = c.es("POST", "/_ingest/pipeline/_simulate", {"pipeline": pipeline, "docs": docs})
        parsed += [d["doc"]["_source"]["lagprof"]["rule"] for d in sim["docs"]]

    by_ts = collections.Counter(p.get("timestamp_field") for p in parsed)
    by_type = collections.Counter(r.get("type") for r in rules)
    supported = [p for p in parsed if p.get("supported")]
    reasons = collections.Counter(p.get("unsupported_reason") for p in parsed if not p.get("supported"))
    fallback_disabled = sum(1 for r in rules if r.get("timestamp_override") and r.get("timestamp_override_fallback_disabled"))

    def reaches_logs(p):
        return any(pat.startswith("logs-") for pat in (p.get("index_patterns_csv") or "").split(",") if pat)

    sup_ts = [p for p in supported if p.get("timestamp_field") == "@timestamp"]
    sup_ing = [p for p in supported if p.get("timestamp_field") == "event.ingested"]
    at_risk = [p for p in sup_ts if reaches_logs(p)]

    margins = collections.Counter()
    intervals = collections.Counter()
    for p in supported:
        if p.get("interval_s") and p.get("lookback_s"):
            intervals[p["interval_s"]] += 1
            margins[p["lookback_s"] - p["interval_s"]] += 1

    rows = [
        ("Prebuilt rules installed", f"{total}"),
        ("Search on event.ingested (timestamp override)", f"{by_ts['event.ingested']} ({pct(by_ts['event.ingested'], total)})"),
        ("Search on @timestamp", f"{by_ts['@timestamp']} ({pct(by_ts['@timestamp'], total)})"),
        ("Other timestamp fields", f"{total - by_ts['event.ingested'] - by_ts['@timestamp']}"),
        ("event.ingested rules with fallback to @timestamp disabled", f"{fallback_disabled}"),
        ("Supported by the profiler", f"{len(supported)} ({pct(len(supported), total)})"),
        ("Supported, on @timestamp (lag can cause misses)", f"{len(sup_ts)} ({pct(len(sup_ts), total)})"),
        ("  of those, reaching logs-* data streams", f"{len(at_risk)} ({pct(len(at_risk), total)})"),
        ("Supported, on event.ingested (lag can't cause misses)", f"{len(sup_ing)} ({pct(len(sup_ing), total)})"),
    ]
    w = max(len(k) for k, _ in rows)
    if args.markdown:
        print("| Measure | Count |\n| --- | ---: |")
        for k, v in rows:
            print(f"| {k.strip()} | {v} |")
    else:
        for k, v in rows:
            print(f"{k:<{w}}  {v}")
    print("\nRule types:", dict(by_type.most_common()))
    print("Unsupported reasons:", dict(reasons.most_common()))
    print("Intervals (s):", dict(sorted(intervals.items())))
    print("Safe margins, look-back minus interval (s):", dict(sorted(margins.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

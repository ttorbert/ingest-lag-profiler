#!/usr/bin/env python3
"""Milestone 0 ES|QL spikes, run against the lab.

Answers, with small throwaway indices (lagprof-spike-*, deleted afterwards):
  1. Does LOOKUP JOIN return one row per matching lookup document?
  2. When source and lookup share a field name, which value wins, and what
     happens to the source value when there's no match?
  3. Does a multivalued join key in the lookup index match?
  4. Do DATE_TRUNC windows and INLINE STATS work?
  5. Do the three pack queries run, using a recent window instead of the last
     full hour?

    spike_esql.py --target-file targets/docker.env
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402

SRC, LOOKUP = "lagprof-spike-src", "lagprof-spike-lookup"


def esql(client, query, allow_error=False):
    code, out = client.es("POST", "/_query", {"query": query}, allow=(400, 500) if allow_error else ())
    if code >= 400:
        return None, (out or {}).get("error", {}).get("reason", str(out))[:300]
    cols = [c["name"] for c in out["columns"]]
    return [dict(zip(cols, row)) for row in out["values"]], None


def setup(client):
    for idx in (SRC, LOOKUP):
        client.es("DELETE", f"/{idx}", allow=(404,))
    client.es("PUT", f"/{LOOKUP}", {"settings": {"index": {"mode": "lookup"}},
                                    "mappings": {"properties": {"k": {"type": "keyword"},
                                                                "rule": {"properties": {"name": {"type": "keyword"}}},
                                                                "tag": {"type": "keyword"}}}})
    client.es("PUT", f"/{SRC}", {"mappings": {"properties": {"k": {"type": "keyword"},
                                                             "rule": {"properties": {"name": {"type": "keyword"}}}}}})
    docs = [
        (LOOKUP, {"k": "a", "rule": {"name": "lookup-rule-1"}, "tag": "x"}),
        (LOOKUP, {"k": "a", "rule": {"name": "lookup-rule-2"}, "tag": "y"}),
        (LOOKUP, {"k": ["m1", "m2"], "rule": {"name": "multi"}, "tag": "mv"}),
        (SRC, {"k": "a", "rule": {"name": "firewall-rule"}}),
        (SRC, {"k": "nomatch", "rule": {"name": "firewall-rule-unmatched"}}),
        (SRC, {"k": "m2", "rule": {"name": "src-mv"}}),
    ]
    for idx, doc in docs:
        client.es("POST", f"/{idx}/_doc?refresh=true", doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-file", default="targets/docker.env")
    ap.add_argument("--window", default="15 minutes", help="recent window for the pack queries")
    args = ap.parse_args()
    client = lagprof.Client(lagprof.load_config(args.target_file))
    setup(client)
    findings = {}
    try:
        rows, err = esql(client, f"FROM {SRC} | WHERE k == \"a\" | LOOKUP JOIN {LOOKUP} ON k | KEEP k, rule.name, tag | SORT tag")
        findings["1. rows per match"] = err or f"{len(rows)} rows for 2 matching lookup docs: {rows}"
        findings["2a. name collision, matched"] = err or f"rule.name values after join: {[r['rule.name'] for r in rows]} (source had 'firewall-rule')"

        rows, err = esql(client, f"FROM {SRC} | WHERE k == \"nomatch\" | LOOKUP JOIN {LOOKUP} ON k | KEEP k, rule.name, tag")
        findings["2b. name collision, no match"] = err or f"{rows} (source had 'firewall-rule-unmatched')"

        rows, err = esql(client, f"FROM {SRC} | WHERE k == \"m2\" | LOOKUP JOIN {LOOKUP} ON k | KEEP k, rule.name, tag", allow_error=True)
        findings["3. multivalued lookup key"] = err or f"{rows}"

        rows, err = esql(client, "ROW x = 1 | EVAL start = DATE_TRUNC(1 hour, NOW()) - 1 hour, stop = DATE_TRUNC(1 hour, NOW())")
        findings["4a. DATE_TRUNC window"] = err or f"{rows}"

        rows, err = esql(client, f"FROM {SRC} | INLINE STATS n = COUNT(*) | KEEP k, n | LIMIT 3", allow_error=True)
        findings["4b. INLINE STATS"] = err or f"works: {rows}"

        rows, err = esql(client, "ROW a = 5 | EVAL g = GREATEST(a + 60, 2), c = TO_INTEGER(CEIL(7 / 60.0)) * 60", allow_error=True)
        findings["4c. GREATEST and CEIL"] = err or f"{rows}"

        for name in ("stream-stats", "host-lag", "miss-rate"):
            q = (ROOT / "esql" / f"{name}.esql").read_text()
            q = q.replace("DATE_TRUNC(1 hour, NOW()) - 1 hour", f"NOW() - {args.window}")
            q = q.replace("DATE_TRUNC(1 hour, NOW())", "NOW()")
            q = re.sub(r"- 25 hours", "- 25 hours", q)
            rows, err = esql(client, q, allow_error=True)
            if err:
                findings[f"5. {name}"] = f"ERROR: {err}"
            else:
                findings[f"5. {name}"] = f"{len(rows)} rows" + (f"; first: {json.dumps(rows[0])}" if rows else "")
    finally:
        for idx in (SRC, LOOKUP):
            client.es("DELETE", f"/{idx}", allow=(404,))

    for k, v in findings.items():
        print(f"{k}\n    {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

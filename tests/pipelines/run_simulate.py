#!/usr/bin/env python3
"""Run the ingest pipeline fixtures against a cluster with the _simulate API.

The pipelines are sent inline from elasticsearch/ingest-pipelines/, so nothing
needs to be installed first. Expectations use dotted paths; null means the field
must be absent, and "*" means it must be present with any value.

    run_simulate.py --target-file targets/docker.env
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402

MISSING = object()


def get_path(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def check(doc: dict, expect: dict) -> list[str]:
    errors = []
    for path, want in expect.items():
        got = get_path(doc, path)
        if want is None:
            if got is not MISSING:
                errors.append(f"{path}: expected absent, got {got!r}")
        elif want == "*":
            if got is MISSING:
                errors.append(f"{path}: expected present")
        elif got is MISSING or got != want:
            errors.append(f"{path}: expected {want!r}, got {'absent' if got is MISSING else repr(got)}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-file", default="targets/docker.env")
    args = ap.parse_args()
    client = lagprof.Client(lagprof.load_config(args.target_file))
    fixtures = json.loads((ROOT / "tests/pipelines/fixtures.json").read_text())

    failures = total = 0
    for pipeline, cases in fixtures.items():
        body = lagprof.strip_notes(json.loads(
            (ROOT / f"elasticsearch/ingest-pipelines/{pipeline}.json").read_text()))
        _, out = client.es("POST", "/_ingest/pipeline/_simulate", {
            "pipeline": body,
            "docs": [{"_source": c["input"]} for c in cases],
        })
        for case, result in zip(cases, out["docs"]):
            total += 1
            if "error" in result:
                errs = [f"pipeline error: {json.dumps(result['error'])[:300]}"]
            else:
                errs = check(result["doc"]["_source"], case["expect"])
            status = "ok " if not errs else "BAD"
            print(f"{status} {pipeline}: {case['name']}")
            for e in errs:
                print(f"      {e}")
            failures += bool(errs)
    print(f"\n{total - failures}/{total} pipeline cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

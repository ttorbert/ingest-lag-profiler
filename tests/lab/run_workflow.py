#!/usr/bin/env python3
"""Import (or update) a workflow YAML in the lab, run it once, and report.

    run_workflow.py kibana/workflows/lagprof-hourly-all-spaces.yaml
    run_workflow.py build/lagprof-hourly-space-soc.yaml --space soc

Prints the execution status, failed steps with their errors, and document counts
in the lagprof-* indices.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lagprof  # noqa: E402

INDICES = ("lagprof-rules", "lagprof-rule-streams", "lagprof-results", "lagprof-stream-stats",
           "lagprof-host-lag", "lagprof-runs")


def upsert(c: lagprof.Client, prefix: str, yaml_text: str) -> str:
    """Import or update the workflow and return its ID.

    Matched by name, like the installer: Kibana doesn't reuse a deleted workflow's ID,
    so a re-created workflow's ID is "<name>-1" and so on, not its name."""
    name = re.search(r"(?m)^name:\s*(\S+)", yaml_text).group(1)
    _, listing = c.kb("GET", f"{prefix}/api/workflows?size=1000")
    matches = [w for w in (listing or {}).get("results", []) if w.get("name") == name]
    if not matches:
        _, out = c.kb("POST", f"{prefix}/api/workflows", {"workflows": [{"yaml": yaml_text}]})
        w = out["created"][0]
        print(f"imported {w['id']} (valid={w.get('valid')})")
        return w["id"]
    if len(matches) > 1:
        print(f"WARN {len(matches)} workflows named {name}: {', '.join(w['id'] for w in matches)}; using the first")
    wid = matches[0]["id"]
    _, w = c.kb("PUT", f"{prefix}/api/workflows/workflow/{wid}", {"yaml": yaml_text})
    print(f"updated {wid} (valid={w.get('valid')})")
    return wid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml")
    ap.add_argument("--space")
    ap.add_argument("--target-file", default="targets/docker.env")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    c = lagprof.Client(lagprof.load_config(args.target_file))
    prefix = lagprof.space_prefix(args.space)

    wid = upsert(c, prefix, (ROOT / args.yaml).read_text() if not Path(args.yaml).is_absolute()
                 else Path(args.yaml).read_text())
    _, r = c.kb("POST", f"{prefix}/api/workflows/workflow/{wid}/run", {"inputs": {}})
    eid = r["workflowExecutionId"]
    started = time.time()
    while True:
        _, e = c.kb("GET", f"{prefix}/api/workflows/executions/{eid}")
        if e["status"] not in ("pending", "running", "waiting") or time.time() - started > args.timeout:
            break
        time.sleep(3)
    print(f"execution {eid}: {e['status']} in {e.get('duration', '?')} ms")
    if e.get("error"):
        print("  error:", json.dumps(e["error"])[:600])
    seen = set()
    for s in e.get("stepExecutions", []):
        if s["status"] == "failed" and s["stepId"] not in seen:
            seen.add(s["stepId"])
            print(f"  FAILED {s['stepId']}: {json.dumps(s.get('error'))[:600]}")
    for idx in INDICES:
        code, n = c.es("GET", f"/{idx}/_count", allow=(404,))
        print(f"  {idx:22} {n.get('count') if code == 200 else 'missing'}")
    return 0 if e["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

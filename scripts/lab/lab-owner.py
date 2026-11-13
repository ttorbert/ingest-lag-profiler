#!/usr/bin/env python3
"""Create the lab's workflow owner: user lagprof-runner with only the lagprof-workflow
role, and an API key created as that user (saved to build/lab-owner-key).

Then `LAGPROF_WORKFLOW_API_KEY=$(cat build/lab-owner-key) lagprof.py install ...` saves
the workflow as lagprof-runner, so its scheduled runs use that role's privileges.
`make lab-owner` does both. Lab only.
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lagprof  # noqa: E402

KEY_FILE = lagprof.PACK_ROOT / "build" / "lab-owner-key"


def main() -> int:
    cfg = lagprof.load_config("targets/docker.env")
    if cfg.target != "docker":
        print("Lab only.", file=sys.stderr)
        return 2
    admin = lagprof.Client(cfg)
    if KEY_FILE.exists():
        print(f"Owner key already exists: {KEY_FILE.relative_to(lagprof.PACK_ROOT)}")
        return 0
    pw = secrets.token_urlsafe(18)
    code, _ = admin.es("GET", "/_security/user/lagprof-runner", allow=(404,))
    if code == 404:
        admin.es("PUT", "/_security/user/lagprof-runner",
                 {"password": pw, "roles": ["lagprof-workflow"], "full_name": "Ingest Lag Profiler runner (lab)"})
    else:
        admin.es("PUT", "/_security/user/lagprof-runner/_password", {"password": pw})
    runner_cfg = lagprof.load_config("targets/docker.env")
    runner_cfg.username, runner_cfg.password = "lagprof-runner", pw
    _, key = lagprof.Client(runner_cfg).es("POST", "/_security/api_key", {"name": "lagprof-workflow-owner-lab"})
    KEY_FILE.parent.mkdir(exist_ok=True)
    KEY_FILE.write_text(key["encoded"])
    KEY_FILE.chmod(0o600)
    print(f"Created lagprof-runner and its API key: {KEY_FILE.relative_to(lagprof.PACK_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

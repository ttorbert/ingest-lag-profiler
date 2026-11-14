#!/usr/bin/env python3
"""Build kibana/dashboards/lagprof-overview.json, the Ingest Lag Profiler dashboard.

The workflow writes a notice document to lagprof-rules (lagprof.rule.type
"lagprof_notice") when a space has more than 10,000 enabled rules. It shows in
Unsupported rules, and Rule coverage leaves it out of the counts.

The JSON is a Kibana Dashboards API body (PUT /api/dashboards/lagprof-overview, 9.5+).
Every panel reads the lagprof-* indices with ES|QL, never raw logs.

    build.py                                       write the JSON
    build.py --check --target-file targets/docker.env   also run every query first

Panel queries see the dashboard's time range on @timestamp. "Latest hour" panels pick
the newest window inside that range with INLINE STATS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "lagprof-overview.json"

INTRO = """**Ingest Lag Profiler.** Estimated share of events each detection rule misses because logs arrive late. \
Results are **estimates**: `est_missed` counts events in the data streams a rule searches that fall outside its \
window, not missed alerts. Rules that search on `event.ingested` can't miss events because of lag; see *Rule coverage*. \
For rules that run every minute, the estimate can be a few points low when rule start times vary on a busy Kibana. \
Updated hourly by the lagprof-hourly workflow."""

Q = {
    "rules_at_risk": """FROM lagprof-results
| INLINE STATS latest = MAX(lagprof.window.start)
| EVAL in_latest = lagprof.window.start == latest
| STATS events = SUM(CASE(in_latest, events, 0)),
        missed = SUM(CASE(in_latest, est_missed, 0.0)),
        recommended_s = MAX(recommended_lookback_s),
        lookback_s = MAX(lagprof.rule.lookback_s),
        interval_s = MAX(lagprof.rule.interval_s)
     BY space = lagprof.space.id, rule = lagprof.rule.name
| EVAL miss_rate_pct = ROUND(missed / events * 100, 2),
       est_missed = TO_LONG(ROUND(missed)),
       interval_min = interval_s / 60.0,
       lookback_min = lookback_s / 60.0,
       recommended_min = recommended_s / 60.0,
       recommended_additional_min = (recommended_s - interval_s) / 60.0
| WHERE miss_rate_pct > 0.1
| KEEP rule, space, est_missed, events, miss_rate_pct, interval_min, lookback_min, recommended_min,
       recommended_additional_min
| SORT est_missed DESC, miss_rate_pct DESC
| LIMIT 200""",

    "miss_rate_over_time": """FROM lagprof-results
| STATS missed = SUM(est_missed), events = SUM(events)
     BY hour = BUCKET(@timestamp, 1 hour), rule = lagprof.rule.name
| EVAL miss_rate_pct = ROUND(missed / events * 100, 2)
| KEEP hour, rule, miss_rate_pct""",

    "lag_by_stream": """FROM lagprof-stream-stats
| WHERE lag_p95_s IS NOT NULL
| STATS p95_lag_s = MAX(lag_p95_s)
     BY hour = BUCKET(@timestamp, 1 hour), stream = data_stream.dataset""",

    "slowest_hosts": """FROM lagprof-host-lag
| INLINE STATS latest = MAX(lagprof.window.start)
| WHERE lagprof.window.start == latest AND lag_p95_s > 60
| STATS p95_lag_s = MAX(lag_p95_s), max_lag_s = MAX(lag_max_s), events = SUM(events)
     BY host = host.name, stream = data_stream.dataset
| SORT p95_lag_s DESC
| LIMIT 100""",

    "clock_problems": """FROM lagprof-host-lag
| WHERE negative_lag_events > 0
| STATS events_in_future = SUM(negative_lag_events), last_seen = MAX(@timestamp)
     BY host = host.name, stream = data_stream.dataset
| SORT events_in_future DESC
| LIMIT 100""",

    "measurement_gaps": """FROM lagprof-stream-stats
| STATS events = SUM(events), unmeasured = SUM(unmeasured_events) BY stream = data_stream.dataset
| EVAL unmeasured_pct = ROUND(unmeasured * 100.0 / events, 1)
| WHERE unmeasured > 0
| SORT unmeasured DESC
| LIMIT 100""",

    "rule_coverage": """FROM lagprof-rules
| WHERE COALESCE(lagprof.rule.type, "") != "lagprof_notice"
| EVAL coverage = CASE(lagprof.rule.supported == false, "Unsupported",
                       lagprof.rule.timestamp_field == "event.ingested", "Unaffected (event.ingested)",
                       "Measured (@timestamp)")
| STATS rules = COUNT(*) BY coverage
| SORT rules DESC""",

    "unsupported_rules": """FROM lagprof-rules
| WHERE lagprof.rule.supported == false
| STATS rules = COUNT(*) BY reason = lagprof.rule.unsupported_reason
| SORT rules DESC""",

    "health": """FROM lagprof-runs
| EVAL workflow = COALESCE(lagprof.space.id, "all spaces")
| INLINE STATS latest = MAX(@timestamp) BY workflow
| EVAL is_last = @timestamp == latest
| STATS last_run = MAX(@timestamp), runs = COUNT(*),
        no_data = SUM(CASE(status == "no_data", 1, 0)),
        failed = SUM(CASE(status == "failed", 1, 0)),
        last_status = MAX(CASE(is_last, status, null)),
        last_note = MAX(CASE(is_last, reason, null)),
        last_rules_seen = MAX(rules_seen)
     BY workflow
| EVAL last_note = CASE(last_status == "failed", "See the error field in lagprof-runs", last_note)
| KEEP workflow, last_run, last_status, last_note, runs, no_data, failed, last_rules_seen""",
}


def esql(query: str) -> dict:
    return {"type": "esql", "query": query}


LABELS = {
    "rule": "Rule", "space": "Space", "miss_rate_pct": "Est. miss rate (%)", "interval_min": "Runs every (min)",
    "lookback_min": "Total look-back (min)", "recommended_min": "Recommended total look-back (min)",
    "recommended_additional_min": "Set additional look-back to (min)", "est_missed": "Est. missed events",
    "host": "Host",
    "stream": "Data stream", "p95_lag_s": "p95 lag (s)", "max_lag_s": "Max lag (s)", "events": "Events",
    "events_in_future": "Events stamped in the future", "last_seen": "Last seen", "unmeasured": "No event.ingested",
    "unmeasured_pct": "No event.ingested (%)", "coverage": "Coverage", "rules": "Rules", "reason": "Reason",
    "workflow": "Workflow", "last_run": "Last run", "runs": "Runs", "failed": "Failed",
    "last_status": "Last status", "last_note": "Note", "no_data": "No data",
    "last_rules_seen": "Rules seen", "hour": "Hour",
}


def col(name: str, width: int | None = None) -> dict:
    c = {"column": name, "label": LABELS.get(name, name)}
    if width:
        c["width"] = width  # pixels; keeps long values such as rule names from being cut off
    return c


def table(title: str, query: str, rows: list[str], metrics: list[str], grid: dict, description: str = "",
          widths: dict[str, int] | None = None) -> dict:
    widths = widths or {}
    cfg = {"type": "data_table", "title": title, "data_source": esql(query),
           "rows": [col(c, widths.get(c)) for c in rows], "metrics": [col(c, widths.get(c)) for c in metrics]}
    if description:
        cfg["description"] = description
    return {"type": "vis", "grid": grid, "config": cfg}


def line(title: str, query: str, x: str, y: str, breakdown: str, grid: dict, description: str = "") -> dict:
    cfg = {"type": "xy", "title": title, "layers": [{
        "type": "line", "data_source": esql(query), "x": col(x),
        "y": [col(y)], "breakdown_by": col(breakdown)}]}
    if description:
        cfg["description"] = description
    return {"type": "vis", "grid": grid, "config": cfg}


def g(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def build(version: str) -> dict:
    panels = [
        {"type": "markdown", "grid": g(0, 0, 48, 4), "config": {"content": INTRO}},
        table("Rules at risk (latest hour)", Q["rules_at_risk"],
              ["rule", "space"],
              ["est_missed", "events", "miss_rate_pct", "interval_min", "lookback_min", "recommended_min",
               "recommended_additional_min"], g(0, 4, 48, 14),
              "Rules with an estimated miss rate above 0.1% in the newest hour, most missed events first. "
              "Total look-back is the rule's interval plus its additional look-back. The recommendation keeps the "
              "estimate under 1%, from the highest hourly p99 lag in the time range. To apply it, set the rule's "
              "Additional look-back time in Kibana to the last column.",
              widths={"rule": 320}),
        line("Miss rate over time (%)", Q["miss_rate_over_time"], "hour", "miss_rate_pct", "rule", g(0, 18, 24, 14)),
        line("95th percentile lag by data stream (seconds)", Q["lag_by_stream"], "hour", "p95_lag_s", "stream",
             g(24, 18, 24, 14)),
        table("Slowest hosts (latest hour)", Q["slowest_hosts"], ["host", "stream"],
              ["p95_lag_s", "max_lag_s", "events"], g(0, 32, 24, 12)),
        table("Clock problems: events stamped in the future", Q["clock_problems"], ["host", "stream"],
              ["events_in_future", "last_seen"], g(24, 32, 24, 12),
              "Events stamped more than a minute after they arrived. Usually a source system's clock is wrong."),
        table("Measurement gaps: events with no event.ingested", Q["measurement_gaps"], ["stream"],
              ["events", "unmeasured", "unmeasured_pct"], g(0, 44, 24, 12),
              "Lag can't be measured for these events. Rules on event.ingested fall back to @timestamp for them."),
        table("Rule coverage", Q["rule_coverage"], ["coverage"], ["rules"], g(24, 44, 12, 12)),
        table("Unsupported rules", Q["unsupported_rules"], ["reason"], ["rules"], g(36, 44, 12, 12)),
        table("Profiler health", Q["health"], ["workflow"],
              ["last_run", "last_status", "last_note", "runs", "no_data", "failed", "last_rules_seen"],
              g(0, 56, 48, 8),
              "No data means the run worked but had nothing to measure: no logs in logs-* yet, or none with "
              "event.ingested. Note says which.",
              widths={"last_run": 220, "last_note": 520}),
    ]
    return {
        "title": "Ingest Lag Profiler",
        "description": f"Estimated detection rule miss rates from ingest lag. Managed by lagprof (pack v{version}).",
        "time_range": {"from": "now-7d", "to": "now"},
        "panels": panels,
    }


def check(target_file: str) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import lagprof  # noqa: E402
    c = lagprof.Client(lagprof.load_config(target_file))
    bad = 0
    for name, q in Q.items():
        timed = q if "lagprof-rules" in q.split("\n")[0] else q.replace(
            "\n", "\n| WHERE @timestamp >= NOW() - 7 days\n", 1)
        code, out = c.es("POST", "/_query", {"query": timed}, allow=(400,))
        if code >= 400:
            bad += 1
            print(f"FAIL {name}: {(out or {}).get('error', {}).get('reason', out)}")
        else:
            print(f"ok   {name}: {len(out['values'])} rows, columns {[col['name'] for col in out['columns']]}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="run every query before writing")
    ap.add_argument("--target-file", default="targets/docker.env")
    args = ap.parse_args()
    if args.check and check(args.target_file):
        return 1
    version = (ROOT / "VERSION").read_text().strip()
    OUT.write_text(json.dumps(build(version), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

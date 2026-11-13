#!/usr/bin/env python3
"""Monte Carlo check of the Ingest Lag Profiler miss formula.

Simulates a detection rule that runs every I seconds and, on each run at time t,
searches events with @timestamp in [t - L, t]. An event with timestamp T arrives
(becomes searchable) at T + d. The event is caught if any run at or after its
arrival has a window that contains T.

The simulation checks every run in a range after arrival, not just the first, so
it also tests the claim that later runs never catch an event the first run missed.

Compares the simulated miss rate with the closed form:

    P(miss) = min(1, max(0, (d - (L - I)) / I))

Usage:
    simulate_rule_schedule.py                 # full run, 100k trials per point
    simulate_rule_schedule.py --trials 20000  # quicker
    simulate_rule_schedule.py --markdown      # print a markdown table for docs

Exits non-zero if any point is further from the formula than its tolerance
(4 standard errors plus 0.2 points).
"""

from __future__ import annotations

import argparse
import math
import random
import sys

# (interval seconds, look-back seconds, label)
SCHEDULES = [
    (300, 540, "every 5m, look back 9m"),
    (300, 300, "every 5m, look back 5m (no extra look-back)"),
    (60, 360, "every 1m, look back 6m"),
    (3600, 3900, "every 1h, look back 65m"),
]


def formula(d: float, interval: float, lookback: float) -> float:
    return min(1.0, max(0.0, (d - (lookback - interval)) / interval))


def lags_for(interval: float, lookback: float) -> list[float]:
    """Lag values that cover the interesting parts of the curve."""
    margin = lookback - interval
    points = {
        -120.0,                       # clock ahead: should always be caught when L >= I
        0.0,
        max(0.0, margin * 0.5),
        margin,                       # edge of the safe zone
        margin + interval * 0.2,
        margin + interval * 0.5,
        margin + interval * 0.8,
        lookback,                     # always missed from here
        lookback + interval,
    }
    return sorted(points)


def simulate(d: float, interval: float, lookback: float, trials: int,
             rng: random.Random, exec_delay: float = 0.0,
             refresh_delay: float = 0.0, runs_checked: int = 5) -> float:
    """Return the simulated miss rate.

    exec_delay:    each run starts up to this many seconds late (uniform), but its
                   window is still [start - L, start].
    refresh_delay: an event becomes searchable this many seconds after arrival.
    """
    misses = 0
    phase = rng.random() * interval  # the rule's schedule offset
    for _ in range(trials):
        ts = rng.random() * interval * 1000.0          # event timestamp
        searchable = ts + d + refresh_delay            # when a search can see it
        # The rule must also not search before the timestamp (window ends at "now").
        earliest = max(searchable, ts)
        # Index of the first scheduled run at or after `earliest`.
        k = math.ceil((earliest - phase) / interval)
        caught = False
        for j in range(k - 1, k + runs_checked):
            start = phase + j * interval
            if exec_delay:
                start += rng.random() * exec_delay
            if start < earliest:
                continue
            if start - lookback <= ts <= start:
                caught = True
                break
        if not caught:
            misses += 1
    return misses / trials


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--exec-delay", type=float, default=0.0,
                    help="max seconds a run starts late (default 0)")
    ap.add_argument("--refresh-delay", type=float, default=0.0,
                    help="seconds before an arrived event is searchable (default 0)")
    ap.add_argument("--markdown", action="store_true", help="print markdown tables")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    failures = 0

    for interval, lookback, label in SCHEDULES:
        rows = []
        for d in lags_for(interval, lookback):
            expected = formula(d, interval, lookback)
            observed = simulate(d, interval, lookback, args.trials, rng,
                                args.exec_delay, args.refresh_delay)
            se = math.sqrt(max(expected * (1 - expected), 1e-9) / args.trials)
            tolerance = 4 * se + 0.002
            ok = abs(observed - expected) <= tolerance
            failures += 0 if ok else 1
            rows.append((d, expected, observed, ok))

        if args.markdown:
            print(f"\n**{label}** (interval {interval}s, look-back {lookback}s)\n")
            print("| Lag (s) | Formula | Simulated | Match |")
            print("| ---: | ---: | ---: | :---: |")
            for d, e, o, ok in rows:
                print(f"| {d:g} | {e*100:.1f}% | {o*100:.1f}% | {'yes' if ok else 'NO'} |")
        else:
            print(f"{label}")
            for d, e, o, ok in rows:
                flag = "ok " if ok else "BAD"
                print(f"  {flag} lag {d:>7g}s  formula {e*100:6.2f}%  simulated {o*100:6.2f}%")

    if args.exec_delay or args.refresh_delay:
        print("\nNote: delays were added, so small differences from the formula are expected.")
    print(f"\n{'All points match.' if not failures else f'{failures} point(s) differ.'} "
          f"({args.trials} trials per point)")
    return 1 if failures and not (args.exec_delay or args.refresh_delay) else 0


if __name__ == "__main__":
    sys.exit(main())

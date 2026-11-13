# Miss model

How the profiler turns ingest lag into an expected miss rate, and how that was checked.

## The formula

A rule runs every *I* seconds. Each run searches events whose `@timestamp` falls in the window from *L* seconds ago up to now. An event with lag *d* is stamped at time *T* and becomes searchable at *T + d*.

```
P(miss) = min(1, max(0, (d - (L - I)) / I))
```

- **Lag up to L − I:** never missed. This is the rule's safe margin, the extra look-back beyond its interval.
- **Lag from L − I to L:** the chance of a miss rises in a straight line.
- **Lag of L or more:** always missed.

**Why.** The first run after the event becomes searchable falls at a uniformly random point in the next interval. That run catches the event only if its window still reaches back to *T*. Every later run's window starts later still, so if the first run misses the event, every run does.

**Clock problems.** An event stamped in the future (negative lag) is caught as long as *L ≥ I*. The first run after its timestamp has a window that reaches back far enough.

## Simulation check

[tests/model/simulate_rule_schedule.py](../tests/model/simulate_rule_schedule.py) simulates rule schedules directly, without using the formula. It places each event at a random point against the rule's schedule and checks every run after the event arrives, not just the first one. It ran 100,000 events per point.

With no execution or refresh delay, **all 34 points match the formula** within 4 standard errors (`make simulate`):

| Schedule | Lag | Formula | Simulated |
| --- | ---: | ---: | ---: |
| every 5m, look back 9m | 4m (edge of margin) | 0.0% | 0.0% |
| | 5m | 20.0% | 19.9% |
| | 6.5m | 50.0% | 50.0% |
| | 8m | 80.0% | 79.9% |
| | 9m | 100.0% | 100.0% |
| every 5m, look back 5m | 1m | 20.0% | 20.0% |
| | 2.5m | 50.0% | 50.2% |
| every 1m, look back 6m | 5m12s | 20.0% | 19.8% |
| | 5m30s | 50.0% | 49.9% |
| every 1h, look back 65m | 17m | 20.0% | 20.1% |
| | 35m | 50.0% | 50.1% |
| | 53m | 80.0% | 80.1% |

The simulation also confirms that later runs never catch an event the first run missed. It checks up to five runs after arrival.

## What the formula leaves out

The formula assumes runs start exactly on schedule and events are searchable the moment they arrive. Running the simulation with up to **10 seconds of run-start delay and 1 second of refresh delay** (`--exec-delay 10 --refresh-delay 1`) shows how much that matters:

| Schedule | Lag | Formula | With delays |
| --- | ---: | ---: | ---: |
| every 1m, look back 6m | 5m (edge of margin) | 0.0% | 3.7% |
| | 5m12s | 20.0% | 21.4% |
| | 5m30s | 50.0% | 51.6% |
| every 5m, look back 9m | 4m (edge of margin) | 0.0% | 0.7% |
| | 5m | 20.0% | 20.5% |
| every 5m, look back 5m | 0 (no lag at all) | 0.0% | 0.7% |
| every 1h, look back 65m | 5m (edge of margin) | 0.0% | 0.06% |

A late start shrinks the safe margin by about the length of the delay, so the error is roughly *delay ÷ interval*. It matters for rules that run every minute, is under 1 point for rules that run every 5 minutes, and is negligible for hourly rules.

A rule with **no extra look-back** (look-back equal to its interval) misses some events even when logs arrive instantly, because a run that starts late leaves a small gap behind it. This is the reason to give every rule at least some extra look-back, whatever its lag.

**What the lab measured (9.5.6).** Kibana's event log records each rule run's schedule delay. The validation rule's delay was **2.6 to 2.9 seconds on every run**, nearly constant and growing by about 0.05 seconds per run. A constant delay moves every run by the same amount, so it doesn't change the chance of a miss. Only *variation* in delay matters, and that was well under a second. The 10-second random delay above is therefore a pessimistic case. It would apply only on a busy Kibana where task delays vary a lot.

**The mechanism matches.** For every validation event, the lab predicted whether it would be caught from the rule's *actual* run start times. The rule: only the first run that starts after the event is searchable can catch it, and it does if the event's timestamp is within the look-back before that run started. In the smoke run, **all 150 predictions matched the real alerts**. See [validation-results.md](validation-results.md).

**Consequences:**
- For 1-minute rules, the profiler's estimate is slightly low near the edge of the margin. The README and dashboard should say so.
- The recommended look-back already rounds the 99th percentile up to a whole minute, which absorbs a few seconds of delay.
- The lab validation (Milestone 5) measures real Kibana task delays. If they turn out large, a configurable delay allowance can be added to the margin in the miss-rate query.

## What the estimate does not mean

`est_missed` counts *events in the data streams a rule searches* that fall outside its window. It is not a count of missed alerts. The rule's own query filter isn't applied, so a stream's overall lag stands in for the lag of the events the rule matches. See the design doc's "What the estimate means" section.

# Validation results

**Result: PASS.** Real Elastic Security detection rules missed late events at the rates the profiler's formula predicts. The profiler's own miss-rate query estimated the overall miss rate within 0.2 points.

Run `full1`, Nov 13, 2026: Elasticsearch and Kibana 9.5.6, trial license, single-node lab on Rancher Desktop (8 GB VM). Reproduce with `make validate`.

## Method

- **Two custom query rules** on `logs-lagtest.synthetic-default`. Both run every 5 minutes and look back 9 minutes, so the safe margin is 4 minutes. One searches `@timestamp`; the other is the same rule with `timestamp_override: event.ingested`. Alert suppression is off, and up to 1,000 alerts are allowed per run.
- **3,000 events: 600 at each of five lags**, 3, 5, 7, 9 and 11 minutes. Each event was stamped `lag` seconds before it was sent, and sent at a random moment within a 90-minute window (18 rule intervals). That puts arrivals at random points in the rule's schedule. An ingest pipeline set `event.ingested` to the arrival time.
- **Each event had a unique `event.id`**, which was matched to the alerts in `.alerts-security.alerts-default`. An event with no alert from a rule counts as missed by that rule.
- **Three checks:**
  1. The observed miss rate for each lag, against the formula.
  2. The pack's miss-rate query ([esql/miss-rate.esql](../esql/miss-rate.esql), through the real lookup join), against the observed overall rate.
  3. A per-event check against the rule's actual run times in Kibana's event log.

## Results

### Miss rate by lag (the `@timestamp` rule)

| Lag | Events | Formula | Observed | 95% interval | Difference | Pass (±5 points) |
| --- | ---: | ---: | ---: | --- | ---: | :---: |
| 3 min | 600 | 0% | 0.0% | 0.0 to 0.6% | 0.0 | yes |
| 5 min | 600 | 20% | 21.5% | 18.4 to 25.0% | +1.5 | yes |
| 7 min | 600 | 60% | 56.3% | 52.3 to 60.2% | −3.7 | yes |
| 9 min | 600 | 100% | 100.0% | 99.4 to 100% | 0.0 | yes |
| 11 min | 600 | 100% | 100.0% | 99.4 to 100% | 0.0 | yes |

Every 95% interval contains the formula's value.

### Profiler estimate against reality

| | Miss rate |
| --- | ---: |
| Profiler estimate (pack query, 3,000 events) | **55.8%** |
| Observed (alerts) | **55.6%** |
| Difference | +0.2 points |

### Rule mechanics

The check predicts each event's outcome from the rule's actual run start times in Kibana's event log. The model: only the first run that starts after the event is searchable can catch it, and it does if the event's timestamp falls within the 9 minutes before that run started.

- **0 of 2,963 predictions differed** from the real alerts.
- **37 events weren't judged.** They were sent within 2 seconds of a run starting, where searchability, which follows a 1-second refresh, can't be timed precisely enough to call.
- **Later runs never caught an event the first run missed**, which confirms the claim behind the formula.

### Searching on `event.ingested`

The copy of the rule that searches `event.ingested` **missed 0 of 3,000 events**, at every lag, including 11 minutes. Lag can't cause misses for these rules.

### Rule scheduling

All 20 runs over 100 minutes ran on schedule. Delays were between 0.05 and 3.0 seconds, averaging 1.4 seconds. Each run took under a second. Delays this small and steady don't affect miss rates; see [miss-model.md](miss-model.md).

## Smoke run

A 12-minute run with 30 events per lag (`smoke1`) checked the scripts beforehand.
- The per-event check matched 149 of 149 predictions.
- The profiler estimate came within 4.8 points.
- Two lags fell outside the 5-point tolerance, which is expected at 30 events per lag: the 95% intervals are about ±15 points wide.

## Limits of this test

- **One rule type and schedule.** The run covered a custom query rule every 5 minutes, looking back 9 minutes. EQL, threshold and new terms rules use the same search window, but they weren't run here. Sequences can only miss more than a single event, so the estimate is a lower bound for them.
- **A quiet single-node lab.** On a busy Kibana, rule start delays vary more, and per [miss-model.md](miss-model.md) that adds up to about *delay variation ÷ interval* to the miss rate near the edge of the margin.
- **Lag simulated by backdating `@timestamp`.** Arrival was real, measured by the ingest pipeline. Lag that happens before an event gets its ingest time isn't something the profiler sees in any case.

## Files

- **Lab output** (in `build/validation/`, not committed): `full1.jsonl` lists every event sent; `full1-score.txt` is the score output.
- **Scripts:** [scripts/lab/validate.py](../scripts/lab/validate.py) (setup, send, score).

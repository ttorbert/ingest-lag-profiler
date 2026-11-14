# Field reference

Every `lagprof-*` index except the lookup index shares one component template, [lagprof-common](../elasticsearch/component-templates/lagprof-common.json), with `dynamic: false`. Fields not listed here are kept in `_source` but not indexed.

`lagprof-rule-streams` has its own [small mapping](../elasticsearch/index-templates/lagprof-rule-streams.json). `LOOKUP JOIN` adds every field the lookup index maps, and a lookup field replaces any query column with the same name. So it maps only what pair documents contain. `lagprof.py lint` fails if that mapping shares a name with a column the miss-rate query creates before its join.

**Naming:**
- Profiler fields are under `lagprof.`.
- ECS fields keep their ECS names (`@timestamp`, `data_stream.*`, `host.name`).
- Measurements are flat.

## Indices

| Index | Type | One document per | Retention |
| --- | --- | --- | --- |
| `lagprof-rules` | Standard index | Enabled rule (ID = rule ID) | Rewritten every run; stale rules deleted |
| `lagprof-rule-streams` | Lookup index | Rule and data stream pair (ID = `<rule id>:<data stream>`) | Rewritten every run; stale pairs deleted |
| `lagprof-results` | Data stream | Rule, data stream and hour | 90 days |
| `lagprof-stream-stats` | Data stream | Data stream and hour | 90 days |
| `lagprof-host-lag` | Data stream | Data stream, host and hour (slow or skewed hosts only) | 30 days |
| `lagprof-runs` | Data stream | Workflow run | 90 days |

## Shared fields

| Field | Type | Meaning |
| --- | --- | --- |
| `@timestamp` | date | Results: start of the hour measured. Rules: when the run saw the rule. Runs: when the run finished. |
| `lagprof.run.id` | keyword | Workflow execution ID |
| `lagprof.window.start` | date | Start of the clock hour measured (results, stream stats, host lag) |
| `lagprof.space.id` | keyword | Kibana space of the rule (rules, pairs, results) or of the per-space copy (runs) |
| `data_stream.dataset`, `data_stream.namespace` | keyword | The measured data stream; the join keys |

## `lagprof-rules`

| Field | Type | Meaning |
| --- | --- | --- |
| `lagprof.rule.id` | keyword | Rule saved-object ID |
| `lagprof.rule.rule_id` | keyword | Rule's stable `rule_id` |
| `lagprof.rule.name` | keyword | Rule name |
| `lagprof.rule.type` | keyword | `query`, `eql`, `threshold`, `new_terms`, `threat_match`, `esql`, `machine_learning`, … `lagprof_notice` marks the notice written when a space has more than 10,000 enabled rules (ID `__lagprof_rule_cap_<space>`); it isn't a rule. |
| `lagprof.rule.interval_raw`, `from_raw` | keyword | Schedule as written, for example `5m` and `now-9m` |
| `lagprof.rule.interval_s`, `lookback_s` | long | Schedule in seconds |
| `lagprof.rule.index_patterns` | keyword | All patterns, as written |
| `lagprof.rule.index_patterns_csv` | keyword | Local patterns only, comma-joined, for `_resolve/index` |
| `lagprof.rule.remote_patterns_skipped` | boolean | The rule also searches remote clusters, which aren't measured |
| `lagprof.rule.data_view_id` | keyword | Set when the rule uses a data view |
| `lagprof.rule.timestamp_override` | keyword | The rule's timestamp override, if any |
| `lagprof.rule.timestamp_override_fallback_disabled` | boolean | `false` means the rule falls back to `@timestamp` when the override field is missing |
| `lagprof.rule.timestamp_field` | keyword | Field the rule's window uses: the override, or `@timestamp` |
| `lagprof.rule.supported` | boolean | The profiler can estimate this rule |
| `lagprof.rule.unsupported_reason` | keyword | Why not, when `supported` is false |

## `lagprof-rule-streams`

The mapping contains only these fields:
- `data_stream.name`, `data_stream.type`, `data_stream.dataset`, `data_stream.namespace`
- `lagprof.rule.id`, `name`, `interval_s`, `lookback_s` and `timestamp_field`
- `lagprof.space.id`, `lagprof.run.id` and `lagprof.parse_error`

The installer recreates the index when its mapping has fields this template doesn't. Every workflow run rebuilds the index, so nothing lasting is lost.

`lagprof.parse_error` is set when a data stream name doesn't split into type, dataset and namespace. That document has no dataset, so it never joins.

## `lagprof-results`

| Field | Type | Meaning |
| --- | --- | --- |
| `lagprof.rule.id`, `name`, `interval_s`, `lookback_s` | | The rule |
| `events` | long | Events that arrived in the hour (by `event.ingested`) in this data stream |
| `est_missed` | double | Expected number of those events outside the rule's window |
| `miss_rate_pct` | double | `est_missed / events × 100` |
| `lag_p50_s`, `lag_p95_s`, `lag_p99_s` | double | Lag percentiles (approximate), seconds |
| `lag_max_s` | long | Largest lag, seconds |
| `negative_lag_events` | long | Events stamped more than 60 seconds after they arrived |
| `recommended_lookback_s` | long | Interval + p99 lag rounded up to a minute, at least interval + 60 |

Rules that search on `event.ingested` have no results rows, because lag can't make them miss events.

## `lagprof-stream-stats`

| Field | Type | Meaning |
| --- | --- | --- |
| `events` | long | Measured events (arrived in the hour) plus unmeasured events (stamped in the hour) |
| `unmeasured_events` | long | Events with no `event.ingested` |
| `lag_p50_s` … `lag_max_s`, `negative_lag_events` | | As in results, for the whole data stream |

The two kinds of event are placed in the hour differently. Measured events count by when they arrived (`event.ingested`). Unmeasured events have no arrival time, so they count by when they happened (`@timestamp`).

## `lagprof-host-lag`

The fields are `host.name`, `events`, `lag_p95_s`, `lag_max_s` and `negative_lag_events`. A host and data stream gets a row only if its p95 lag is over 60 seconds, or it has events stamped in the future.

## `lagprof-runs`

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | keyword | `success`; `no_data` (the run worked but had nothing to measure: no logs in `logs-*` yet, or none with `event.ingested`); or `failed` |
| `reason` | keyword | For `no_data` runs, which of the two it was, in plain words |
| `error` | match_only_text | Error message for failed runs |
| `lagprof.mode` | keyword | `all-spaces` or `per-space` |
| `lagprof.wrote_shared_stats` | boolean | Per-space copies: whether this copy writes stream stats and host lag |
| `spaces_with_rules` | long | All-spaces runs: spaces that had enabled rules |
| `rules_seen`, `rules_supported` | long | Enabled rules, and how many are supported (rule cap notices aren't counted) |
| `streams_matched` | long | Rule and data stream pairs |
| `result_rows` | long | Rows the miss-rate query returned |

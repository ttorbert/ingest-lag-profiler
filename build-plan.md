# Ingest Lag Profiler: Build Plan

Nov 13, 2026 · companion to [ingest-lag-profiler-design.md](ingest-lag-profiler-design.md)

This plan turns the design into a shippable pack. It has three parts: changes the design needs before building, the repository layout with what goes in each file, and the build sequence with tasks and done criteria for each milestone.

Revised estimate: **37 to 56 hours** (the design said 24 to 38). The difference is:
- a new Milestone 0 to settle the open questions first
- a per-stream stats query
- Kibana space support with two workflow versions
- an installer for four deployment types
- tests

## Progress (Nov 13, 2026)

The lab runs on Rancher Desktop with Elasticsearch and Kibana 9.5.6. `make lab-up` builds it and installs the pack.

**Done and checked in the lab:**
- **Milestone 0:**
  - The simulation confirms the formula and quantifies the effect of late rule runs ([docs/miss-model.md](docs/miss-model.md)).
  - The workflow, ES|QL and rules API questions are answered (design doc, "Questions answered in the lab").
- **Milestone 1:** the stream-stats and host-lag queries, their templates, and the installer.
- **Milestone 2:**
  - Both ingest pipelines, with 19 out of 19 fixture cases passing.
  - Both workflow versions run stages 1 to 4.
  - Tested with 3 spaces. Stale cleanup in per-space mode stays inside its own space.
- **Milestone 3:**
  - The miss-rate query and result writing, through bulk create, so reruns skip existing rows.
  - The run summary in both the success and failure paths.
  - On lab traffic, the estimates matched the predicted rates within 0.3 points.
- **Installer:**
  - `check`, `install`, `status`, `uninstall`, `lint` and `render-workflow`, with workflow import through the API.
  - Running `install` again reports everything `unchanged`.
  - 30 unit tests against a fake cluster.
- **Alert rules:** both are installed, and Kibana accepted their ES|QL settings. They haven't been made to fire yet.

**Changes from the plan, found in the lab:**
- **Bulk writes, not loops.** One `_bulk` request per query or rule, built with Liquid and the `json` filter. The per-row write loops, rule paging and the inner loop's reference to an outer loop's item are all gone.
- **Role feature IDs:** `securitySolutionRulesV4` and Workflows sub-feature privileges. The old `siemV2` was accepted silently.
- **Rule indices created at install,** so the miss-rate query works before the first run.
- **Workflows matched by name,** because Kibana doesn't reuse a deleted workflow's ID.
- **Lab Compose file:** one-time `certs` and `setup` services, so `up` can run again on a running lab.
- **Data requirement:** events must carry `data_stream.*` fields. Fleet sets them; other sources may not.

**Done in the second session:**
- **Prebuilt rules:** 89% search on `event.ingested`, and 146 supported ones are at risk. The fallback to `@timestamp` is confirmed. See `tests/lab/analyze_prebuilt_rules.py`.
- **Workflow identity:** scheduled runs use the account that last saved the workflow. Added `LAGPROF_WORKFLOW_API_KEY` and `make lab-owner`, and the role is confirmed as enough for a full run.
- **Milestone 4:**
  - All ten dashboard panels are built through the Dashboards API and checked with a rendered screenshot.
  - The miss-rate alert fired on the lab's rules.
  - The stale-profiler query flags stopped workflows.
- **Installer fingerprints:** a changed file is updated even when the version hasn't moved.
- **`docs/fields.md` written.**
- **Scale:** found the 10 MB step-output limit and raised it to 64 MB. Stages 1 to 3 then ran over 2,295 rules in 111 seconds.
- **Bulk write checks:** a failed rule or pair write now stops the run and records why. Tested with a missing pipeline.
- **Milestone 5: validation PASSES** (`docs/validation-results.md`).
  - Every lag is within its interval of the formula.
  - The profiler estimated 55.8% against 55.6% observed.
  - All 2,963 per-event predictions matched the actual alerts.
  - The `event.ingested` rule missed 0 of 3,000.
- **Lookup index mapping:** `lagprof-rule-streams` now has its own 12-field mapping, so result fields can't enter the join.
  - A lint check catches name clashes with the query's columns from before the join.
  - The installer recreates an out-of-date lookup index.
  - Checked in the lab: the index was recreated and refilled by the next run, with identical query output and the validation still passing.
- **Query cost:** the miss-rate query now joins rules to per-second lag counts instead of raw events. That's about 20 times faster, and flat in rules per stream: 3 million events with 50 rules in 3.8 s. Results are identical apart from noise in the approximate percentiles.

**Next:**
1. Run the installer smoke tests on Elastic Cloud and Serverless. They need accounts.
2. Measure query time on a production-size cluster. The lab measured up to 3 million events an hour, at about 1 second per million.
4. ES|QL rule support, low priority: 348 of 354 prebuilt ES|QL rules search on `event.ingested`, so it matters only for custom ES|QL rules.

---

## Part 1: Design review, changes to make before building

**Status: all 20 changes have been applied to the [design doc](ingest-lag-profiler-design.md)** (Nov 13, 2026). This section stays here as the record of why.

Ordered by impact. Items marked **verify** are my best understanding and should be checked in the Milestone 0 lab.

### High impact

1. **Many prebuilt rules search on `event.ingested`, so lag doesn't affect them.** Elastic's prebuilt rules usually set `timestamp_override: event.ingested` (**verify** what share of the lab's enabled rules do this). The query rightly leaves these rules out, but the design doesn't say that this probably covers most prebuilt rules. The profiler's real audience is custom, third-party and imported rules that search on `@timestamp`. Change:
   - Say so in the problem statement and README.
   - Add an "immune (searches on event.ingested)" count to the rule coverage panel, so a mostly empty "Rules at risk" panel reads as good news, not as a broken tool.
   - These rules fall back to `@timestamp` when `event.ingested` is missing, because `timestamp_override_fallback_disabled` defaults to false (**verify**). So for them, the unmeasured events are the risk, not the measured lag. Show unmeasured events for each of these rules too.

2. **ECS `rule.*` fields collide with the lookup fields.** Firewall, IDS and proxy integrations fill in `rule.id` and `rule.name` in the events themselves. After `LOOKUP JOIN`, lookup fields replace source fields with the same name. That gives the right result now, but it's fragile and confusing. Rename the lookup and results fields to `lagprof.rule.*` (for example `lagprof.rule.id`). Do the same for `data_stream.name` in the lookup index.

3. **Unmeasured events are never stored.** `lagprof-results.unmeasured_events` is always empty, because the miss-rate query already drops events with no `event.ingested`. The unmeasured query's output has nowhere to go, yet the Measurement gaps panel reads it. Fix: add a fourth query and index, **`lagprof-stream-stats`**, one document per data stream per run. It holds `events`, `unmeasured_events`, the lag percentiles and `negative_lag_events`. This also fixes the next item.

4. **Per-stream panels would double count.** "Lag by data stream" and "Clock problems" read `lagprof-results`, which has one row per rule and stream pair. A stream that three rules search shows up three times. Point both panels at `lagprof-stream-stats`.

5. **Kibana spaces are missing.** Detection rules and workflows both belong to a space, so the find API only returns rules from the workflow's own space. Add `lagprof.space.id` to every rule-derived document. **Decided:** ship both approaches and let the user choose:
   - `lagprof-hourly-all-spaces.yaml` loops over every space using `/s/<space>/` paths (**verify** that `kibana.request` accepts the prefix).
   - `lagprof-hourly-per-space.yaml` is installed once in each space.

   [kibana/workflows/README.md](kibana/workflows/README.md) explains how to choose.

### Medium impact

6. **Fix the query window to whole hours.** `event.ingested >= NOW() - 1 hour` has no upper bound and moves with the run's start time, so back-to-back runs overlap or leave gaps. Use the last full hour, which needs no workflow parameter:
   ```sql
   | WHERE event.ingested >= DATE_TRUNC(1 hour, NOW()) - 1 hour
       AND event.ingested <  DATE_TRUNC(1 hour, NOW())
   ```
   Store `window.start` and `window.end` on every result. Build document IDs from `window.start`, rule and stream, and write with `_create`, so a rerun of the same hour is skipped rather than duplicated.

7. **Add an `@timestamp` bound to limit the scan.** Backing indices are split by `@timestamp`, so a filter on `event.ingested` alone may scan every backing index (**verify** in the cost test). Add `AND @timestamp >= DATE_TRUNC(1 hour, NOW()) - 25 hours`. This drops events more than 24 hours late, which every rule misses anyway. Count them separately in the stream stats as `lag_over_24h`, using a cheaper query if needed.

8. **Make sure writes are visible before the query.** The ES|QL query must see this run's `lagprof-rule-streams` writes. Add a `POST /lagprof-rule-streams/_refresh` step after stage 3, and run the stale-pair delete only after every stage 3 write succeeded. If the run fails partway, don't delete anything, because a partial run would remove good pairs.

9. **`lagprof-rules` also needs stale cleanup.** The same run-ID delete-by-query should apply to it, or disabled rules will stay on the coverage panel forever.

10. **Fix the recommended look-back.**
    - Set a floor: `GREATEST(rule.interval_s + 60, …)`. Today a negative p99, from bad clocks, gives a look-back shorter than the interval.
    - Recommend per rule, not per rule and stream. In the dashboard, take the highest value across the rule's streams.
    - One hour's p99 is noisy. In the dashboard, base the recommendation on the highest hourly p99 over 7 days.
    - Add `lagprof.rule.lookback_s` to the `BY` clause, so the dashboard can show the current look-back next to the recommendation.

11. **Show clock problems by host.** Goal 4 is about source systems, but negative lag is only counted per stream. Add `negative_lag_events` to the host query, and change its filter to `WHERE lag_p95_s > 60 OR negative_lag_events > 0`. Today the filter hides exactly the hosts with fast clocks.

12. **Say exactly what the estimate measures.** `est_missed` is the share of *events in the searched streams* that fall outside the rule's window. It is not missed alerts. The rule's query filter (for example `event.category:process`) isn't applied, so a stream's overall lag stands in for the lag of the events the rule matches. Put this in the README and on the dashboard. For new terms rules, also mention that late events can create false positives as well as misses.

### Small fixes and consistency

13. The license status disagrees with itself. The open questions mark Enterprise as confirmed, but the key decisions and requirements say it still needs confirming. Pick one and cite the subscriptions page.
14. The YAML sketch mixes `${{ }}` and `{{ }}`. Settle the syntax in Milestone 0.
15. Data streams only accept create operations. Writes must use `PUT /<ds>/_create/<id>` or `POST /<ds>/_doc`, not `PUT /<ds>/_doc/<id>`.
16. The resolve index API returns `indices`, `aliases` and `data_streams` separately. Read only `data_streams`, or the backing `.ds-…` indices will turn up in the results. Patterns that exclude (`-logs-foo*`) or point at remote clusters (`remote:logs-*`) must be handled or marked unsupported.
17. End every query with an explicit `KEEP`. ES|QL returns rows as arrays by position, so the workflow's write loop depends on a fixed column order.
18. Resolve each rule's patterns in **one** call (`/_resolve/index/<p1>,<p2>,…`), not one call per pattern. That cuts the calls from hundreds to the number of rules.
19. **Take the free edition out of the design.** It's out of scope. Change the "Workflows needs Enterprise" risk's mitigation to "Document the license requirement clearly; a free edition isn't planned."
20. **Use one naming convention throughout.** Profiler fields live under `lagprof.` (`lagprof.rule.*`, `lagprof.space.id`, `lagprof.run.id`, `lagprof.window.start`). ECS fields keep their ECS names (`@timestamp`, `data_stream.*`, `host.name`). Measurements stay flat (`events`, `miss_rate_pct`, …).

---

## Part 2: Repository layout and contents

```
ingest-lag-profiler/
├── README.md                         What it does, what it estimates, tested version and license, install
├── docs/
│   ├── design.md                     The design doc, moved here with Part 1 changes applied
│   ├── miss-model.md                 Formula, derivation, assumptions, simulation results
│   ├── fields.md                     Field reference for every lagprof-* index
│   └── validation-results.md         Lab results (Milestone 5)
├── elasticsearch/
│   ├── component-templates/
│   │   └── lagprof-common.json       Shared mappings: run.id, window.*, space.id, lagprof.rule.*
│   ├── index-templates/
│   │   ├── lagprof-rules.json
│   │   ├── lagprof-rule-streams.json index.mode: lookup
│   │   ├── lagprof-results.json      data stream, lifecycle data_retention 90d
│   │   ├── lagprof-stream-stats.json data stream, 90d (new)
│   │   ├── lagprof-host-lag.json     data stream, 30d
│   │   └── lagprof-runs.json         data stream, 90d
│   ├── ingest-pipelines/
│   │   ├── lagprof-rules-parse.json
│   │   ├── lagprof-streams-parse.json
│   │   └── lagprof-stamp.json        Sets @timestamp to arrival time on run summary documents
│   └── security/
│       └── lagprof-workflow-role.json
├── esql/
│   ├── miss-rate.esql
│   ├── stream-stats.esql             Replaces unmeasured.esql; also covers per-stream lag and clock problems
│   └── host-lag.esql
├── kibana/
│   ├── workflows/
│   │   ├── README.md                 How to choose between the two versions (written)
│   │   ├── lagprof-hourly-all-spaces.yaml   One workflow for every space (sketch written)
│   │   └── lagprof-hourly-per-space.yaml    One copy per space (sketch written)
│   ├── dashboards/lagprof-overview.ndjson
│   └── alerting/
│       ├── miss-rate.json
│       └── stale-profiler.json
├── scripts/
│   ├── lagprof.py                    Installer: check | install | status | uninstall, for all four targets
│   └── lab/
│       ├── docker-compose.yml        9.5.6 ES + Kibana on Rancher Desktop, TLS, trial license (Fleet agent profile: later)
│       ├── .env.example              Lab passwords and ports
│       ├── generate-events.py        Steady background traffic into several logs-* data streams with set lag per host
│       ├── setup-validation.sh       Test stream, pipeline, rules (@timestamp and event.ingested copies)
│       ├── send-lagged-events.py     Sends events with chosen lag, random jitter and unique IDs
│       └── score-validation.py       Joins sent IDs to alerts, computes the observed miss rate per lag, compares to the profiler
├── targets/
│   ├── docker.env                    Points at the lab (committed; lab-only passwords)
│   ├── self-managed.env.example
│   ├── cloud.env.example
│   └── serverless.env.example        Real target files are git-ignored
├── tests/
│   ├── model/simulate_rule_schedule.py   Monte Carlo check of the formula
│   ├── installer/                    Unit tests for target detection, Cloud ID decoding, template checks
│   ├── pipelines/                    _simulate fixtures and expected outputs for both pipelines
│   └── esql/                         Fixture data with known lags and expected query output
└── Makefile                          lab-up, lab-down, install TARGET=…, test, validate, package
```

### File details

**`lagprof-rules-parse`** (Painless script processor)
- Parse `interval` with `^(\d+)([smhd])$` into `rule.interval_s`.
- Parse `from` with `^now-(\d+)([smhd])$` into `rule.lookback_s`. Anything else gets `supported=false` and `unsupported_reason="unparsed from"`.
- `rule.timestamp_field = timestamp_override ?: "@timestamp"`. Also store `timestamp_override_fallback_disabled`.
- Set `supported=false` with a reason for `machine_learning`, `esql`, rules without `index` or `data_view_id`, and patterns that point at remote clusters.
- Handle the empty template output, such as `""` or `"null"`, that the workflow sends for missing fields.
- Set `lagprof.rule.index_patterns_csv` (patterns joined with commas). The workflow uses it in the `_resolve/index` path.
- Tests: `_simulate` cases for `5m`, `1h`, `30s`, `now-9m`, `now-1h`, a missing override, an ML rule, an ES|QL rule and junk input.

**`lagprof-streams-parse`**
- Dissect `%{data_stream.type}-%{data_stream.dataset}-%{data_stream.namespace}`.
- Set `_id = rule.id + ":" + data_stream.name`. The workflow can also set the ID in the path, which is simpler.
- Tests: `logs-system.security-default`, `logs-endpoint.events.process-prod`, and a name that doesn't parse, which should fail the document rather than write a bad pair.

**`miss-rate.esql`**: the design's query with Part 1 changes 2, 6, 7, 10 and 17 applied, plus `window.start` taken from `DATE_TRUNC`.

**`stream-stats.esql`**: grouped by `data_stream.dataset, data_stream.namespace`, using an `@timestamp` window so unmeasured events are counted. It returns `events`, `unmeasured_events = COUNT(*) - COUNT(event.ingested)`, `lag_p50/p95/p99/max`, `negative_lag_events`. Note that this uses an `@timestamp` window and the miss rate uses an `event.ingested` window. Write that down in `fields.md`.

**Workflows** (both sketches are written): the design's stages, with Part 1 changes applied:
1. Page through enabled rules, using a fixed list of page numbers and skipping empty pages, so no while loop is needed. The all-spaces version first lists spaces and loops over them.
2. Save rules through `lagprof-rules-parse`, refresh, then read back only the **supported** rules. That way the pipeline alone decides what's supported.
3. Make one `_resolve/index` call per supported rule, then loop over `data_streams` to write pairs.
4. Refresh, then delete stale documents. The per-space version limits the delete to its own space.
5. Run the miss-rate, stream-stats and host-lag queries. Each ends with `KEEP`, and the windows are whole clock hours.
6. Write rows with `_create` and IDs built from the window, so reruns don't duplicate. Write a `lagprof-runs` document on success and through the failure fallback.

Both use single-document writes. If Milestone 0 shows that `_bulk` with an NDJSON body works, switch to it. The miss-rate loop can write thousands of documents an hour.

**Dashboard** (`lagprof-overview.ndjson`): ES|QL-backed Lens panels. For "latest run", use `INLINE STATS latest = MAX(@timestamp) | WHERE @timestamp == latest` (**verify** that `INLINE STATS` is GA in 9.5). Panels:
- Rules at risk, rolled up per rule as `SUM(est_missed)/SUM(events)`, with current look-back and the recommendation from the 7-day highest p99
- Miss rate over time
- Lag by stream (stream-stats)
- Slowest hosts
- Clock problems by host and by stream
- Measurement gaps (stream-stats plus unsupported rules)
- Rule coverage: supported, unsupported and immune counts
- Profiler health

**Installer** (`scripts/lagprof.py`, Python standard library only):
- **Connection:** reads a target file (`--target-file targets/prod.env`) or environment variables, using the settings in the design doc's "Deployment targets and installer" section. It decodes `CLOUD_ID` into Elasticsearch and Kibana URLs, and sends API keys as `Authorization: ApiKey` to both.
- **Target detection:** `GET /` returns `version.number` and `version.build_flavor`, which is `serverless` on Serverless. If the detected type doesn't match `LAGPROF_TARGET`, it warns.
- **`check`:** stops on any blocking problem. Each target checks something different:

  | Target | What `check` looks at |
  | --- | --- |
  | Docker (the Rancher Desktop lab) | Version is 9.5.6. Starts a trial if the license is basic. |
  | Self-managed and Cloud | Version is 9.5.6 or later. License is Enterprise or trial. |
  | Serverless | Project type is Security. The Workflows API answers. Custom roles are available, otherwise it says the role is a manual step. |
  | All targets | Samples `logs-*` for `event.ingested` coverage. Warns only. |

- **`install` order:** component templates, then index templates, ingest pipelines, the role (Kibana role API), the workflow (Workflows API, **verify** the import endpoint), the dashboard (saved objects import) and the alert rules (alerting API). Each step compares `_meta.lagprof_version`, and skips or updates.
- **Workflow flags:** `--workflow all-spaces` (the default), or `--workflow per-space --space <id> [--shared-stats]`. It refuses to set `--shared-stats` if another space's copy already has it.
- **Per-target adjustments live in one function.** If the resource files need different values on different targets, that logic goes in this one function, never in duplicated files. The aim is to need no adjustments at all. A unit test fails if any template contains shard, replica or ILM settings.
- **`uninstall`:** removes resources in reverse order. Keeps history unless `--delete-data` is given.
- **Output:** a short line per resource (`created`, `updated`, `unchanged`, `skipped: reason`) and an exit code CI can use.

**Lab on Rancher Desktop** (`scripts/lab/`), using the dockerd (moby) engine with an 8 GB, 4 CPU VM:
- **`docker-compose.yml`:**
  - Elasticsearch and Kibana 9.5.6, single node, with security on and TLS on the Elasticsearch HTTP port, so the self-managed code path (CA certificate) is exercised.
  - A setup container creates certificates, sets the `kibana_system` password and starts the trial. `targets/docker.env` is committed and reads the random passwords from `scripts/lab/.env`.
  - `rancher-setup.sh` checks the engine and VM memory, and raises `vm.max_map_count`. With `--persist`, it keeps that setting across VM restarts.
  - The optional `agent` profile adds Fleet Server and an Elastic Agent with the System integration, for real data.
- **`generate-events.py`:** writes steady traffic to 4 or 5 `logs-*` datasets. Each simulated host has a lag profile (normal, slow, bursty or clock ahead), so every dashboard panel has something to show without needing Fleet.
- **Make targets:** `make lab-up` starts the lab and runs `lagprof.py install --target-file targets/docker.env`. `make lab-down` stops it, and `make lab-reset` wipes the volumes, which also gives a fresh 30-day trial.

**Alerts**: both are ES|QL rule-type alerting rules.
- Miss rate: rolls up per rule from the latest window and fires when the miss rate is above `1.0`. The threshold is a single editable value in the query.
- Stale profiler: counts `lagprof-runs` with `status == "success"` in the last 3 hours and fires when the count is `< 1`.

---

## Part 3: Build sequence

Each milestone ends with something that works on its own. Milestone 0 comes first because six open questions decide how the workflow is built.

### Milestone 0: Lab and spikes (5 to 8 h)

Tasks:
- [ ] Write `scripts/lab/docker-compose.yml` for 9.5.6 on Rancher Desktop, with security on, TLS on Elasticsearch and a trial license. Add a first version of `generate-events.py`. Install the prebuilt rules and enable a realistic set, plus 3 custom `@timestamp` rules. Use 3 spaces.
- [ ] Serverless and Cloud desk check: from the docs, find out whether Workflows, custom roles and the Kibana role, saved objects and alerting APIs are available on Serverless Security projects. If Workflows isn't available on Serverless, record Serverless as unsupported for now. The installer's `check` then reports that instead of failing partway.
- [ ] Write `tests/model/simulate_rule_schedule.py`. Simulate a rule with interval I and look-back L, random arrival phase, and fixed lag d, with 100k trials per point. Check the formula's curve for (5m, 9m), (1m, 6m) and (1h, 1h5m). This is a cheap first check before the lab test, and it goes into `docs/miss-model.md`.
- [ ] Workflow syntax spike: a throwaway workflow that tests each of these:
  - template syntax (`{{ }}` or `${{ }}`)
  - the run ID variable
  - `elasticsearch.request` to `/_query`, `/_resolve/index/a,b` and `/_bulk` with an NDJSON body
  - `foreach` over `values` arrays from an ES|QL response, with positional access such as `foreach.item[0]`
  - nested `foreach`
  - `kibana.request` with a `/s/<space>/` path, and the spaces API output shape
  - how an inner loop refers to an outer loop's item (the sketches use `steps.<loop>.item` as a placeholder)
  - `if` step condition syntax (the per-space version uses it for `write_shared_stats`)
  - Liquid filters in paths and values (`{% if %}`, `| size`)
  - ES|QL `params` passed through `elasticsearch.request`
  - workflow-level failure handling (`settings.on-failure.fallback`) and the error variable
  - the maximum number of `foreach` iterations and the execution timeout
- [ ] Rules API spike: check the `_find` paging limit and confirm the field names (`interval`, `from`, `index`, `data_view_id`, `timestamp_override`, `timestamp_override_fallback_disabled`). Record what share of enabled rules use `event.ingested`.
- [ ] ES|QL spike:
  - Does `LOOKUP JOIN` produce one row per matching lookup document?
  - What happens when names collide?
  - Does `DATE_TRUNC` work in the window?
  - Is `INLINE STATS` available?
  - Does a multivalued `data_stream.dataset` in the lookup index match? If it does, stage 3 could write one document per rule instead of one per pair, which simplifies the workflow a lot.
- [ ] Privileges spike: find the smallest working role for the workflow.

**Done when:** every open question in the design has an answer written into `docs/design.md`. Part 1 changes are applied to the design. The workflow approach (bulk or single writes, one or more spaces, pair or multivalued lookup) is decided.

### Milestone 1: Lag measurement (5 to 7 h)

- [ ] Write `stream-stats.esql` and `host-lag.esql`, with fixtures in `tests/esql/`.
- [ ] Create index templates for `lagprof-stream-stats` and `lagprof-host-lag`.
- [ ] Write the installer skeleton. It needs target files, Cloud ID decoding, target detection, `check`, and `install`/`status` for templates only. Test it against the Rancher Desktop lab, with unit tests in `tests/installer/`.
- [ ] Run the queries by hand, write the results with a short script, and build the first two panels.

**Done when:** in the lab, adding 3 minutes of lag to one host makes that host top the slowest hosts panel, and its stream's p95 moves.
**Content:** blog post 1, "How late are your logs?", with lag measurement only.

### Milestone 2: Rule inventory (9 to 14 h)

- [ ] Write both ingest pipelines, with `_simulate` tests in `tests/pipelines/`.
- [ ] Create templates for `lagprof-rules` and the `lagprof-rule-streams` lookup index.
- [ ] Get stages 1 to 4 of **both** workflow versions running, fixing the sketches with what Milestone 0 found.
- [ ] Test spaces in a lab with 3 spaces: rules in each, plus one space with no rules. Run the all-spaces version, then switch to per-space following the README. In per-space mode, check that one copy's stale delete leaves the other spaces' pairs alone. Check what happens with zero and with two copies set to `write_shared_stats: true`.
- [ ] Add the rule coverage panel: supported, unsupported with reasons, and immune.

**Done when:** after one run, every enabled rule is in `lagprof-rules` with the right seconds values. Every pattern that matches a `logs-*` data stream gives pairs. Disabling a rule removes it from both indices on the next run. A forced failure in stage 3 deletes nothing.

### Milestone 3: Miss estimates and history (5 to 7 h)

- [ ] Write `miss-rate.esql` with the Part 1 changes, plus fixtures with known lags and expected miss rates. Build these from the same numbers as the simulation.
- [ ] Create templates for `lagprof-results` and `lagprof-runs`, with lifecycle retention.
- [ ] Build workflow stages 5 and 6: positional row mapping, `_create` with deterministic IDs, and a run summary in both the success and failure paths.
- [ ] Measure the query's runtime and memory in the Rancher Desktop lab. Scale `generate-events.py` up to the highest rate the lab can sustain (aim for 10 million or more events an hour), at 1, 5 and 20 rules per stream. Record the results with the lab's size, and describe how the cost grows rather than claiming production numbers. Repeat one measurement during the Cloud smoke test in Milestone 5.

**Done when:** hourly runs fill history for 24 hours without duplicates. Running the same hour again by hand changes nothing. The query runtime at the target volume is recorded.

### Milestone 4: Dashboard, alerts and full installer (6 to 9 h)

- [ ] Build the remaining panels on ES|QL, with per-rule rollups and the 7-day recommendation.
- [ ] Create both alerting rules and test each one: inject lag to make the miss-rate rule fire, and disable the workflow to make the stale rule fire.
- [ ] Export the dashboard and rules to `kibana/`.
- [ ] Finish the installer:
  - pipelines, role, both workflow modes, dashboard and alert rules
  - `uninstall`, and `status` with versions
  - the `--shared-stats` guard
  - the check that every target file loads with no per-target adjustments
- [ ] Write the `targets/*.env.example` files and the README's install section for each target.

**Done when:**
- `make lab-reset && make lab-up` produces a working dashboard and alerts with no manual steps.
- Running `install` a second time reports everything `unchanged`.
- `uninstall` leaves nothing behind except history.

### Milestone 5: Validation and release (7 to 11 h)

The full validation runs in the Rancher Desktop lab. Keep the machine awake for the whole 3-hour run (for example with `caffeinate -i` on macOS). A paused cluster skips rule runs and creates misses for the wrong reason.

- [ ] Write `setup-validation.sh`. It creates the test stream with a `final_pipeline` that sets `event.ingested`, plus two rules: one on `@timestamp` and one on `event.ingested`. Both run every 5 minutes, look back 9 minutes, have suppression off, and allow up to 1,000 alerts.
- [ ] Write `send-lagged-events.py`. It interleaves the lag values, adds random jitter to send times so they land at random points in the rule's schedule, and gives each event a UUID.
- [ ] Write `score-validation.py`. It matches each sent UUID to its alert (searching `.alerts-security*`, **verify** the pattern), computes the observed miss rate per lag with 95% intervals, and compares the profiler's estimate.
- [ ] Run the further checks from the design: the ingest-time rule misses nothing, unmeasured events are counted, future-stamped events show as clock problems, the slow host shows up, and a disabled rule drops out.
- [ ] Write `docs/validation-results.md` and the README, with the tested version, the license, the "estimates" wording, and what the estimate does and doesn't measure.
- [ ] Installer smoke tests, time-boxed to about 1 hour each. On each target, run `check`, `install`, one workflow run (trigger it by hand), `status` and `uninstall`:
  - **Self-managed:** the Rancher Desktop lab with an API key and CA certificate instead of the `elastic` user
  - **Elastic Cloud:** a hosted 9.5.6 trial deployment, plus one repeat of the query cost test
  - **Serverless:** a Security project trial, if the Milestone 0 desk check found Workflows available

  Record the results, and the Serverless test date, in the README's compatibility table.
- [ ] Tag `v0.1.0` and attach a packaged zip of `elasticsearch/`, `esql/`, `kibana/`, `targets/*.example` and `scripts/lagprof.py`.

**Change the sample size in the validation plan.** At 200 events per lag value, the standard error at a 60% miss rate is about 3.5 points, so a ±5 point tolerance would fail a correct model about 1 time in 6. Use either:
- **600 or more events per lag value** (standard error 2 points or less), keeping ±5 points; or
- a pass rule of "expected rate within the observed 95% interval".

600 events × 5 lag values over 3 hours is about 85 events per run, well under the 1,000 alert limit.

**Done when:** the pass criteria are met, or the formula is corrected and the correction is written up.
**Content:** blog post 2, the validation results, confirming or correcting the formula in public.

---

## Estimate summary

| Milestone | Hours |
| --- | --- |
| 0. Lab and spikes | 5–8 |
| 1. Lag measurement | 5–7 |
| 2. Rule inventory | 9–14 |
| 3. Miss estimates and history | 5–7 |
| 4. Dashboard, alerts and full installer | 6–9 |
| 5. Validation and release | 7–11 |
| **Total** | **37–56** |

## Decisions

- **Spaces:** both versions ship, and the user chooses. See [kibana/workflows/README.md](kibana/workflows/README.md).
- **Free edition:** out of scope.
- **Lab:** Rancher Desktop (dockerd engine, VM raised to 8 GB and 4 CPUs). Elastic Cloud and Serverless are only used for short installer smoke tests in Milestone 5.
- **Install targets:** one installer, `scripts/lagprof.py`, loads the same resource files into the Rancher Desktop lab, self-managed, Elastic Cloud and Serverless.

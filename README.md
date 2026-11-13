# Ingest Lag Profiler

Estimates how many events each Elastic Security detection rule misses because logs arrive late. It runs entirely inside Elastic: an hourly workflow measures the lag between when events happened (`@timestamp`) and when they arrived (`event.ingested`). It then estimates each rule's miss rate from its schedule and look-back, and keeps the history in ordinary indices with a dashboard and alerts.

> **Status: in development (v0.1.0-dev).** Everything runs end to end on 9.5.6 in the lab: workflows, queries, pipelines, installer, dashboard and alerts. **The lab validation against real rule alerts passed:** the profiler estimated 55.8% against an observed 55.6% ([results](docs/validation-results.md)). Installer tests on Elastic Cloud and Serverless are still to do. See [build-plan.md](build-plan.md).

**Results are estimates, not proof.** `est_missed` is the expected number of events, in the data streams a rule searches, that fall outside the rule's window. It isn't a count of missed alerts. See [docs/miss-model.md](docs/miss-model.md).

**Who it's for.** In 9.5.6, **89% of Elastic's prebuilt rules** (2,039 of 2,297) search on `event.ingested`, so lag can't push events out of their window. They do fall back to `@timestamp` for events that have no `event.ingested`, and the profiler counts those events.

The rules at risk are:
- the 146 supported prebuilt rules that search on `@timestamp`
- custom, third-party and imported rules

The dashboard's *Rule coverage* panel shows which group each rule is in.

**Cost.** The hourly miss-rate query takes about 1 second per million events in the hour on a single-node laptop lab, however many rules search each stream.

## Requirements

| | |
| --- | --- |
| Elastic Stack | 9.5.6 or later (Workflows, multi-field `LOOKUP JOIN`) |
| License | Enterprise or trial (Workflows) |
| Serverless | Security project; Workflows availability still to be confirmed |
| Data | `logs-*` data streams with `@timestamp` and `event.ingested` (Fleet integrations set both) |
| Installer | Python 3.9 or later; no packages needed |

## Install

1. Create a target file from one of the examples in [targets/](targets/): `self-managed`, `cloud` or `serverless`.
2. Check the target, then install:

   ```bash
   python3 scripts/lagprof.py check   --target-file targets/prod.env
   python3 scripts/lagprof.py install --target-file targets/prod.env
   ```

   This installs the index templates, ingest pipelines, workflow role, workflow and alert rules. The workflow starts its first run straight away, then runs hourly.

3. Choose a workflow version. **All spaces** is the default. See [kibana/workflows/README.md](kibana/workflows/README.md) for when to use **per space** instead:

   ```bash
   python3 scripts/lagprof.py install --target-file targets/prod.env \
       --workflow per-space --space soc --shared-stats
   ```

Other commands: `status` (installed versions), `uninstall` (keeps history unless `--delete-data`), `lint` (offline checks).

### Workflow identity

A workflow's **scheduled runs use the permissions of the account that last saved it.** Manual runs use whoever starts them. Both were confirmed on 9.5.6. If the installer saves the workflow as an administrator, the hourly runs get administrator permissions.

To keep them limited:

1. Create a dedicated user, for example `lagprof-runner`, with only the `lagprof-workflow` role. The installer creates the role.
2. Sign in as that user and create an API key: `POST /_security/api_key` with `{"name": "lagprof-workflow-owner"}`.
3. Add `LAGPROF_WORKFLOW_API_KEY=<encoded key>` to your target file and run `install` again. The workflow is then saved by `lagprof-runner`.

Without the key, `install` warns that scheduled runs will use the installer's permissions.

## Lab (Rancher Desktop)

The lab runs Elasticsearch and Kibana 9.5.6 with security, TLS and a trial license, on [Rancher Desktop](https://rancherdesktop.io) with the dockerd (moby) engine.

```bash
make lab-up        # checks Rancher Desktop, creates passwords, starts the lab, installs the pack
make lab-traffic   # sends lab events with slow, bursty and clock-skewed hosts
make lab-down      # stop (keeps data); make lab-reset deletes everything
```

- **Rancher Desktop:** needs at least 6 GB for its VM (8 recommended). Set it in Preferences > Virtual Machine, or run `rdctl set --virtual-machine.memory-in-gb 8`.
- **Kernel setting:** `make lab-up` raises the VM's `vm.max_map_count` until the next VM restart. To keep it across restarts, run `scripts/lab/rancher-setup.sh --persist` once.
- **Login:** Kibana is at http://localhost:5601. The user is `elastic`, and the password is in `scripts/lab/.env`. The dashboard is **Ingest Lag Profiler** under Dashboards.
- **`make lab-owner`** creates a `lagprof-runner` user and saves the workflow with its API key. See [Workflow identity](#workflow-identity).
- **`make validate`** runs the lab validation: real rules, events with known lag, and alerts matched to events (about 105 minutes). See [docs/validation-results.md](docs/validation-results.md).

## Development

```bash
make test             # lint, unit tests, quick formula simulation (no cluster needed)
make simulate         # full formula simulation
make test-pipelines   # ingest pipeline fixtures against the lab
```

| Path | Contents |
| --- | --- |
| [elasticsearch/](elasticsearch/) | Component and index templates, ingest pipelines, workflow role |
| [esql/](esql/) | The three hourly queries; `lint` checks they match the workflows |
| [kibana/workflows/](kibana/workflows/) | The all-spaces and per-space workflows |
| [kibana/alerting/](kibana/alerting/) | Miss-rate and stale-profiler alert rules |
| [scripts/lagprof.py](scripts/lagprof.py) | Installer for Docker, self-managed, Cloud and Serverless |
| [scripts/lab/](scripts/lab/) | Rancher Desktop lab, traffic generator, validation |
| [kibana/dashboards/](kibana/dashboards/) | Dashboard; `build.py` writes the JSON from readable ES\|QL queries |
| [tests/](tests/) | Formula simulation, installer unit tests, pipeline fixtures |
| [docs/](docs/) | Miss model, field reference, validation results |

Design: [ingest-lag-profiler-design.md](ingest-lag-profiler-design.md). Build plan: [build-plan.md](build-plan.md).

# Choosing a workflow

Detection rules belong to a Kibana space, and a rule search only returns rules from one space. The profiler comes in two versions, which differ in how they deal with that. Install **one** of them, not both.

| | All spaces | Per space |
| --- | --- | --- |
| File | `lagprof-hourly-all-spaces.yaml` | `lagprof-hourly-per-space.yaml` |
| Copies to install | One, in the Default space | One in each space that has detection rules |
| Finds spaces | Automatically, from the spaces API, every run | You list them, by installing a copy in each and setting its `space_id` |
| New space added later | Picked up on the next run | Not profiled until someone installs a copy there |
| Workflow role | Needs to read Security rules **in every space** | Each copy's role only needs its own space |
| Cluster cost per hour | Scans `logs-*` 3 times | Scans `logs-*` once per space, plus twice in the copy that writes shared stats |
| If a run fails | No space gets new results that hour | Only that copy's space is affected |
| Setup mistakes to watch for | Few | Exactly one copy must set `write_shared_stats: true`. If none do, the stream and host panels go empty. If several do, their duplicate rows are skipped, but each one repeats two `logs-*` scans. The installer refuses a second `--shared-stats` copy unless you add `--force`. |
| Dashboard and alerts | One dashboard and two alert rules, in the Default space. Results carry `lagprof.space.id`, so you can filter by space. | Each copy gets its own dashboard, *Ingest Lag Profiler (<space>)*, and alert rules, both covering only that space's rules and workflow. Stream and host panels are shared. |
| Role | `lagprof-workflow` | One per copy: `lagprof-workflow-<space>` |

## Which to pick

**Use all spaces unless you can't.** It's simpler to run, finds new spaces on its own, and scans the logs the fewest times.

**Use per space when one role that reads rules in every space isn't acceptable.** This is typical for a managed security provider, or any setup where each space belongs to a different team or customer and access is kept separate on purpose. Each copy runs with a role limited to its own space, and each space's results can be managed by that space's owners.

Either way, the raw logs (`logs-*`) aren't divided by space. Rules in every space search the same data streams, so the stream and host statistics are the same for everyone. That's why per-space mode writes them from one copy only.

## Installing the all-spaces version

`scripts/lagprof.py install` does all of this for you (see the [setup guide](../../docs/setup-guide.md)). These are the manual steps, and what the installer does.

1. Create the workflow role with Security rule read privileges in all spaces (`spaces: ["*"]`). See `elasticsearch/security/lagprof-workflow-role.json`.
2. Import `lagprof-hourly-all-spaces.yaml` in the Default space.

Each run fetches up to 10,000 enabled rules per space in one call. Rules after that aren't profiled, and the dashboard's *Unsupported rules* panel shows a notice for that space.

## Installing the per-space version

`lagprof.py install --workflow per-space --space <id> [--shared-stats]` does this for one space. Manually, for each space with detection rules:

1. Create a role for that space only.
2. Import `lagprof-hourly-per-space.yaml` into the space.
3. Edit the copy (`lagprof.py render-workflow --workflow per-space --space <id>` writes an edited copy to `build/`):
   - `name`: replace `CHANGEME` with the space ID, so the copies can be told apart.
   - `consts.space_id`: the space ID.
   - `consts.write_shared_stats`: `true` in exactly one copy and `false` in the rest. The Default space is a good choice for the `true` copy.

Check: after the first hour, the Profiler health panel should show one successful run per space, and exactly one of them with `wrote_shared_stats: true`.

## Switching between versions

Uninstall the current version (`uninstall`, or `uninstall --space <id>` for each per-space copy; history is kept), then install the other one. The installer refuses to install one version while the other is installed, unless you add `--force`. The next run's stale-pair cleanup removes leftover rule documents. In per-space mode, a space with no copy any more keeps its old rule documents until you delete them:

```
POST /lagprof-rules,lagprof-rule-streams/_delete_by_query
{ "query": { "term": { "lagprof.space.id": "<space id>" } } }
```

History in `lagprof-results` is kept either way.

Removing some per-space copies is safe: while any profiler workflow remains, `uninstall` keeps the shared templates, pipelines and indices.

## Status

Both versions have run successfully on 9.5.6 in the Rancher Desktop lab:
- **All spaces:** 2 spaces with rules, plus 1 without.
- **Per space:** three copies, with the shared-stats switch confirmed on and off.

`scripts/lagprof.py install` imports the chosen version through the Workflows API. Use `--no-import-workflow` to get only the YAML in `build/`. Kibana runs a newly imported scheduled workflow once straight away.

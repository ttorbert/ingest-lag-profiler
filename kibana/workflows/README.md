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
| Dashboard | Same for both. Results carry `lagprof.space.id`, so you can filter by space. | Same |

## Which to pick

**Use all spaces unless you can't.** It's simpler to run, finds new spaces on its own, and scans the logs the fewest times.

**Use per space when one role that reads rules in every space isn't acceptable.** This is typical for a managed security provider, or any setup where each space belongs to a different team or customer and access is kept separate on purpose. Each copy runs with a role limited to its own space, and each space's results can be managed by that space's owners.

Either way, the raw logs (`logs-*`) aren't divided by space. Rules in every space search the same data streams, so the stream and host statistics are the same for everyone. That's why per-space mode writes them from one copy only.

## Installing the all-spaces version

1. Create the workflow role with Security rule read privileges in all spaces (`spaces: ["*"]`). See `elasticsearch/security/lagprof-workflow-role.json`.
2. Import `lagprof-hourly-all-spaces.yaml` in the Default space.
3. If you have more than 2,500 enabled rules in any one space, lengthen `consts.rule_pages`.

## Installing the per-space version

For each space with detection rules:

1. Create a role for that space only.
2. Import `lagprof-hourly-per-space.yaml` into the space.
3. Edit the copy:
   - `name`: replace `CHANGEME` with the space ID, so the copies can be told apart.
   - `consts.space_id`: the space ID.
   - `consts.write_shared_stats`: `true` in exactly one copy and `false` in the rest. The Default space is a good choice for the `true` copy.

Check: after the first hour, the Profiler health panel should show one successful run per space, and exactly one of them with `wrote_shared_stats: true`.

## Switching between versions

Disable or delete the current workflow, then install the other one. The next run's stale-pair cleanup removes leftover rule documents. In per-space mode, a space with no copy any more keeps its old rule documents until you delete them:

```
POST /lagprof-rules,lagprof-rule-streams/_delete_by_query
{ "query": { "term": { "lagprof.space.id": "<space id>" } } }
```

History in `lagprof-results` is kept either way.

## Status

Both versions have run successfully on 9.5.6 in the Rancher Desktop lab:
- **All spaces:** 2 spaces with rules, plus 1 without.
- **Per space:** three copies, with the shared-stats switch confirmed on and off.

`scripts/lagprof.py install` imports the chosen version through the Workflows API. Use `--no-import-workflow` to get only the YAML in `build/`. Kibana runs a newly imported scheduled workflow once straight away.

# Why use the Ingest Lag Profiler

## The problem: detection gaps nobody sees

Security teams rely on detection rules to catch threats in their logs. Each rule checks a fixed slice of recent time on a schedule, for example the last 9 minutes, every 5 minutes. Logs don't always arrive promptly. Agents queue events, networks slow down, forwarders back up, cloud services deliver audit logs in batches.

**When a log arrives after the rule has already checked its time slice, the rule never sees it.** Nothing breaks and nothing reports an error. The dashboards look normal, and the alert that should have fired simply doesn't.

Most teams have no measurement of this. How far back each rule looks is usually a default, or a guess made when the rule was written, and it's rarely revisited as data sources change.

## What the profiler does

Every hour, inside your existing Elastic deployment, the profiler:

1. **Measures how late logs arrive** for every data source your detection rules use.
2. **Estimates, for each rule, what share of its events arrive too late to be checked**, and recommends a setting that would close the gap.
3. **Points to the cause:** a slow host, a source system with a wrong clock, or logs that can't be measured at all.
4. **Keeps a history and alerts** when any rule's estimated miss rate passes a threshold (1% by default).

It runs entirely in Elastic, using Workflows, ES|QL, a dashboard and alerting rules. There's no extra infrastructure, agent or external service.

## An example

In the lab, a rule ran every 5 minutes and looked back 6 minutes, a common setup. One of its six log sources delivered events about 4 minutes late, and another was occasionally 10 minutes late.

- **Without the profiler:** the rule looked healthy. It ran on time, raised alerts and reported no errors.
- **With the profiler:** an estimated **11.7% of the rule's events arrived too late to be checked**, with a recommended look-back of 16 minutes.

The method behind that number was tested against real rules: the profiler estimated **55.8%** of events missed, and the real rules missed **55.6%** ([validation results](validation-results.md)).

## Business value

| Value | How |
| --- | --- |
| **Lower risk of undetected attacks** | Shows exactly which rules are missing which share of their data, so gaps are closed instead of assumed away. |
| **Evidence for audits and leadership** | A 90-day record of measured detection coverage, per rule and per hour, gives an objective answer to "are our detections actually seeing the data?" |
| **Targeted fixes instead of blanket changes** | Recommends a setting per rule, so only the rules that need longer windows get them. Making every window longer costs search capacity. |
| **Accountability for data sources** | Lag per data source and per host shows which sources, teams or vendors deliver late, with numbers to back up the conversation. |
| **Better return on detection content** | Rules are only as good as the data they see. The profiler protects the investment in writing and tuning them. |

## Operational value

| Team | What they get |
| --- | --- |
| **Detection engineering** | Per-rule miss rates and recommended look-backs, plus the knowledge of which rules are affected by lag at all: in 9.5.6, 89% of Elastic's prebuilt rules aren't. |
| **SOC** | Confidence that a quiet hour means nothing happened, not that the data arrived late. An alert when that confidence is at risk. |
| **Platform and ingest operations** | Early warning of slowing pipelines, often before rules start missing events. Slow hosts and broken clocks are identified by name. Data sources without arrival times are found. |
| **Security leadership** | One coverage measure to track over time: the share of measured rules under 1% estimated misses. |

## What it costs

| Cost | Detail |
| --- | --- |
| **License** | Elastic Enterprise, which Workflows needs. Many Elastic Security customers already have it. |
| **Compute** | One query per hour. In the lab it took about **1 second per million events in the hour**, whatever the number of rules, on a single node. Larger clusters spread the work. |
| **Storage** | Small. Summary rows only: one per rule, data source and hour, kept for 90 days. No log data is copied. |
| **People** | About 30 minutes to install ([setup guide](setup-guide.md)), then a few minutes a week, plus time to act on what it finds. |

## What it doesn't do

Being clear about the limits matters as much as the value:

- **It estimates; it doesn't prove.** The figures are expected miss rates from measured lag, validated in the lab but still estimates. They describe events searched, not alerts missed.
- **It doesn't change rules.** It recommends, and a person decides.
- **It can only measure logs with an arrival time** (`event.ingested`). It lists the sources without one, so the gap is visible.
- **Some rule types aren't covered:** machine learning rules, ES|QL rules, and rules that use a data view. They're listed so nothing is hidden.
- **It can't see delay that happens before a log is timestamped.** If a source or collector stamps `@timestamp` when it sends or receives a log, rather than when the event happened, earlier delay is invisible. Logs stamped when the event happens are measured fully, even if a device holds them for a while before sending.

## Getting started

1. Install it: [setup guide](setup-guide.md).
2. Learn to read the results: [user guide](user-guide.md).
3. See how the method was tested: [validation results](validation-results.md) and [miss model](miss-model.md).

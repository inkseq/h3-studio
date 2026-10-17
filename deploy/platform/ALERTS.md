# Operator push alerts (#218)

The finite host monitor runs every 60 seconds through a systemd timer. It reads
Linux CPU/memory counters, exact original Compose service health, the app's
loopback counters, and `python -m studio_platform.alert_snapshot` inside the
existing app container. It never calls a provider, rents/stops capacity, changes
a job, hydrates secrets, or runs another business database. The evaluator is the
stdlib-only `studio_platform/alert_conditions.py` installed as `alert_conditions.py`
next to `alert_monitor.py` in `/opt/inkseq-alert-monitor-v1`.

Install reviewed exact source bytes and units through the protected host update
procedure. This independent root-owned directory contains exactly
`alert_monitor.py`, `alert_conditions.py`, and `deployment_target.py`. Copy
`deployment_target.py` byte-for-byte from the accepted release revision; record
and compare its SHA-256 with the approved host helper before installation. It
uses the existing protected host target record and contains no credentials.
Do not add monitor files to the release helper directory: its exact file closure
is an independent release protection. Neither installation nor future monitor
updates change that helper closure or its active marker.

Install the directory root-owned mode 0755, all three Python files root-owned
mode 0644, and unit/timer files root-owned mode 0644. No parent may be writable
by a non-root user. The unit runs Python with `-B`, so the directory stays
read-only without bytecode writes. No timer is enabled by adding these source
files. Accept staging before production using the same reviewed monitor bytes
and an environment-specific protected configuration.

## Protected configuration

Create `/etc/inkseq/alert-monitor.json` as a root-owned regular file, mode 0600,
with root-controlled parent directories. For staging its closed configuration is:

```json
{
  "version": 1,
  "environment": "staging",
  "instance_id": "i-07805df7bd6984bd9",
  "topic_arn": "arn:aws:sns:ap-southeast-1:829135631045:sixnine-staging-operator-alerts",
  "thresholds": {}
}
```

The sealed deployment target must match. Production uses its own instance ID and
`sixnine-production-operator-alerts` topic in the same account/region; it cannot
publish to the staging topic. The topic/subscription is provisioned separately.
The unit excludes static credential/config sources and IMDSv1; the SDK must
resolve the existing EC2 instance role. No keys are copied or logged.

Threshold overrides are a small closed dictionary accepted by `alert_conditions`:
CPU above 70% or memory above 85% for 900 seconds; unhealthy required processes
for 900 seconds; app health down for 120 seconds; at least 3 server errors and
5% error ratio in the rolling 300-second request window. GPU maximum runtime
defaults to 7200 seconds, stuck work 1800 seconds, repeated wake count above 10.
`terminal_reservation_stale` reports original terminal jobs or stopped/destroyed
instances whose admission reservation is still held for more than
`terminal_reservation_seconds` (default 3600 seconds). References are deduplicated
across account layers. Age uses the retained terminal event or native stop time;
if unavailable, the evaluator requires a sustained observed interval. Releasing
an admission hold into `invoice_pending` ends this held-reservation condition,
not the invoice obligation: its original estimate remains in Settings exposure
until actual billing is reconciled. The alert never releases money or stops work.

Existing business GPU idle limits and Sydney-day test-spend accounting remain
the authority. Spend alerts represent **projected exposure / 保守预算占用**,
including unresolved reservations; they do not represent an actually paid bill.
Changing thresholds changes alert sensitivity, not budget or
stop policy. Edit/review the protected config atomically; the next timer pass
reads it. Missing observations never resolve an active condition, and gaps over
180 seconds reset an unfired sustained interval.

## Delivery and recovery semantics

`/var/lib/inkseq-alert-monitor` is mode 0700 and holds only bounded notification
deduplication, pending delivery and prior CPU counters. One file lock prevents
concurrent runs. Atomic fsync/replace persists the evaluator transition and a
pending event before SNS publication. An acknowledgement removes the pending
event. A failure retains it with exponential retry from 60 to 900 seconds. A
missing acknowledgement may mean SNS already accepted the request, so retries
can duplicate an email; this is not exactly-once delivery. Pending events are
ordered, at most 128, and at most three are attempted per pass. Corrupt/foreign
state fails closed rather than resetting and flooding recipients.

Messages contain only allowlisted condition code, environment, canonical UUID
or null, elapsed seconds, fixed console link, and firing/resolved transition.
No raw exceptions, logs, user prompts/media or credentials enter SNS. First
condition and recovery transitions are deduplicated across restarts. A new
closed generation-failure kind is an event, with no fabricated recovery.

## Minimal role permissions and external alarms

The monitor requires exactly these additional AWS actions:

- `sns:Publish` on the one environment-specific topic ARN.
- `cloudwatch:PutMetricData` on `*`, conditioned with
  `StringEquals: {"cloudwatch:namespace": "Inkseq/OperatorAlerts"}`.

It needs no SNS subscription management, IAM, EC2, Secrets Manager, S3, provider
or CloudWatch alarm-management permissions. Existing unrelated host permissions
are not changed by this implementation. SNS and CloudWatch clients use explicit
regional endpoints, 3-second connect/5-second read limits and one SDK attempt;
durable host state owns retry timing.

Provision independent CloudWatch alarms in the root operations scope. Metrics
use namespace `Inkseq/OperatorAlerts`, dimensions `Environment` and `InstanceId`:

- `MonitorHeartbeat`: value 1 after real `/proc` host sampling and a complete
  monitor pass. Missing data must alarm, covering host shutdown/timer failure.
- `SnapshotHealthy`: 1 when the original ledger snapshot is complete, otherwise
  0. Alarm on repeated zero or missing data; a monitor heartbeat alone does not
  establish database/queue observation health.
- `DeliveryHealthy`: 1 only when no pending/failed delivery remains, otherwise
  0. Alarm on repeated zero or missing data through the separate external path.

Use metric math `FILL(m1,0)` with exactly one returned expression. The input
`m1` retains the exact namespace/environment/instance dimensions, `Minimum`,
`Count`, and 60-second period; alarm on `< 1` for three of three periods. Keep
both ALARM and OK actions on the matching environment topic. A direct metric
with only missing-data-as-breaching can reuse older healthy points from the
expanded evaluation window; the actual staging pause exposed this behavior.
A single delayed zero cannot satisfy three of three. Verify a bounded staging
timer pause and recovery through the actual recipient inbox before production.
New alarms may briefly report missing pre-installation history and then recover
as real samples arrive; do not describe startup history as a production drill.

The host cannot email after it is powered off; external missing-data alarms are
required acceptance, not an optional replacement for the timer. Use the same
environment-scoped SNS action for those alarms and verify trigger/recovery on
staging. Installation, IAM changes and actual email receipt are operational
acceptance, not established by the local fake-clock/fake-SNS tests.

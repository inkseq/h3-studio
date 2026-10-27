# #688 planning contract (not activated)

`studio_platform.capacity_lookahead` is a pure decision model. It has no callers
in demand-start, controller or worker execution. Existing live behaviour, rent
permissions, idle timers, settings and credentials are unchanged. This slice is
preparation while #685 awaits review and #659 runs its preserved acceptance cases.

## Inputs and decisions

Call once per exact compatible pool/configuration/model/mode. Count each owned
machine once, regardless of the number of journals referring to it. Cross-pool
machines consume global headroom but never this pool's forecast throughput.
Queued means eligible work, not failed/cancelled/unqualified requests. Outstanding
running work is **remaining seconds**, divided by the configured job duration;
do not count a whole running job again when estimating its remainder.

Defaults: job 30 seconds, boot 600 seconds, minimum backlog 20 jobs, max 10 machines.
Ready machines can each process boot/job jobs over the horizon. Starting machines
contribute max(0, boot - ETA)/job. Unknown ETA contributes zero throughput but still
occupies a machine slot and prevents the zero-capacity floor. Uncertain creates
must set admission_blocked, never masquerade as a missing machine.

The remaining backlog at ready is clamped at zero. With actual queued work and
zero ready/starting machines, ask for at least one machine; otherwise floor the
remaining backlog divided by 20. Forecast alone never starts a machine. Proposed
starts are bounded by both the pool maximum and available_slots (the minimum
under global, hourly, reservation/task and other existing caps). Unknown budget
headroom means zero. A disabled policy or safety pause must block admission.

Ready idle machines are candidates only after the configured boot interval and
zero queued/running work. Missing idle evidence is never a stop candidate. This
list is independent of start permissions; disabling starts must not disable idle
stop. Candidate IDs are sorted for stable evidence. Values are validated before
calculating a decision; invalid input raises without producing an admission.

## Integration boundary

The result is a snapshot, not permission or an idempotency key. A later integration
must persist decision inputs/reason with the existing journal, then recheck all
gates under the existing transaction before each individual reservation/start.
Re-read capacity after every start: a repeated tick cannot reuse a stale positive
count. Fresh lease/queue/version checks are required before any stop. Reuse the
existing watchdog, fenced attempts, provider-removal proofs, upload receipts and
settlement; unknown invoices cannot be zeroed to manufacture headroom.

The hybrid routing proposal in #688 is a separate integration: low demand uses
a Modal floor worker; sufficient lookahead can also select dedicated capacity.
This module does not select a provider or assume Modal ETA/price/credentials.
Claude's committed worker reference, scoped worker identity, verified provider
hard cap and existing reservation/settlement integration are prerequisites.
Modal's proposed 60-second idle timeout is not applied to dedicated machines.
No production auto-start or new provider is authorized by this planning code.

## Open-source comparison and verification

Compared [Ray Serve's policy](https://github.com/ray-project/ray/blob/master/python/ray/serve/autoscaling_policy.py)
and [configuration](https://github.com/ray-project/ray/blob/master/python/ray/serve/config.py):
use a separate zero-capacity path, bounded decisions and time-based downscaling.
Inkseq's issue-specific boot-time lookahead is implemented locally without adding
Ray or copying its scheduler. No new queue or persistent table is needed.

Tests cover the owner examples (1/0 => 1, 30/1 => 0, 45/1 => 1), exact thresholds,
partially booted capacity, missing ETA, fractional running work, hard caps,
forecast-only demand, idle/no-work conditions, duplicate identities, invalid
numbers and deterministic immutable results. These are planning tests, not GPU
acceptance or spend evidence. Provider integration remains Tier2 with one review.

## Prospective administrator start rules

All routes use the existing human-administrator boundary and no-store responses.
Prefix: `/v1/operator/dstack/admin/start-rules`.

- `GET ?profile_id=<operator-profile-id>` returns `binding`, `value`, `version`,
  `revision`, `keep_alive_seconds`, `execution_connected:false`, `applied:false`.
- `POST /preview` takes `{profile_id, expected_revision, value}`; value is the
  complete `{job_seconds, boot_seconds, minimum_jobs}` object. It returns the
  before state, proposed value, derived keep-alive and `confirmation_digest`.
  This is a configuration comparison, not a live capacity quote or start permit.
- `PUT` takes the same fields plus `confirmation_digest` and a nonempty `reason`,
  with `Idempotency-Key`. Preview binds the actor, tenant, current operator config,
  exact pool/configuration/mode, old revision and candidate. A changed binding or
  revision requires a new preview. No fleet, price, budget, demand-enable or
  provider field can be written through these routes.
- `GET /history?profile_id=...` returns at most 50 newest existing outbox audit
  records for that tenant and exact binding, with actor, reason, command and
  before/after states. Existing outbox retention applies; no second audit store.

Absent rows read the 30/600/20 defaults without creating anything. Durations are
integer seconds 1–86400; minimum jobs is an integer 1–10000. Keep-alive is derived
from boot seconds. These are validated configuration ranges, not additional
spending authority. The current demand loop and idle stop do not consume them.
The UI must describe these as prepared settings and must not display “active”.
Measured p50/p90 telemetry and execution integration remain separate G2 work;
these routes neither invent measurements nor overwrite accepted jobs/rentals.

Configuration reuses `platform_operator_capacity_policy`; commands reuse the
existing idempotency journal, and history reuses the transactional outbox. No
new tables or credentials. Writes take the account and capacity locks, recheck
canonical admin status before replay, compare the original revision, and commit
policy, command and audit together. Replaying an original command returns its
original response. A failed transaction cannot leave an unaudited policy.

Errors include `start_rules_invalid` / `start_rules_profile_invalid` (422),
`start_rules_conflict` / `start_rules_confirmation_changed` (409), and existing
authorization/idempotency errors. Stored identity corruption refuses reads.
This configuration slice requires one Tier2 PR review. It does not qualify or
enable the later runtime integration; that integration must receive its own
review of the changed execution behaviour before adoption.

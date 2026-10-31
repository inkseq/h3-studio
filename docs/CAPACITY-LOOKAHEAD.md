# #688 planning contract (not activated)

`studio_platform.capacity_lookahead` is a pure decision model. The separate
`capacity_lookahead_runtime` integration is OFF unless the protected operator
configuration explicitly sets `policy.demand_start.lookahead_version: 1`.
Code deployment and saving prepared rules do not enable that rollout flag.

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
Code does not grant production auto-start or provider authority. See the latest
owner change record, accepted staging evidence and the separate adoption gate.

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
  `revision`, `keep_alive_seconds`, `execution_connected`, and `applied`. The last
  two are false until the explicit protected rollout flag connects execution.
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
spending authority. Without the rollout flag, the demand loop and idle stop do
not consume them. The UI must describe disconnected rules as prepared settings.
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

## Opt-in execution and rollout

The runtime integration is a separate Tier2 change. After review, use a fresh
staging zero-work window to adopt `lookahead_version: 1` in the existing protected
`demand_start` object. This changes the existing demand profile hash: do not edit
the flag while old demand journals/reproductions are in flight, and do not reset
their hashes or uncertain commands. Malformed versions refuse demand authority.
Removing the flag returns future decisions to the existing path; disable the
ordinary demand switch first when stopping new allocations. Existing attempts,
invoices, collections, leases and frozen rental idle timers remain original.

Each tick first reconciles original commands/backoff, then reads the existing
fresh native capacity witness. Per exact owner/pool/configuration/model/manifest,
eligible queued jobs supply backlog. Compatible live registered workers supply
ready throughput; a busy slot adds its remaining configured job-time estimate.
Known allocations without usable registration count as starting. Their ETA is
boot minus age when no registration exists; stale registrations supply no ETA
or throughput. Unknown/duplicate/stopping matching capacity cannot authorize a
new allocation. Different pools/owners/configurations never supply throughput.

The planner can propose at most one start per tick. It never claims affordable
headroom: unchanged registration, fleet, supplier, test-task and owner budgets
still gate the original reservation transaction. That transaction recomputes
lookahead from fresh queue/capacity state, compares the prepared rules revision
and frozen requested idle interval, and refuses obsolete previews before any
intent/provider creation. Manual starts retain their existing path.

New demand previews freeze the configured boot seconds as their requested idle
interval. Existing max-runtime and test idle ceilings remain stronger guards;
queue/lease/unsafe-attempt checks in the current controller still govern stops.
No existing rental's timer is rewritten. Planning snapshots live in the original
demand journal; changed decisions and committed admission snapshots are recorded
as `capacity.lookahead_decision` in the existing outbox, without content/prompts.
Identical planning snapshots do not append duplicate audit events.

Read-back must verify both the source revision and the explicit rollout flag.
GPU acceptance still requires the 1/0 floor, 30/1 covered, 45/1 extra, and guarded
idle cases; CPU tests are not a provider/spend acceptance receipt. Modal routing
and measured p50/p90 remain independent work, not enabled by this integration.

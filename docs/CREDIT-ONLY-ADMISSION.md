# Credit-only customer admission (#409)

Approval: proposal 6322001656 and Claude 6322026427. This source change is not
schema adoption, a charging toggle, production enablement or staged acceptance.

## Frozen quote and original USD row

On adopted schema `20261022_0007`, fresh enforced quotes use version 2 and freeze
`admission: "credits"` plus private `budget_rows` containing the exact protected
selected account IDs, scope and `budget_role`. An active canonical customer's
owner-only account is `accounting`; shared/project rows are `enforced`. Requests
and PATs cannot select either field. Actual canonical row scope and the current
immutable price are recomputed before first admission, in the original ledger
transaction and lock order. Missing/stale/modified version-2 data requires a new
preflight. New unaccepted positive-price version-1 plans require re-preflight
after007; included zero-credit quotes retain the exception described below.

The exemption requires a strictly positive integer original hold. Included
zero-credit CPU work stays a freshly recomputed v1/USD quote and all its USD
rows remain enforced; stale/OFF/repriced or modified zero quotes still refuse.
For positive credit admission, more than one selected owner-only row is ambiguous and fails409
`credit_budget_binding_requires_review`, with an administrator-review detail
and no protected IDs. Exactly one candidate means the actual immutable
customer-scoped row selected by the protected generation policy, not a generic
account-purpose assertion. A policy deliberately selecting a single manual
owner row as its generation binding remains a protected-configuration review
boundary; this code adds no purpose registry or inference from audit records.

Every original USD reservation keeps its full amount, job/account/reference
identity and normal settlement. Only the accounting row's USD allowance check
is skipped. Shared/project caps, manual/test admission, per-task USD20 fleet
guards and credit-unit settings/confirmation still run. Admission must produce
the exact original credit hold in the same transaction. A concurrent policy OFF
or price change, insufficient credits, missing hold or audit error rolls back
the job, USD reserve and credit hold together.

Demand allocation retains a separate trusted account selection. Generation
accounts come from `ExecutionPolicies.evaluate`'s protected policy templates
formatted with the customer scope. `dstack_demand._profile_for` instead selects
the protected fleet profile (including its configured rental owner), and
`dstack_demand_start.start_once` calls `_preview_for_owner`/`_start_for_owner`
with that profile owner. `DstackOperator._start_for_owner` passes exactly the
profile's `budget_account_ids` to `reserve_instance_intent`; it does not copy
the customer's job account IDs or accounting roles. The instance reference
therefore retains its ordinary enforced USD/fleet/test guards. A configuration
that explicitly reuses a zero-funded customer row for the rental still refuses
allocation; this change does not loosen or repair that configuration. The
future staging proof must inspect the actual protected job-versus-fleet IDs;
a CPU proof alone does not establish real demand allocator readiness.

Schema 006 retains its existing dual USD/credit behavior and version-1 quote;
there is no unrecorded column or implicit bypass. Accepted/idempotent jobs and
their original holds, capture/release, pending exposure and late supplier
invoices are not repriced, backfilled or reinterpreted after adoption.

## Additive public API

Preflight/job quote projections add top-level `admission: "credits" | "usd"`.
The existing public `execution` adds `budget_rows` with only `scope` and
`budget_role`; protected account IDs remain private. Legacy/unquoted plans
report `usd`. Clients can use credits-only wording for credit admission; admin
and manual/legacy flows retain the existing USD information.

`GET /v1/admin/credits/prices` keeps existing estimates and report fields and adds
`report_30d.credit_cost_margin`. The cohort is original credit jobs with
`jobs.created_at` in `[window_start, window_end)` for the trailing 30 days. Each
capture uses its own immutable `price_revision.configuration.credit_usd`.
`captured_value_microusd` is gross nominal captured-credit value, not net cash
receipts after tax, refunds or fees. Original provider cost is grouped once per
job across duplicate owner/shared rows. Actual, reserved, invoice-pending and
unconfirmed exposure are separate; a late invoice updates its original job's
cohort. Unknown/conflicting cost or price evidence remains unknown. Zero
exposure or nonpositive captured value makes margin and `below_30_percent`
undefined. The 30% diagnostic never admits, refuses or caps a task.

The admin report reuses the existing budget read snapshot (PostgreSQL
REPEATABLE READ / SQLite read transaction) for its captures, price revisions and
cost rows. Missing reservation/actual-cost evidence, including an estimated-zero
CPU job, remains unknown; a zero reserve or zero pending-invoice estimate cannot
prove actual zero. Only an original settled/released row with actual zero is
reported as known zero cost.

## Schema and rollback boundary

007 adds only original `platform_budget_reservations.budget_role VARCHAR(20)
NOT NULL DEFAULT 'enforced'` and its finite check. Existing rows stay enforced;
no financial amount or identity changes. Frozen 005/006 source is unchanged.
Runtime table definitions remain 006-compatible; only the adopted path accesses
the new column. Schema and business-backup readers preserve either shape.

The explicit `schema_baseline adopt-credit-admission --expected-fingerprint ...`
path requires PostgreSQL, the exact adopted 006/007 schema snapshot, charging
and Stripe OFF, and no nonterminal jobs/live job leases under the original
account/migration locks. Before invoking it, the operator must hold host
`release.lock` and positively prove both current and retained rollback images
contain this 007 reader/backup bridge. Deploy source/readers first; capture a
native 006 dump and financial fingerprints; prove 006->007 and both readers on
the isolated copy; only then consider separately authorized staging adoption.
Production schema 006 remains unchanged. No constructor runs ALTER or backfill.

This follows the selected two-phase reader floor. Reader Release N merged at
`4fba4092bce401f4b30fab296608c90b0d07ae0f` contains recognition/backup support
and no adopter. N+1 adds this guarded writer and admission on top of that real
release. Both are initially deployed on006 with charging OFF. Actual approved
current N+1 and retained N artifacts still require source/image binding and the
isolated native006-copy proof before DDL. The source merge alone is not that
proof; old production792 cannot read007. Never use an empty commit, fabricated
SHA or rewritten approved binary to create the appearance of compatibility.
No host floor proof or schema adoption is included here. Reader N supports
historical terminal settlement, not fresh unheld v2 admission.

## Finite staging QA extension

The existing human-admin `POST /v1/admin/staging/qa-accounts` accepts the complete
legacy pair qa-admin/admin plus qa-user/user, or that pair plus exactly
qa-credit/user. It preserves matching original UUIDs/password hashes/status and
the existing audited `after.is_test` marker. Wrong/partial/duplicate names or
roles refuse; production is unavailable before database access. Password auth
adds only this exact staging name, without a wildcard, provider signup or a
general user-create endpoint. Defaults remain the original pair. Three-name
provisioning requires `grant_test_credits:false` and cannot bootstrap another
admin authority if qa-admin login fails.

The current protected-file validator accepts those two complete shapes. The
canonical tool's `credit_fixture_accounts` prepares the one extension only in
memory, preserves original records and reuses existing third-record material.
It does not write, truncate, replace or copy credentials to another file. A
separately reviewed finite operator must resolve durable publication exclusively
to the canonical private JSON before the actual fixture. Historical task-specific
strict-two-name operators stay preserved; future operators use the reviewed
current loader. Check the applicable Claude reader before a third-record write.

Bind the disposable canonical UUID through the existing
`POST /v1/admin/users/{user_id}/generation-budget-binding` (`{reason}` plus
Idempotency-Key), inserting absent zero USD rows only. The existing audited
adjustment is `POST /v1/admin/users/{user_id}/credits`, exact `{delta,reason}`
with its own stable key. This source does not invoke either route or alter any
existing QA USD20/credit row. The legacy qa-user2000 grant path remains unchanged
and is not the disposable proof grant.

## Staged proof remains separate

Existing qa-user has USD20 and must not be reset or represented as a zero-USD
proof. The approved staged slice uses exactly qa-credit, its zero USD binding,
one audited adjustment of at most200 credits, and supported Standard. First
submit an actual quote above balance and prove402 with no job/reservation/hold;
then submit one fitting quote and confirm its original v2 hold/accounting row.
No price publication or CPU repricing is implied. Actual protected profile,
positive price and separate rental account eligibility remain operational facts
to verify; a binding template does not establish capacity or owner eligibility.

Use the approved supervised tenant-wide window, target30/hard60 minutes: announce
it, positively check no other identity has queued/nonterminal work, and use the
normal audited `POST /v1/admin/credits/policy` with exact
`{expected_revision,enabled,reason}` and original separate ON/OFF keys. Turn OFF
and read back OFF immediately after confirming the original hold, before waiting
for final capture. The existing terminal path follows that hold after OFF.
Finally also turns OFF and reconciles unknown original writes; no new scheduler,
expiry or credential component is added, and process-crash recovery is supervised.

Queued cancellation is valid only if genuine normal preflight admits without
allocation. Otherwise the separately announced one Standard GPU job uses the
existing USD20/task/max_price/idle/max-runtime/stop guards; do not retry a failure
to obtain capture. Cleanup adjusts only the reconciled disposable remainder to0,
without deletion or fabricated settlement. Preserve original request/hold/row
IDs, existing QA rows/sessions, and honest unknown/pending supplier exposure.
Source tests are not this staged proof. Production charging/Checkout remain OFF;
pre-existing staging WorkOS ON stays under #395 fix-only, with no hosted loop.

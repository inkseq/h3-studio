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
preflight. New unaccepted version-1 plans require re-preflight after 007.

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

This is a two-phase reader floor. Deploying one combined #409 source release on
006 leaves its retained predecessor unchanged; old staging 8b / production 792
artifacts do not recognize 007. That single release is therefore insufficient
authority for adoption. Either split a genuine reader-first source release
from the writer/admission release, or wait for a genuine independently changed
compatible descendant so both actual current and retained artifacts contain
the bridge. Never use an empty commit, fabricated SHA or rewritten approved
binary to create the appearance of compatibility. The future host adoption
operator must refuse until it proves those actual immutable artifacts under
release.lock; no such host proof or adoption is included here.

## Staged proof remains separate

Existing qa-user has USD20 and must not be reset or represented as a zero-USD
proof. CPU render is currently included at zero credits, while ordinary staging
charging is OFF. A positive-credit CPU acceptance needs an explicitly reviewed
disposable staging identity/test quote path (zero USD, positive test credits),
or an approved disposable zero-USD real-generation task under the existing
USD20/task guard. Neither is silently created by this source change. Prove a
real positive hold/capture/completion and insufficient-credit 402 with no new
job/reserve/hold; preserve financial receipts. Source tests are not that staged
proof. Production charging/Checkout remain OFF. Existing staging WorkOS ON is
preserved under #395's fix-only approval; no hosted loop is claimed here.

# Versioned capacity admission settings (#456-A)

This is the backend settings and admission slice. It reuses the admin
`platform_spending_settings` row, profile-price overlays, original rental/job
records and outbox audit. No new ledger, scheduler or table is introduced.
Installing this code does **not** adopt defaults, change prices, enable demand
start, or raise the current two-machine limit. Production adoption requires the
separate accepted configuration action.

## Admin API

`GET /v1/operator/admin/spending-settings` retains `version` and `value`, adding
`capacity_policy` metadata and `tenant_daily_supplier_spending`. Metadata includes
defaults, numeric bounds, adoption state, the latest actor/time/reason for each
field and `pending_implementation` flags. The original five values remain intact
on legacy reads. Legacy unchanged PUT echoes do not adopt the new policy.

1. `POST /v1/operator/admin/spending-settings/preview` with
   `{expected_version, changes}` (or the compatible `value` object).
2. Show the returned finite `consequences` codes and numeric parameters.
3. PUT the same route without `/preview`, retaining the original request and
   adding a nonblank `reason` (1–500 characters), `confirmation_digest` and exact
   `profile_snapshots` returned by preview.

The original capacity fence serializes settings, price updates and paid
admissions. Both the admin version and individual profile snapshots must still
match. Stale confirmation writes nothing. Partial updates preserve unrelated
values; validation applies to changed fields, allowing unchanged legacy values
outside the new recommended bounds. An explicit first confirmed write adopts
`capacity_policy_version=1`; simple reads and deployment never do.

`GET /v1/operator/dstack/admin/prices` adds `expected_settings_version`.
`POST /v1/operator/dstack/admin/prices/{profile_id}/preview` accepts that version,
the profile's `expected_snapshot`, `max_price_microusd` and
`price_ceiling_microusd`. PUT the price route with the same fields plus `reason`
and returned `confirmation_digest`. Adopted policy changes require this flow.
The audit retains both price versions, the admin version, actor, timestamp,
reason and confirmed consequences. Unchanged price writes create no audit.
Price-preview consequences declare `consequence_scope=selected_profile_repeated`
and `profile_ids`: their fleet arithmetic describes copies of the edited profile,
not a promise that a mixed fleet or the corresponding stock is available.

## Effective limits and boundaries

Defaults and bounds live in `studio_platform/capacity_settings.py`. The daily
supplier admission ceiling defaults to USD100, warning fraction0.8 (range0.5–0.95),
fleet ceiling USD3/hour and maximum machines2 (configurable1–8). Mode ceilings
default to REF USD1.50/hour and FL/other USD0.85/hour; other cannot exceed either
FL or REF. Each offer must satisfy the profile ceiling, mode ceiling, admin
per-machine ceiling, fleet ceiling and original account/budget guards. No silent
clamping or retroactive shutdown occurs.

The adopted max replaces old positive operating-count ceilings across demand,
manual start and original intent reservation. Missing or zero global/pool gates
remain disabled. Registering the exact already-admitted one-GPU native resource
does not become a new admission after an administrator lowers the count.

New original jobs and rentals retain their accepted policy snapshot. Normal
replay, settlement and rolling funding do not re-admit them against a new daily
limit. Test tasks keep their independent USD20/task accounting, max price,
idle-stop and maximum runtime. Production has no new fixed expiry.

The active/quiet idle policy, wait targets, batch share and user share settings
are explicitly **pending B/C/D**. Their stored values do not imply implemented
scheduling behavior, guaranteed latency, or a trustworthy ETA.

## Supplier exposure, not customer charges

The tenant daily projection uses Australia/Sydney calendar days, including DST.
It counts one original rental once across its owner and shared budget layers.
Known internal worker jobs, customer prices, credits, holds and captures are not
additional supplier cost. Distinct original supplier allocations remain additive.
Unknown external supplier obligations are visible blockers; no new per-job
supplier billing adapter is assumed.

Confirmed actual settlements belong to their original settlement day. Original
unsettled rental exposure carries across midnight until positively settled or
released. A proven staging test rental is excluded from production daily usage
but remains in its original isolated test task. Missing/conflicting bindings,
amounts, states or timestamps produce `usage_unconfirmed`, a null total and
admin-only finite diagnostic details, never an invented bill or zero.

Daily statuses: `policy_not_adopted`, `within_limit`, `warning`, `limit_reached`,
`usage_unconfirmed`. The latter two block new paid admission with
`tenant_daily_limit_reached` or `tenant_daily_usage_unconfirmed`. Warning is
visible state only; no new notification channel is added.

This is an **admission ceiling**, not a guaranteed cash maximum. Already accepted
work continues, and its final supplier invoice can exceed the daily ceiling.
Confirmation explicitly includes `admitted_work_may_exceed_daily_limit`.

## Verification boundary

Focused SQLite and PostgreSQL tests cover CAS/confirmation races, budget-layer
deduplication, unknown invoice carry, DST, cross-owner admission serialization,
editable machine counts, disabled gates, replay and accepted-work continuation.
These tests use fake providers and isolated databases, not real rentals.
Staging adoption/readback and Claude acceptance are separate release gates;
neither a passing local test nor a merged PR is production activation.

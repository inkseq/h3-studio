# Customer demand uses the existing fleet — Refs #724 / #659 / #374

The producer of a generation job and the owner funding its GPU fleet are
separate identities. An active customer does not need an operator role,
an owner-named rental profile or a cloud credential to use that fleet.

## Existing path, made explicit

1. New WorkOS signup runs `ensure_owner_binding` in the same transaction as
   canonical identity creation and any separately approved welcome grant.
   Account IDs come only from the protected execution-policy templates. The
   owner row starts with a zero USD allowance and an audit record; no provider
   allowance is granted. Existing login and public reads do not repair rows.
2. With schema 007 adopted, charging ON and a positive credit quote, the
   customer's canonical owner row becomes `accounting` for that original job.
   The shared/global rows remain `enforced`. A positive original customer
   credit hold must commit atomically with the job and USD reservations.
   Charging OFF or a zero/included quote cannot grant the accounting exemption.
3. A cold v7 job selects a compatible protected FL/REF fleet profile by tenant,
   pool, configuration, model, recipe, output delivery and native manifest.
   `dstack_demand._profile_for` prefers a same-owner profile when one exists but
   permits the existing shared fleet for other job owners. No wildcard owner
   or per-customer profile expansion is required.
4. `start_once` reserves the original rental under that fleet owner's project
   and its existing supplier budgets. The customer's job/plan, signed input
   authorization, attempts and credit attribution retain the customer owner.
   Registration, supplier funding, global machines/hourly limits, protected
   test budgets and original unknown-operation reconciliation still apply.
5. New compatible customers share the same ready/starting fleet capacity.
   A second queued customer cannot authorize another floor host while the first
   original host covers that configuration. FL and REF remain distinct.

The #723 profiles remain explicit superdan/supervan fleet funding profiles;
those names are not a customer eligibility allowlist. Job template
`demand-job-owner-{owner_id}-20261004` is expanded to the actual customer.
Never substitute the fleet owner into a customer's execution or credit quote.

## Adoption and launch proof

- First adopt the reviewed #723 v7 execution/fleet bindings, preserving all
  original v6 jobs and budget rows. Record current protected source digests and
  prove v7 FL/REF authority-current. Enabling production demand and switching
  its default remain the separately gated Claude-owned launch actions.
- Read-only inventory must identify existing active canonical accounts missing
  their current protected owner binding. New signups already use the atomic
  path; do not re-create them or grant additional welcome credits.
- For an existing missing binding, use the existing human-admin route
  `POST /v1/admin/users/{user_id}/generation-budget-binding`, with a reason and
  one retained `Idempotency-Key`. It adds absent zero-allowance owner rows and
  an audit. It preserves existing financial fields. Reconcile an unknown
  response by its original command; do not use SQL upserts to overwrite money.
- Before charging ON, read back schema 007, current price revision and the
  original user credit/owner-binding scope. The affected backend contract tests
  use a new `u_<uuid>` WorkOS customer, real v7 compilation and original ledger
  demand admission against a fake provider. They exercise both FL and REF,
  zero-account roles, supplier/global refusals, account suspension and original
  create reconciliation. These are not a live GPU acceptance receipt.
- At the separately approved production auto-start gate, Claude can use one
  owned real new-customer v7 job to verify automatic start and completion.
  Preserve its actual customer job ID, fleet intent, credit hold/capture and
  provider invoice identities. No queue or rental replay belongs to adoption.

This slice adds no budget, provider account, credential or schema. It does not
turn on production Stripe mode, charging or demand, publish production policy,
modify accepted work, or infer a supplier invoice from removal evidence.

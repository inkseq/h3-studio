# Stripe LIVE readiness (#374)

This slice prepares LIVE support; it does not enable purchases, credit charging
or execute schema adoption. Importing an existing owner credential into Secrets
Manager is separate from mounting it on production or enabling its consumer.

## Boundaries

- Staging accepts only the unchanged exact v1 TEST credential document and TEST
  provider objects. Production accepts only an explicit v2 LIVE document and
  LIVE objects, with the canonical `https://inkseq.com` origin. Existing legacy
  origin aliases remain allowed; they never select a Stripe account.
- The v2 document contains `version`, `mode`, `account_id`, `secret_key`,
  `publishable_key`, `webhook_secret` and `webhook_endpoint_id`. `mode` is `live`;
  the existing restricted or secret LIVE key is held only in protected storage.
  Hosted Checkout does not require a publishable key. The webhook secret and
  endpoint ID are either both absent or both present. Missing webhook setup
  blocks new purchases.
- Each original purchase freezes its account, mode, pack, inclusive USD price,
  credits and accepted terms before provider I/O. Returned sessions, signed
  events and retrieved payment proof must match those original facts. TEST and
  LIVE context cannot be substituted. Unknown checkout results retain the same
  purchase and idempotency key; a success-page visit never grants credits.
- `INKSEQ_STRIPE_MODE=off` blocks new Checkout requests. When an explicit,
  correctly bound webhook configuration remains mounted, signed events for
  existing obligations still follow the existing DBOS workflow. Duplicate
  delivery cannot grant the original purchase twice. Refunds preserve original
  cents, protect held credits and retain unmet clawbacks for review.
- Credit charging is a separate audited credit-policy switch. Preparing LIVE
  credentials, catalog prices or schema does not turn either switch on.

## Credential import

`tools/import_stripe_credentials.py --target production` is an explicit owner
operation, not a startup hook. It reads only the canonical local owner drop,
confirms the current Stripe account and a LIVE catalog object with the pinned
SDK, and targets only AWS account `829135631045`, region `ap-southeast-1`, secret
`/sixnine/platform/stripe`. Credential values never belong in argv, environment,
logs, issue comments, operation receipts or SSM commands.

The importer suppresses SDK logging and console output for the operation. It
creates a missing secret with a durable operation token; it refuses differing
existing values and pre-existing empty containers. An uncertain create outcome
is reconciled against the same token/current version rather than overwritten.
Only after exact-version and `AWSCURRENT` readback plus a durable private
no-value receipt does it delete the unchanged, exclusively locked source by
its original Windows file handle. A completed retry with the source absent
rechecks the recorded version/current pointer without a Stripe call or a new
AWS write. Any mismatch preserves the available source/history for review.

## Migration and activation order

1. Stage, verify and accept this reader bridge; promote its identical artifact.
   Ensure both the active and retained rollback images understand exact 005/006
   schemas and can export/restore their business history before changing DDL.
2. Keep Stripe purchases and credit charging OFF. Verify the protected LIVE
   account, the four actual LIVE pack prices and the owner-authorized webhook's
   URL, API version and event selection. Catalog changes use the existing
   admin revision/CAS/idempotency/audit contract. Never copy TEST price IDs into
   production or infer account identity from a price ID.
3. Separately prepare and review guarded revision `20261021_0006` adoption under
   the existing schema/release lock, fingerprint and verified-backup procedure.
   This slice provides the frozen definition and readers, **not an executed
   adoption or an adoption CLI**. Frozen 005 stays unchanged. 006 changes only
   the two named `livemode = 0` checks to integer `livemode IN (0, 1)` checks;
   original payment/event columns, identities and refund bounds are preserved.
   Unmarked, mixed or altered 006 families do not authorize LIVE writes.
4. With the reviewed schema present, explicitly hydrate/mount the mode-matched
   production secret and read back readiness. Business-only backups retain the
   exact 005/006 family, omit authentication/authority markers and restore only
   to a new disabled recovery copy. Do not downgrade or delete payment history.
5. The owner-approved audited enable and real-card purchase → generation →
   download → refund loop remain separate gates. Do not enable purchasing or
   credit charging as part of installation, CI, import or a health check.

## Readiness API

Human administrators can read `GET /v1/admin/billing/status`. It returns
`configured_mode`, `checkout_enabled`, `credit_charging_enabled`,
`webhook_endpoint_id`, `webhook_configured`, `registration_basis`,
`last_signed_event_at`, `schema_ready` and a safe `reason_code`. It performs no
Stripe request or financial mutation and exposes no key or signing secret.

`registration_basis=protected_configuration` reports the stored endpoint ID;
it does not claim a fresh provider-side registration check. The last signed
event time is filtered by the exact configured account and mode. An imported
but unmounted credential is not runtime readiness; 005 cannot authorize LIVE
writes. OFF/readiness is not proof that a real-card loop has passed.

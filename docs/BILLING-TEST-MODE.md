# Staging credit purchases

This document describes the isolated Stripe TEST integration on staging; TEST
credentials cannot be used on production. Purchasing defaults OFF and public
packs default to an empty list. No startup schema changes or outbound calls run.
Production LIVE preparation and its separate gates are documented in
[STRIPE-LIVE-READINESS.md](STRIPE-LIVE-READINESS.md).
Revision005 must be adopted under the reader/backup procedure in
[SCHEMA-MIGRATIONS.md](SCHEMA-MIGRATIONS.md) before processing any payment.

## API contract

- Public `GET /v1/billing/packs` returns the agreed catalog without provider IDs.
- Human admin `GET/POST /v1/admin/billing/packs` reads or changes that catalog.
  Writes require `Idempotency-Key`, `expected_revision`, `packs`, and `reason`;
  the existing audit and document store retain the exact original action.
- Human `POST /v1/billing/checkout {pack_id}` requires `Idempotency-Key` and
  current reviewed terms. Missing/draft terms refuse; no consent is inferred.
  The original purchase freezes before Stripe I/O. Retry its original key after
  an unknown result; never submit a replacement purchase. After23h, an unbound
  outcome needs operator reconciliation rather than another Checkout Session.
- Signed `POST /v1/billing/stripe/webhook` bounds raw input to512KiB, verifies it,
  and atomically records a minimized event plus its existing DBOS queue wake.
  The success-page URL cannot grant credits. The workflow retrieves price proof
  outside SQL, then binds it to the original test account/session/pack/amount.
- Human `GET /v1/billing/payments?cursor=...&limit=50` returns only owned history.
  Machine credentials cannot buy credits, edit packs or accept terms.

Config uses the protected host `/run/sixnine-secrets/stripe` file, mounted only
into API/controller at `/run/secrets/stripe`, never ambient keys: version1,
account_id, publishable_key, secret_key, webhook_secret.
Only `sk_test`/`pk_test` keys and the exact staging target/origin are accepted.
`INKSEQ_STRIPE_MODE=off` blocks new Checkout requests but preserves signed-event
processing for existing obligations when the explicit config remains mounted.
Stripe SDK16.0.0 pins API2026-09-30.endive and the official API endpoint.

Managed Payments uses catalog prices and `managed_payments.enabled=true`; it
does not add incompatible Stripe Tax/embedded-checkout options. Inclusive USD
price/credits are checked server-side; local presentment is not invented FX.
Refunds use cumulative original cents, round credit clawback up once, protect
held credits and retain any unmet amount for review. Optional untrusted receipt
links are omitted from responses without changing original financial history.

## Acceptance and decisions still required

The source safety checks cover signatures, isolation, replay/concurrency,
refund-before-payment, holds, audit rollback and real DBOS/PG atomic enqueue.
These do not substitute for hosted test-card/async/refund/browser acceptance.
The test policy restores purchasing after a signed dispute-won closure only
for that payment; other open/lost disputes remain blocking. Owner review is
required before any production policy adopts this behavior.

Staging configuration/signing-secret setup, reviewed terms, provider category
and Managed Payments activation remain explicit gates. Live-mode credentials,
real charges, legal approval and production promotion are outside this slice.
Primary integration reference: [Stripe Managed Payments Checkout](https://docs.stripe.com/payments/managed-payments/update-checkout).

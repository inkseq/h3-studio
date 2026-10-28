# Payment monitoring and anonymous checkout funnel (#725)

This slice adds observations. It never repairs/replays a payment, grants credits,
changes a catalog, or turns on Checkout, charging or a provider. It uses the
existing alert monitor/SNS, payment DBOS workflow, ledger and scoped documents.

## Operational observations

The existing business snapshot keeps version1 and all GPU/reservation fields.
`payments.complete=false` means unknown, including an absent optional schema or
failed query; it cannot resolve an active payment incident. The unchanged GPU/job
snapshot runs first. The root monitor then invokes `studio_platform.payment_alerts`
as a separate optional subprocess with its own existing ten-second Docker deadline,
an independent read-only connection and a five-second PostgreSQL statement timeout.
Slow queries, timeout or malformed payment output leave the original GPU/job
snapshot intact and mark only payment health unknown.

The fulfillment condition covers a paid purchase missing its exact original
positive purchase entry, and signed paid events still received/deferred/review
while the purchase has not been credited (including a still-created purchase).
The proof binds tenant, account, mode, user, original payment/session, amount in
credits, purchase kind, origin and actor. Current wallet balance is never read:
spending or refunding a correctly granted purchase is healthy fulfillment.
Ambiguous/uncorrelated signed evidence has a separate stable UUID incident.

The snapshot also observes pending processing and recent payment failures,
refunds and disputes. Activity has a finite rolling window; an outage longer
than the window does not replay old activity, while fulfillment/review incidents
remain unwindowed until their original evidence is healthy. No signed payload,
checkout URL, customer identity, amount, signature or free-text error leaves.

Protected monitor threshold additions:

| Key | Default | Meaning |
| --- | ---: | --- |
| payment_fulfillment_seconds | 300 | Original paid/signed evidence age before alarm |
| payment_processing_seconds | 300 | Original pending event age before alarm |
| payment_window_seconds | 300 | Activity/counter window; integer60–3600 seconds |
| payment_failure_count | 1 | Failure/activity count that fires an alarm |

`/internal/alert-counters` retains its exact four keys and behavior. The separate
`GET /internal/payment-alert-counters?window_seconds=300` exposes only epoch,
window and signature/processing failure counts. Both require the actual direct
loopback peer, reject forwarded assertions, and accept GET only. Payment buckets
hold at most one hour of scalar counts. Invalid signatures never become trusted
payment/event rows and their raw request/signature/error is never retained.

The root monitor adds only closed payment codes to its existing notification
guard and delivery journal. No new SNS topic, metric namespace, queue or service
is introduced; legacy backup/HTTP/host/GPU notification behavior remains.

## Optional frontend contract

```
POST /v1/billing/checkout
{
  "pack_id": "creator",
  "analytics": {"consent": true, "distinct_id": "<anonymous UUIDv4 or UUIDv7>"}
}
```

Existing `{pack_id}` requests work unchanged. `analytics` is optional and malformed
or denied context suppresses analytics without blocking Checkout. The frontend
must omit it when collection is not permitted, DNT/opt-out applies, or its SDK
is disabled. Never derive the ID from an account, email or name. Claude owns
this consent/anonymous-ID handoff; the current browser emits checkout-started.

Attribution is stored once in existing scoped documents for the original
purchase. It never enters the immutable money fingerprint, pack snapshot,
pricing, terms or provider idempotency key. Reconciled Checkout does not rewrite
an accepted attribution. There is no new schema or analytics queue.

After the original money transaction commits, the existing payment turn may
emit `payment_succeeded` only with committed original paid/grant proof, or
`payment_failed` for an authoritative async failure. Timeout, SDK error,
Checkout unknown, expiry or a zero wallet never counts as payment failure.
Duplicate delivery uses a deterministic event UUID for the original outcome.
Analytics failure cannot roll back money or defer/replay the original event.

The only product fields are anonymous distinct_id, event UUID, environment and
pack_id, with person-profile processing and GeoIP disabled. A final SDK
before_send rebuild strips request data, amounts, customer/payment/user IDs,
profile updates, context, release and other enrichment.

The official synchronous Python SDK is pinned to7.60.0 (public PyPI release
2026-09-23); dispatch lock includes full artifact hashes. Wheel SHA256:
`48ecc52c124d8262779bef4fd3dc268ef66a08905449700c4ddfe44f5b0027d1`.
The SDK uses no consumer/poller and no queued capture. Each HTTP request has
timeout2seconds, and SDK-level retries are zero. Its requests adapter separately
allows two connection retries, so this is not a hard elapsed-time or exactly-once
claim. Fake actual-SDK tests verify those details and the final outbound bytes.
The existing browser project destination/public write key is reused; no new
credential or collector is introduced.

## Sentry and adoption gates

The approved `inkseq-api` Sentry destination and privacy sanitizer already exist.
Use the existing optional `INKSEQ_SENTRY_DSN` plus explicit deployment target in
the normal config/render lane. Do not add a global initializer or broaden the
destination allowlist. No requests, prompts, source/local variables, logs,
attachments, tracing or profiles belong in the deliberate test event.

This PR does not activate configuration or send deliberate events. After one
Claude Tier2 review and staging Stripe TEST readiness, adopt staging, read app
and controller enabled-state/target back without dumping configuration, then
send one deliberate sanitized exception event and retain its event ID and
ingestion proof. Production installation uses a fresh #193 hold, zero-work
gate, saved configuration/release/image CAS and read-back. Claude alone owns
production promotion and the final charging gate. Keep unknown original
operation/delivery receipts; never replay a money operation to test monitoring.

# Credit expiry (CR-20261028-75)

Refs #749 / #739 / #374. This code is dormant for existing price revisions.
It does not publish a policy, enable charging, or rewrite an accepted quote.

## Adoption and public contract

After Tier2 review and the zero-work gate, use the existing authenticated
`POST /v1/admin/credits/prices` publication path, its expected price revision,
and an original idempotency key. Keep the accepted rates/configuration and add:

```json
{"expiry_policy":{"version":1,"purchase_months":12,"promotion_days":30}}
```

The first publication refuses nonterminal generation work, live job leases,
or unresolved image credit holds. The operator must also verify the normal
environment/release/zero-work gates. Later price publications retain the policy
when the field is omitted; an altered policy is refused. Read back the original
publication and public prices rather than repeating an unknown write.

Public prices include the adopted `expiry_policy`. Balance responses for a
tracked account include `expiry` with `expired_unspent`, `next_at`,
`next_credits`, and `upcoming` (at most 20 `{at, credits}` items, UTC epoch
seconds). Public balance/available already exclude expired unheld credit.
GET is read-only; the next successful account mutation appends the corresponding
`expire` entries in the same transaction. A rejected transaction changes nothing.

## Provenance and accepted work

- Existing balances become a nonexpiring legacy lot on first mutation after
  adoption. No historical grant, payment, capture, or user balance is rewritten.
- New purchases expire 12 calendar months after the verified paid-event time,
  not webhook delivery time. February 29 clamps to February 28 the next year.
- New welcome/campaign grants expire after 30 days. Explicit administrative
  adjustments remain nonexpiring; plan credits are outside this slice.
- Holds allocate earliest-expiring credit first. An accepted hold remains
  available to its original job even across expiry. Releasing an expired hold
  immediately expires that portion; capture retains its original allocation.
- A full job waiver restores the original lots without extending their expiry.
  Any already-expired restored portion receives an append-only expiry entry.
- Payment refund clawback uses only the original purchase's available lot.
  Consumed, held, or expired portions stay in the existing `unmet_credits`
  accounting. Pre-adoption payments can use only the legacy pool; they cannot
  confiscate unrelated new purchases. Stripe refund amounts/permissions are
  unchanged, and this code never requests a refund.

The account row lock serializes ledger, lot state, and immutable entry allocation
documents in one existing SQL transaction. Documents use reserved internal scope
and tenant/user identity. Corrupt/missing allocation evidence fails closed.
After adoption, preserve lot/allocation evidence across deployments: do not
delete documents or roll back to code that ignores allocations. Fix forward.

The design follows the inbound/outbound provenance pattern in Lago's
`wallet_transactions/track_consumption_service.rb` and `void_service.rb`, adapted
to the existing H3 ledger and holds; it adds no billing service, schema, queue,
provider call, credential, or membership implementation.

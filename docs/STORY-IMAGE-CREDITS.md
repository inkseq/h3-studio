# Story-card image credit primitive (#463)

Internal Python API; no new HTTP route or schema. Claude integrates the image route.
All three writes require a caller-owned `repo.transaction()` (SQLite BEGIN IMMEDIATE
or PostgreSQL READ COMMITTED). Never hold that transaction across the provider call.

- `credits_pricing.image_price(conn, tenant)` returns `{credits, price_revision}` or
  `None` while OFF / without explicit image pricing. ON plus None means refuse, not free.
- `service_credits.service_hold(conn, repo, scope, reference_id, *, binding, quote=None)`
- `service_credits.service_capture(conn, repo, scope, reference_id, *, binding)`
- `service_credits.service_release(conn, repo, scope, reference_id, *, binding, reason)`

`reference_id` is already `story_chat_image:<request_id>`. `binding` is exactly
`tenant, owner, project_id, card_id, request_id, input_hash, actor_id`, matching the
server scope. Hash normalized card name/description, film style and instruction;
never pass an authoritative browser price. An optional quote is validated against
server pricing for a new hold. On replay omit it to recover the held price, or pass
the original one; changed binding/quote conflicts.

Results contain `account`, `entry`, `created`, `hold`, `terminal`, `attachment`.
OFF with no original hold returns None. An existing open hold returns created:false;
without a committed attachment or terminal it means in-progress, not permission to
call Google again. A recovered attachment must be captured idempotently before the
stored picture is returned. A recovered terminal is the original outcome.

Attachment proof uses the original immutable Guided edit receipt and
`journey.storyChat.images[request_id] = {card_id, asset_id, entity_id, at}`, the linked
card/image and ready asset. Store the receipt in the same Guided.mutate as attachment,
with `key="story-chat-image-" + request_id`. This preserves proof even if a later
whole-project save removes the current card or image map; the mutable map alone
cannot authorize capture or prove non-attachment for release.
Upload through AssetService with `client_asset_id="story-chat-image:" + request_id`;
capture verifies that identity as well as tenant/owner/project and ready image kind.
Capture uses the original amount/revision/actor even after OFF or a price change.
Release reasons are provider_failed, provider_timeout_possible_output, card_missing,
version_conflict, store_failed, stale_unattached. Unknown commit/capture outcomes must
be read back using the original identity; never blindly release or call Google again.

There is no automatic stale timeout. Any later stale-release route must fence the
original operation from attaching after release; age alone does not prove a live
provider request is gone. Keep unattached uploads for normal reconciliation.

Admin pricing GET adds image_credits (null if unpriced) and recommended_image_credits5.
Its existing audited/CAS PUT accepts image_credits as a positive integer. Publishing
video rates without this field preserves an existing image rate, and leaves a legacy
unpriced revision unpriced. Reads and deployment never enable customer charging.
New holds also require an explicit credit-unit review of any retained nonzero USD
owner/connection limits. This admission gate does not block original settlement.
Before any production image-price publication, its retained rollback reader must
also support the optional image configuration field. This slice publishes no price.

Finite error contract: insufficient_credits / user_daily_limit_reached /
connection_daily_limit_reached are402; credit_idempotency_conflict /
credit_limits_review_required / credit_terminal_conflict /
credit_attachment_required / credit_attachment_exists /
credit_attachment_outcome_unconfirmed are409; credit_service_binding_invalid /
credit_release_reason_invalid are422; credit_image_price_required is503.
Unknown attachment outcomes retain the hold for original-receipt recovery. The route
maps an existing open hold without attachment/terminal to409 story_chat_image_in_progress.

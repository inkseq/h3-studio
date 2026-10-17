---
name: inkseq
description: Use an Inkseq / 映序 link to create video takes, organise an authorized story and references, and return editable drafts and results through the same business API as the website.
---

# Inkseq / 映序

Use the user's exact HTTPS origin (HTTP only for loopback). A link permits discovery, not editing or spending. Preserve user prompts, model/profile, precision, references and accepted work. Relay outcomes in the user's language; keep API codes and IDs unchanged.

Read [API fields/examples](reference/api.md) when constructing requests and [recovery](reference/recovery.md) for uncertain writes or refusals. Both are available under the same origin's `/for-agents/reference/`. Public discovery is not proof of live authentication or available generation. Authenticated `/v1/agent-guide`, `/v1/guided-schema`, `/v1/quick-chat/schema` and `/v1/capabilities` describe the actual release.

## Connect an assistant

1. Have the signed-in owner choose this session, this story or all their work; organise-only or create/generate; and a daily cap. Organise permits draft/asset work but no paid generation or assistant calls. Do not create or broaden a grant yourself.
2. Inspect `/for-agents/connect.py` and compare its SHA-256 with `/for-agents/connect-manifest.json`. Use the returned origin, connection ID, owner, tenant and fingerprint exactly. A matching digest checks release consistency, not independent trust.
3. Supply the one-time code through the hidden prompt or `--code-stdin`, never argv. The helper saves its locally generated token/verifier in OS-protected storage **before exchange**. Missing storage stops: no plaintext fallback or automatic installation.
4. Check `whoami`, then the authenticated contract. Use the returned editable TTL/policy; existing scopes and expiry remain frozen. Pause/revoke does not delete creations or cancel accepted jobs.

```sh
python scripts/connect.py connect --server ORIGIN --connection CONNECTION_ID --account OWNER --tenant TENANT --fingerprint FINGERPRINT
python scripts/connect.py whoami --server ORIGIN --connection CONNECTION_ID
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/agent-guide
```

Replace placeholders with actual returned values. Example safe response projection: `{"status":"connected","connection_id":"CONNECTION_ID","owner":"OWNER"}`; never a token. Lost exchange? Resume the same saved connection; see recovery.

## Quick Create: plan → takes → choose → story

1. Open an authorized session or create one. Read complete `next_settings`, schema and copy preferences. Upload references, wait for ready receipts, then explicitly select roles/ranges; upload alone selects nothing.
2. Save a card with complete profile, recipe, prompt, controls, inputs and copies. Card = group; revisions are immutable. A fixed seed applies only to the first new take, others receive distinct random seeds. The owner copy maximum defaults to 4 and is editable.
3. Read the revision; refresh exact profile/mode availability and preflight its `input_hash`. Inspect price, readiness and machine allowance. Submit only the unchanged ready preflight within authorized spending and any required human confirmation. Saving or Again alone never generates.
4. Observe original submissions/jobs; list all group pages and inspect outputs/full recipes. Star via annotation CAS. Display `group_number`; retain original item/job/execution IDs for actions.
5. Tweak via a complete new revision, or use Again for a fresh revision in the same group. Separately preflight/confirm it; Again is not retry of an uncertain execution.
6. Transfer the selected take to an owned story scene at its current project version. This copies the complete recipe/references and seed lock into an editable shot, **without enqueueing**. Return its same-origin work link.

```http
POST /v1/quick-chat/sessions/SESSION/cards/CARD/again
Idempotency-Key: saved-again-key

{"expected_card_version":3,"source_revision_id":"REVISION","item_id":"TAKE","copies":1}
```

Example response projection: `{"card":{"id":"CARD"},"revision":{"id":"NEW_REVISION","input_hash":"HASH"}}`. Read that ID/hash; never auto-chain generation. Exact preflight, star and transfer bodies are in the API reference.

## Build or edit a story

1. Read the owned project/version and guided schema. Preserve unrelated content; use project actions instead of replacing the document.
2. Create/edit chapters, scenes, shots, characters, places and props. Characters support multiple `looks`; shot cast can select `{characterId,lookId}`. Use ready owned assets and preserve user names.
3. Configure saved shot drafts, then inspect effective inputs/defaults before planning. Preserve omitted fields; never trim references or change profiles to bypass blockers.
4. For authorized multi-shot work, POST the story's `/storyboard/preflights` with `{expected_version,shot_ids,copies}` and one saved key. Inspect aggregate cost and retained per-copy plans before using the explicit-plan batch API. Copies share the owner's editable limit; only the first uses a fixed seed.
5. Inspect results; adopt with `select:false` to retain the current choice. Use `shot.select` only when choosing that version is intended. Return original receipts and story/shot links.

```http
POST /v1/batches
Idempotency-Key: saved-scene-batch-key

{"client_project_id":"PROJECT","plan_ids":["READY_PLAN_A","READY_PLAN_B"]}
```

Example response projection: `{"batch_id":"BATCH","status":"active"}`; inspect its actual per-item list. HTTP 202 means admission, not inference success. Observe the original batch/per-item jobs; partial admission retains its original identity.

Read a shot's `/versions` for immutable recipes; old missing recipes stay unknown. `storyboard.restore` restores known effective settings without generating. `storyboard.star`, atomic bulk draft/final/move, account→story→shot defaults and project-local profile forks use the [story contract](reference/api.md#storyboard-versions-and-bulk-work). Duration/seed stay per shot; no automatic deletion or global usage search.

## Upload and organise references

1. Identify the authorized session/project; read `capabilities.upload_constraints`. Preserve original media, with no silent cropping or transcoding.
2. Save a stable client asset ID; upload once. An uncertain response requires finding/resuming that receipt, not uploading a replacement.
3. Bind ready receipt IDs to intended card inputs, shots or character looks. Keep video soundtrack selection, audio and source ranges distinct.
4. Assets keep their original project/session ownership. Discover authorized projects' files with `GET /v1/library` filters and inspect current shot usage with `GET /v1/library/usage`. Tag/file through the original project's versioned actions; use explicit import for another story. See the [library contract](reference/api.md#project-scoped-library-and-explicit-imports-238).

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID upload --session SESSION --asset-id SAVED_CLIENT_ASSET_ID --file reference.png --output upload.json
```

Example ready receipt projection: `{"asset_id":"ASSET","status":"ready"}`. Use that `asset_id`, not filename or document entity ID.

## Trash or restore Quick Create history

1. List sessions with `view=active` or `view=trash`; hiding tests is a view filter only.
2. Request a trash/restore preview for exact IDs/versions and inspect eligibility/shared-reference warnings.
3. Confirm the fresh preview with one saved key/body; read the original action until complete. Pending/unknown is not deletion success.
4. Trash has no automatic expiry. Restore is unavailable once irreversible purge intent exists. Purge/empty-trash requires separate explicit authorization and preview. For stories, entities, looks, location versions, files and takes use `/v1/trash/preview` then `/v1/trash/actions`; exact CAS/recovery examples are in [API reference](reference/api.md#universal-trash). Do not substitute hard `entity.delete`.

```http
POST /v1/quick-chat/sessions/history/preview

{"action":"restore","sessions":[{"id":"SESSION","expected_version":2}]}
```

Example response projection: `{"preview_id":"PREVIEW","expires_at":1790000000}`; use the actual fresh expiry. Preview changes nothing; confirm the returned `preview_id` separately.

## Before paying, and when refused

1. Refresh exact-profile availability and preflight. Read current limits; model constraints remain. Machine allowance is not an ETA; unknown price stays unknown.
2. Check the whole requested batch. Human-only spending confirmations bind exact quoted plans/settings; an Agent cannot approve itself or raise caps.
3. After admission, `/v1/generations/queue-position?job_id=JOB` gives advisory shared-queue information, no other owner's identity or ETA. Never rent a GPU to bypass unavailable capacity.
4. Stop new paid work on cap/unknown-charge refusal. Return the same editable draft, stable error code and readable explanation; no replacement job/key or automatic retry. Zero connection cap adds no extra connection limit; owner credit and other safeguards remain. Sydney-day exposure includes unresolved prior obligations, not only invoices.

```json
{"code":"connection_daily_limit_reached","explanation":"This assistant's daily cap was reached / 已达到此助手的每日上限"}
```

This is a user-facing summary example, not a fabricated API receipt. See recovery for stale settings, owner cap, human confirmation and unknown charges.


### Find and import library references
1. Read `GET /v1/library?tag=rain&type=file`; follow `next_offset`.
2. Inspect `/v1/library/usage` for a returned target; unused is not purge permission.
3. Tag/file with original story CAS or explicitly import to another authorized story.
4. Keep the import key/body; observe its receipt before resuming. See [API](reference/api.md#project-scoped-library-and-explicit-imports-238).
Example: `python scripts/inkseq.py request GET '/v1/library?tag=rain'`. Imports never generate.

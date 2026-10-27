---
name: inkseq
description: Create video takes and organise authorized Inkseq / 映序 stories and references through the website's business API.
---

# Inkseq / 映序

Use the user's exact HTTPS origin (HTTP only for loopback). A link permits discovery, not editing or spending. Preserve user prompts, video quality/mode, references and accepted work. Relay outcomes in the user's language; keep API codes and IDs unchanged.

Read [API](reference/api.md) and [recovery](reference/recovery.md) at the same origin's `/for-agents/reference/`. Public discovery proves neither authentication nor capacity. Authenticated `/v1/agent-guide`, `/v1/guided-schema`, `/v1/quick-chat/schema` and `/v1/capabilities` describe the release.

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

Use actual returned values; never display tokens. Lost exchange? Resume the same saved connection; see recovery.

## Unified story cards: author → preflight → versions

Video uses `tier: standard|refine|fast`, `mode: fl|ref`; Fast is unavailable. Read [aliases/refusals](reference/api.md#video-quality-selection) and live capabilities, never a hard-coded REF reference limit.

1. Read the owned project/version and guided schema. A story chat and Works edit the same project: character, location, prop and shot cards are existing entities. Use project actions instead of replacing the document or creating a second session.
2. Create/edit chapters, scenes, shots, characters, places and props. Bind shot cast with `{characterId,lookId}`, the location with `data.storyboard.place.locationId`, and props with `data.storyboard.propIds`. These are entity IDs, not upload receipts. See the complete [card binding example](reference/api.md#unified-card-binding-example). Preserve user names and ready owned assets.
3. Configure saved shot drafts with the chosen tier/mode, then inspect effective inputs/defaults before planning. Preserve omitted fields; never trim references or change quality to bypass blockers.
4. For authorized multi-shot work, POST the story's `/storyboard/preflights` with `{expected_version,shot_ids,copies}` and one saved key. Inspect aggregate cost and retained per-copy plans before using the explicit-plan batch API. Copies share the owner's editable limit; only the first uses a fixed seed.
5. Inspect results; adopt with `select:false` to retain the current choice. Use `shot.select` only when choosing that version is intended. For edits within that take, use [ordered shot segments](reference/api.md#ordered-shot-segments) through `shot.segments`; never change generation duration or regenerate to apply a cut. Return original receipts and story/shot links.

Return `ORIGIN/quick-chat?story=PROJECT` with the owned project ID. Attaching a story uses browser-only `POST /v1/story-chats/PROJECT/open`. PATs must not call `/open`, impersonate a browser or put tokens in links. Entity `/actions` call no conversation model and generate no media.

```http
POST /v1/batches
Idempotency-Key: saved-scene-batch-key

{"client_project_id":"PROJECT","plan_ids":["READY_PLAN_A","READY_PLAN_B"]}
```

HTTP 202 is admission, not success. Observe the original batch/per-item jobs; partial admission retains its identity.

Read a shot's `/versions` for immutable recipes; old missing recipes stay unknown. `storyboard.restore` restores known effective settings without generating. `storyboard.star`, atomic bulk draft/final/move, account→story→shot defaults and project-local profile forks use the [story contract](reference/api.md#storyboard-versions-and-bulk-work). Duration/seed stay per shot; no automatic deletion or global usage search.

For an authorized whole-film request, check `capabilities.story_film.enabled`, then follow [one-click film](reference/api.md#one-click-film). Preflight freezes the story; inspect reuse, missing shots and cost before confirming. Observe/resume that original run, retaining per-shot budgets and grants. Read [film recovery](reference/recovery.md#one-click-film-recovery) after an uncertain response.

Caption burn:2 lines ×18 chars/cue; no silent trim/rewrap. [Risk advice](reference/api.md#story-chat-risk-advice) never authorizes edits/generation.

## Legacy Quick Create compatibility

Existing authorized sessions keep their session/card/revision APIs during migration; new stories use project actions above. Read the [legacy requests](reference/api.md#legacy-quick-create-compatibility) for complete bodies. Follow actual versions, input hashes, per-item seeds and take cursors. Again creates a fresh revision, never a retry of an uncertain execution; preflight and confirm it separately. Transfer copies a selected take to an owned story without enqueueing.

Keep source IDs and receipts. A star is a favourite, not a chosen result; browser preview focus is not persisted selection. Never convert/delete a session or regenerate old results implicitly. For `resume_admission_available`, use [original admission recovery](reference/recovery.md#original-admission-recovery), not a new take/key or cap increase.

## Upload and organise references

1. Identify the authorized project (or existing legacy session); read `capabilities.upload_constraints`. Preserve original media, with no silent cropping or transcoding.
2. Save a stable client asset ID; upload once. An uncertain response requires finding/resuming that receipt, not uploading a replacement.
3. Attach ready uploads to the owned project before profile binding. Character looks, places, props and shot bindings use document entity IDs; executable generation inputs use ready asset receipt IDs. Keep video soundtrack selection, audio and source ranges distinct.
4. Assets keep their original project/session ownership. Discover authorized projects' files with `GET /v1/library` filters and inspect current shot usage with `GET /v1/library/usage`. Tag/file through the original project's versioned actions; use explicit import for another story. See the [library contract](reference/api.md#project-scoped-library-and-explicit-imports-238).

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID upload --project PROJECT --asset-id SAVED_CLIENT_ASSET_ID --file reference.png --output upload.json
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

1. Refresh exact tier/mode availability and preflight. Read current limits; input constraints remain. Machine allowance is not an ETA; unknown price stays unknown.
2. Check the whole batch using server credit quotes when enabled; never convert USD caps. Human approval binds exact plans/settings; Agents cannot approve themselves or raise caps.
3. After admission, `/v1/generations/queue-position?job_id=JOB` gives advisory shared-queue information, no other owner's identity or ETA. Never rent a GPU to bypass unavailable capacity.
4. Stop new paid work on cap/unknown-charge refusal. Return the same editable draft, stable error code and readable explanation; no replacement job/key or automatic retry. Zero connection cap adds no extra connection limit; owner credit and other safeguards remain. Sydney-day exposure includes unresolved prior obligations, not only invoices.

Shot `data.clipSound` is on/off; see [audio](reference/api.md) for defaults and legacy behavior.
See [recovery](reference/recovery.md) for refusals.

## Long-shot windows

Long-shot [preview/confirm and recovery](reference/api.md#confirmed-long-shot-windows).

## Reviews and feedback

[Verdicts, CAS, summaries and suggestions](reference/api.md#explicit-take-reviews).
[Redo diff preview](reference/api.md#redo-from-marks).

[Feedback](reference/api.md#feedback).

## Inky advice

Discover `inky` on the target origin before using [Inky suggestions](reference/api.md#inky-suggestions).
Read current advice without evaluation or spend; an empty list is not a quality verdict.
Suggestions never authorize edits, generation, or a human usable/redo verdict.
Only ignore or mute on explicit user intent. Re-read the shot before acting on advice.

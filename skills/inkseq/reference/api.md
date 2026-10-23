# API reference

Read this for exact request construction; the [task guide](../SKILL.md) chooses the workflow. Paths are relative to the exact authorized origin. Substitute actual returned IDs, integer versions and hashes; examples are templates, not live receipts. Read authenticated OpenAPI directly at `/openapi.json` (the generic helper intentionally accepts only relative `/v1/` paths).

## Helper and credentials

`scripts/inkseq.py --base-url ORIGIN --connection ID request METHOD /v1/PATH --json-file body.json --idempotency-key SAVED_KEY --output receipt.json` supports GET/POST/PUT/PATCH/DELETE. Omit body/key for reads. Save the exact body/key before writes. Each independently intended write needs its own key; original-request recovery reuses it. Output files are exclusive. Use a new output filename for another observation, not a new paid request. Python/httpx are required; connect.py is standard-library only. Choose one credential source: activated OS-protected connection, process-only `INKSEQ_API_KEY`, or an already configured explicit registry profile. Never copy secrets into project files.

## Connection owner controls (browser-only)

The Agent exchanges an owner-issued grant; it cannot issue grants or change spending settings with its PAT. POST `/v1/account/agent-connections` retains required `authorization_profile_id` and `authorization_profile_version`, optional `name`, and supports `scope:{kind:"all"}` or `{kind:"session"|"story",id:"OWNED_ID"}`, `permission:"organise"|"generate"`, `daily_limit_microusd`. Read the actual creation schema/profile rather than inventing omitted fields. Omitting scope and permission preserves legacy fingerprint shape. Organise excludes paid jobs/assistant calls, not draft edits.

GET/PATCH `/v1/account/agent-connection-settings`: `{expected_version:INTEGER,value:{code_ttl_seconds:INTEGER,recovery_ttl_seconds:INTEGER,key_lifetime_days:INTEGER,max_pending_connections:INTEGER}}`; complete value and Idempotency-Key required. Defaults are 600/300/90/5; use returned owner settings/bounds. New-grant changes never extend existing expiry or recovery.

GET `/v1/account/agent-connections/{id}` returns public authorization/status, `state_version`, `paused`, effective `daily_limit_microusd` and conservative `usage`. PATCH uses `{expected_version:state_version,paused:BOOLEAN}` and/or `daily_limit_microusd`, with one stable key. Zero adds no connection cap. DELETE revokes access, not creations. Activity GET `.../{id}/activity?limit=20&offset=0` yields safe event IDs/types/times and `next_offset`, not prompts or ledger contents. `created_by` is immutable proven attribution; unknown historical/browser attribution remains null.

## Credit quotes and limits

When `/api/auth/me` reports `credits.enabled:true`, customer amounts are integer
credits, independently of the retained provider USD ledger. GET `/v1/credits`
returns `{balance,held,available,today_used,daily_limit}`; available is balance
minus held. GET `/v1/credits/entries?cursor=...` reads the original append-only
history. GET `/v1/credits/prices` is public. Immutable preflights expose `credits`,
`price_revision` and `breakdown`; aggregate credits can be null while incomplete.
Use that original quote, not a local duration or price calculation. Ordinary
credit-mode projections omit supplier dollar estimates; legacy quotes retain
their explicit units.

Owner/browser spending settings in credit mode return `unit:"credits"`,
`daily_limit_credits`, `ask_above_credits` and `connection_credit_limits`.
Connection creation/update supports explicit `daily_limit_credits`; read its
returned `unit`. Existing `daily_limit_microusd` settings are retained separately,
never converted or treated as credits. Neither mode permits Agent self-approval,
self-increasing caps, or repeated submission after an unknown outcome.
HTTP402 `insufficient_credits` includes integer `need` and `have`; return the
same draft and request a top-up. A hold happens with original job admission,
validated success captures once, proven failure/cancellation releases, and
unknown execution retains the hold. Human confirmation is still required when
the exact original quote exceeds the owner's threshold.

## Video quality selection

New video authoring uses `tier:"standard"|"refine"|"fast"` and
`mode:"fl"|"ref"`, alongside the existing prompt, controls, inputs, copies and
version fields. Fast is currently unavailable; reducing steps never selects it.
Read the actual tier/mode limits and availability, then preflight. Do not silently
switch quality, mode, references or GPU when a choice is unavailable.

Public compatibility aliases remain supported: `deployment_profile_id` is
`quality:standard` (likewise `quality:refine`/`quality:fast`), and `recipe_id` is
`video-fl-v1` or `video-ref-v1`. A request must use one consistent selection;
conflicting tier/mode/alias combinations return `quality_selection_conflict`.
Every Agent, including an admin-owned PAT, uses public tiers. Raw internal video
selection returns `model_selection_admin_required`. Conversation model selection
and optional title metadata are independent and unchanged.

Example new direct card (replace controls with those returned by the live schema):

```json
{"tier":"standard","mode":"fl","prompt":"A leaf moves in warm sunlight.","controls":{"duration":5,"resolution":"480P"},"inputs":{},"copies":1}
```

`quality_tier_unavailable` preserves the draft for an administrator's configuration;
`quality_tier_ambiguous` requires that administrator to resolve routing. Neither is
permission to substitute another selection. Historical unknown quality remains
unknown. Original accepted requests replay with their original body/key, plan/job
IDs and frozen quote before new selection; never rewrite their history or price.
Rates come from the returned immutable price revision. Tier selection bypasses
neither terms, moderation, scopes, human confirmation nor budgets.

## Legacy Quick Create compatibility

Use project/actions for new story-card authoring. Existing session/card routes
remain supported during the migration compatibility window; no cutoff or bulk
conversion is implied. Preserve source IDs, immutable revisions and original
jobs. A star is a favourite, not a chosen take. Do not reconstruct a browser-only
preview selection or regenerate historical results.

Let B = `/v1/quick-chat/sessions/SESSION`.

| Intent | Method/path | Body / readback |
|---|---|---|
| New session | POST `/v1/quick-chat/sessions` | `{title:"User title"}` or `{}`; omitted title allows configured automatic naming, which can be separately paid. Explicit title avoids requesting auto naming. Return session ID/version/model_id/complete next_settings. |
| Direct card | POST B`/cards` | Complete `tier`, `mode`, `prompt`, `controls`, `inputs`, `copies`; consistent neutral aliases are compatible. Read actual card/revision response. |
| Revise card | POST B`/cards/CARD/revisions` | Same complete snapshot plus integer `expected_card_version`; optional same-session `source_revision_id`. This is not a patch. |
| Read revision | GET B`/revisions/REVISION` | Immutable `input_hash`, settings, per-item seeds. |
| Preflight | POST B`/revisions/REVISION/preflights` | `{revision_hash:input_hash,capabilities_version:CURRENT_VERSION}`. |
| Submit | POST B`/revisions/REVISION/submissions` | `{revision_hash:input_hash,preflight_id:READY_ID,confirmed:true}`; only after readiness/user authority and required human spending approval. |
| Read original submission | GET B`/submissions/SUBMISSION` | Original `revision_id`, `items[].id`, job/execution IDs and `resume_admission_available`. |
| Resume original admission | POST B`/submissions/SUBMISSION/resume-admission` | `{item_ids:[ORIGINAL_ITEM],fresh_preflight_id:READY_SELECTED_PREFLIGHT,confirmed:true}`; separate saved key, original resumable items only. See below; this is not execution retry. |
| Group takes | GET B`/cards/CARD/takes?limit=20&before_cursor=CURSOR` | Follow returned cursor; original item/submission/job/execution IDs, `group_number`, `recipe`, annotation. |
| All session takes | GET B`/takes?limit=40&before_cursor=CURSOR` | Read-only original summaries across all cards; follow `before_cursor`, retain `card_id/revision_id/submission_id`, filter explicit `trash_state`. No story export or generation. |
| Star | PATCH B`/takes/ITEM` | `{expected_version:ANNOTATION_VERSION,starred:true}`. |
| Again | POST B`/cards/CARD/again` | `{expected_card_version:CARD_VERSION,source_revision_id:REVISION,item_id:ITEM,copies:1}`; fresh revision only. |
| Transfer | POST B`/takes/ITEM/story` | `{target_project_id:PROJECT,expected_project_version:VERSION,parent_id:SCENE}`. No enqueue. |
| Transfer status | GET B`/story-transfers/TRANSFER` | Match `id`, `source_session_id`, `item_id`, `project_id`, original `expected_project_version`, `parent_id`; completed also has project_version/shot_id/seed_locked/web_url. |

Direct card/revision omitted selection does NOT inherit session preferences: copy known tier/mode and the full intended settings. Unknown historical quality needs an explicit new selection. Turn-to-card is an alternative using current `expected_version`, returned conversation `model_id`, `assistant_mode:"none"`, `create_card:true`, and full `text`; it inherits session materials/next_settings. None disables conversation assistance, not independently configured automatic naming. Never address hidden Quick Chat project projections.

GET/PATCH `/v1/quick-chat/preferences` uses owner CAS `{expected_version, copies_max, takes_fold_count}` as defined by the live schema. Both are editable positive integers (defaults4/8), subject to existing document/model bounds. Lowering a preference does not rewrite historical reads or accepted submissions. Fixed seed belongs only to the first new take; other new takes draw distinct random seeds. Preserve seeds as their returned decimal strings.

### Resume never-admitted original items

Use `/for-agents/guide.json` → `quick_chat.job_changes.resume_admission`.
After the owner resolves an admission blocker, GET the **original submission**.
Select only its current `items[].id` with `resume_admission_available:true`;
retain their original job/execution IDs. Read `submission.revision_id`, then GET
that immutable revision and current `/v1/capabilities`. Do not substitute the
card's newer revision, create a new card, or reuse initial submission confirmation
expecting it to attempt admission again.

Save a new selected-item preflight body and its stable key:

```json
{"revision_hash":"ORIGINAL_REVISION_INPUT_HASH","capabilities_version":"CURRENT_CAPABILITIES_VERSION","item_ids":["ORIGINAL_RESUMABLE_ITEM"]}
```

POST it to B`/revisions/ORIGINAL_REVISION/preflights`. **Omit
`retry_of_execution_id` for never-admitted items**; linked execution retry is a
different operation for eligible failed/cancelled executions. Inspect the fresh
preflight's exact items, readiness, quote and required human spending approval.
GET B`/preflights/PREFLIGHT` observes this original preparation. Only within the
user's generation authorization, save a separate resume body/key:

```json
{"item_ids":["ORIGINAL_RESUMABLE_ITEM"],"fresh_preflight_id":"READY_SELECTED_PREFLIGHT","confirmed":true}
```

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/quick-chat/sessions/SESSION/submissions/SUBMISSION --output original-submission.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/quick-chat/sessions/SESSION/revisions/ORIGINAL_REVISION --output original-revision.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/quick-chat/sessions/SESSION/revisions/ORIGINAL_REVISION/preflights --json-file selected-preflight.json --idempotency-key saved-selected-preflight-key --output selected-preflight-receipt.json
# Inspect the returned preflight; do not automatically chain a paid write.
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/quick-chat/sessions/SESSION/submissions/SUBMISSION/resume-admission --json-file resume-admission.json --idempotency-key saved-resume-admission-key --output resumed-original.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/quick-chat/sessions/SESSION/submissions/SUBMISSION --output original-submission-after.json
```

Resume admits original planned/blocked work without replacing its jobs,
executions, attempts or accounting. It does not make an unknown execution safe
or failed work free. Caps and approval remain owner-only; the Agent must not
change either, automatically loop this sequence, or switch connections to evade
a refusal. Read [resume recovery](recovery.md#original-admission-recovery) before
repeating a write.

### Explicit helper sequence

The variables below are actual returned values. Create each JSON file from the table before its write; keep it for reconciliation.

```powershell
$base = "/v1/quick-chat/sessions/$session"
python scripts/inkseq.py --base-url $origin --connection $connection request GET "$base/cards/$card/takes?limit=20" --output takes.json
python scripts/inkseq.py --base-url $origin --connection $connection request PATCH "$base/takes/$item" --json-file star.json --idempotency-key saved-star-key
python scripts/inkseq.py --base-url $origin --connection $connection request POST "$base/cards/$card/again" --json-file again.json --idempotency-key saved-again-key --output again.json.receipt
# Read the new revision; set $revision to that returned ID.
python scripts/inkseq.py --base-url $origin --connection $connection request POST "$base/revisions/$revision/preflights" --json-file preflight.json --idempotency-key saved-preflight-key --output preflight.receipt.json
```

Stop and inspect readiness/quote and any human-only approval. Only then, within explicit generation authority:

```powershell
python scripts/inkseq.py --base-url $origin --connection $connection request POST "$base/revisions/$revision/submissions" --json-file confirm.json --idempotency-key saved-submit-key --output submission.json
```

Transfer is separately authorized authoring:

```powershell
python scripts/inkseq.py --base-url $origin --connection $connection request POST "$base/takes/$item/story" --json-file story.json --idempotency-key saved-story-key --output transfer.json
python scripts/inkseq.py --base-url $origin --connection $connection request GET "$base/story-transfers/$transfer" --output transfer-status.json
# Only explicit reconciliation, same body/key, never automatic retarget:
python scripts/inkseq.py --base-url $origin --connection $connection request POST "$base/takes/$item/story" --json-file story.json --idempotency-key saved-story-key --output transfer-resumed.json
```

## Story authoring and generation

GET `/v1/projects` and `/v1/projects/PROJECT`; POST `/v1/projects` creates an owned story with title/logline. POST `/v1/projects/PROJECT/actions` uses `{expected_version:VERSION,actions:[...]}` and Idempotency-Key. Use `/v1/guided-schema` for exact entity.create/update shapes. `entity.update` merges one data level; nested objects replace whole values. Never overwrite unrelated nested data.

A character look is `{id:"day",name:"Day",description:"User description",version:1,gallery:{}}`; gallery accepts front/side/full entity IDs. A character's two looks can be referenced by two shot cast entries `{characterId:"CHARACTER",lookId:"day"}` / a second actual look ID. Gallery references must be authorized existing document assets; do not substitute upload receipt IDs where entity IDs are required.

### Unified card binding example

The chat's character, place, prop and shot cards are entities in this same owned
project. Read its current version and `/v1/guided-schema`; choose unused IDs for
new entities. This example creates document structure and bindings only. It does
not attach media, call a model or enqueue generation. Replace `expected_version`
with the actual version and save one Idempotency-Key before POSTing to
`/v1/projects/PROJECT/actions`:

```json
{
  "expected_version": 1,
  "actions": [
    {"op":"entity.create","entity":{"id":"chapter-arrival","type":"chapter","title":"Arrival"}},
    {"op":"entity.create","entity":{"id":"scene-station","type":"scene","parentId":"chapter-arrival","title":"Station"}},
    {"op":"entity.create","entity":{"id":"character-mia","type":"character","title":"Mia","data":{"looks":[{"id":"raincoat","name":"Raincoat","description":"A yellow raincoat","version":1,"gallery":{},"imageIds":[]}],"defaultLookId":"raincoat"}}},
    {"op":"entity.create","entity":{"id":"location-platform","type":"location","title":"Platform","data":{"versions":[{"id":"night","name":"Night","description":"A rain-soaked platform","imageIds":[],"version":1}],"defaultVersionId":"night"}}},
    {"op":"entity.create","entity":{"id":"prop-letter","type":"prop","title":"Letter","data":{"imageIds":[]}}},
    {"op":"entity.create","entity":{"id":"shot-letter","type":"shot","parentId":"scene-station","title":"The letter","data":{"seconds":5,"prompt":"Mia opens a letter on the platform.","cast":[{"characterId":"character-mia","lookId":"raincoat"}],"storyboard":{"version":1,"mode":"ref","place":{"locationId":"location-platform","versionId":"night"},"propIds":["prop-letter"]}}}}
  ]
}
```

Use the returned project version for the next edit. When editing existing shots,
preserve other `data.storyboard` keys: an `entity.update` nested object replaces
that whole object. Profile image/gallery bindings use attached document entity
IDs; `shot.configure_generation.inputs` uses ready upload receipt IDs instead.
An upload alone does not choose its role. Read `/v1/capabilities` for the exact
REF limits before configuring/preflighting; never truncate references to fit an
assumed limit. No available references may still mean a blocked preflight.

Return `ORIGIN/quick-chat?story=PROJECT` for a human to continue. The website may
attach an ordinary owned project through `POST /v1/story-chats/PROJECT/open` in
its signed-in browser. This route is human-only, including for an admin-owned
PAT: Agents use `/v1/projects` and `/actions`, not `/open` or a browser credential.
The link must never contain a key. Legacy `?session=` links stay legacy until an
explicit conversion is accepted; do not rewrite them to guessed project IDs.

`shot.configure_generation` takes `shot_id` and requested tier/mode, prompt, controls and inputs; consistent neutral recipe/profile aliases also work. GET `/v1/projects/PROJECT/shots/SHOT/generation-draft`, then POST its `/generation-plans` with `{expected_version:project_version}` (optional current capabilities_version). Plan generation does not enqueue. POST `/v1/jobs` with `{plan_id:READY_PLAN}` or `/v1/batches` with `{client_project_id:PROJECT,plan_ids:[READY_PLAN,...]}` only within generation authority. Batch admission may partially succeed; observe its original receipt.

`artifact.adopt` with artifact_id/shot_id/select:false adds a candidate; `shot.select` uses the adopted entity_id, not artifact receipt ID. Adopt actual returned audio separately; `sound.generated` requires a selected adopted video and exactly one matching FLAC from the same succeeded job. Never invent missing audio.

### Storyboard versions and bulk work

GET `/v1/projects/PROJECT/shots/SHOT/versions?limit=20&offset=0` returns newest-first original video/image artifacts, `next_offset`, project version, `recipe_status`, `recipe` and original `authoring_context`. The recipe contains prompt, recipe/profile IDs, controls including exact seed, ready receipt inputs with original roles/ranges/audio. Unknown historical recipes are null: never reconstruct them from today's profiles. Selecting a candidate and restoring its settings are different edits; neither enqueues work.

Use the existing project actions envelope and one saved Idempotency-Key:

```json
{"expected_version":7,"actions":[{"op":"storyboard.star","shot_id":"SHOT","artifact_id":"ARTIFACT","starred":true}]}
```

`storyboard.select` takes the same shot/artifact IDs (without starred), authorizes the original shot receipt, and explicitly adopts/selects it through the existing artifact path. `storyboard.restore` takes those same IDs. It saves the exact effective recipe as explicit inputs and retains original profile context separately. It never rewrites accepted jobs or source assets. `storyboard.bulk` takes `shot_ids`, `operation:"draft"|"final"|"move"`, and `parent_id` for move. All selected shots change in one project CAS or none. Quality is intent, not automatic model replacement: explicitly include `shot.configure_generation` actions with actual selected profile IDs when requested. No delete/undo action is added (#236).

GET/PATCH `/v1/storyboard/preferences` reads/replaces account defaults with `{expected_version,defaults:{recipe_id?,deployment_profile_id?,controls:{...}}}` plus a saved key. `storyboard.defaults` action stores `{value:DEFAULTS}` on the story. Defaults inherit global→story→shot; generation-draft and versions expose stored layers, effective values and provenance. Duration and seed are shot-only. Original Quick Chat `copies_max` remains the shared editable copies limit. Default changes invalidate unaccepted new quotes at the transaction gate; original accepted replay is unchanged.

POST `/v1/projects/PROJECT/storyboard/preflights` with saved body `{"expected_version":7,"shot_ids":["SHOT"],"copies":2}` and key creates an unpaid durable operation. GET the returned `/storyboard/preflights/ID` to observe; explicit same-body/key POST resumes missing plan projections. Items retain `shot_id,index,seed,plan_id,plan`; a fixed seed applies only to copy zero, other seeds are distinct in the selected runtime domain. Never redraw after timeout. `estimate.cost_microusd` and `machine_seconds` are nullable; machine allowance is not ETA. Only explicitly approved ready plan IDs go to original `/v1/batches`; aggregate spending confirmation and partial batch recovery stay unchanged.

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/projects/PROJECT/storyboard/preflights --json-file storyboard-preflight.json --idempotency-key saved-preflight-key --output preflight.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/projects/PROJECT/storyboard/preflights/RETURNED_ID --output observed.json
```

Do not automatically chain admission. Pending/unknown retains the original key/body. A matching `closed_preflight:true,status:"blocked",no_generation_submitted:true` permits explicitly abandoning that unpaid preflight; original partial plans remain retained. New storyboard-only action CAS failures positively report `no_mutation_committed` with canonical `request_hash`; generic errors are not proof of no write.

GET `/v1/projects/PROJECT/storyboard/profiles` reports project-local shot usage only. `storyboard.fork_profile` action takes `profile_id,new_profile_id,shot_ids,title?`; one CAS copies a character/location/prop and rewires selected shots, leaving original profiles, unselected shots and other projects untouched. Authorized cross-project discovery and current-authoring usage are available through `GET /v1/library` and `GET /v1/library/usage`; writes retain original project permissions. 中文：旧配方缺失显示未知；预检不生成，未知结果保留原操作；档案副本只改所选镜头，删除撤销另属 #236。

### One-click film

Use this only for the user's authorized whole-story output. Read authenticated
`GET /v1/capabilities`: `story_film.enabled` must be true; retain its
`workflow_version`. Disabled/missing means unavailable, not permission to submit
each shot separately. The connection needs this owned story and the existing
project/asset/job scopes for the work; organise-only does not permit generation.

Let F = `/v1/projects/PROJECT/films`. Read the project's current integer version,
selected takes/ranges and generation drafts first. Configure intended profiles,
materials, captions and parameters through normal story actions before preflight.

| Intent | Method/path | Exact body / result |
|---|---|---|
| Freeze and preflight | POST F`/preflights`, saved Idempotency-Key | `{expected_version:VERSION,resolution:"720P",aspect:"16:9",burn_subtitles:false}`; only `expected_version` is required. Resolution defaults720P; aspect may be omitted, burn_subtitles defaultsfalse. Inspect returned run `id`, `project_version`, `snapshot_hash`, `workflow_version`, `generation`, `chapters`, `estimate`, `counts`, `warnings`. |
| Confirm original run | POST F, separate saved Idempotency-Key | `{preflight_id:RUN_ID,confirm:true,spending_settings_version:CURRENT_SETTINGS_VERSION}`; the settings version is optional but, when supplied, must be current. Response202 is accepted orchestration, not finished video. |
| Observe | GET F`/RUN_ID` | No body/key; use original run ID. |
| Request cancellation | POST F`/RUN_ID/cancel` | No body/key. Cancels original work and stops future admission; `cancel_requested` is not yet `cancelled`. |
| Explicit resume | POST F`/RUN_ID/resume` | No body/key. Same actor/grant and original run/jobs; not a failed-job regeneration API. |

Preflight creates no generation job. Reused selected takes retain their ranges;
missing/outdated shots receive original immutable plans and seeds in
`generation[].plan_id`. Known recipe changes affect freshness; ordinary selection
does not. `film_selected_recipe_unknown` means an authorized historical take is
reused without proven recipe freshness. Report that warning; do not replace the
take automatically. The frozen run is independent of subsequent editable Story
changes; changing the intended film requires a deliberate new preflight.

Inspect returned `credits` and `price_revision` in credit mode, otherwise
`estimate.cost_microusd`; null or incomplete quotes are unknown, not free.
`confirm:true` records the requested film; it is **not**
human spending approval, a cap increase or a whole-film lump-sum reservation.
Every shot/chapter/export uses the original batch/job admission and accounting.
If the server requires human confirmation, the signed-in owner must approve the
exact returned generation plan IDs through the existing spending-confirmation
flow. The Agent must not call that browser-only approval as itself. A header or
202 response cannot bypass a paused/revoked grant, current scopes, asset/trash
rules, quotes or per-connection/owner limits.

The original controller generates missing shots, renders all chapters, burns
already confirmed captions when requested, then concatenates the film and saves
its original video/separate audio artifacts. It does not generate caption text,
images or replacement look photos. Unsupported duration/output/input combinations
are explicit blockers; never trim parameters or references to force admission.

For `burn_subtitles:true`, **each cue has at most two nonempty lines, each
1–18 characters**, plus the existing plain-text and frame/timing bounds. SRT
export or explicit confirmation does not waive burn validation. A confirmed
draft with invalid text/timing returns `film_captions_invalid`; absent/stale
confirmation returns `film_captions_require_confirmation`. Never silently trim
or rewrap text. If the user intends different text, line breaks or timing,
save that change explicitly and confirm the exact changed draft before a fresh
film preflight. An accepted film retains its original confirmed snapshot.

Read the original run at bounded intervals, respecting Retry-After if present.
Phases include `generating`, `chapters`, `exporting`, `admission_blocked`, `blocked`,
`authorization_required` and `cancel_requested`; terminal phases are `completed` and `cancelled`.
`counts` includes `shots`, `reused`, `generate`, `generated_completed`,
`chapters_completed` and `export_completed`. Preserve finite `error_code` and
safe `generation[]`/`chapters[]`/`export` plan/job/artifact IDs. GET does not resume.

On completion, `export.artifact_id` identifies the final video and `export.job_id`
its original job. GET `/v1/jobs/EXPORT_JOB_ID/artifacts`; match the returned video
ID and inspect each actual audio artifact. Use its advertised `content_url` or
`download_url` and full `sha256`; do not infer a separate audio file from video
metadata or expose R2 object keys/signed redirects. Chapter artifact IDs use the
same original routes. No adoption into the editable Story is implied.

Existing helper commands suffice; save non-secret bodies in the named JSON files:

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/projects/PROJECT/films/preflights --json-file film-preflight.json --idempotency-key saved-film-preflight-key --output film-preflight-receipt.json
# Inspect the receipt and existing permission/confirmation requirements first.
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/projects/PROJECT/films --json-file film-confirm.json --idempotency-key saved-film-confirm-key --output film-accepted.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/projects/PROJECT/films/RUN_ID --output film-observed-1.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/jobs/EXPORT_JOB_ID/artifacts --output film-artifacts.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID download /v1/artifacts/VIDEO_ARTIFACT_ID/content --output film.mp4 --sha256 RETURNED_VIDEO_SHA256
```

The helper's `poll --job` observes a job, not a film run. Use generic GET for the
film header and a fresh receipt filename per observation. Downloads default to a
512MiB client limit; explicitly choose `--max-bytes` from the actual artifact size
if needed, retaining the SHA check. See [film recovery](recovery.md#one-click-film-recovery)
before repeating a write.

### Input slots and partial edits

`shot.configure_generation` accepts optional `tier`, `mode`, neutral selection aliases, `prompt`, `controls`, `inputs`. Omitted fields/slots preserve current values. Controls merge by field. Explicit `[]` clears a list slot; `null` clears first/last frame. Changing only quality/mode does **not** erase incompatible references: inspect saved inputs and preflight errors, and clear incompatible slots explicitly when requested.

| Slot | Value, using same-project upload receipt IDs |
|---|---|
| `first_frame`, `last_frame` | `{"asset_id":"..."}` or `null`; FL, and REF when the selected profile advertises these anchors |
| `images` | `[{"asset_id":"...","purpose":"reference"}]`; Ref recipe |
| `videos` | `[{"asset_id":"...","purpose":"motion","include_audio":false}]`; REF references, or an explicitly selected FL control-video mode advertised by the chosen profile |
| `audios` | `[{"asset_id":"...","purpose":"audio"}]`; REF references, or FL soundtrack input when the chosen profile advertises `audio_conditioning:"soundtrack"` |
| `guides` | `[{"media_id":"...","time_seconds":0,"use_audio":false}]`; timed anchors |

Audio/video list entries and guides may include `source_range:{"start":0,"end":4}` in seconds. On the same existing association, omitting the range preserves it; explicit `source_range:null` clears it. Stored selections are compiled into real derivatives at preflight, which requires `assets:write`. Preserve originals; do not silently trim, rescale or mute the user's input to bypass constraints.

For full-control profiles, FL video input requires explicit `controls.video_conditioning:"denoise"` or `"in_context"`; FL does not accept ordinary reference-video mode. FL audio input requires `controls.audio_conditioning:"soundtrack"`. The separate `"from_video"` audio mode is supported only with FL denoise video and no separate audio input or selected soundtrack. `controls.video_excerpts` and `controls.audio_excerpts` instead require REF with one video in reference mode; each entry uses `time_seconds` and `duration_seconds`. Audio excerpts additionally require that video's `include_audio:true`, `audio_conditioning:"reference"` and no separate audio input. These controls differ from a source file's `source_range`. Read the selected profile's current capabilities and preflight for the exact permitted combination; never infer support from this table or change inputs to pass a check.

Read the selected profile's `generation_support[fl|ref].controls`, `input_support`, `limits` and `supported_constraints`; recipe names alone do not determine input compatibility. FL supports text-only, first-only, last-only or both. Additive full-control native profiles also map timed image injections, explicitly selected control-video/soundtrack modes and, in REF, first/last anchors alongside ordinary references. Retained profiles keep their original FL/REF separation and silent-video limits. Choose only modes and controls advertised for the exact selected profile; never discard a supplied input to make it fit.

REF limits distinguish ordinary images, effective video/audio references, injected images and anchors. Full-control profiles map up to nine ordinary images, three video references and three audio references within twelve native reference rows; an audio reference needs a matching visual reference. Video soundtrack selection, excerpts and guide roles can change that count, so use the returned limits and exact preflight rather than adding every uploaded file together. The server normalizes explicit clip selections and frame times before freezing the plan; it rejects native aggregate trimming. `joint_cases` and `verified_cases` remain historical timing examples, never an allowlist. Supported unmeasured combinations need no qualification run or extra confirmation. File/type validity, actual mode incompatibilities, ownership, resource ceilings and budgets still apply. A failed generation preserves its original request and reports an actionable stage/reason; a manual retry needs fresh preflight and explicit authorization, never an automatic paid loop.

Recipe IDs are not supplier model IDs. Seed is a **decimal string**; read the selected capability's `controls.seed.maximum_decimal`. The pinned WanGP runtime accepts unsigned32-bit seeds (`0`…`4294967295`); Comfy retains its unsigned64-bit range. Reject an explicit seed outside the selected range rather than truncating it or changing a saved request. When a deployment preset applies to unset controls, use its supported values only for still-unset fields; never override the user's saved choices silently.

Output duration is separate from the edit timeline and reference selections. Native frame snapping may make the sampled duration slightly longer than the requested export; inspect `output_spec` at preflight. A duration allowance can increase the operator's runtime and budget reservation for a longer clip; it is not measured speed or a final charge. The capability window describes the baseline: each actual request must still pass its own runtime, cold-start, budget and deadline checks. Never shorten the user's requested clip silently to pass admission.


## Media, history and money

### Small previews and session-wide review (#287)

Asset, Library and video-artifact responses advertise `thumbnail_url`, `preview_url`, `rendition_status` and verified `renditions` when display copies exist. Use the advertised thumb for grids and preview for detail; download the original through its original content/download URL. These display copies are not new generation input IDs. Missing/pending copies never mean the original is absent, and a content GET does not create a copy. Keep private signed redirects out of prompts and saved configuration; use the original authenticated API URL for later reads.

```powershell
# $base is /v1/quick-chat/sessions/$session; no paid work or story copy.
python scripts/inkseq.py --base-url $origin --connection $connection request GET "$base/takes?limit=40" --output session-takes-page-1.json
# Continue with the returned before_cursor, using a new output file per page.
python scripts/inkseq.py --base-url $origin --connection $connection request GET "$base/takes?limit=40&before_cursor=$cursor" --output session-takes-page-2.json
```

Session summaries include the original card/revision/submission IDs, status and annotation, plus safe artifact display metadata. `total_count` includes explicit trashed rows; creator grids hide them. For full immutable parameters or compare/star/adopt actions, read that original card's existing group-takes API. Open the same browser card with `/quick-chat?session=SESSION&card=CARD&revision=REVISION&submission=SUBMISSION`; omit submission only for an unsubmitted draft. Reading pages or opening the card must not submit a job, replace the current composer draft, or restart an unknown original operation.

POST `/v1/assets` multipart uses client_project_id/client_asset_id/file; Quick Chat uses B`/assets` with client_asset_id/file. GET assets with original scope and stable client ID, then POST the original receipt resume endpoint if appropriate. For authorized aggregate discovery, tags and usage, see [the current library contract](#project-scoped-library-and-explicit-imports-238); original project ownership and write authority remain required.

History: GET sessions with view=active|trash; POST `/v1/quick-chat/sessions/history/preview` with action and exact `{id,expected_version}` targets; POST `/v1/quick-chat/sessions/history/actions` with `{preview_id,confirmed:true}` and saved key; GET `/v1/quick-chat/sessions/history/actions/ACTION`. Empty-trash preview uses an empty sessions list and returns the full target set. Trash never auto-expires; explicit irreversible purge prevents restore. Other content uses universal trash below.

Owner/browser spending settings and confirmations are not machine-PAT controls. POST `/v1/account/spending-confirmations` accepts exactly `{plan_id:ID}` or `{plan_ids:[ID,...]}` and validates current quotes/settings. Group estimates expose cost_microusd and machine_seconds or null. Queue GET `/v1/generations/queue-position?job_id=OWNED_JOB` is advisory, no ETA. Limits include Sydney-day unresolved prior obligations.


## Universal trash

`GET /v1/trash?kind=shot&limit=100&offset=0` lists owner-scoped trashed/purging items; follow `next_offset`. `kind` filters the displayed entity type. Target identities are `{kind,project_id,id,parent_id?}`. Kinds: `project` (id equals project_id), `entity` (chapter/scene/shot/profile/file entity), `look` (parent_id character), `location_version` (parent_id location), `asset` (uploaded receipt), `artifact` (generated version receipt), `take` (parent_id Quick Chat session). Conversations retain the original history API above; do not claim one atomic transaction across the two APIs.

1. Save a JSON preview request: `{"action":"trash","items":[{"kind":"entity","project_id":"RETURNED_PROJECT","id":"RETURNED_SHOT"}]}`. POST `/v1/trash/preview`. This only reads; it discovers exact `items[].expected_version` and `affected_project_versions`. A blocker rejects the entire selection; running/uncertain work and the last chapter/scene cannot be removed.
2. Save the action body with the returned item versions and `expected_project_versions: PREVIEW.affected_project_versions`. POST `/v1/trash/actions` with one saved Idempotency-Key. Inspect `operation_id`, `request_hash`, `state` and every item; never fabricate success from HTTP timeout.
3. Undo uses a new explicit `restore` preview for the same targets, then the new returned versions. Restoration uses a three-way reference merge, preserves unrelated edits and refuses conflicting changes. If the containing story is trashed, restore it first.
4. Permanent deletion uses `purge` preview and explicit `confirmed:true`. It is asynchronous: GET `/v1/trash/actions/OPERATION_ID`. Same original body/key returns that operation; it does not start another purge. `pending`/unknown never means absent, no auto-retry with a new key. Restore is forbidden after purge intent. Emptying the trash is an explicit, fully paginated selection followed by this same atomic preview/action; newly arriving trash is not silently added.

```sh
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/trash/preview --json-file trash-preview.json --output preview.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request POST /v1/trash/actions --json-file trash-action.json --idempotency-key saved-trash-key --output operation.json
python scripts/inkseq.py --base-url ORIGIN --connection CONNECTION_ID request GET /v1/trash/actions/RETURNED_OPERATION_ID --output observed.json
```

Known closed refusals include matching `resource:"trash"`, canonical `request_hash` and `no_mutation_committed:true`; only that positive proof allows abandoning the saved command and explicitly selecting again. A bare 4xx/5xx is not that proof. `removed`/`kept` report actual media handling; immutable accepted inputs, current other references and accounting/audit identities remain. A completed purge may retain referenced media. Physical deletion uses the original activated reconciler, exact storage receipts, full SHA and confirmed absence; no prefix sweep. No automatic expiry, no background deletion merely because something is old. Replayed historical authoring receipts cannot reattach tombstoned content. Unsaved browser drafts are not server references.

Rename/edit uses original `project.update`/`entity.update` actions and existing Quick Chat title/card APIs. Immutable takes and generated receipts are not rewritten to rename them. 中文：回收站可恢复且不会自动清空；永久删除须逐次明确确认，未决状态保留原操作身份。已接收输入与账务不改，仍被引用的媒体保留。


### Reversible trash and display titles (#236)
Permanent purge requires the original project/assets/jobs read+write cleanup scopes. A project with another recoverable child trash item cannot be purged (`trash_dependent_items_retained`): restore the parent and review each child explicitly. No automatic expiry or implicit child purge occurs. Asset display titles retain the original filename; generated versions use owner authoring titles or their original identifier.

Rename a take through the existing annotation authority: `PATCH /v1/quick-chat/sessions/{session_id}/takes/{item_id}` with the original Idempotency-Key and JSON `{"expected_version":0,"title":"Chosen take"}`. `starred` and `title` are optional individually, at least one is required; omitted fields are preserved. Read `takes[].annotation.{version,title,starred}`. A positively closed CAS failure includes top-level `resource=quick_chat_take`, `session_id`, `take_id`, original `request_hash`, and `no_mutation_committed=true`; any other uncertain response retains the original body/key.

Rename a generated story version with the existing project action `{"op":"storyboard.star","shot_id":"RETURNED_SHOT_ID","artifact_id":"RETURNED_ARTIFACT_ID","title":"Chosen version"}` inside `{expected_version,actions}` at `POST /v1/projects/{project_id}/actions`. Omitted `starred` is preserved; the title is an owner project annotation projected as `versions[].title`, never an edit to the immutable artifact, accepted recipe, or job. Existing configured project byte limits bound annotation data.

Original conversation history remains its existing authority. Only `history_preview_expired`, `history_preview_changed`, or `history_action_blocked` from confirmation return a positively rolled-back `detail` proof (`resource=quick_chat_history`, `action=confirm`, original confirmation-body hash, `no_mutation_committed=true`). Original accepted action replay is checked before preview expiry. Do not treat arbitrary 4xx responses as proof of no write.

中文：回收没有自动到期；永久删除沿用原完整清理权限，父故事存在可恢复子项时拒绝清空。试片与生成版本重命名只改可变标题标注，保留星标和原任务/配方/账务。未知响应保留原请求与幂等键，只有匹配原请求的明确未写入回执才允许重新选择。


## Project-scoped library and explicit imports (#238)

The storage decision is [the project-scoped storage decision](https://github.com/inkseq/h3-studio/blob/main/docs/LIBRARY-STORAGE.md): keep original project ownership and upload accounting. `GET /v1/library` aggregates only authorized current projects. Filters are `project_id`, exact `type` (or `profile`/`file`), `work` (computed actual current-authoring usage, not owning story), `unused`, exact `tag`, text `q`, and transport pagination `limit/offset`. `GET /v1/library/usage?project_id=...&kind=entity|asset|artifact&id=...` returns shot links. `usage_kind=current_authoring`; historical accepted-input retention is explicitly unmeasured here and unused never authorizes purge. Profile filing alone is not shot usage. Original default/selected look, place, voice, audio, frame and guide compiler projections remain authoritative.

Returned items contain `target={project_id,kind,id}`, type/title/tags, source story, original receipt identity when applicable, project_version, usage, work_ids, unused and optional import provenance. Attached media uses its entity target; unbound original receipts remain addressable. Aggregate reads do not grant write permission. No tag search over another owner's data exists.

Use original `POST /v1/projects/{id}/actions`, expected project version and original Idempotency-Key:
- `library.tag`: `{targets:[{kind,id}],tags:["rain","lead"]}` replaces tags; an empty array clears tags.
- `library.rename`: `{target:{kind,id},title:"Chosen reference"}` changes display metadata, never original receipt filenames.
- `library.file`: `{profile_id,targets:[{kind,id}],variant_id?}` (or existing `entity_ids`) attaches raw same-story receipts when needed and files in the same CAS. Ordinary image/video/audio filing is organizational. Optional look/location-version image slots require images; no silent voice/ambient selection. Original image controls remain unchanged except explicitly selected image filing.
Mixed-story bulk requests must be separate per-story CAS operations with separate outcomes. Deletion uses #236 `/v1/trash` only.

`POST /v1/library/imports` takes `{source:{project_id,kind,id,expected_version?},target_project_id,expected_project_version,title?}` and an Idempotency-Key. Source expected_version binds the selected library row; otherwise the server returns the exact accepted current-source snapshot version/hash. Both story permissions are checked before any intent or upload. Profiles and their files are copied into new destination IDs using original upload journals, original quotas and full SHA/size verification. Existing source bytes, tags and profile variants are retained; references are remapped rather than linked across story scope. No model job is submitted.

Read `GET /v1/library/imports/{operation_id}` without advancing. Receipts bind original source/body/hash/destination, source_project_version/source_snapshot_sha256, state pending/completed/blocked, copied asset_ids and completed entity_ids/project_version. Explicitly resume only the original POST/body/key. Network uncertainty never allocates a new upload identity. A blocked destination CAS retains all copied assets and has `no_authoring_committed=true`; only an explicit new user intent may choose another target. Initial source/target version conflicts return a matching `resource=library_import`, request_hash and no_mutation_committed proof only before an operation exists. Other 4xx/unknown responses never imply no write.

中文：素材和档案仍属于原故事；跨故事列表只聚合有权查看的内容。标签、归档、重命名复用故事版本CAS。导入复用原上传回执、配额和完整哈希，不迁移原数据、不自动生成或删除。用在哪里只表示当前创作引用，未使用不等于允许永久删除；未知结果保留原请求与键。


Helper examples (authenticated original connection; use actual returned IDs/versions in JSON files):
```sh
python scripts/inkseq.py request GET '/v1/library?tag=rain&type=file'
python scripts/inkseq.py request GET '/v1/library/usage?project_id=STORY&kind=entity&id=FILE'
python scripts/inkseq.py request POST /v1/projects/STORY/actions --json-file tag.json --idempotency-key library-tags-1
python scripts/inkseq.py request POST /v1/library/imports --json-file import.json --idempotency-key library-import-1
python scripts/inkseq.py request GET /v1/library/imports/RETURNED_OPERATION_ID
```
For explicit recovery, repeat only the same import POST, JSON file and key. Do not chain this to paid generation. `tag.json` uses the original `{expected_version,actions}` envelope; `import.json` uses the import body above. Source/destination IDs are account data, never credentials.

Pending library imports block source/destination lifecycle changes under the original owner reference gate, even before receiving bytes. Terminal receipt reads/replays retain current owner/scopes without requiring active project content. Only positively completed same-owner import audit capsules are excluded from an explicit universal-purge reference census; pending and blocked capsules remain conservative references. No audit rows are removed. Original guided validation rollback after verified copies produces `blocked`, `reason=library_authoring_validation_failed`, `no_authoring_committed=true`; retain original copies/key and explicitly choose a new intent if needed. / 未完成导入阻止相关生命周期变更；已确认的校验回滚保留原回执和副本，不自动重试或清理。

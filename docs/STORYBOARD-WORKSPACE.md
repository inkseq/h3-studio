# Storyboard workspace contract

Owner-approved direction: 2026-10-14. Delivery parent: [#156](https://github.com/inkseq/h3-studio/issues/156).
This is the versioned layout/domain/API contract derived from the approved prototype's
HANDOFF and model rules, with the owner's current capability and persistence requirements.
It specifies intended behavior; implementation checks and exact release/live evidence
belong to the linked issues, not to this document. Initial live acceptance is pending
[#161](https://github.com/inkseq/h3-studio/issues/161).

## 1. View and creation loop

Ship **layout A only** as the guided story workspace's main screen: shot cards above
a docked player/timeline, with a self-contained resizable shot editor in a right drawer.
The old seven-step guide and canvas remain reachable. Do not ship layouts B/C or a
layout switcher. Resizable library/editor/player panels remember their sizes; dragging
splitters resizes and double-click restores defaults.

The loop is: author a shot, preflight and explicitly confirm generation, watch candidate
versions, choose a version, then edit/regenerate the individual shot. Keep old versions.
Drag shot cards to reorder using existing shot order; clicking a timeline segment opens
that same shot in the editor. The project style is editable beneath the chapter title.
The player and timeline use selected existing artifacts and their real duration. Existing
rough-cut assembly is retained; a new final audio mix/music-track system is out of scope.

Library tabs expose character, location and prop profiles, and raw image, motion-video
and audio files. Search, expansion, per-shot usage indicators and an unfiled inbox reuse
the existing project/entities and owner asset records. A filename-based suggestion is
only a proposed filing action; it never calls a model or moves assets without a user
choice. Clicking a profile opens its editor; “add to shot” uses the selected shot.

Drag profiles/files onto cards, slots or the shot-content area. Show the actual drop
result before release. Right-click a supported slot/card opens a searchable picker
restricted to accepted item types; Enter chooses the first result. Desktop drag and
file-picker uploads use existing asset upload/attachment paths. Character profiles add
cast; location profiles set/replace the place; props add unique prop bindings. A raw
image can fill first/last-frame slots; only images are accepted there. A location/prop
profile dropped on a frame slot uses its main image if available. A character profile
in that slot becomes cast, not a fabricated frame image. Video/audio dropped in FL
remain saved as inactive REF extras, with that outcome made clear.

## 2. Existing authority and additive mapping

Keep the version-4 project schema, existing IDs and entity versions. Existing project
and guided action routes are the single authoring authority. Raw media are existing
image/video/audio entities attached to owner/project-scoped `/v1/assets` receipts.
The media entity's `data.cloudAssetId` identifies the server asset; profile and shot
fields below reference **entity IDs**, not receipt IDs, URLs, paths or new media rows.

| Concept | Existing authority / additive fields |
|---|---|
| Story, chapter, scene, shot and ordering | Existing project entities and guided actions; keep IDs. |
| Character | Existing character description/looks; add default look, ordered/main look images and voice choices. |
| Location | Existing location description/reference images; add versions and ambient audio. |
| Prop | Add `prop` to existing profile entity types, with description and ordered images. |
| Upload/inbox | Existing owner asset upload plus `asset.attach`; inbox means unfiled entities, with local filename suggestions. |
| Shot inputs | Existing cast plus optional version-1 `data.storyboard`; compile through existing saved-shot generation routes. |
| Candidate versions/selection | Existing shot jobs/artifacts and candidate adoption; selected video entity remains `data.selectedAssetId`. |
| Save frame | Owned successful video artifact becomes an ordinary new image asset through bounded CPU extraction. |
| Queue/machines | Existing authorized P4 workspace API/projections; no new scheduler, rental authority or operator permission. |

### Character fields

Retain each existing look's `id`, `name`, `description`, `version` and `gallery`.
`character.data` adds:

- `defaultLookId`: an existing look ID; an empty binding uses the selected default
  (or the first look when no default is configured).
- `voices: [{id, assetId, clipStart}]`: unique voice IDs and existing audio entity IDs.
  `clipStart` is a finite nonnegative number selecting a three-second source clip.
- `mainVoiceId`: an existing voice ID, used when a look has no explicit voice override.
- Each `looks[]` may add ordered `imageIds: [imageEntityId, ...]` and `voiceId`.
  The first image is main. Missing `imageIds` preserves the legacy gallery fallback;
  an explicit empty list clears the look's photos. Missing/empty `voiceId` uses main voice.

The profile editor supports looks, descriptions, add/reorder/set-main photos, voice
playback and clip-start selection. Computer uploads, desktop drag and existing library
assets all attach through the same existing assets. AI look-photo generation is hidden
or disabled with a coming-soon indication; it is not implemented in this batch.

### Location and prop fields

`location.data` adds:

- `versions: [{id, name, description, imageIds, version}]`, with ordered image entity IDs.
- `defaultVersionId`, used by an empty version binding; no implicit replacement of an
  explicitly selected missing version.
- `ambient: null | {assetId, clipStart}`, selecting a three-second clip from an audio entity.

Existing location reference-image fields remain the fallback when versions are absent.
`prop.data.imageIds` is an ordered list of existing image entity IDs; the first is main.
Profiles without a photo still contribute their descriptions and explain the missing
visual reference instead of inventing an image.

### Shot fields

Existing `shot.data.cast: [{characterId, lookId}]` is preserved. For an opted-in storyboard
shot, a **present cast key is the exact override**, including `[]`; only a missing key
inherits the scene cast. On first opting an old shot into this workspace, materialize
its effective inherited cast/place before editing, so the user's existing content remains.
Legacy non-storyboard compilation is unchanged.

`shot.data.storyboard` is optional and closed to the following fields:

```json
{
  "version": 1,
  "mode": "fl",
  "quality": "draft",
  "place": {"locationId": "location-entity-id", "versionId": "version-id"},
  "propIds": ["prop-entity-id"],
  "firstFrameId": "image-entity-id",
  "lastFrameId": "image-entity-id",
  "continuePrevious": false,
  "extraRefs": [{"entityId": "media-entity-id", "clipStart": 0}],
  "previousFrame": null
}
```

`version` and lowercase server `mode` (`fl`/`ref`) are required. The UI may display FL/REF.
Other fields are optional: `quality` is `draft` or `final`; empty frame IDs mean no
selected image; explicit `place:null` clears inherited place while a missing place
retains legacy fallback. `previousFrame`, when set, is
`{shotId, selectedAssetId, frameAssetId}` referencing the exact previous shot, its selected
video entity and the saved last-frame image entity. No implicit document migration or
destructive defaults apply. Existing recipe, deployment profile and generation controls
remain in `shot.data.h3`; project style is `project.journey.storyboard.style`.

The exact machine-readable field rules are discoverable in `/v1/guided-schema.storyboard`.
Validate bindings, finite source ranges, duplicates and media kinds through the existing
project validation. Deleting an entity removes its authoring references but never deletes
uploaded files, old candidate artifacts, jobs or unsettled accounting as a side effect.

## 3. Compiler and real capability rules

Prompt assembly shown as “actually sent to the model” is ordered:
**project style → place/version → cast/look → props → shot action**.
Users author the action in the shot; the pure server compiler is authoritative for the
joined prompt and selected input receipts. Missing action and unsupported/broken binding
produce specific safe diagnostics. The frontend's local preview cannot override the
saved server draft/preflight.

Cast, place and props are independent of FL/REF. FL uses their descriptions and optional
first/last images; REF uses main images, selected voices and extra references. Switching
modes preserves inactive inputs and explains why they are unused. Preserve real FL/REF
mutual exclusion, permission, controls, resource ceilings and budget validation.

Read reference counts, aggregate files/durations, dimensions and allowed controls from
the current `/v1/capabilities` and exact selected deployment profile. Prototype sample
counts, qualified historical cases and `joint_cases` are **not allowlists**. Supported
first-only, last-only or combined images do not require additional “unverified” approval.
Never hard-code prototype resource or reference limits.

Within the real current limits, select references in this priority:

- Images: cast main images → place main image → prop main images → extra images.
- Audio: cast selected voices → extra audio → place ambient sound.
- Extra video/audio uses its explicitly selected three-second source range; existing
  asset inspection/derivation remains authoritative. Distinct clips from the same source
  are distinct inputs; exact duplicate receipt/slot/range is omitted with a reason.

Over-limit references stay saved and appear grey with their specific omitted reason;
only sent references enter `draft.inputs`. REF video references explicitly exclude their
source audio. No silent model fallback, precision/step change or invented media is allowed.

## 4. Existing generation routes and immutable versions

1. Sync referenced media through existing asset upload/attachment, then save the project.
2. `GET /v1/projects/{project_id}/shots/{shot_id}/generation-draft` reads the saved shot
   against current capabilities. Existing envelope/IDs remain unchanged.
3. `POST /v1/projects/{project_id}/shots/{shot_id}/generation-plans` uses
   `{expected_version, capabilities_version?}` and existing preflight/admission.
4. Present the normal estimate/blockers and obtain explicit confirmation; submit the
   returned exact plan through existing `/v1/jobs`, preserving its identity/idempotency.
5. Read existing jobs/artifacts and adopt a candidate through the existing guided action.

The draft keeps normal `recipe_id`, `prompt`, `controls`, `inputs` and optional exact
`deployment_profile_id`. It additionally exposes:

```text
storyboard.prompt_parts: [{kind, label, text, entity_id?}]
storyboard.references: [{entity_id, asset_id, slot, why, sent, reason, source_range?}]
storyboard.notes: [nonblocking explanation]
storyboard.limits: current exact limits, or null when not available
storyboard.previous_shot_id: immediate prior shot ID, or null
```

Use the plan's exact `client_ref` and `request_hash`, not a frontend-computed replacement.
Style, selected profiles, media, cast/place, controls and previous chosen version are
included in existing source identity checks; a changed source invalidates stale plans.
Do not turn model output quality judgments into parameter allowlists.

Draft uses an explicitly selected H3 Pruned INT8 deployment. Final uses an explicitly
selected H3 Base deployment and must report unavailable capacity honestly; do not relabel
Pruned as Final. “Redo in Final” reuses the chosen version's frozen prompt and inputs,
with optional seed reuse, and creates a new explicit Base preflight/confirmation. Existing
versions remain inspectable. Generating all shots still preflights/confirms the bounded
set of ordinary per-shot jobs; it does not create a second bulk job ledger.

Queue and machine visibility reuse the authorized P4 workspace contract and current
job/availability projections. Distinguish FL/REF and the exact profile. A forbidden or
unavailable projection is not evidence of an empty queue or available machine, and the
story UI flag does not grant rental permissions. Existing owner isolation and budget
reservations, attempts, leases, unknown outcomes and collection recovery are unchanged.

## 5. Save-frame and previous-shot continuity

`POST /v1/projects/{project_id}/artifacts/{artifact_id}/frames` accepts a required
`Idempotency-Key` and either `{position:"last"}` or `{position:"time",time_seconds:number}`.
Before reserving processing capacity, authorize the owner/project, `assets:write` and
the owned job's `jobs:read` scope; require a successful verified video artifact belonging
to that project. No client URL/path is accepted. Time must be finite and inside the real
video duration. Bounded CPU extraction reads/validates the original private stored video,
selects its exact decoded frame and creates an ordinary image asset through the existing
upload journal/storage quota and conditional-publication path. It never runs a model.

The normal public asset response includes authoritative selection provenance:
`kind=artifact_frame`, `artifact_id`, `job_id`, `position`, actual snapped `time_seconds`,
`frame_index`, `source_sha256`, `source_frame_count` and `source_fps`. The same provenance
is in `metadata.artifact_frame`; existing `asset.attach` stores it in the image entity's
`data.sourceSelection`. Filing suggestions use profiles in that shot, otherwise inbox.
They do not move or overwrite the original video.

Continuation is FL only. Require the immediate prior ordered shot's exact currently
selected video entity and a saved **last** frame from its exact artifact. Admission
re-reads the owned ready image receipt; editable document provenance alone is insufficient.
Changing the prior selected version requires saving/reselecting its last frame. Preflight
does not extract it automatically. Replay resumes the same receipt; a changed extraction
decision conflicts, uncertain publication reconciles original keys, and changed store
identity rejects without mutating receipts, quotas or objects.

## 6. Rollout and scope

The explicit owner flag is `SIXNINE_STORYBOARD_OWNERS`; absent/empty defaults off.
`/api/auth/me.features.storyboard_workspace` enables the view only for a browser session
whose owner is explicitly selected. Machine credentials never receive that UI flag.
The existing protected dstack `migration/paths.json` configuration and approved app
overlay pass this nonsecret value only to the app. They do not grant operator permission
or change any job/budget authority. Protected configuration hashes, independent source
approval and exact release/rollback invariants remain unchanged.

The owner-approved prototype permits implementation and the bounded #161 acceptance,
not arbitrary paid runs or release bypass. Record the exact source/release, each live
criterion and any current spend in the existing issues. Keep rollout and rollback
receipts separate from source merges.

Out of scope: image/keyframe generation, AI look photos, white-box/3D layout, layouts
B/C, multi-GPU/Both-mode, and new final mix/music tracks beyond the existing rough cut.

## 7. Acceptance gates

Track current evidence in [#161](https://github.com/inkseq/h3-studio/issues/161), with
`pass/partial/unverified/fail` and exact revision/environment. The initial live gate is
unverified; no source check or prototype simulation substitutes for these outcomes:

- Live layout A; original seven-step guide and canvas still reachable.
- Character with two looks, ordered photos/main image and selected voice clip; add by
  drag and right-click, change look and inspect exact prompt/selected inputs.
- Location versions/ambient, prop profiles, uploads/inbox filing and per-shot bindings.
- Dynamic REF limits and omission reasons; FL/REF switching retains saved inactive inputs.
- Draft all shots, observe real queue, review chosen timeline, regenerate/adopt one shot.
- Explicit real H3 Base Final with frozen chosen inputs, then previous chosen last-frame
  continuation; unavailable matching capacity remains explicit.
- R2 upload, reference consumption, saved frame, original video and separate-audio
  browser/Agent download; retry collection without another paid generation.
- Owner/project isolation, preflight confirmation, existing budgets and stale-plan checks;
  ordinary entry works only for explicitly enabled owners after gated acceptance.

Implementation scopes: [#157](https://github.com/inkseq/h3-studio/issues/157) compiler/profile
contract; [#158](https://github.com/inkseq/h3-studio/issues/158) frame assets;
[#159](https://github.com/inkseq/h3-studio/issues/159) layout/domain interactions;
[#160](https://github.com/inkseq/h3-studio/issues/160) live generation/queue integration;
[#161](https://github.com/inkseq/h3-studio/issues/161) coordinated release/live acceptance.

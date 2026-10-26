# Redo from marks: read-only preview (#637)

This is the first backend slice of #580 step 5. Claude owns the compare UI and
acceptance. It produces a scoped prompt diff from the original accepted take;
it does not apply a draft, create a plan, reserve credits, call a model or start
generation. No new table, operation receipt or worker.

## API

POST `/v1/projects/{project}/shots/{shot}/takes/{artifact}/redo-preview`:

```json
{
  "expected_project_version": 4,
  "expected_review_revision": 2,
  "edits": [{"mark_id": "camera-change", "start": 12, "end": 18,
             "replacement": "a slow push in"}]
}
```

Use actual IDs and versions. Browser/PAT must have `projects:read` and
`jobs:read` on the same owner/project. Original accepted video/attempt proof,
verified migrated-shot aliases, and content/project trash/purge fences are the
same as take reviews. No current selection or star is needed. Another owner's
artifact is never made accessible by an admin role. Responses are `no-store`.

Send `edits:[]` first to read the frozen recipe and current actual marks. It
returns `needs_edits`, or `no_change_marks` when there is no actual change mark.
An explicit whole-take redo verdict alone is not a range edit. Suggestions that
have not been accepted into actual marks are not used.

Edits reference Unicode **code-point** offsets in the original prompt, not byte
offsets or JavaScript UTF-16 units. Clients can index `[...prompt]` to include an
emoji as one code point. Each nonempty text span must be within the prompt;
multiple edits are sorted and checked for overlaps before any candidate is
returned. Every offset addresses the same original text, even when an earlier
replacement changes its length. Up to 50 edits and 12,000 resulting characters.
Replacement may be empty to remove a span, but the resulting prompt cannot be
blank. No-op replacements and unknown fields refuse.

Every edit must cite an actual `change` mark. Missing IDs, `keep` IDs and
suggested-only IDs refuse. A selected change range overlapping a keep range
refuses, so the user must resolve contradictory marks first. Touching endpoints
are allowed. The API cannot prove that a text rewrite semantically concerns only
that range: this association is explicit caller input and needs human review.
Mark notes are not automatically appended to prompts or sent to a model.

Response:

- `status:preview`, `baseline` and `candidate`: detached creator-safe recipe
  views, plus `diff:[{mark_id,start_s,end_s,start,end,before,after}]` and `marks`.
- `binding`: original project/shot/artifact/job and source-shot IDs, current
  project/review versions and frozen recipe hash. `source_hash` binds that
  snapshot; `candidate_hash` hashes the exact unprojected candidate. These are
  comparison identities, **not** authorization tokens or plan IDs.
- `preserved:{seed,non_prompt_hash}`, `applied:false`, `executable:false`,
  `preflight_status:not_checked`, `pixel_preservation:not_guaranteed`.

Reads use one repeatable database snapshot, without an authoring lock or write.
Project/review version mismatch returns 409. Repeating the same preview at the
same versions is deterministic and has no side effect, so no Idempotency-Key
or durable receipt is required. After an edit, reread and reconcile; never
blindly increment versions to force an old diff through.

## Preservation and limits

Use only the original job's immutable `authoring_recipe`. Seed must match the
original effective request's resolved integer seed. Missing or inconsistent
historical proof returns `redo_historical_recipe_unknown` or
`redo_seed_proof_unknown`; no reconstruction from today's mutable defaults.
Every non-prompt recipe field stays byte-equivalent internally, including seed,
references/ranges/audio flags, controls and profile. Only creator-safe views are
returned, even to an admin; raw job data, authoring context and worker/provider
fields are not exposed. Sensitive creative text stays in the authorized response,
never a new analytics or receipt record.

PW/split-window recipes and effective requests carrying `window_seconds` refuse
with `redo_window_recipe_requires_explicit_resplit`. This prompt-only slice must
not alter a compiled paragraph plan implicitly. Window-aware edits are a separate
follow-up.

Same seed and settings do not guarantee unchanged pixels in kept intervals.
Ranges are review instructions, not a frame mask or compositing operation.
The compare UI must show the full diff and this limitation. Current reference
availability, model/profile compatibility and price are deliberately not claimed
by this historical recipe preview.

After human confirmation, draft application belongs on the existing project CAS
path, followed by fresh preflight and existing generation confirmation. This
slice adds no apply/admission endpoint and must not be used to bypass those gates.
Charging/Checkout, production state and provider capacity remain unchanged.

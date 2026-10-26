# Explicit take reviews (#626)

Backend/API slice of [#580](https://github.com/inkseq/h3-studio/issues/580).
Claude owns the two whole-take buttons and range editor. This document is a code
contract, not evidence of deployment. No generation, spending, selection or
charging is performed by these routes.

## API

`GET` and `PATCH /v1/projects/{project_id}/shots/{shot_id}/takes/{artifact_id}/review`.
Browser access is original-owner scoped. PATs need `projects:read` for GET and
both `projects:read` and `projects:write` for PATCH, on this project. A human
admin does not receive cross-owner access here. PATCH requires `Idempotency-Key`.

```json
{
  "expected_revision": 0,
  "verdict": "keep",
  "marks": [
    {"id": "mark-1", "start_s": 1.0, "end_s": 2.5,
     "tags": ["camera"], "verdict": "change", "note": ""}
  ]
}
```

- Whole-take `verdict`: `keep` (usable), `redo`, or null (retract).
  Never infer it from range marks, selection, stars, export or machine suggestions.
- At least verdict or marks is required. Omitted fields are preserved. Marks
  replace the entire list; `[]` clears it. Max50 marks; unique nonempty IDs of
  up to100 ASCII letters/digits/underscore/hyphen; note≤500 characters.
- Each range uses finite source seconds, `0 <= start_s < end_s <= duration_s`
  from the accepted artifact's immutable metadata. Ranges may overlap; no frame
  snapping because these are annotations, not clip edits. Missing duration
  blocks nonempty marks, but still allows a whole-take verdict.
- Mark verdict is `keep` or `change`; tags are a nonduplicated subset of
  performance, camera, consistency, subtitles, quality, other. Empty tags/note
  are allowed; all mark fields are required. Unknown fields are rejected.
- GET returns `{revision, verdict, marks, actor_kind, updated_at}`. Before the
  first write: revision0, verdictnull, marks[], actor_kind/updated_atnull.
- PATCH returns only `{revision, verdict, mark_count, actor_kind, updated_at}`.
  Read GET for full marks. Timestamp is server Unix seconds. `actor_kind` is
  human/agent derived from authentication; the client cannot supply it.

The revision belongs to the review, not the project. A successful write does not
increment the project version or edit its selected take. CAS409 commits nothing.
An identical key/body returns its original small receipt even after later writes;
same key with a changed body returns409. Keys are scoped by owner/project/actor/
artifact. Reload on CAS failure and confirm intended changes; do not overwrite
another edit or retry with a random key. Trash/purge fences are checked before
replay, so an old receipt never resurrects annotations.

## Batch summary read (#630)

`GET /v1/projects/{project_id}/shots/{shot_id}/take-reviews` returns a JSON list:
`[{artifact_id,revision,verdict,mark_count,actor_kind,updated_at}]`. Same
`projects:read` scope and stable read-only snapshot as single-review GET; no
authoring locks or writes. Cache-Control is no-store. No notes, ranges, prompt
text or media URL is returned.

The census follows the original shot/verified migration aliases, with the stricter
take-review proof: same owner/project and client_ref, succeeded current attempt,
accepted video artifact. Newest job first, artifact ID breaks ties. Unreviewed
eligible takes have revision0, verdictnull, mark_count0 and null actor/time.
Trashed/purging/deleted, unaccepted or unproven takes are omitted; a trashed
project is refused. No eligible takes returns `[]`.

Read at most100 candidate videos plus an overflow sentinel. Overflow refuses
with `409 take_review_summary_limit`, never returns a partial success. An
exceptionally large shot can use the existing paginated versions listing and
single-take reads; this does not broaden eligibility. Current review rows are
read together in one bounded query. Source availability uses existing content
fence checks inside the same snapshot. Writes and immutable receipts are unchanged.

## Identity, storage and privacy

An artifact must be an accepted video of the shot's original succeeded job and
its current succeeded attempt, in the same tenant/owner/project. Verified
migration aliases reuse the existing server receipt validator; editable aliases
alone have no authority. An imported video without this lineage is not eligible.
Reviews follow the artifact when current selection changes.

Reuse `platform_documents`: current `take_review` keyed by artifact; immutable
`take_review_receipt` keyed by a hash of actor/artifact/idempotency key. CAS and
first creation share the original owner reference gate and project row lock
with authoring and purge. There is no schema migration or new service.
GET uses a stable read-only snapshot and plain selects, without the owner gate,
project row lock or SQLite writer reservation. Existing project/history and
artifact trash/purge checks still apply. Only PATCH takes authoring locks.

Only the current review contains notes and ranges. Immutable receipts contain
scoped source IDs, request hash and small response, plus whether the whole-take
verdict was explicitly written. They contain no note, prompt, media URL or
range text. Notes are user content, not analytics. Proven permanent artifact or
project purge redacts current marks in the same existing purge transaction;
redacted receipts are retained. Review rows are non-retaining only for the exact
owned purge targets; other and foreign content references retain the usual guard.
Account deletion already redacts the owner's scoped documents.

V1 retains the small immutable receipts; there is no automatic compaction or
TTL. A later retention change must preserve original idempotency replies and
explicit verdict history (including retractions and actor identity) needed by
the metrics. It requires a separate bounded retention proposal and tests before
deleting receipts. The bounded projection reports incomplete coverage on overflow
instead of silently dropping history or treating it as zero.

## Telemetry

The #290 projection reads only redacted explicit verdict receipts. Latest whole
verdict revision at `as_of` wins, including null retraction. Range-only writes
do not change the decision or its actor. Human first-candidate verdicts drive
`first_usable_rate`; agent first-candidate counts are separate coverage, never
inferred human approval. A newer agent whole verdict is an agent decision until
a human explicitly evaluates again. First-take-shipped remains independent.

Coverage includes reviewed first candidates, agent-reviewed first candidates
and shots with any current human `keep`. Unreviewed first candidates do not enter
the explicit-rate denominator. Full cost and complete command metrics remain
null until the other sources are implemented. A bounded8000-receipt overflow or
unverified lineage makes the affected metric incomplete. No all-users aggregate.

Tier2 review is required for scope and purge integration. Disposable SQLite and
PostgreSQL tests cover CAS/replay/concurrency, range limits, accepted-attempt
proof, scopes, trash fences, purge redaction and verdict history. No production
data was edited as part of this implementation.

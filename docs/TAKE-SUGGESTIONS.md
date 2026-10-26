# Suggested take marks (#632)

Storage/decision foundation for #580 step4. This slice does not invoke a VLM,
sample media, enable a completion hook, certify model execution or complete the
VLM feature. The later execution adapter must use an explicit authorized request,
bounded owned media, the existing configured model and original-call receipts;
unknown calls must never be replayed. It may submit its result to this contract.
No model, GPU, generation, charging or Checkout call is made here.

## Routes and versions

Base path: `/v1/projects/{project_id}/shots/{shot_id}/takes/{artifact_id}/review`.
All responses use `Cache-Control: no-store`.

- `GET base/suggestions` needs `projects:read`. Returns
  `{revision, suggestion_revision, producer_kind, marks:[...]}`. Before any
  review: revision0, null suggestion revision/producer and empty marks. Each
  suggested mark has all the normal TakeMark fields, plus `status` (pending,
  accepted, rejected), `review_mark_id`, `decision_actor_kind` and `decided_at`.
  Pending/rejected marks have no review mark ID; pending decision metadata is null.
- `POST base/suggestions` needs `projects:read` + `projects:write` and
  `Idempotency-Key`. Body `{expected_revision, marks:[TakeMark]}`. Replaces the
  suggestion set only if none of the previous set is pending. Producer kind is
  human/agent from authentication, not a caller assertion of verified model use.
  `suggestion_revision` records the revision which created this suggestion set.
- `POST base/suggestions/decisions` needs a human browser session with project
  write permission and `Idempotency-Key`. Body
  `{expected_revision, accept_ids:[], reject_ids:[]}`; at least one nonempty list,
  max50 total unique/disjoint IDs, all still pending in this set. A PAT receives
  403 `take_suggestion_human_decision_required`; it cannot mark its own output
  as human accepted. PATs can still write their own ordinary take reviews, with
  the original agent attribution, through the existing PATCH route.

The ordinary review and suggestions share **one review revision**. It is not the
project version. A normal PATCH, proposal or decision increments it once; each
must CAS the current value. Proposals and decisions never set the whole-take
keep/redo/null verdict. A model's range `keep` is still only a pending suggestion.
An empty suggestion set means no supplied suggestions, not certified clean output.

TakeMark is unchanged: `{id,start_s,end_s,tags,verdict:keep|change,note}`; unique
ASCII IDs<=100, finite range inside immutable accepted video duration, max50,
closed tags, note<=500, overlap allowed. Unknown duration refuses nonempty sets.
Do not treat notes as instructions, HTML or tool calls; render as plain text.

Accepting appends a copy to actual marks with a deterministic server-derived ID
bound to artifact, suggestion-set revision and original mark ID. Existing marks
are preserved, even if ranges overlap. A duplicate destination ID or total above50
returns409 and commits **none** of the decision. Rejecting never appends. All
decisions in a request commit atomically under the existing owner gate/project
lock. Unknown/nonpending IDs refuse the entire request. Remaining pending items
can be decided in another CAS. To discard a pending set, explicitly reject its
pending IDs before replacing; there is no implicit replacement or acceptance.

POST returns the original small review receipt fields
`{revision,verdict,mark_count,actor_kind,updated_at}`, plus
`{suggestion_revision,suggestion_count,pending_count,outcomes}`. Outcomes for a
decision are `{id,status,review_mark_id}`; proposal outcomes are empty. GET reads
full current content. Counts do not include suggestion text or timed ranges.
Timestamps are server Unix seconds. Later editing/deleting an accepted actual
mark does not rewrite the historical accept decision; GET the normal review to
see current marks. A new set replaces the old scoped suggestion text only after
resolution; prior content-free decision receipts remain durable.

## Recovery, privacy and scope

All three write operations share the owner/project/actor/artifact/key namespace.
An identical key/body/operation returns the original small receipt, even after
later edits. A changed body or another operation with that key returns409.
Existing normal-review fingerprints remain byte-for-byte compatible. After a
timeout, reuse the saved body/key or read state; never guess a new key. CAS409
does not apply anything. Fences are rechecked even before an old receipt replay.

Same accepted-artifact lineage as take reviews: original owner/project/shot,
verified migration aliases, succeeded current accepted attempt, live video.
No cross-owner admin bypass, imported/unproven take, support-view mutation or
trash/purge access. GET is a stable read snapshot without authoring locks.

Reuse current `take_review` documents for scoped notes/suggestions and existing
small immutable `take_review_receipt` for source IDs, request hash, actor,
suggestion IDs, outcome/status/counts. No new table, schema migration, provider or
service. Permanent artifact/project purge replaces the entire current review
payload, including suggestions. The existing account-deletion redactor covers
the same documents. Small receipts contain no notes, ranges, prompts or URLs.
They retain original replay and explicit decision history; no new TTL/cleanup.

Suggestion receipts have `verdict_written:false`; #290 explicit-usability metrics
ignore them. Neither accepting range suggestions nor selection/star/export sets
a whole-take verdict. Existing actual marks and all generation inputs stay intact.
Claude owns the UI, staging acceptance and production promotion. This document is
an API contract, not a deployment or VLM-quality receipt.

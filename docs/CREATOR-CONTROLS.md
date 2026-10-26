# TaoMate creator controls (#649)

New creator requests targeting
`h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7` retain `duration`, `seed`,
`resolution`, `aspect_ratio` and `generate_audio` inside `controls`. Aspect ratio
and audio output are creator choices, not internal model tuning; they retain
native enum/bool validation. Resolution is the existing Standard (480P)
or HD (768P) wire choice. Quality tier, FL/REF mode, copies and reference inputs
remain in their existing fields. Hidden controls such as steps, sampler, sigma
shifts and VAE settings use the immutable profile's defaults. In particular,
stored `steps=50` must compile with five steps, without changing the source record.

The projection happens after account/story/shot defaults are combined and before
draft validation, and again at new-plan compilation. Confirmed script windows
are still supplied by the server after this projection. No accepted plan, job,
attempt, revision or artifact is rewritten. New stored-shot plans record
`creator_control_policy=taomate-creator-v1`; film freshness uses this policy only
for those receipts. Historical receipts retain their original comparison.

Public quality selections are creator requests even for an administrator.
Explicit human-admin internal API selections retain the native compiler's
profile-specific permissions and validation: a permitted sampler override can
work, while `steps=50` remains invalid for TaoMate. Other profiles are unchanged.

## Read-only compatibility census, 2026-10-26

`tools/audit_creator_controls.py` reads the existing app database in a read-only,
repeatable-read transaction with a 30-second statement timeout. Each hidden
field is checked independently against the existing TaoMate compiler using a
synthetic FL request; no generation, provider call or database write occurs.
Reports contain aggregates only, not owner IDs, prompts, media or credentials.
This is a counterfactual control-compatibility inventory, including archived and
trashed documents; it is not a count of currently failing jobs or media readiness.

| Environment / layer | Records with controls | Hidden controls | Definite refusal |
| --- | ---: | ---: | ---: |
| staging / account | 0 | 0 | 0 |
| staging / story | 3 | 3 | 0 |
| staging / shot | 192 | 135 | 105 |
| production / account | 0 | 0 | 0 |
| production / story | 0 | 0 | 0 |
| production / shot | 298 | 295 | 295 |

Staging: 105 step refusals across five owners. Production: 294 step refusals
and two encoder-device refusals across two owners; one shot overlaps, so the
unique total is 295. No context-dependent refusals were reported.

Original SSM command receipts (both Success, target identity checked first):

- staging: `ee30f266-861a-4b97-acd0-a0d143b62b9b`
- production: `22db3180-a004-4682-ae3d-652795fbfd6b`

Follow-up read-only aspect census for review6415940764 (both Success):

| Environment | Shot non-default aspect | Story non-default aspect | Account |
| --- | --- | --- | ---: |
| staging | 3 (`1:1`) | 3 (`9:16`) | 0 |
| production | 0 | 0 | 0 |

These are independently stored layer counts, not a sum of effective portrait
shots (a story default may affect multiple shots). No invalid aspects were
found; absent/empty/default16:9 is excluded. Only known enum values are emitted.
Staging hidden-control records are now105 shots/0 stories after correctly
classifying aspect/audio as creator controls; definite refusal totals remain
105 staging/295 production. The table above retains the original census's
classification and receipt rather than rewriting historical evidence.
Follow-up SSM receipts: staging `523a4737-17f1-4b37-8250-869a954e4845`, production
`dde1c5f9-6c29-4310-92ca-287fae2f483d`. No data was changed.

## Optional cleanup proposal, not executed

Ship the new-request projection first: generation no longer requires a bulk
storage migration. If cleanup is later approved, snapshot each eligible editable
creator document with its original version/hash and scope; remove only hidden
control keys through the normal scoped CAS write path. Keep quality, mode,
duration, seed, copies, aspect ratio, audio output and input references. Skip uncertain admin/debug intent
and uncertain archived records. Never touch accepted plans/jobs/attempts,
historical revisions, artifacts, billing or user assets. Production requires
the usual independent review and zero-running-jobs gate. Rollback restores a
snapshot only if its post-cleanup version/hash still matches, never overwriting
concurrent edits. No cleanup authority is inferred from this census.

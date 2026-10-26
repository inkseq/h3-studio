# Story Chat generation risk advice (#628)

This is the backend contract for #580 step 3. Claude owns the UI and acceptance.
It is an additive advisory part of the existing Story Chat turn, not a new model
service, generation gate, automatic editor or deployment receipt.

## Flow and response

Before proposing video generation, the existing conversation model checks the
proposed story cards for simultaneous speakers, excessive speech or actions for
their duration, and too many scene/time changes. It returns optional bounded
`risk_review` entries alongside its original reply/actions. No extra model call
is made. A pure rule pass runs against the proposed cards after validating the
normal actions. Advice itself emits no actions, jobs, retries or spending.

An existing video proposal gains this object in both `turn.proposal` and its
assistant message's `proposal` (including history reads):

```json
{
  "risk_review": {
    "version": 1,
    "source_hash": "sha256-of-proposed-card-sources",
    "status": "warnings",
    "rule_status": "checked",
    "model_status": "reported",
    "warnings": [{
      "shot_id": "existing-shot-id",
      "code": "speech_too_dense",
      "source": "rule",
      "message": "Speech may not fit the selected duration.",
      "suggestion": "Split the lines across shots/windows without repeating a line; review timing before generation."
    }]
  }
}
```

Closed codes: `simultaneous_speakers`, `speech_too_dense`, `action_density`,
`plot_density`. Each warning is bound to a proposed shot ID; new refs resolve
through the original plan resolver. Rules and model findings remain identifiable.
Rules use interface-language fixed messages; model suggestions are untrusted
display text, never instructions to execute.

## Bounds, coverage and freshness

- Model report: at most60 entries; each has exactly `shot`, `codes` (1–4 unique
  closed codes) and `suggestion` (1–500 trimmed characters). Unknown/foreign refs
  or malformed entries invalidate the model assessment as a whole; rule findings
  survive. Dedupe by shot/code/source bounds the result to8 findings per shot.
- `status`: warnings, no_findings, incomplete, stale. No findings means no detected
  issues in a checked rule pass and an explicitly returned model assessment. It
  is not an assurance of output quality. Missing assessment stays `not_checked`;
  malformed assessment is `invalid`. Partial rule coverage is explicit.
- Rules recognize overlapping speaker cues outside canonical `<d>…</d>` lines,
  speech density (Han characters/6 plus Latin words/3.5 seconds), successive
  action cues and scene/time transition cues. These are advisory heuristics,
  with known false positives/negatives and limited language coverage. They are
  not measured speech speed or hard model limits.
- The hash covers ordered proposed shot IDs, card text, story-card seconds,
  cast/place/prop bindings. It does not certify effective compiler controls,
  media or current capacity: normal saved-draft preflight remains required.
  After source changes, responses mark the old advice stale without rewriting
  stored model receipts. Older turns and image-only proposals have no report.
- A concurrent project version change discards model findings as stale and
  recomputes only deterministic rules on the current validated proposal. Normal
  conflicting-card CAS checks still apply. The same turn key reuses its original
  response, never repeats the model call. A new review needs an explicit new turn.

## Unchanged authorities

Scopes, original turn idempotency/CAS, editing permissions, confirmation and
provider accounting are unchanged. No second storage family, credentials,
queue, analytics service or migration. Suggestions do not rewrite dialogue,
select takes, change seed/references/duration, increase the fixed5steps, charge,
generate or auto-redo. Second-speaker lip sync remains the accepted model limit;
burned-in subtitles remain detection/reporting only. A suggested re-split needs
an explicit authoring request and the existing confirmation flow.

Discovery exposes `story_risk_review` in guided schema and agent guide. Tests
use the existing scripted client and disposable local data only: additive HTTP
response, replay, concurrent edits, new references, invalid model output,
source staleness, old/image-only history, no additional call or generated job.
Staging UI acceptance is separate from these code tests. Rollback removes the
advisory response without deleting existing customer content or receipts.

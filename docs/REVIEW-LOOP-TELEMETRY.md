# Review-loop telemetry proposal (#290)

Status: proposed contract, not an implemented endpoint or production receipt.
Source audit: `f7638d710c2b760358d84e9fe823377b6efb1fa4`, 2026-10-26.
Plan: [#580 review loop](https://github.com/inkseq/h3-studio/issues/580#issuecomment-6408772792).
Codex owns backend; Claude owns frontend, acceptance and promotion.

## Existing evidence and its limits

| Source | Reusable evidence | Cannot establish |
| --- | --- | --- |
| `repository.py`: jobs, attempts, artifacts | Tenant/owner/project, original job identity, attempt number/fence, artifact identity, timestamps and nullable actual attempt cost | A human verdict; fully allocated supplier/LLM/export cost |
| `storyboard_versions.py`: `versions`, `migration_source_shots` | Scope-checked shot lineage, historical generated artifacts, current selected/starred state, migrated aliases | Selected/starred means usable; historical selection time; first visible row means first take (listing is newest-first) |
| `project_activity.py`: `append_activity` | Successful project version, browser/API-key actor, deduplicated operation names and bounded target IDs | Click count, individual command-to-shot attribution for bundled edits, discarded history |
| `guided.py`: `platform_edit_receipts` | Scoped idempotency key/fingerprint/response for an accepted edit | Every human interaction; generation or export actions outside this route |
| `film_runs.py`: `film_run` documents | Frozen project/version, per-shot results, generation jobs, export artifact and run outcome | A current selection is the take that an earlier export used; preflight is a completed export |
| `generation_stats.py` | Existing human-admin read-only route and successful-attempt latency/cost groups | Full cost: successful attempts only, bounded recent cohort, missing costs omitted from cost summaries |
| `credit_margin.py`: `exposure` | Reservation-layer deduplication and explicit actual/reserved/pending/unknown distinctions | Reserved amount is actual spend; nominal credits are revenue or provider cost; charging-off jobs belong to its credit-enforced cohort |
| `subtitle_quality.py` | Advisory machine quality flags | A user's acceptance/rejection or whole-take usability |

This is a source audit, not a query of customer records. Existing #290 exporter
installation receipts remain separate; no telemetry event or installation was replayed.

## Metric version 1

Use a fixed UTC cohort `[from, to)` of logical shots whose first original generation
job was accepted in that interval. Return `as_of` separately; subsequent attempts,
reviews and costs are observed up to that time. Scope every join by tenant, owner
and project. Resolve migrated identities only with verified source mappings; list
unresolvable lineage as incomplete, never count both an alias and its original.
Imports with no generation history are a separate coverage category.

1. **First-usable rate** means first *delivered video take* accepted as a whole.
   Numerator: shots whose first delivered candidate has an explicit whole-take
   `keep` verdict. Denominator: shots with an explicit whole-take verdict on that
   first candidate. Use the latest accepted verdict revision at `as_of`, including
   retractions. Return first-candidate review coverage against all delivered shots.
   Failed attempts before delivery are reported separately and still cost money.
   A range marked `keep`, a star, current selection, export or automated suggestion
   is not an implicit whole-take verdict. No reviews means `null`, not zero.
2. **Full cost per usable shot**: all attributable costs for the entire shot cohort
   through `as_of`, including failed/reclaimed attempts and unsuccessful shots,
   divided by shots with at least one explicitly accepted whole take. Return the
   confirmed subtotal separately. Full cost is `null` until attempt, supplier
   startup/idle, generation-assistance/quality-check and export allocations are
   all known or explicitly not applicable. Never duplicate shared reservation
   layers or manufacture per-shot allocation of shared machine/export spend.
   Unknown is not zero. Credits, price quotes and reservations are not actual cost.
3. **User actions per usable shot**: count committed human commands across that same
   cohort, divided by explicitly usable shots. A command is one accepted generate,
   review, marks update, redo, pick or export submission; polling, page views,
   retries of the same idempotency key and automatic agent/tool steps do not count.
   Deduplicate a multi-shot command once in an account aggregate; do not sum
   overlapping per-shot counts. Report agent commands separately. This is not a
   click metric; PostHog owns UI clicks. Missing actor/command lineage makes the
   complete metric `null`, with the known command count reported separately.

Each metric returns numerator, denominator, `value`, completeness and reason codes.
Zero denominator returns `null` with `no_eligible_shots`. A known zero numerator
with a nonzero denominator returns zero. Do not extrapolate from a truncated scan.
Trash/purge must not silently improve historical rates: use retained, scoped
receipts where lawful, otherwise expose a history gap; never copy deleted content
to analytics. Completed export attribution comes from the frozen run, not today's
project document. Repeated export downloads are not new exports.

## Proposed API (not live)

`GET /v1/operator/admin/review-loop?owner_id=OWNER&from=UTC&to=UTC`

- Existing human-admin authorization; PATs denied. Tenant comes from the trusted
  principal/settings, never a caller-supplied tenant. Explicit owner filter is
  required for v1; no accidental all-customer join. Maximum interval 90 days.
- Return `schema_version: 1`, `as_of`, cohort counts, the three named metrics and
  coverage (`reviewed_first_candidates`, `cost_components`, `command_lineage`,
  `unresolved_lineage`, `history_gaps`). `Cache-Control: no-store`.
- Metric shape: `{value: null, numerator: null, denominator: 0,
  status: "insufficient_data", reasons: ["review_verdicts_missing"]}`.
  Statuses: `complete`, `partial`, `insufficient_data`; a partial full-cost or
  action metric keeps `value: null` and exposes a distinctly named known subtotal.
- Aggregate response only: no prompts, OCR, notes, titles, URLs, email, actor
  labels, raw requests or media. IDs stay in the scoped product store; no automatic
  forwarding to PostHog, Sentry or public GitHub.
- Bound query work. If row limits or absent indexes prevent complete calculation,
  return explicit incompleteness rather than a deceptively precise rate. Set the
  row budget from fixture/query-plan validation before implementing the endpoint.

## Small implementation choice

**Fix-only:** derive selection counts and successful-attempt cost from current
records. Cheap but these do not answer the requested metrics; reject as substitutes.

**Small (recommended):** reuse scoped jobs/attempts/artifacts, film-run documents
and existing transactional document/receipt storage. First ship a coverage-aware
read-only projection; historical unavailable metrics remain null. In the subsequent
take-marks slice, commit an immutable, redacted review/command receipt alongside
the CAS write, identified by original command plus review revision. Add an explicit
whole-take verdict distinct from range marks. Capture actor kind, scoped IDs,
command kind and timestamp, not mark notes. Link redo to its original shot and
source artifact; preserve original job IDs across automatic attempt recovery.
Do not repurpose the job-delivery outbox as an analytics queue. Validate existing
store indexes/retention before adding bounded receipt document kinds; if reuse
cannot support them, return a concrete storage proposal instead of silently adding
a table family. This audit adds no schema, endpoint or background collector.

**Larger:** new event warehouse, queue or vendor. Unnecessary now; outside this claim
and requires separate authority. Sentry feedback from plan step 6 is a different
slice, not the authoritative source of take verdicts or financial accounting.

## Acceptance cases for implementation

- Owner/tenant/project collisions, foreign artifact IDs, PAT denial and no content
  leakage in normal/error responses.
- First candidate rejected then second accepted; no verdict; range-only keep;
  suggestion not accepted; whole-take verdict revised/retracted; failed first
  attempt then first delivered candidate accepted.
- Reclaimed/late-fenced attempts, duplicated reservation layers, incomplete shared
  cost, charging OFF, zero actual cost versus unknown actual cost.
- Duplicate idempotent commands, CAS conflict, browser versus agent, multi-shot
  command, automatic retry, failed command, successful export versus preflight.
- Frozen export after selection changes, migrated aliases, imports, deleted
  history, time boundaries, zero denominator and query-limit incompleteness.

Review requested: metric/cohort semantics, whole-take verdict distinction, and the
existing-store Small approach. Claude can prepare labels for unknown/partial
metrics now; do not present this proposed route as deployed.

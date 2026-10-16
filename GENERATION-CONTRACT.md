# Generation lifecycle and compatibility contract

## Dispatch migration extension (2026-10-11)

The operator-owned execution policy can specify `dispatch_backend=hatchet-v1`
for WanGP. Admission freezes it into the existing immutable execution plan;
missing historical values mean `legacy`. Neither user input nor a rollout flag
can change an accepted job's route. Exact-job claims and all recovery purposes
filter that route. The existing business ledger, physical slot locks, fencing,
submission intent and output receipts remain authority under at-least-once
PostgreSQL lease-queue dispatch (historical route identity `hatchet-v1`). Lost dispatch responses may be delivered again; uncertain GPU
submission is reconciled on the original attempt, never regenerated blindly.
Worker/timeouts/cancellation do not establish an idle GPU. See
[DSTACK-MIGRATION.md](DSTACK-MIGRATION.md) and [#132](https://github.com/inkseq/h3-studio/issues/132).
Cloud telemetry is best effort and cannot alter task or accounting outcomes.

Explicit new dstack `lifecycle.version=2` capacity is projected from live owned
dstack runs and provider observations, never legacy Lium/Targon or historical
intent counts. A terminated run absent from its provider does not occupy the
new-path machine/GPU/hourly capacity limit; its accounting history remains.
New v2 requests have null fixed deadlines and `max_duration: off`, with
configurable business idle stop and keep-on through a finite hold or until
manually stopped. Matching accepted queued work, active execution, unknown
outcomes and collection obligations pause idle stop.
Original owner/global spend caps and hourly `max_price` still apply: rolling
funding increases the same original reservations, never resets spent amounts
or settles an invoice. Availability, claim and late submission check identical
exact runtime/lifecycle proof and paid-start-based funded runway, including
the accepted runtime estimate, 120-second collection margin and 300-second
shutdown margin. Funding failure closes new admission and requests normal
drain; recovery and collection retain the original attempt. Historical finite
deadline contracts and guardian evidence are unchanged.

## GPU artifact delivery extension (#174)

New immutable FL deployment profiles explicitly bind `output_delivery=native-frames-r2-v1`.
Their protected execution-policy enabled bits are the rollout gate; qualification
precedes general activation. Admission freezes the policy and native-frame
delivery specification into each job. Historical `native-frames-v1` and absent
policies keep CPU collection; later flags/defaults cannot reroute accepted work.
Keep the CPU route for at least 14 stable days and outstanding old obligations.

The pinned GPU host encodes the same H.264 MP4 and independent 32 kHz stereo FLAC,
preserves every native frame, fully decodes and seals size/SHA-256/MD5 and media
evidence. An extension in the original operation journal binds the original
job/attempt/manifest/slot/incarnation, owner and fixed delivery parameters, without
changing PreparedRequest or generation-receipt identity. No new queue or ledger.

Existing ArtifactWriter reserves final-object quota before GPU processing, then
commits descriptors and immutable owner/attempt keys before issuing short-lived
conditional R2 PUT authorization. URLs/headers are ephemeral: never journaled,
logged, or passed through process environment/arguments. No storage credentials
reach the GPU. CPU verification uses HEAD size/type and bounded streaming SHA-256;
no local output staging, full decode or re-encode. Only both verified role receipts
permit existing atomic publication, quota and job settlement. An uncertain PUT
verifies the same key; it cannot overwrite a wrong object, change stores,
regenerate or release an unknown quota.

[#174](https://github.com/inkseq/h3-studio/issues/174) records exact source, review
and live media/security/recovery/memory evidence. This source contract alone does
not enable a protected profile or prove GPU qualification.

## Current authoring and admission refinements

As clarified in [#126](https://github.com/inkseq/h3-studio/issues/126), historic measurements are not a parameter whitelist. Input compatibility follows the explicitly selected deployment profile and immutable compiler epoch. Retained profiles preserve their existing FL first/last and REF reference-only mappings. Additive `full-controls-v2` profiles map first/last anchors in both modes, timed image injections, REF images/video/audio, video soundtrack selection, explicit control-video/soundtrack conditioning and source-clip excerpts. They never silently drop inputs or reinterpret an accepted legacy request. Capability discovery describes these exact controls, native role counts and adapter gaps; a recipe name alone is not an input-compatibility rule.

Preflight checks ownership, parameter/media validity, actual mode incompatibilities, configured resource ceilings and budget. Native reference rows exclude timeline anchors and injected images; effective video/audio excerpts still consume their corresponding reference slots. Clip selections and native image-frame times are normalized before the plan is frozen, and aggregate bounds prevent upstream trimming. Supported unmeasured combinations require no qualification run or extra confirmation. Runtime failures retain the original job/attempt, safe control/input summary and stage/reason; a deliberate retry requires fresh preflight and authorization, never an automatic paid retry. Diagnostics do not expose credentials, prompts or private media paths. Source support does not upgrade a running worker bundle or widen a protected policy implicitly.

As clarified in [#122](https://github.com/inkseq/h3-studio/issues/122), durable generation acceptance clears the submitted round's prompt and material selection, not persistent model/resolution preferences, stored assets, cards or accepted jobs. New card revisions retain an immutable composer snapshot. The shared browser/Agent confirmation records a reset receipt and clears only unchanged matching binding versions; later edits, including remove/re-add ABA changes, survive. The browser independently fences prompt clearing with the submitted origin/edit token. Rejection preserves the draft; an unknown response reconciles the original submission/key before clearing or retrying. Following rounds reuse assets explicitly. For legacy cards, a newly prepared preflight or explicit new card revision freezes only current bindings that exactly match the submitted inputs and their role/range/audio metadata. Already-existing originless preflights/submissions remain unchanged and preserve material selections rather than guessing or retroactively associating newer edits.

For queued jobs, authorized `GET /v1/jobs/{id}` adds a read-only `wait_diagnostic` with observation time, a static reason code and aggregate exact-capacity counts. It does not alter the task/error history, budgets or queue, query a provider, prove scheduler liveness or submit another attempt. Managed provider proof, execution permission, exact profile and job-duration window apply consistently to availability, preflight, claim and the late submit guard; original collection/reconciliation is exempt from new-inference admission closure. The canceled original [#120](https://github.com/inkseq/h3-studio/issues/120) and successful replacement remain distinct evidence.

Additive manual-capacity/profile selection contract (2026-10-09): [OPERATOR-CAPACITY-CONTRACT.md](OPERATOR-CAPACITY-CONTRACT.md). Optional explicit deployment identity preserves the legacy route when omitted and never changes accepted requests.

Version: **generation-contract-v1**, frozen 2026-10-06 for A2 ([#11](https://github.com/inkseq/h3-studio/issues/11)).
Baseline: [CURRENT-BASELINE.md](CURRENT-BASELINE.md). Direction: [PROJECT-PLAN.md](PROJECT-PLAN.md). Decision rationale: [DECISIONS.md](DECISIONS.md).
Source-status wording reviewed against PR #25 / merge `d8c811db79683507a959d3260cdf99b072d0022e` on 2026-10-08; the v1 API and recovery semantics are unchanged. This editorial review is not a production observation.
This is the binding contract for incremental B/C/D work. “Required” below is an acceptance condition, not a claim that every future engine is implemented. Existing narrower queue/worker/fleet/storage contracts remain applicable. Material semantic changes require updating this version and its issue before implementation.

## 1. One business API and ledger

Web and Agent clients use the same authenticated owner, projects, assets, plans, jobs and artifacts. Scenario services own creative documents; the generation service owns admission; the durable queue owns execution; adapters own engine protocol; the capacity controller owns provisioning. A runtime queue or MCP server is never a second business authority.

Keep current IDs and field types. Recipe IDs, model IDs, configuration IDs, engine IDs and credential profile IDs are different identities. Existing recipes must not silently acquire a different model, precision, compiler or control meaning.

## 2. Public route compatibility map

| Existing interface | Contract to preserve | Target service |
|---|---|---|
| `/api/auth/*`, account API-key routes | Browser session and scoped Bearer credentials identify the caller; request-supplied owner is not authoritative. | Identity/authentication |
| `/v1/projects` and guided entity/action routes | Optimistic document versions, owner/project permissions, existing story and shot IDs. | Scenario services |
| `POST /v1/assets`, `GET /v1/assets`, asset content/derivative routes | `client_project_id` in the multipart upload/query identifies a project subject to authorization; server inspection, stable media ID, explicit selected clips and owner-scoped access remain. | Asset service |
| `GET /v1/capabilities` | Existing capability version, recipes, typed controls and `execution_support`; upload validity is not execution qualification. | Generation capabilities |
| `POST /v1/generation-plans` and per-shot plan/draft routes | Validated source snapshot, normalized input/control request, native output specification, policy snapshot, quote and expiry. Existing source/project/shot binding remains. | Generation admission |
| `POST /v1/jobs` | Exactly `{ "plan_id": "..." }` plus `Idempotency-Key`; HTTP 202 does not mean GPU started or result completed. | Generation admission |
| `GET /v1/jobs`, `GET /v1/jobs/{job_id}`, activity summary | Same job identity, scoped visibility and truthful persisted state/error code. | Job queries |
| `POST /v1/jobs/{job_id}/cancel` | Cancellation request is distinct from proven stop; repeat calls preserve identity. | Job control |
| `GET /v1/jobs/{job_id}/artifacts`, artifact content/download | Stable artifact IDs, kind/MIME/size/hash/metadata and authenticated content routes; Range support remains. | Artifact delivery |
| Existing batch routes | Batch/item identities and common job admission; no competing engine batch queue. | Generation batches |

Quick Chat `/v1/quick-chat/sessions/...` routes reuse the authoring source preserved at `efc79c27e2bc0022149e8addabe1699b0d574cfc` (draft [PR #23](https://github.com/apedintensor/h3-studio/pull/23)). Session/turn/card/revision/submission/item/execution identities sit above the existing job ledger. `GenerationAdmission` handles the same current-engine preflight, explicit confirmation and enqueue as story/direct clients. Main's engine/manifest/delivery bindings remain authoritative; hidden project/shot projections are not separately editable sources. [G1 #20](https://github.com/inkseq/h3-studio/issues/20) retains the remaining UX/public release gates; [B1 #12](https://github.com/inkseq/h3-studio/issues/12) records source extraction and compatibility acceptance.

`generation_services.py` provides the explicit HTTP-independent collaborators: `GenerationAccess` checks the authenticated Principal's tenant/owner/project/PAT permissions; `GenerationPlanning` compiles an already authorized source snapshot and retains source/policy checks; `GenerationRead` authorizes job/list reads and projects the existing public fields. `GenerationAdmission` receives access and planning objects, rather than access/plan/source closures defined by `create_app`. Direct plans use `GenerationAdmission.plan`, story/Quick Chat drafts use `preflight`, and Quick Chat result reads use `GenerationRead.job`. Low-level projection and compilation helpers accept authorized records, not raw untrusted callers. Authentication, HTTP parsing/errors and media streaming remain at the route boundary. This extraction changes no v1 route, DTO, actor/business idempotency namespace, ledger authority or runtime policy; it does not enable a frontend or model.

Existing PATs can author via this API without a website assistant call: use `assistant_mode=none, create_card=true` or save a card directly, then preflight and explicitly confirm. The authenticated `/v1/quick-chat/schema` and additive `quick_chat` entry in `/for-agents/guide.json` describe those steps. New-session creation requires owner-wide project create/read/write authority; scoped media and job operations retain their own permission checks. No G2 credential-exchange route is introduced by this slice. The website assistant remains disabled by default; named model options and preserved adapter source do not establish hosted-model availability or enabled generation. Frontend publication and [G2 #21](https://github.com/inkseq/h3-studio/issues/21) onboarding acceptance remain separate.

Refreshing an inert Quick Chat plan uses the same global capacity lock and delivery-configuration identity fence as job creation/enqueue, in addition to the no-attempt/no-reservation conditions below. Backup includes Quick Chat authoring and operation identities; restore expires preflights and places unfinished authoring executions on a recovery hold, without re-enqueueing or repeating an assistant call.

Public jobs retain `id`, `status`, `phase`, `request_hash`, `error_code`, `plan_id`, `project_id`, `client_ref`, `recipe_id`, `effective_request`, `simulation`, `result` and `artifacts`. Today `phase` mirrors status. Future progress details are additive; do not replace old strings with a new incompatible enum or silently return a different envelope.

### Cancellation reasons and original job change feed (#176)

Jobs add `cancelled_by` (`owner`, `operator`, `system` or null), `reason_code`, a
closed short `message`, and `retry_advice {safe_to_retry, charged,
upstream_still_running}`. Charge/upstream values are nullable when not proven;
unknown cost is never represented as free. Dedicated nullable job metadata
survives execution/result updates. Existing owner cancellation, identity,
fences, reconciliation and settlement rules remain authoritative. A failure or
an accepted stop alone does not establish permission to resubmit.
If the job DTO changes between its initial read and the bounded proof snapshot,
reason fields remain null and retry advice is conservative/unknown until the
next read; never attach a newer retry grant to an older queued/running phase.

`GET /v1/job-events?since=<cursor>&limit=100` requires `jobs:read` for a PAT and
filters the original outbox by tenant, owner and current PAT project authority
before pagination. Only new commit-ordered job events after activation are
included; earlier NULL-sequence events and original job/attempt history are
preserved. Delivery acknowledgement does not remove an event. The opaque
`je1.<event UUID>` cursor is resolved under the identical authority filter;
unavailable, foreign or revoked-project cursors return the same HTTP410.
Pages contain closed event status/reason/advice snapshots and IDs, never raw
outbox payloads, prompts, media, actor credentials or global sequence counts.
`next_cursor` advances only to the last returned authorized event; empty pages
retain the input cursor. Drain `has_more` pages, then observe
`poll_after_seconds`. Refresh current job/submission before a new explicit
retry; event snapshots describe their original transition, not current state.

The original transaction finalizer assigns append-only `job_event_seq` values
after business locks, under one fixed PostgreSQL advisory transaction lock and
READ COMMITTED, and immediately commits; rollback removes both event and
sequence. SQLite retains its existing serialized write transaction. No separate
event broker/ledger or historical backfill is introduced. A future retention
change must preserve valid cursors or explicitly require resynchronization.

Human administrators use `GET /v1/operator/admin/jobs` and
`POST /v1/operator/admin/jobs/cancel` with one durable Idempotency-Key,
`{jobs:[{id,expected_snapshot}],reason_code}`. Only queued/waiting jobs proven
unsubmitted and unleased qualify. Sorted original job locks, snapshots and all
original attempt/worker obligations are rechecked atomically; any stale or
ineligible selection rejects the entire batch. Cancellation/refund, the original
operator command and audit commit together. Running/uncertain work is never
silently included. Reasons are closed codes; account/admin/CSRF guards remain.
Unknown command results reuse the exact key/body; no automatic new command,
new job, machine rental or budget reset follows.

Safe linked retry reuses the immutable Quick Chat revision/prompt/inputs/seed,
requires fresh original-item preflight and explicit confirmation, and records
`retry_of_execution_id`. Initial confirmation replay retains the old job.

Current errors are HTTP 401 authentication, 403 browser-origin rejection, 404 missing/inaccessible resources, 409 conflicts, 413 size limits, 422 invalid requests, 429 budget/capacity limits and 503 transient service failures. Existing `detail` responses are not yet a universal structured-error schema. Add safe codes compatibly; never expose raw provider exceptions, secrets, filesystem paths or signed URLs. Private prompts/media are only returned through existing authorized user-data responses, not operational logs.

Direct generation independent of a story is a target, not an invented available endpoint. Preserve current source mappings until a versioned contract and migration exist.

## 3. Immutable admission and replay

1. Authenticate and authorize the real principal on every write/read, including Agent calls. Preserve project scope and account-change protection.
2. Resolve media from the asset service and snapshot selected derivatives/clips, owner, source versions and content identity. Never accept a client path or arbitrary URL as a trusted model input.
3. Freeze normalized controls, canonical seed (existing decimal string), request/source hashes, requested duration and native output geometry/frame duration. When an operator-qualified native delivery policy is selected, also freeze its explicit delivery specification in the execution plan. Later user edits cannot change accepted work.
4. Plan/preflight/assistant/card creation must not provision a GPU or start video generation. Only explicit confirmed admission can start paid video inference or demand-triggered GPU provisioning. Assistant model calls and storage have separate authorization/cost semantics and are not claimed to be free. Reservations and job admission are transactional; unknown cost is not zero.
5. Legacy idempotency namespace is tenant/owner/project/actor/key. Same key with equivalent admitted data returns the existing job; conflicting data rejects. Do not globally change that namespace.
6. Quick Chat additionally uses stable submission/item/execution business identity across Agents. Its queue actor is an internal idempotency namespace, not an authenticated user. Preserve actual caller authorization separately.
7. Local inert-plan refresh is a narrow exception: only planned/blocked Quick Chat jobs, same source/recipe identity, no lease/attempt/history/reservation. It keeps job ID and does not enqueue. No accepted execution snapshot is mutable through refresh.
8. Network replay, reconciliation, collection retry, user retry and another variation are distinct actions. User retry can create a linked new job only under the existing explicit item-retry policy; the original failure remains visible.

## 4. Lifecycle and ownership

| Persisted state | Authority and required next behavior |
|---|---|
| `planned`, `blocked` | Saved intention; no generation implied. Explain admission blockers. |
| `waiting_capacity` | Accepted original job waits for matching capacity; retain identity, reservation and reason. |
| `queued` | Eligible for one fenced generation claim. |
| `claimed` | Worker lease/attempt exists; preparation remains reversible. |
| `submitting` | Durable submission intent was committed before the external start. |
| `submission_unknown` | Start may have been accepted. Reconcile the same attempt; never blindly submit again. |
| `running` | Bound upstream execution; lost status is not proof it stopped. |
| `collecting` | Upstream output exists or is complete; recover original output, never regenerate to repair storage. |
| `cancel_requested` | Preserve original phase and obligations until attempt-specific stop evidence; retain late successful output. |
| `recovery_hold` | Ambiguous identity/history or ownership; quarantine and reconcile, not an empty queue. |
| `succeeded` | Verified durable artifacts were committed, even if supplier billing is still pending. |
| `failed`, `cancelled` | Proven safe terminal execution outcome; accounting may still require reconciliation. |

The queue keeps lease fences and original attempt identity across reconcile/collect claims. An expired worker cannot update a replacement claim. Drain blocks new work while allowing protected reconciliation/collection. A global engine interrupt must not cancel another attempt.

Recovery acceptance joins the WanGP receipt journal/host and adapter to the production queued worker, business ledger and scaler destruction guard. Missing/replaced journals or a changed authenticated incarnation retain the original attempt, occupied slot and reservations. Cancellation acknowledgement without stop cannot release them; late success remains collectable. A runtime that has stopped and sealed output is not yet business-idle while required video/audio collection is incomplete. Only both verified, durable business artifacts and their settled write receipt can complete the job before idle destruction; unresolved job cost and rental billing remain reserved independently. The synthetic `test_platform_wangp_recovery_integration` fixtures exercise these boundaries over SQLite and isolated PostgreSQL with CPU media and fake Session/provider calls. They neither reconstruct a lost fleet-parent process nor certify live fault injection.

Capacity has its own persistent instance-intent states (`reserved`, `creating`, `creation_unknown`, `starting`, `ready`, `busy`, `draining`, `destroying`, `destroyed`). Uncertain deletion remains `destroying` with evidence pending; a diagnostics counter named `destroy_unknown` is not an implemented persisted transition. Rental unknowns are not job unknowns and have their own evidence/reconciler. Empty provider inventory, failed status or an accepted DELETE does not settle costs or prove an ambiguous creation was never accepted.

An operator Lium manifest may set `min_download_mbps` to a positive finite number. It is absent by default; old configuration documents and fingerprints remain unchanged. The explicit floor goes to Lium's executor-list filter or both dry-run and actual rent-by-spec payloads, with no fallback that lowers it. Lium excludes missing or insufficient trusted measurements; no matching inventory retains `capacity_no_matching_gpu`, while an unconfirmed inventory check is distinct. This is a provider-measured host bandwidth constraint, not guaranteed Hugging Face transfer speed or permission to enable a new operating configuration. See the [official network-filter contract](https://docs.lium.io/developers/executor-interconnect) and [rent-by-spec parameters](https://docs.lium.io/developers/quickstart).

Provider preparation has a separate, opt-in operating timeout: `provider_preparation_timeout_s` (120–7200 seconds; absent/null keeps historical behavior and hashes). It measures from the original instance intent's creation, not the latest observation or controller restart, and is unrelated to the `cold_start_s` scheduling estimate. The existing rental receipt ledger commits `awaiting_provider` before creation and irreversibly commits `bootstrap_started` before any boot/SSH work. Only that positive unused proof, fresh exact-owned `PENDING` timeout or `FAILED`/`STOPPED` facts, and locked checks excluding workers, attempts and runtime evidence permit `retiring_unused`. The original approval closes atomically before deletion; ambiguous deletion reconciles once without replay. Service rollover preserves accepted job IDs, requests, original wait deadlines and reservations, and waits for confirmed removal and billing settlement before replacement. Provider status alone never proves idle. Legacy intents without this receipt require explicit controlled adoption; absent files do not make them eligible. Enabling this policy does not migrate an existing service fingerprint or authorize cloud work.

New Lium rentals created through the coordinator with a durable rent journal freeze an absolute TTL before the rent POST: the earlier of the original instance-intent creation plus the actual requested hours and its approved hard deadline. An exact pod observation then schedules this bound once; observed earlier deadlines are retained. The same private rent marker records schedule intent before POST and verification afterwards. An unknown POST is GET-only and may need operator recovery. Only an acknowledged, verified pending schedule permits one separately journaled shortening when the provider transitions to RUNNING and overwrites it. TTL checks gate initial lifetime/SSH entry, exposing `capacity_provider_ttl_unconfirmed` when that gate fails; they never renew the rental from readiness time. Cached ProductionBoot receipts continue enforcing the shorter database deadline. This does not continuously re-attest the provider schedule after a host is connected. Existing unbound markers retain strict read-only lifetime checks and gain no inferred scheduling authority. This source behavior is not provider-side compare-and-swap or a guarantee against independent external schedule writers.

A named, one-use protected controller handoff may also retain an explicitly stopped Lium `creation_unknown` intent with no provider ID. Its existing private `post_started` rent journal must match the original tag, selected executor where applicable, creation time and absolute deadline; its raw hash and TTL, active stop command, complete original start ordinals, immutable configuration and reserved accounting are frozen across retirement and successor-image validation before credentials or launch. Missing/changed journals, running desired state, active jobs, unsafe attempts, bound workers or unconfirmed old local ownership block the transfer. This transfers exclusive reconciliation ownership only: the original rental stays unknown and reserved, the successor never rents it again, and local shutdown does not assert cloud removal or zero cost. The positively identified pending-deletion path remains unchanged.

The provider protocol is documented in [Lium scheduled termination](https://docs.lium.io/pod-users/scheduled-termination): `POST /api/pods/{id}/schedule-removal` takes UTC `removal_scheduled_at`. The [official CLI reference](https://docs.lium.io/developers/cli/reference/up) describes scheduling immediately after receiving the rental ID, before RUNNING, from CLI 0.0.42. Readiness-time deadline drift was observed in this project's protected provider evidence; its one-correction allowance is defensive behavior, not a claim that every provider always overwrites explicit schedules. These sources establish the protocol; this code's fake HTTP tests do not establish live provider acceptance.

With the timeout enabled, `provider_preparation_failure_limit` (1–5, default 2) additionally bounds consecutive failed actual provider pods: normally the initial pod and one replacement. The streak is reconstructed from original capacity-cycle/start-barrier receipts, not a resettable process counter. Inventory refusals and authoritative no-rent cycles neither count nor reset it; a committed `bootstrap_started` transition ends this provider-preparation streak, while subsequent runtime failures retain their separate repair hold. At the limit, the original task remains held under `capacity_provider_preparation_retry_limit` / `awaiting_provider_repair`; provider deletion, billing reconciliation and the original waiter deadline still apply. No further rental is created automatically. Changing configuration or clearing files is not an authorized reset. Selection still uses the approved filters and may choose the same executor; failed-executor exclusion and frontend-specific wording are separate follow-ups, not verified capabilities of this slice.

## 5. Inference adapter seam

The existing worker remains the execution owner. The adapter provides:

```text
kind, enabled, slot_key
prepare(job, attempt_tag, storage, heartbeat) -> prepared
submit(prepared, attempt_tag) -> upstream_task_id
reconcile(attempt_tag, upstream_task_id=None) -> Outcome
poll(attempt_tag, upstream_task_id) -> Outcome
cancel(attempt_tag, upstream_task_id) -> acknowledgement, NOT stop proof
fetch(job, attempt_tag, upstream_task_id, target_dir, heartbeat) -> owned output paths
is_idle() -> bool  [B2 readiness extraction; only literal True permits readiness]
optional cost resolver and close()
```

Preserve `Outcome.state`, optional `task_id` and optional integer micro-USD cost. `succeeded` requires completed output evidence; `failed`/`cancelled` require verified upstream stop because the existing runner treats those terminal values as stop proof. Transport errors, missing in-memory tasks and unconfirmed disappearance must remain `unknown`. Preserve exception identity via compatibility imports: `BackendError`, `NotReady`, `SubmissionRejected`, `SubmissionUncertain`. A definitely rejected start differs from an uncertain start. Unknown adapter response or malformed/failed idle check fails closed.

An optional `Outcome.error_code` carries only a closed diagnostic vocabulary.
For a positively stopped WanGP failure, the facade classifies the first pinned
`GenerationError` into an allowlisted stage (`validation`, `generation`, `runtime`,
or `unknown`) and message-signature category. The existing operation receipt's
`reason`, CPU job/attempt `error_code`, and queued-task failure evidence retain
that static code after the GPU is removed. Cancellation, other adapters and old
receipts retain their existing behavior; unrecognized adapter codes fall back to
`upstream_generation_failed`. Unknown upstream messages use `unclassified`.
Raw messages, tracebacks, prompts, asset paths and URLs are never persisted by
this diagnostic path. Upstream discards the original exception class, so a
signature label is not an attested exception type or definitive root cause.
Diagnostics cannot establish stop, authorize retry, release a reservation, or
qualify a modality. A failed receipt write retains the original runtime handle
for reconciliation; it never regenerates. This is the source fix for [#63](https://github.com/inkseq/h3-studio/issues/63),
not proof that the failed REF video case has been corrected or rerun.

`prepare` may validate/stage inputs, but must not start inference. `submit` is called only after durable intent. `reconcile`/`poll` never trigger another inference. `fetch` only retrieves the existing attempt's artifacts. Idle confirmation is evidence for a currently empty matching engine; it is not proof of full model coverage or successful H3 inference.

B2 extracted the Comfy seam in [PR #17](https://github.com/apedintensor/h3-studio/pull/17), preserving its existing imports, defaults, controls, queue rules and CPU/mock paths. [PR #25](https://github.com/apedintensor/h3-studio/pull/25) subsequently added configured-slot/attempt engine binding and original-engine recovery with offline evidence. Engine-bound cold approvals, bootstrap/readiness and reconnect integration remain [D2 #22](https://github.com/inkseq/h3-studio/issues/22); configured routing alone is not a production migration.

Before WanGP activation, immutable execution configuration/attempt binding must identify engine code revision, image identity, compiler/recipe version, full component model revisions/precision and slot topology. Preserve old accepted Comfy bindings for reconcile/collect after new-task routing changes. One global backend switch cannot perform this migration safely.

The WanGP queued-task policy may explicitly admit first/last image slots with
`allow_first_last: true` and an aggregate `max_reference_files` of 0–2. With that
switch off the aggregate cap must remain zero. First/last images still undergo
the same owner/project checks, immutable asset snapshot, aggregate count and
inspected image-pixel limits. Ordinary image/video/audio references, reference
video sound and guides remain disabled in this recipe; raising the aggregate
cap does not enable REF. This policy validation and owned-upload admission
coverage are not real first/last-frame fidelity evidence. The first live proof
remains text-to-video; image-guided qualification and the selected native output
contract require their own actual artifacts before a parity claim. No existing
operator policy or accepted job is changed automatically.

The WanGP cold path extends the existing controller and capacity ledger. Its
approval, waiter, worker slot and attempt bind the same backend and manifest
digest. The private client also pins the runtime incarnation; generation POSTs
carry that precondition and a replaced host refuses them before dispatch. Reads
of original receipts remain available. A transport reconnect reuses the same
instance, host key, token and local port, including during draining; it does not
upload, reinstall, restart or resubmit. A complete controller-process restart
after fleet launch still requires explicit recovery under C2; transport recovery
must not be reported as acceptance of that wider requirement.

WanGP source/dependency staging is a pollable upload-only operation within this
same controller. Its identity-bound `staging` receipt precedes remote bytes;
an OS-held per-intent lock prevents concurrent uploads on the durable controller
host. The main loop continues leader renewal, wait/deadline reconciliation and
safe progress snapshots while transfer runs. Before setup starts, it rechecks the
current leader fence, approval, original waiter deadline, remaining rental life
and confirmed demand. Upload completion is not setup permission. A process lost
before setup may resume only verified source bytes; `bootstrap_starting` and
later phases reconcile the original remote marker and never replay setup.
Preparation cancellation is cooperative and may wait for a bounded in-flight
SSH operation. Pending transfer is not child-exit or upstream-idle evidence;
staging failure retains original accepted backlog and accounting for repair.

Model transfer runs only after the pinned environment/import checks, in one owned
Linux child using the already locked Hugging Face SDK. The manifest's exact file
allowlist and full revisions, official endpoint and explicit public `token=False`
remain fixed. Two file workers, a bounded SDK retry per file, parent/child elapsed
deadlines, disk/cache guards and parent-death termination bound preparation; SDK
partials never authorize a second bootstrap or job submission. Only safe completed
file/byte counts and static errors enter status. Native SDK logs are discarded,
and the child must be stopped/reaped before local preparation can be idle.
The owner's separate failure receipt retains the original static download error
and `stop_status` (`confirmed`, `unconfirmed`, or `not_observed` when no child
handle was returned). Bootstrap and retained CPU diagnostics project those same
bounded fields. Failed stop/reap keeps `model_download_stop_unconfirmed` as the
outward error; these diagnostic fields never replace fresh process/idle proof,
authorize replay, or expose SDK exception text. Receipt-write failure still
attempts child cleanup and cannot mask the original safe cause.
Transfer completion does not certify model bytes: the launcher acquires exclusive
journal/slot ownership, then performs full source/configuration/environment and
model size/hash verification before Session initialization or HTTP readiness.
Normal bootstrap launches that process once without a preceding duplicate
`--verify-only`; explicit standalone verification remains available. Its private,
atomic verification receipt binds manifest, slot, PID and host incarnation. The
same authenticated readiness incarnation must confirm an idle runtime. Receipts
never bypass verification in a new process; stale/mismatched evidence or startup
uncertainty cannot authorize another start. The existing launch deadline and
bootstrap marker remain binding, with verification and initialization shown as
separate preparation phases. Model-transfer speed, native
Xet interruption reuse and cross-instance model caching are not offline acceptance
claims. See [the runtime preparation guide](deploy/wangp/README.md#bounded-public-model-transfer).

Continuing single-slot operation is an explicit `service_policy`, validated
against the existing scope, absolute window, budget ceiling and idle interval.
`max_cycles: null` removes the old test-cycle count only. Each new rental remains
bounded by its own TTL and the same cumulative spent/reserved account balance;
unknown outcomes and repair holds still block unsafe rotation. Changing this
configuration does not alter previously accepted request identities, leases,
deadlines or bills, and does not itself grant spending authority.

### Opt-in pool member admission and original-pair controller

An operator approval may explicitly freeze `pool_members: {version: 1,
member_ids: ["a", "b"]}`: exactly two distinct stable member IDs, each one GPU
and one slot. Omitting it retains the legacy payload/hash and single-intent
cycle. The existing ledger stores the unique `(approval_id, member_id)` to
intent binding; intent creation, budget reservation and this binding commit
atomically under the shared capacity/account locks. Each member binds at most
one original intent. E1 has no replacement generation, provider call or second
task/rental ledger; removal does not release an unconfirmed bill or renew a
member binding, approval or deadline.

Cold plans freeze `capacity_binding: "pool-members-v1"` with the exact approval
ID/hash and current engine configuration. Their waiters belong to that approval,
with no single `intent_id`. The first compatible ready member may activate the
original job without rewriting its request, execution plan, budget reservation
or deadline. Readiness must match the approved tenant/pool, recipe/model,
configuration, backend/manifest/delivery and a live bound instance with enough
original lifetime. Both members may be eligible, but `WorkerControl` and the
existing queue remain the sole atomic claim/attempt authority. Generate claims
recheck membership and current approval; an unbound same-configuration worker
cannot take a pool-bound job. Existing unknown attempts retain their worker,
original attempt identity and reservations; another member may serve distinct
work, and approval revocation does not prevent original-attempt reconciliation.

The explicit `continuing-two-members` service policy adds a sorted, two-item
`member_ids` list; historical single-slot policy fields and fingerprints remain
unchanged. Both members use one real provider selector, rather than fabricated
offer IDs. Each immutable approval marks `pool_controller:
"continuing-two-members-v1"`. Warm requests require that current marker and
freeze the same approval/member binding as cold requests; they never fall back
to an unbound warm job. Legacy cold/finite controllers and E1-only warm grants
continue to refuse the new lifecycle. The original shared queue remains the
only generation dispatcher.

One confirmed task can finance the two original approved members, subject to
the existing account and capacity gates. The create transaction also checks
cumulative spent plus reserved amounts against the lesser of the service
ceiling and each locked account limit; a failed check rolls back the new member
before any provider call. The sole pool leader commits each
member's instance reservation, immutable binding, scaler action and optional
provider-preparation start barrier in one transaction before its sole create
call. Repeated ticks/restarts reconcile that action; unknown A is never rented
again, and separately approved original B may still serve different work.
Sorted member IDs determine stable ports, independent of readiness or creation
order. Foreign/unbound live instances and duplicate observed pod identities
are held for reconciliation. Two pod IDs alone do not attest distinct physical
host fault domains; that is a separate provider/live acceptance requirement.

Member preparation failures and runtime quarantine do not revoke a healthy
peer's approval. Existing exact-instance stop/idle evidence still gates
destruction, and all original attempt recovery remains attached to its worker.
The existing rental receipt ledger commits member quarantine with its worker
drain fence; restart replays that same evidence before activation. Missing
private diagnostic files cannot clear a committed hold. Controller status
names the held intents and reports repair-required when none remains usable,
without changing an unknown attempt into a new waiting job.
The member readiness projection reads only each current exact member binding,
matching Worker identity/spec hash, registration expiry, drain/current-job and
unresolved-attempt evidence. Raw rental states remain unchanged: `starting`
does not imply that an already registered Worker is still loading. The status
lists ready, busy, preparing, unavailable, held and unknown members separately;
a healthy sibling stays visible alongside explicit rental, budget or repair
blockers. Ready counts describe observed slots, not eligibility for any given
job. A remaining waiter may report `capacity_matching_slot_pending`, while
missing current readiness reports `capacity_worker_readiness_unconfirmed`;
only existing admission can activate a compatible job. These observations do
not establish upstream stop proof, new rental permission or physical isolation.
Unknown/cancel/collection obligations prevent pool-idle shutdown but do not, by
themselves, create new redundancy demand. Positive waiting/queued/claimed/
submitting/running work supplies that demand. Drafts and preflight remain inert.
The existing all-pool obligation timer gates the configured idle interval
(600 seconds under the accepted operating target); a fast intervening job
resets it. Global authority expiry/revocation still drains the entire service.

With no replacement policy, E2 has no in-place member replenishment. A failed original
member is not rebound or replaced with a third node. A fresh pair may open only
after the previous pair is physically removed, rental bills settled, children
proven stopped and original jobs safely transferable without another attempt.
Runtime/preparation repair holds require explicit repair rather than automatic
pair replacement. Rotation preserves original budgets, absolute authority,
accepted requests and waiter deadlines. Offline tests do not enable this mode
or establish provider operation, physical-host independence or production timing.

### Optional immutable member replacement generations (E3)

A **new** two-member service policy and its new approval can explicitly freeze
`member_replacement: {version: 1, max_replacements: 3, backoff_s: 60,
failure_limit: 2}`. These example values are not defaults or spending authority.
The validated ranges are 1–16 replacements per member/approval, 60–86400 seconds
initial backoff, and 1–5 consecutive failures. Omission preserves historical
payloads, fingerprints and original-pair behavior. Existing live approvals are
never adopted, rewritten or silently enabled.

Generation zero stays in the original membership table. The existing ledger
adds immutable `(approval_id, member_id, generation)` rows, each with a unique
predecessor and new intent. Current membership is the highest admitted ordinal,
including reserved/creation-unknown states; it never falls back to an older
ready worker. New generate claims use only that binding. Existing attempts retain
their original worker and protected reconciliation route. Ports are deterministic
`port_start + 2 * generation + member_index`, with the complete bounded range
validated before activation; a historical generation's port is never reused.

Replacement requires positive exact provider removal and settled/released
reservations for every approved rental account, with identical final amounts.
The normal invoice poll may settle after the removal receipt; any recorded final
provider cost must agree. A registered predecessor must be retired with released
devices and no lease, live/unknown/collecting or unstopped submitted attempt.
Local closure requires the original owned exited fleet children plus successful
transport closure, bound to intent/pod/worker, source/config and port. Lost
handles or absent files alone are not proof. A never-registered predecessor
instead requires exact owned failed-preparation evidence, or the irreversible
`retiring_unused` start barrier with no contradictory runtime evidence.
An authoritative `not_created` result is a separate no-rent case: the exact
current intent's create receipt and applied action must positively agree on
absence, all its reservations must be released at zero, and no pod, worker,
attempt or bootstrap evidence may contradict it. Later fresh demand may use a
new ordinal; the old intent is never retried. This consumes the absolute ordinal
cap but neither increments nor resets the paid preparation-failure streak.

The same fenced capacity/account transaction reserves the next generation,
records ancestry, and commits its sole create action before the provider call.
Both that transaction and the immediate pre-POST guard require current authority
and a fresh eligible waiting/queued original job with no attempt or lease.
Unknown outcomes reconcile that same action; no replacement is financed solely
by unknown/cancel/collecting or already-running work. The healthy sibling's
grant, worker, attempt, port and accepted requests remain unchanged.

Backoff begins at the predecessor's recorded destruction time, grows by powers
of two with the failure streak (capped at 16 times the configured base), and
does not reset on restart. Runtime quarantine or unused-provider retirement
counts as failure. Only that exact member generation's durable successful,
stopped actual attempt with validated published video resets its streak;
sibling success or bootstrap readiness does not. The absolute per-approval,
per-member generation cap never resets. Expired authority, exhausted budget,
unknown invoices, missing stop evidence, caps and absent demand remain held.
Protected status exposes the member, ordinal, limit, safe reason and retry time;
it does not authorize execution. Whole-pool 600-second idle/obligation handling,
accepted waiter deadlines and cumulative accounting remain unchanged. Backup
preserves ancestry but restores approvals/jobs disabled. Offline evidence does
not enable this policy or prove live replenishment, speed or fault-domain separation.

## 6. Output contract

Engine success is not platform success. The worker validates expected geometry, duration, actual decodability and audio, normalizes the requested export, and publishes through the existing `ArtifactWriter` receipt. Audio-enabled generation requires an independent audio output as well as valid video; an engine returning only a muxed file needs an explicit verified extraction step.

Exports are MP4/H.264, 24 fps, and independent FLAC for audio-enabled jobs. Requested duration is distinct from padded native sampling duration. Content hash/size, ownership, job/attempt linkage, storage quota and durable publication are recorded before completion.

Delivery is versioned and immutable; it is not inferred from the current default at collection time:

| Accepted execution contract | Delivery behavior |
|---|---|
| No `output_delivery` / `delivery_spec` | Historical requested-duration export, unchanged for existing jobs, Comfy and chapter roughcuts. A five-second H3 request delivers 120 frames. |
| Explicit `output_delivery: native-frames-v1` | New explicitly configured WanGP FL or bounded REF plans freeze `delivery_spec` with policy, fps, frame count, video duration and requested duration. All native frames remain: five requested seconds means 124 frames / 24 fps = 5.1667 seconds. No end-frame crop, frame-rate conversion, retiming or silence padding. |

The native policy requires exactly matching video frame count, 24 fps, zero-based timestamps and native video-stream duration before and after export. Both the MP4 audio and independent FLAC derive from the complete generated waveform; neither is cut to the integer requested duration. Their observed lengths must remain within the existing 0.1-second audio tolerance of the native video timeline, including codec/sample rounding; larger mismatch rejects collection instead of being corrected silently. Video, container and audio durations remain distinct evidence. This tolerance is not a claim of perceptual lip-sync or real H3 audio qualification.

The optional policy/capability is bound through operator policy, capacity approval, boot/config identity, worker registration and the accepted execution plan. Missing fields preserve historical hashes and legacy fleet-file fields. Native and legacy workers cannot share a recorded configuration identity; configuration IDs remain opaque. Historical registrations, immutable cold approvals and accepted jobs retain that binding even after retirement, revocation, expiry or completion. Writers check and persist it under the shared global capacity lock, so conflicting first approvals or stale-plan admissions cannot race a registration. Exact worker capability matching applies to generation and collection, and an old recovery spec cannot attach to a native worker record. Rollout requires a separately qualified configuration/registration; existing workers, accepted attempts and historical artifacts are not relabelled. No default or currently enabled operational configuration is changed by adding this implementation.

Collection receipts for native jobs additionally bind the frozen delivery specification. Publication validates native-only timing metadata against that specification; legacy metadata contracts remain unchanged. API preflight exposes the planned delivery under `execution.delivery_spec`, jobs expose the same descriptor, and downloadable artifacts retain verified timing evidence. Unknown policies or malformed descriptors fail closed before a new engine submission.

[D3 #27](https://github.com/inkseq/h3-studio/issues/27) separates deterministic CPU export evidence from real last-image conditioning. Frame preservation does not establish model fidelity. A real first/last-image example and its actual video/audio/download evidence remain necessary before advertising qualified ending fidelity. Fixed exact-duration retiming is not part of this policy.

On a process restart, an existing collection receipt resumes the same publication. A download, transcode, storage or database failure cannot cause a new generation. GPU scratch files cannot be discarded before durable output/obligation handling permits shutdown. Unknown final supplier billing keeps its reservation independently of video delivery.

## 7. WanGP acceptance requirements and implementation boundary

User authorization obtained on 2026-10-06. Pinned research revision: `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`. Use the upstream headless Session API behind a private adapter; keep upstream unchanged where practical. Do not adopt its UI/shared login/MCP in-memory queue as our account or job system.

[D1 #18](https://github.com/inkseq/h3-studio/issues/18) is accepted within its offline adapter/receipt scope. PR #25 implements a bounded Base FL2VA compiler, protected host/transport and configured routing; full runtime locking, cold-start integration and real output verification remain #22 and [B3 #16](https://github.com/inkseq/h3-studio/issues/16). REF qualification is [D4 #29](https://github.com/inkseq/h3-studio/issues/29): keeping Comfy recovery does not imply automatic REF-to-Comfy routing for new requests. Request-duration ranges do not by themselves establish the executable or publicly enabled envelope.

- Pin dependencies and every model component independently; an upstream `resolve/main` URL is not a frozen weight version.
- Use one active inference per isolated slot initially. Two replicas differ from tensor parallelism.
- Persist a subordinate attempt receipt **before** invoking the in-memory Session start. Record request hash, pinned runtime identity, dispatch state, upstream association, stop evidence and output references. It is evidence, not another scheduler.
- Same attempt+hash replays/reconciles; a conflicting hash fails. A crash between dispatch intent and acknowledgement stays unknown. A missing in-memory Session job after restart never authorizes replay. Do not promise exactly-once inference across process/network failures.
- Preserve all requested controls or reject before starting. Current platform controls cannot silently map to upstream defaults, ignored controls, automatic trimming, quantization, or different steps/samplers. Publish a tested intersection first; unsupported controls remain explicit.
- Verify first/last-frame versus omni-reference exclusions, reference counts/selected durations and audio behavior. Source support, adapter mapping, hardware qualification and current enablement are separate facts.
- Every GPU backend must retain engine identity, capacity guards, endpoint uniqueness and original-attempt recovery routing. Adding an enum or passing configured-slot tests cannot bypass physical GPU limits or the remaining production activation gates.

## 8. Acceptance matrix

### Offline REF candidate boundary (D4 #29)

`deploy/wangp/manifest-ref2va-candidate.json` defines a separate pinned Ref2VA
transformer and `sixnine-h3-ref2va-bf16-50-smallrefs-v1` compiler. It retains Base,
BF16 transformer/text encoder, 50 steps, SDPA and MMGP profile 4. Its initial
qualification input is at most one image, one silent video and one audio, with
at least one visual reference; images/videos are 256--832 px per side, at most
832x480 pixels and aspect ratio 0.4--2.5. A video is an explicit 2--3-second
source selection normalized by the existing asset service to 24 fps and 56 or
73 frames. Audio is 2--3 seconds, PCM16 WAV, 32 kHz stereo. Output is five
requested seconds, 832x480 and 124 native frames. These conservative bounds are
for the first qualification, not claims of the model's maximum capability.

The compiler consumes immutable owned asset snapshots, preserves reference
order, and maps images/video/audio to `image_refs` / `video_guide` /
`audio_guide` with explicit `I`, `V-U`, and `A` flags. There is no soundtrack
extraction, first/last mixing, automatic aggregate trimming or extra guide.
The private resolver rechecks the actual staged content hash, image geometry,
video frame count and absence of any audio track, and audio format/duration.
Media metadata alone cannot attest silence. Typed file copies preserve exact
normalized bytes; they do not transcode or shorten inputs.

The manifest/profile, model-specific pristine Session defaults and owned
launcher are bound together; legacy FL manifests and accepted jobs stay on
their original route. Requests without an additive deployment profile retain
this strict REF compiler. Execution requires a separate explicit singleton REF policy,
`native-frames-v1`, the bounded envelope above, and a matching manifest/worker
or approved cold capacity. The controller opt-in is
`execution_recipe_id: h3-base-ref2va-v1`; absence keeps the historical FL recipe
and hash/serialization. Boot validates that recipe against the manifest's
compiler/profile before staging. An FL policy or a legacy multi-recipe policy
cannot enable new WanGP REF requests. Preflight/card construction never rents
or starts inference; explicit job confirmation retains the existing path.

This source change does not activate a production policy. Freeze a complete
environment package and review a REF configuration before enabling its first
real qualification job; record actual output, peak memory and cost before
advertising GPU parity. Candidate construction or offline fake-Session success
is not production availability. Mixed-reference peak RAM/VRAM have not been
established for this candidate on one B200.

| Gate | Required proof | Evidence level |
|---|---|---|
| Existing API compatibility | Same types/statuses/IDs; owner isolation; source-version rejection; same-key replay and conflict; no paid action on plan/card. | Isolated API/repository tests |
| Adapter extraction | Old imports work; no raw Comfy queue protocol in generic fleet readiness; disabled remains inert; definite rejection vs unknown retained. | Fake HTTP/unit integration |
| Fenced execution | Intent before start; lost accepted response causes no second POST; stale lease cannot complete/interrupt another claim. | Fake upstream + database |
| Cancellation | Pending cancellation, stop confirmation, process loss and late result retain correct obligations. | Fake upstream + queue |
| Collection recovery | Failed download/publication/restart resumes same attempt; MP4 plus independent audio validate; no extra start. | Synthetic CPU media + receipts |
| Mixed engines | Old attempts stay on original config during new-route changes; new backend obeys all GPU/slot limits. | Offline routing/control tests, then isolated live gate |
| WanGP controls/runtime | Exact manifest and control mapping, rejection of unsupported combinations, restart journal and output containment. | Fake Session first; real GPU subsequently |
| PostgreSQL races | Claim, idempotency, admission and receipt contention against isolated PostgreSQL. | Separate database integration; SQLite is not equivalent |
| Public generation | One authorized task cold-starts to verified downloads; warm follow-up; idle shutdown then restart; timings and rental accounting retained. | Protected production receipt |

Existing suites to reuse: `test_platform_repository`, `test_platform_queue`, `test_platform_control`, `test_platform_worker`, `test_platform_fleet`, `test_platform_drain_safe_runner`, `test_platform_artifact_writer`, execution-policy/queued-task suites, and local Quick Chat admission/security suites when their shared entry points change. Add only tests that cover changed seams or unresolved risks; do not repeatedly run the full pipeline for intermediate edits.

## 9. Release and completion gate

A1/A2 can complete with dated baseline/specification evidence. B2 can complete locally after compatibility and fake-engine checks; that does not mean WanGP or generation is live. B/D public acceptance additionally requires a tested release, current operational authorization, exact runtime/model identity, original obligations preserved, real artifact validation and rollback.

No API/profile import, successful model load, empty queue, health endpoint, local mock, or passing CI proves a current GPU is ready. No issue or new specification renews the expired finite window. Keep the existing budget/reservations and review remaining authorization before paid acceptance.

## 10. Owner budgets and per-machine price caps

`GET /v1/operator/budgets` is a human owner's read-only view of their existing
tenant/owner-scoped budget accounts, independent of machine-start permission.
It never exposes a global or other owner's balance. Amounts are cumulative USD
microunits: limit, settled spend, current reservations and remaining. Seven-day
rows use confirmed UTC settlement dates; legacy settlements without a date are
reported separately, never assigned to the reservation's creation day.

Human administrators may read `/v1/operator/admin/budgets` and change an existing
limit with `PUT /v1/operator/admin/budgets/{id}` using the exact snapshot and
`limit_microusd`. The limit cannot be less than settled plus reserved. The same
transaction records actor, time and old/new balances in the original outbox;
spent/reserved amounts are never reset. Audit uses a composite descending cursor.

`/v1/operator/dstack/admin/prices` exposes price-only policy overlays for trusted
deployment profiles. An administrator may CAS-edit a default hourly cap and
ceiling, with an atomic outbox audit. A stale profile binding fails closed. The
hardware, native model, source and engine binding are not editable through this
surface. Owner previews may choose `max_price_microusd` at or below the ceiling.
The cap is frozen in the original request, reservation and continuing funding;
it is a maximum hourly rate, not a quote for actual settled spend.

The price snapshot is checked after provider planning and immediately before a
new start. Each preview is single-use. A prior accepted idempotency key returns
its original command before current-policy checks, without renting again.
Lost admin write responses are resolved by exact readback, never blind replay.
Monetary exhaustion is reported as `budget_exceeded` / “额度不足”; it does not
silently add a queued job or change existing accepted jobs or invoices.

## 11. Creation history lifecycle and content retention

Permanent cleanup is disabled until a protected post-release activation records
that the complete deployed application/execution set is tombstone-aware. Trash
and restore remain reversible before activation. Explicit purge, automatic due
cleanup and pending deletion/finalization all require that immutable activation.
This prevents the first rollout's automatic fallback from running old writers
after a purge. After activation, a manual downgrade to pre-lifecycle binaries is
unsupported; retain their installed rollback files, but use a tombstone-aware
roll-forward or independently reviewed compatible rollback. This is not a promise
that permanent deletion can be undone from retained local migration backups.

Quick Chat session history remains in its original owner/project-scoped authoring
and operation tables. Missing lifecycle metadata means active; it never means
expired or disposable. The public `quick_chat.creation_history` contract defines
the list, exact-version preview, confirmation and original-action readback APIs.
This is separate from story-project deletion and #154's stricter unsubmitted-card
action. The original hidden project is not a second editing or deletion API.

Trash/restore is an atomic metadata batch. Trash retains saved content without
an automatic deadline, including old consented records. Public `purge_at` is null
and `auto_purge_enabled` is false; stored old deadlines remain audit metadata.
Only explicit authorized purge/empty_trash preview and confirmation creates new
deletion intent. Previously persisted irreversible intents still reconcile;
completed deletions, tombstones and financial ledgers are not rewritten.
Restore has no age limit but remains forbidden after irreversible purge intent.
New test sessions may request safe archival after verified acceptance; unresolved
work still blocks it, and archival does not authorize automatic deletion.

Purge preview reports exact targets, removals, retained shared/chosen references
and ledger preservation. Confirmation rechecks ownership, project authority,
versions, preview expiry, the complete captured set and current obligations.
Queued/running/collecting jobs, uncertain submission or recovery, unfinished
admission/import/upload/assistant work and unsafe original attempts cannot be
hidden by a terminal-looking card. Existing generic rejection of managed Quick
Chat plans remains in force; internal admission and completion also honor the fence.

The reference proof covers all saved relevant records, including restorable
trash, historical immutable card revisions, project entity references, character
looks/voice, locations/props/inbox, chosen video and independent audio, derivatives,
plans and original job/collection receipts. Default UI pagination limits are not
reference-scan limits. Unsaved browser drafts cannot be claimed as saved references.

All original reference writers share a database gate and revalidate irreversible
content tombstones after acquiring it. Purge commits its exact per-object intent
under that same gate before object-store IO; no database transaction spans R2 IO.
Stale uploads/imports/collection and idempotent receipt recovery cannot republish
purged IDs or keys. Shared objects are retained rather than force-deleted.

Once any irreversible intent exists, restore is unavailable, even if deletion is
pending or its acknowledgement was lost. Resume and observe the original action
and exact object identities; pending is not deleted. Quota changes require the
original absence/completion proof and occur once. Content can become unavailable
without deleting job/attempt IDs, timestamps, immutable hashes, receipts, money
or unresolved billing. This does not clean migration originals, backups or rollback
releases and never grants retry, cancellation, rental or refund authority.


## Quick Chat take groups (additive #233 contract)

The card ID is the group identity. Immutable revisions/items retain their original
hashes, seeds and execution receipts. GET `/v1/quick-chat/sessions/{session_id}/cards/{card_id}/takes`
returns paginated original submission/item/job/action fields, exact `recipe`,
`annotation`, stable 1-based `group_number`, `total_count` and `before_cursor`.
The original per-revision `index` is never rewritten. Star annotations are owner
CAS metadata outside the generation hash: PATCH `.../takes/{item_id}` with integer
`expected_version` and boolean `starred`, using one durable Idempotency-Key.

GET/PATCH `/v1/quick-chat/preferences` exposes owner CAS `copies_max` (default 4)
and `takes_fold_count` (default 8). Positive JSON integers are editable up to the
integer representation bound; original configured project-byte/model limits
remain authoritative. Preference changes apply to new revisions/Again, not to
historical reads, accepted jobs or original submission replay. A fixed seed is
used only for the first new take; other new takes use distinct random seeds.
POST `.../cards/{card_id}/again` takes `expected_card_version`,
`source_revision_id`, `item_id`, `copies` and creates a fresh revision in that
same group. Original preflight, spending confirmation, admission and execution
receipts remain the only paid authority. Again is not retry of a failed attempt.

POST `.../takes/{item_id}/story` takes `target_project_id`, integer
`expected_project_version`, scene `parent_id` and optional `title`. Both source
and destination owner/scope checks precede media copying. The original asset
journal copies and SHA-verifies exact bytes; roles, ranges, audio selections,
profile, controls and the selected seed lock become a new editable shot. No job
is enqueued. GET `.../story-transfers/{transfer_id}` reads the original operation.
Unknown/pending results keep the same body/key and require explicit same-key
resume; destination CAS conflicts preserve copied assets without cleanup.

GET `/v1/generations/queue-position?job_id={owned_job_id}` projects original pool
fairness only, revealing neither other owners nor an ETA. Preflight's nullable
`estimate.machine_seconds` is a saved known planned machine allowance, not
historical timing or promised completion. Original cost/admission truth is
unchanged. Strict write DTOs and safe field diagnostics are published in OpenAPI
and `/v1/quick-chat/schema`; runnable generic-helper examples are in
[the public Skill](skills/sixnine-yingxu/SKILL.md#take-groups-and-explicit-recipe-transfer-233).

中文：候选组沿用原任务、预检和计费权威。偏好修改不改历史；转入故事只复制完整配方与素材，
不启动生成。未知结果保留原操作并显式续办，不自动重试、不清理复制资产。


A transfer receipt with `status:"blocked"`, `no_shot_committed:true` and
`error_code:"target_version_conflict"` is a closed no-shot result. Retain that
receipt/key and all copied assets; the user may explicitly acknowledge it and
choose a fresh destination/version as a **new** intent. Do not automatically
retarget. Original-key replay returns the same blocked receipt. This proof is
never inferred from HTTP 4xx: unresolved storage journals and unknown outcomes
remain on the original operation. 中文：只有明确“未创建镜头”的封闭回执才可重新选择；
未知存储结果仍须核对原操作，不能自动换Key或清理资产。

The transfer DTO binds `source_session_id`, `item_id`, `project_id`, original
`expected_project_version` and `parent_id` to the persisted command. A client must
match all five before acknowledging any completed/blocked receipt, and must also
match the known `id` on status reads or replay. `project_version` is the resulting
version and must not replace the original expected-version identity check.

# Sixnine durable decisions

Recorded: **2026-10-08 (Australia/Sydney)**. Evidence baseline: [`704202541a51be542bd39c7f931ae8b5c86ff528`](https://github.com/apedintensor/h3-studio/commit/704202541a51be542bd39c7f931ae8b5c86ff528).

This log records accepted direction and its reasons; it is not another specification, task board or production status report. Read the current [project plan](PROJECT-PLAN.md), [generation contract](GENERATION-CONTRACT.md), [workflow](WORKFLOW.md) and dated [baseline](CURRENT-BASELINE.md) for their respective authorities. Source capability, implemented mapping, tested behavior and production enablement remain separate facts.

`Accepted` means an agreed direction or invariant, not completed implementation. `Proposed` alternatives have not been selected. `Superseded` preserves an earlier direction and identifies its replacement. Original decision dates are **unknown unless explicitly stated below**; all other entries were documented by the pinned baseline, not necessarily decided on its commit date. Revisit triggers below are proposed maintenance criteria, not reconstructed historical conversations. Alternatives or reasons absent from the evidence are not invented.

Keep IDs stable. A material reversal receives a new decision ID and a `supersedes` link; preserve the earlier record. Update the affected plan/contract in the same reviewed change. Issues hold claims, progress and acceptance evidence, not overriding architectural instructions.

## DEC-001 — Evolve one authoritative platform in place

- **Status / scope:** Accepted; business backend, identity, data and module boundaries.
- **Decision:** Retain the existing platform and extract application services, inference adapters and capacity policy behind explicit interfaces. Ownership, assets, accepted jobs/attempts, budgets and obligations retain one business authority. A runtime receipt journal is subordinate evidence, not another business queue. Separate worker/controller responsibilities do not require a second backend or database authority.
- **Why:** The accepted plan requires preservation of identities, accepted work, results, recovery evidence and billing while individual implementations change.
- **Alternatives:** A parallel `backend_v2`, duplicate job database and dual-write generation authority are explicitly rejected. No separate historical scoring exercise is documented.
- **Revisit when:** A measured availability or scaling requirement cannot be met within these boundaries; any replacement must first specify identity, obligation and recovery continuity.
- **Evidence / current contract:** [Pinned plan §§3–4][P-platform]; [generation contract §1](GENERATION-CONTRACT.md#1-one-business-api-and-ledger).

## DEC-013 — Use dstack capacity, Hatchet dispatch and Grafana Cloud telemetry

- **Status / scope:** Accepted user direction, 2026-10-11; phased implementation in [#132](https://github.com/inkseq/h3-studio/issues/132).
- **Decision:** dstack 0.22.3 owns new-path provider offers and long-running Vast/RunPod allocations. Hatchet owns durable dispatch, with CPU workers reusing the existing atomic job/slot claim, WanGP SSH transport and original-attempt recovery. Freeze `legacy` or `hatchet-v1` in each accepted execution plan. Grafana Cloud stores safe OTLP metrics/logs/traces; do not deploy a local Grafana or long-term telemetry SQL store. Business identities, immutable requests, attempts, receipts, budgets and unknown obligations remain in the existing ledger.
- **Current capacity and lifetime refinement (2026-10-11):** Explicit dstack `lifecycle.version=2` uses live owned dstack runs and their provider observations for new-path machine/GPU/hourly capacity limits. A terminated run whose provider allocation is absent does not count; legacy Lium/Targon and historical intents never count as dstack capacity. Retain their rows and unsettled billing without adding a physical-removal reconciler. New v2 runs have no fixed start deadline: `hard_deadline` and request `max_duration_s` are null and the pinned run specification uses `max_duration: off`. Lifetime follows configurable business idle stop and keep-on (`idle`, finite `until`, or `until_stopped`), while original matching accepted queued work and active/unknown/collection obligations pause idle shutdown. Enforce hourly `max_price` and the existing owner/global cumulative spend caps by funding rolling windows in the same original instance reservation rows from the original paid-start time. Availability, claim and late submission share exact runtime proof and remaining funded-runway checks; insufficient budget requests normal drain while preserving original work. Old finite requests, bindings, deadlines and guardian evidence remain unchanged.
- **Why:** Replace repeated custom provider/dispatch maintenance without rewriting the working creator API or discarding protections against duplicate generation and lost outputs. Reuse the warm native runtime across jobs.
- **Alternatives:** Another bespoke scheduler, a parallel backend/job database, per-video GPU cold starts and a self-hosted telemetry database are rejected for this migration. GPU pull workers and custom Lium/Targon dstack plugins are deferred pending measured benefit.
- **Constraints:** Hatchet is at-least-once; no automatic retry of uncertain inference. dstack RUNNING is not proof of model-loaded readiness or stopped billing. Initial runs request one physical GPU; larger allocations need all-device execution evidence. Current qualification uses two separate Vast single-GPU runs (FL and REF), with the additive 29 GiB pruned INT8 allocation/headroom policy; #130 total-cgroup evidence does not qualify post-import available headroom. Preserve original manifests and unpruned floors. Select central `vastai--sixnine-dstack-20261011` and `runpod--sixnine-dstack-20261011` explicitly; both may be checked read-only, paid P2 uses Vast only and RunPod remains read-only. P2 warm generation and owned idle/keep-on lifecycle must pass before P3 production activation. Legacy retirement waits for >=14 stable days and recovered tasks/obligations. Existing user authorization governs spending; the plan itself does not reset budgets.
- **Evidence / contract:** [Migration specification](DSTACK-MIGRATION.md), [generation contract](GENERATION-CONTRACT.md). This record accepts direction, not completion or production enablement.

## DEC-002 — Use a thin WanGP runtime adapter; preserve original Comfy recovery

- **Status / scope:** Accepted; inference execution. Original decision date **2026-10-06**, explicitly recorded in the plan.
- **Decision:** Adopt pinned upstream WanGP through a thin private adapter. WanGP owns the model pipeline within a slot; Sixnine retains admission, durable attempts, storage, accounting, recovery and capacity. Preserve Comfy as the historical baseline and original-job/rollback route; new defaults must not reroute accepted attempts. This does **not** authorize automatic Comfy/REF fallback or a second new-feature track.
- **Why:** The recorded choice adopts the existing headless runtime while preserving platform business and recovery responsibilities. No broader engine-performance winner has been established.
- **Alternatives:** The earlier **SGLang-first evaluation order is superseded** by this decision. SGLang Diffusion, vLLM-Omni, Diffusers and external APIs remain explicitly conditional alternatives, not parallel implementation obligations or silent substitutes.
- **Revisit when:** The pinned WanGP recipe fails a concrete accepted control, resource, quality or recovery requirement; compare that requirement before selecting another adapter.
- **Evidence / current contract:** [Pinned plan §7][P-engine], [PR #25](https://github.com/apedintensor/h3-studio/pull/25); [generation adapter contract](GENERATION-CONTRACT.md#5-inference-adapter-seam).

## DEC-003 — Qualify one BF16 Base / 50-step slice before broader capability

- **Status / scope:** Historical initial runtime/public-generation acceptance scope. Superseded by [DEC-011](#dec-011--use-rtx-5090-and-int8-for-future-low-cost-h3-tests) for future test hardware/precision priority only; existing BF16 jobs, evidence and outstanding recovery criteria remain intact.
- **Decision:** Establish one declared FL2VA recipe and one isolated execution slot, with one active generation per slot. Preserve BF16 Base and 50-step semantics; reject unsupported controls instead of substituting quantized, distilled or accelerated variants. The first real service proof is text-to-video. Optional first/last-image source mapping is not proof of delivered last-frame fidelity; REF image/video/audio capability requires its own mapping and qualification.
- **Delivery refinement (recorded 2026-10-08):** New separately qualified WanGP plans may opt into the frozen `native-frames-v1` contract, preserving the native ending and generated waveform instead of silently cutting to integer seconds. Existing snapshots and legacy workers retain their original requested-duration export. This is an explicit follow-up contract under D3, not activation or a change to the first text-only proof's configuration.
- **Why:** The accepted first slice separates a reliable real API-to-download path from broader capability and redundancy claims. The integration records an unresolved native-versus-delivered ending contract.
- **Alternatives:** Broad REF rollout, accelerated variants and two-node acceptance are deferred scopes; their parity has not been established. The recipe document retains exact parameters rather than duplicating them here.
- **Revisit when:** Initial runtime/service gates pass, or a concrete requirement needs a separately declared recipe. Real first/last-image and audio evidence must qualify the chosen native-delivery contract before advertising that fidelity; exact-duration retiming would need another explicit decision.
- **Evidence / current contract:** [Pinned recipe][P-recipe], [pinned plan first-batch scope][P-first], [D3 #27](https://github.com/apedintensor/h3-studio/issues/27), [D4 #29](https://github.com/apedintensor/h3-studio/issues/29); [current recipe](deploy/wangp/README.md). The pinned integration evidence explicitly leaves image/runtime and real inference qualification incomplete.

## DEC-004 — Reconcile uncertainty; distinguish replay, retry and variation

- **Status / scope:** Accepted; generation/rental side effects, cancellation and collection.
- **Decision:** Bind confirmed work to immutable identity and durable intent. Lost responses or unknown execution, cancellation or rental outcomes require reconciliation with evidence, timing and escalation. They do not permit blind resubmission. Recover collection from the original attempt without regenerating; cancellation acknowledgement alone is not stop proof. This is **not a permanent no-retry rule**: a technical new attempt requires confirmed prior stop and explicit policy; an explicit user retry or variation follows its separately defined identity and cost semantics.
- **Why:** The contract distinguishes network replay from a new generation decision and retains outcomes, artifacts and obligations across failures.
- **Alternatives:** Inferring safe retry from an empty queue/provider list, or treating every retry as a new initial job, is rejected. No universal exactly-once inference guarantee is claimed.
- **Operator exception (2026-10-10, clarified 2026-10-11):** An authorized browser operator may mark an exact Lium/Targon record manually reviewed after checking that the provider account has no corresponding machine and no continuing charge, including a stop made directly in the provider dashboard without a platform stop operation or legacy controller heartbeat. A `creating`/`creation_unknown` record with no provider identity requires an exact null identity attestation and no unsafe execution/ownership obligations anywhere in its original pool; never invent an instance ID or retire an unknown actor. This audited mark moves the record to history and excludes it from operational capacity, without asserting physical removal, settling unknown charges, releasing budget reservations, changing the original intent state/deadline or authorizing duplicate creation. Exact expired worker ownership can be retired only with matching protected binding/spec and safely terminal execution history; fresh workers, active jobs and unknown upstream stops reject the mark. Existing guardian proof remains separately evidence-bound. Unconfirmed supplier removal/final billing stays in backlog. See the operator capacity contract and [#107](https://github.com/inkseq/h3-studio/issues/107).
- **Revisit when:** Evidence can establish a previously unknown outcome, or a new provider requires a different explicit recovery protocol; preserve historical attempt evidence.
- **Evidence / current contract:** [Pinned plan §6][P-retry]; [immutable admission](GENERATION-CONTRACT.md#3-immutable-admission-and-replay) and [lifecycle](GENERATION-CONTRACT.md#4-lifecycle-and-ownership).

## DEC-005 — Start capacity from confirmed demand; serve the first ready slot

- **Status / scope:** Accepted target; demand admission and eventual pool behavior.
- **Decision:** Preflight, assistant responses and job cards do not start paid video inference or provision GPUs. Explicitly confirmed queued work creates demand. DEC-012 additionally permits explicit operator manual capacity intents without a synthetic video job. Use the accepted task once for the first authorized real path and deliver its result to its owner. Serve from the first matching ready slot; the later redundancy target is two independent nodes, with no always-on GPU by default and shutdown after **600 seconds without pool obligations**. Unknown/running/collection obligations cannot be erased to declare idle.
- **Why:** The documented target makes useful service possible before the second node is ready and avoids duplicating the user's task as a benchmark.
- **Alternatives:** Always-on capacity and requiring dual-node acceptance before the initial single-slot proof are outside the accepted target. Two replicas are not tensor parallelism or complementary capability coverage.
- **Revisit when:** Measured wait times, reliability requirements or explicit operating authority justify a different idle or redundancy policy; record it as validated policy, not an unreviewed counter change.
- **Evidence / current contract:** [Pinned plan pool policy][P-pool], [confirmed-task rule][P-first]; [generation admission](GENERATION-CONTRACT.md#3-immutable-admission-and-replay), [E #5](https://github.com/apedintensor/h3-studio/issues/5).

## DEC-006 — Preserve obligations and separate validated policy from authority

- **Status / scope:** Accepted; service/test policy, budgets, leases and release authority.
- **Decision:** Preserve the single rental authority, cumulative accounting, reservations, accepted identities and original deadlines. Separate continuing-service policy, finite test windows and node capability. Routine account/window/idle/envelope values belong in validated configuration where behavior already exists; new recovery behavior needs code and tests. Neither path creates spending authority. An issue, merge, restart or configuration migration cannot renew a window, increase a budget, release unknown costs or authorize production deployment.
- **Why:** The plan separates engineering implementation from financial and operational authorization, while keeping outstanding work recoverable.
- **Alternatives:** Repeated hard-coded account/date/controller changes and resetting the ledger to resume service are rejected. No new authorization mechanism is selected here.
- **Revisit when:** The user changes operating authority, policy requirements change, or supplier evidence settles an obligation; apply changes through the existing reviewed policy/ledger path.
- **Evidence / current contract:** [Pinned workflow policy/release rules][P-policy], [C1 #14](https://github.com/apedintensor/h3-studio/issues/14), [C3 #28](https://github.com/apedintensor/h3-studio/issues/28); [current plan](PROJECT-PLAN.md#9-first-abd-batch-and-authorization-boundaries).

## DEC-007 — Keep canonical scenario UX on the shared business API

- **Status / scope:** Accepted; frontend ownership, authoring and integration.
- **Decision:** Edit the canonical `video-studio-design/studio-app` and use the approved Quick Chat mock in `sixnine-design`; `yingxu/` is a generated release snapshot. Quick Chat sessions/turns/material bindings/card revisions own authoring; hidden project/shot objects are compatibility projections. Quick Chat, story production, future scenarios and Agent clients share the same ownership, assets, generation and result contracts. Backend extraction does not authorize UI redesign or frontend publication.
- **Why:** The accepted boundaries preserve the approved interaction and avoid independently editable copies of authoring or accepted requests.
- **Current product clarification (2026-10-10):** [#122](https://github.com/inkseq/h3-studio/issues/122) supersedes automatic next-round material inheritance: after durable generation acceptance clear only the submitted unchanged prompt/material selection. Preserve assets, immutable cards/jobs, later draft edits and persistent preferences; reuse is explicit. Browser and Agent confirmations share the same reset receipt. New legacy-card preflights/edits freeze matching current binding versions; already-existing originless preflight/submission records remain unassociated and preserve selection. See the [generation contract](GENERATION-CONTRACT.md#current-authoring-and-admission-refinements).
- **Alternatives:** Editing the release snapshot directly, replacing the UI with WanGP's UI, or adding a scenario-specific task authority is rejected. No other frontend framework decision is documented here.
- **Revisit when:** An approved scenario requires new domain behavior; extend the shared contract and versioned UX before changing projections or release output.
- **Evidence / current contract:** [Pinned source/product boundaries][P-product], [pinned API direction][P-api]; [current plan §§4–5](PROJECT-PLAN.md#4-module-boundaries-and-data-authority), [G1 #20](https://github.com/apedintensor/h3-studio/issues/20).

## DEC-008 — External Agent onboarding must not require our AI Registry

- **Status / scope:** Accepted; public Agent setup and credential exposure.
- **Decision:** Use the public one-time connection/helper contract so external agents can use the same owner-isolated business API. The internal AI Registry remains an operator dependency, not an external-user prerequisite. Preserve connection authority and expiry; permanent keys must not appear in chat, discovery pages or logs.
- **Why:** The accepted API direction explicitly distinguishes external onboarding from this workstation's internal credential infrastructure.
- **Alternatives:** Requiring external users to install/use the internal AI Registry is rejected. A historical comparison of other onboarding protocols is not documented in the pinned evidence.
- **Revisit when:** Supported clients or security requirements require a new connection protocol; preserve existing identity, expiry, revocation and authorization semantics during migration.
- **Evidence / current contract:** [Pinned product rules][P-product], [pinned API direction][P-api]; [G2 #21](https://github.com/apedintensor/h3-studio/issues/21) and [current AGENTS](AGENTS.md#source-and-product-boundaries).

## DEC-009 — Protect existing storage first; evaluate migration with recovery proof

- **Status / scope:** Accepted near-term boundary; final object-storage provider remains open.
- **Decision:** Initially retain the existing EC2 CPU hosting, PostgreSQL and protected local media storage boundary. Preserve independent backups, retrievable assets and rollback before migrating. Evaluate S3 with verified backup/restore; R2 and Hippius are **Proposed conditional alternatives**, requiring their own storage-semantics evidence. A provider change is not a prerequisite for the first generation proof; GPU scratch cannot be the only deliverable copy.
- **Why:** The plan prioritizes preservation/recovery of existing assets and makes storage migration a separately tested change.
- **Alternatives:** S3 evaluation and conditional R2/Hippius evaluation are documented; no final cost/region/provider winner or migration authorization is established.
- **Revisit when:** Backup/restore evidence, storage semantics and measured scale/cost requirements support a reviewed provider decision.
- **Evidence / current contract:** [Pinned hosting/storage direction][P-storage], [pinned F sequencing][P-pool]; [F #6](https://github.com/apedintensor/h3-studio/issues/6), [current plan](PROJECT-PLAN.md#7-engine-and-deployment-decisions).

## DEC-010 — Agents own bounded PR delivery; acceptance requires scoped evidence

- **Status / scope:** Accepted; cross-session development, source protection and acceptance.
- **Decision:** Use the existing GitHub Project/issues, bounded claims and isolated worktrees. Agents prepare, review, verify and merge authorized batches; routine manual human PR work is not required. Use English on GitHub and independent review for material permission/data/task/billing changes, disclosing authoring overlap. Preserve reviewed source without confusing draft backup, source merge, criterion acceptance and deployment. Record each criterion's evidence and keep unresolved findings owned in existing/follow-up issues.
- **Development release refinement:** During development and public trials, an independently reviewed app-only update may preserve the running controller and accepted queue through the exact-version compatibility receipt. The optional Google naming secret and app egress are an explicitly validated configuration delta, not a reason to cancel user jobs or drain unchanged GPU execution. Database, ingress, other mounts, controller/rental configuration and accepted identities remain unchanged; unrelated configuration/protocol transitions still require their migration plan. Source/manifest approval and rollback evidence remain required. See [development release rules](DEVELOPMENT-RELEASE.zh-CN.md) and [#121](https://github.com/inkseq/h3-studio/issues/121).
- **Why:** The workflow establishes one coordination/acceptance record and makes implementation, verification and release claims assessable across sessions.
- **Alternatives:** Competing task databases, mandatory routine human PR handling, blanket commits of dirty shared work, or declaring completion from test totals alone are rejected. No framework replacement is selected.
- **Revisit when:** Coordination or evidence failures expose a specific missing rule; update the shared workflow without creating a second process or treating inactivity as abandonment.
- **Evidence / current contract:** [Pinned workflow][P-workflow], [PR #30](https://github.com/apedintensor/h3-studio/pull/30); [current workflow](WORKFLOW.md). The stable required `test` check and exact release rules remain defined there.

## DEC-012 — Add explicit operator-managed capacity and tested profile choices

- **Status / scope:** Accepted user direction, 2026-10-09; manual GPU console and explicit recipe selection. Extends DEC-005 without changing accepted tasks or automatic capacity authority.
- **Decision:** Start with Lium and the three tested deployment profiles. The user accepts pruned rank8 INT8 after reviewing results. Operators can request machines independently of user jobs, choose per-machine configuration and set bounded total capacity. Reuse the existing scaler, rental ledger, workers and shared generation API. Keep node billing separate from GPU slot identity. Quick Chat and Agent requests select an explicit deployment profile and reach matching workers.
- **Model-first inventory (2026-10-09):** The user selects model and FL2VA/Ref2VA, then chooses a ranked Lium/Targon allocation. #89 replaces upfront hardware/provider/count selection. Rank qualified deployments first, then whole-allocation price and download bandwidth. Show full multi-card hosts at their real price; admit only qualified topologies with one execution slot per GPU. Bind the exact Lium executor or Targon resource SKU through preview and creation, without substitution. Unknown stock/specifications remain unverified. The #85 next-tier API remains compatible; Targon live qualification remains #86. See `OPERATOR-CAPACITY-CONTRACT.md`.
- **Why:** The user wants control over costs and hardware, and a direct path from a manually started machine to useful generation. Hardware/precision choices and preparation state need to be visible.
- **Alternatives:** No separate experiment scheduler, fake video demand, silent precision substitution, or second business backend. Other providers remain later adapters; displaying one does not establish support.
- **Revisit when:** Measured demand or a different provider requires a new deployment profile/topology. Preserve exact accepted identity and original accounting.
- **Contract / evidence:** [Operator capacity contract](OPERATOR-CAPACITY-CONTRACT.md), [E3 #71](https://github.com/apedintensor/h3-studio/issues/71). Native timing evidence does not establish a production deployment or guarantee.

## DEC-011 — Use RTX 5090 and INT8 for future low-cost H3 tests

- **Status / scope:** Accepted user direction, 2026-10-08; future tests prioritize inexpensive RTX 5090 INT8 for both FL2VA and Ref2VA. Supersedes DEC-003's future hardware/precision priority, not historical acceptance, immutable jobs or required recovery gates.
- **Decision:** Present a bounded machine/filter/startup test proposal before execution, then use measured findings to design an operator panel. Retain the existing WanGP adapter, business API and single capacity/rental authority. Do not silently upgrade to expensive GPUs or change an accepted BF16 request into INT8.
- **Why:** The user wants an inexpensive working service and direct visibility/control over machine configuration and preparation, after substantial time was spent on cold setup.
- **Subsequent selection:** The 2026-10-09 user direction accepts the tested pruned rank8 INT8 recipe and requests an operator console (DEC-012). Exact experiment scope and limits remain evidence-bound; this is not full-control parity or production qualification.
- **Operator selection:** Future tests still prioritize inexpensive INT8. Under the later DEC-012 model-first UI, operators compare compatible Lium/Targon allocations, including larger hardware and full multi-card hosts, before separate preview/confirmation. Hardware choice does not upgrade weights/precision or qualify an untested topology.
- **Revisit when:** The user changes priority, or measured failures require revising the proposed memory/control envelope. Hardware/recipe changes remain explicit; existing jobs and cumulative costs survive.
- **Evidence / proposal:** [User-direction planning claim](https://github.com/apedintensor/h3-studio/issues/22#issuecomment-6056468134); [5090 pilot and panel proposal](docs/research/h3-economics-20261008/5090-test-plan.md). No GPU, image build, model download or production change is authorized by this record.

[P-platform]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L39-L76
[P-engine]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L107-L134
[P-recipe]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/deploy/wangp/README.md
[P-first]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L161-L193
[P-retry]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L91-L105
[P-pool]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L142-L159
[P-policy]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/WORKFLOW.md#L73-L95
[P-product]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/AGENTS.md#L22-L28
[P-api]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L78-L89
[P-storage]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/PROJECT-PLAN.md#L136-L139
[P-workflow]: https://github.com/apedintensor/h3-studio/blob/704202541a51be542bd39c7f931ae8b5c86ff528/WORKFLOW.md

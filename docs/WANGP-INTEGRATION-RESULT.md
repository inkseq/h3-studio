# WanGP integration checkpoint

Date: 2026-10-07, Australia/Sydney. Integration owner: Codex / root.
Work items: [D1 #18](https://github.com/apedintensor/h3-studio/issues/18),
[D2 #22](https://github.com/apedintensor/h3-studio/issues/22), coordinated
[B1 #12](https://github.com/apedintensor/h3-studio/issues/12).

Source checkpoint accepted in [PR #25](https://github.com/apedintensor/h3-studio/pull/25),
merge `d8c811db79683507a959d3260cdf99b072d0022e`. This remains a dated technical
receipt, not the live task board. The 2026-10-08 follow-through in
[A4 #26](https://github.com/apedintensor/h3-studio/issues/26) links unresolved work
below; it does not add a production observation or real-GPU result.

## Implemented scope

The existing API, task ledger, worker and artifact writer remain authoritative.
This batch adds an injected WanGP adapter, subordinate durable execution journal,
private runtime host/transport, a pinned-source FL2VA compiler and explicit fleet
configuration. It does not replace the frontend, queue, database or controller.

- API confirmation, draft preflight and enqueue use the shared
  `studio_platform/generation_admission.py`. Original account, source-version,
  idempotency and budget checks remain in place; preserved Quick Chat source was
  not merged or overwritten. Full B1 completion remains separately reviewed.
- New attempts freeze `wangp-worker` and `engine_manifest_digest`. Physical GPU
  capacity, endpoint exclusivity and retirement-race guards cover both engines.
  Old Comfy spec hashes and fleet v1 fingerprints remain compatible.
- A legacy slot can reconcile/collect after the default engine changes only with
  both `recovery_only` and an explicit `SIXNINE_RECOVERY_BACKENDS` entry. It cannot
  register replacement capacity, claim new work or submit another generation.
- The GPU host commits an operation intent before entering Session, holds an
  exclusive slot lock and retains unknown outcomes across restarts. Missing
  in-memory jobs, HTTP errors and empty runtime state do not authorize resubmission.
- Private HTTP requires a process-only token and a loopback SSH tunnel endpoint.
  Input bytes and output bytes are bounded and hashed; normalized PNG inputs get
  verified `.png` materializations because upstream rejects extensionless blobs.
- Cancellation acknowledgement is not stop evidence. The facade checks the
  pinned worker thread and CUDA quiescence before a terminal result. Shutdown
  waits for the Session and releases ownership only after idle close; timeout
  terminates the whole host process while retaining the journal for reconciliation.
- Video and original generated stereo audio are separately sealed and collected.
  The existing worker performs the explicit requested-duration/CRF export and
  publishes validated MP4 and FLAC artifacts. Failed collection reuses the output.

## Narrow initial control envelope

Pinned upstream: `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`.
The first implemented profile is Base FL2VA: BF16 transformer and text encoder,
50 steps, Euler, SDPA, memory profile 4, text plus optional first/last images.
It exposes the supported duration, resolution, aspect and seed choices. Other
controls are explicitly fixed or rejected; no silent Comfy-to-WanGP translation.

This is not full H3 feature parity. REF inputs, arbitrary decoder tile controls,
CPU encoder selection and accelerated/quantized models are not qualified here.
The source-backed mapping, component revision/hash/size manifest and exclusions
are in [`deploy/wangp/README.md`](../deploy/wangp/README.md).

The component manifest totals 124,300,443,428 bytes. This is download/storage
size, not required VRAM, measured RAM, speed or a successful GPU configuration.
`runtime-recipe.json` is explicitly a candidate, not a complete installed
dependency lock or qualified image. Its `runtime_digest` identifies upstream
requirements source, not an OCI image. Transitive wheel hashes, image digest,
driver compatibility and real host headroom remain D2 requirements.
The existing integer-duration export can cut the native ending (5 seconds is
120 delivered frames versus 124 native frames). Last-frame conditioning is
mapped but its visibility in the delivered ending is not yet qualified. Use
text-to-video for the first B3 proof and resolve this export contract before
claiming last-frame fidelity.

## Verification and review

- 172 affected API, admission, execution-policy, fleet, capacity, Comfy compatibility,
  WanGP receipt/host/transport and release-contract tests passed locally.
- Follow-up compiler/Session/launcher/API checks passed after integration fixes.
  HTTP and launcher checks include the typed-input and process-shutdown fixes.
- Final saved-draft review found and fixed empty guide-region rejection and
  silent omission of explicit controls unsupported by the selected engine.
  20 draft/API regression tests passed; incompatible drafts remain unchanged.
- A CPU-media integration test exercises HTTP plan/confirm/replay, the existing
  queue, injected WanGP Session, immutable receipts, artifact validation/download
  and cross-owner denial. It submits once and retrieves both video and audio.
  This is synthetic CPU evidence, not real H3 inference or quality evidence.
- The CI PostgreSQL job also runs the new engine-routing and API integration
  tests against its isolated test schema. Exact CI results belong to this PR's
  check receipts; no local SQLite pass is represented as PostgreSQL evidence.
- Independent agents reviewed admission/routing, transport uncertainty, token
  files, startup identity and shutdown ownership. Findings were addressed before
  the source checkpoint; the unresolved runtime/cold-start work below remains open.

## Production observation and release boundary

GET-only observation at `2026-10-07T00:23:35Z`: public health/capabilities returned
200; authentication ready; generation/backend/render disabled; Lium inventory
empty and the recorded prior pod absent. Official AWS MCP also confirmed the
known Singapore CPU instance running and SSM online. This batch made no cloud
mutation, runtime installation, model download, paid generation or deployment.

Supplier billing was not freshly settled. The existing GPU budget account is
`demand-gpu-remaining-20261004`; its historical balances and overlapping aggregate
account counters are not today's remaining cash. Keep prior reservations and the
expired operating-window record. Do not recreate a fresh US$50 account.

## Required next integration before B3 public proof

WanGP works through an explicitly configured fleet slot in code. The old on-demand
controller still has Comfy-specific approval, boot and recovery boundaries. D2 is
therefore **not complete**, and this checkpoint must not enable public generation.

1. Extend the existing capacity approval and finite/on-demand configuration with
   explicit engine/manifest binding, preserving old payload fingerprints. Relevant
   modules: `capacity.py`, `production_scaler.py`, `on_demand_scaler.py` and the
   cold-admission branch of `execution_policy.py`.
2. Add one engine boot strategy inside the existing `BootController` lifecycle:
   `lium_bootstrap.py` and `production_scaler_boot.py`. Reuse rental intents,
   provider reconciliation, leader leases and the same budget account. Do not
   create another controller or rerun setup to recover an uncertain existing host.
3. Bind the queued-user-task qualification and protected deployment package to
   the new engine: `queued_task_runner.py`, `qualification_profiles.py` and
   `deploy/platform/gpu_scaler.py`. Keep FL-only evidence distinct from REF.
4. Build/lock the real runtime image, verify source/model/config identity on the
   chosen GPU, reconcile current supplier obligations and establish the bounded
   operating configuration under the user's existing cumulative ceiling.
5. B3 then proves a real public request through cold start, warm reuse, original
   result download and idle shutdown/restart. Only that receipt can establish
   current public generation availability. Broader controls and dual-node
   redundancy remain separate gates.

Cold-start regression must include old approval compatibility, wrong-engine
denial, one rental per original demand, lost-start response reconciliation,
missing-journal quarantine, collection before destruction and unknown destruction
without replacement rental. Existing Comfy cold approvals intentionally cannot
authorize WanGP until these extensions are accepted.

## Follow-up ownership (2026-10-08)

| Remaining outcome | Authoritative task / acceptance boundary |
|---|---|
| Shared plan/access/read service completion and Quick Chat compatibility | [B1 #12](https://github.com/apedintensor/h3-studio/issues/12); existing preflight/confirmation/enqueue are partial implementation |
| Full runtime lock, engine-bound cold approvals, bootstrap/readiness/reconnect and real packaging | [D2 #22](https://github.com/apedintensor/h3-studio/issues/22); no production activation from this report |
| Continuing operating configuration | [C1 #14](https://github.com/apedintensor/h3-studio/issues/14) |
| Offline cold-start uncertainty, duplicate prevention and collection-before-destruction cases | [C2 #15](https://github.com/apedintensor/h3-studio/issues/15), coordinated with D2 |
| Real public cold/warm/idle-restart/download proof | [B3 #16](https://github.com/apedintensor/h3-studio/issues/16) |
| Native ending versus delivered duration and last-frame fidelity | [D3 #27](https://github.com/apedintensor/h3-studio/issues/27); known trimming behavior, actual H3 visual impact unverified |
| Historical pending reservations and supplier statement reconciliation | [C3 #28](https://github.com/apedintensor/h3-studio/issues/28); historical counts are not current cash or duplicate-charge proof |
| REF image/video/audio inputs and additional supported controls | [D4 #29](https://github.com/apedintensor/h3-studio/issues/29); not part of initial FL2VA acceptance |
| Broader variants/performance and dual-node redundancy | [D #4](https://github.com/apedintensor/h3-studio/issues/4) and [E #5](https://github.com/apedintensor/h3-studio/issues/5); separate recipes and evidence required |

Current criterion results and ownership belong to those issues. Future batch
handoffs belong in their PRs/issues rather than another competing status report.

# Generation foundation batch: A1, A2 and B2

Date: 2026-10-06 (Australia/Sydney). Integration session: `01a10a97-d9d9-7162-adfa-b07a7a2d0a50`.
Issues: [A1 #10](https://github.com/apedintensor/h3-studio/issues/10), [A2 #11](https://github.com/apedintensor/h3-studio/issues/11), [B2 #13](https://github.com/apedintensor/h3-studio/issues/13).

## Changes

- Recorded a fresh, read-only production/source baseline in [CURRENT-BASELINE.md](CURRENT-BASELINE.md). Website health is distinct from generation availability: generation is disabled and the finite controller has exited; obligations remain recorded.
- Froze the existing API, identity, immutable admission, unknown/recovery and artifact contracts in [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md), distinguishing local unpublished Quick Chat from deployed behavior.
- Extracted the current Comfy implementation into `studio_platform/inference/comfy.py`, shared exceptions/result/protocol into `protocol.py`, and output requirement helpers into `outputs.py`.
- Kept compatibility exports in `worker.py`. Worker execution, fencing, cancellation, reconciliation, collection and accounting logic remain unchanged.
- Fleet readiness now invokes the adapter's `is_idle()`; only literal `True` permits readiness. Malformed, busy or failed evidence cannot start the store/runner.
- Included the new package in the exact Git/Docker allowlists; existing release fingerprints recursively include it.
- Aligned the roadmap with the selected WanGP runtime. This batch does not add a runtime enum, change new-job routing, migrate accepted jobs or replace the frontend.

## Verification

- Independent static review compared the prior worker AST with extracted Comfy methods: only preparation delegates its existing idle check; other Comfy methods are unchanged. WorkerRunner method bodies, mock and disabled backends are unchanged.
- Focused shared-checkout tests: 48 passed (`test_platform_inference`, `test_platform_fleet`, `test_platform_worker`), isolated SQLite, fake HTTP and synthetic CPU media.
- Exact selective-tree regression: **259 tests passed in 48.634 seconds**, isolated SQLite, fake engine HTTP and synthetic CPU media. Suites cover inference, worker, fleet, repository, queue, control, drain-safe runner, artifact writer, render backend, external-provider compatibility, queued-task runner/hold, execution policy, diagnostics and release fingerprints. The tested source tree is `589c358d560161f8a950b6fd3ab8252fdb5d2b4e`; the final receipt update changes documentation only. This verifies the selected batch without the unrelated Quick Chat changes.
- No PostgreSQL live race check, GPU inference, model installation, production release or new cloud resource occurred. AWS/provider reads were limited to A1 and are separately documented.

## Remaining gates and handoff

1. B1 #12: reconcile and complete the existing local `GenerationAdmission` extraction with the Quick Chat owner; do not duplicate it.
2. D #4: pin a full WanGP runtime/model manifest and a control mapping; implement a private headless adapter with durable attempt receipts. Upstream in-memory task disappearance cannot cause resubmission.
3. Before adding the backend: preserve per-attempt engine/config binding during mixed-engine recovery, generalize physical GPU capacity/endpoint guards, and remove global selection as a recovery blocker.
4. C1 #14 / C2 #15: separate continuing service policy from finite acceptance windows and prove lost-response/restart/cancel/collection behavior without duplicate side effects.
5. B3 #16 / D: qualify real outputs and public downloads under a current operating window, exact release and remaining-budget checks. The prior window is expired; this batch does not renew it.

Deployment classification: backend/worker compatibility changes, **Not released**. The new source fingerprint means this must follow the protected backend release process; it cannot be shipped as a frontend-only update. Preserve accepted jobs, billing reservations, historical Comfy recovery bindings and rollback evidence.

Unrelated local Quick Chat/UI/config changes are intentionally excluded from this batch's Git tree. New sessions should use the plan/workflow and linked issues, not assume the shared working directory equals the released source.

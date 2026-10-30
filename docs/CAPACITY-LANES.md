# Capacity lanes: pods and serverless behind one router

Owner approved, 2026-10-30. This doc records the target and the migration order; each step is its own PR.

## Today

- **Queue.** The Postgres job queue (DBOS) is the only source of truth. Keep it.
- **Pods.** `dstack` (self-hosted, 0.22.5) rents whole machines on Vast. Lium, RunPod pods and CloudRift arrive with #836, and AWS g7e with #837. Workers pull jobs. Reconciliation reads every provider account.
- **Modal.** `modal_*` (~3,000 lines, 14 modules) runs Modal **sandboxes** as long-lived hosts that also pull jobs. That makes it a second, hand-written pod lane, with its own budget, admission, host and lifecycle code.
- **Enhance.** RunPod Serverless (#795) is a standalone test. It is not connected to the router.
- **Routing rules are spread across several places:**
  - `modal_admission.start_after_seconds`;
  - `dstack_demand_start.vast_share` (`vast_min_queue`, `vast_after_seconds`);
  - `capacity_lookahead.Policy` (boot time, job time, minimum jobs).

## Target

```
queue ──> router (one rules table) ──┬─> pod lane        (dstack: Vast, Lium, RunPod pods, CloudRift, AWS)
                                     └─> serverless lane (adapters: Modal functions, RunPod Serverless)
                     both lanes run the same worker image; outputs go to R2; one cost/telemetry record per job
```

**Pod lane.** Unchanged in concept: dstack rents, workers pull, the account sweep and idle stop stay.

**Serverless lane.** Its purpose is a **wait-time guarantee**: when no warm pod is free and a pod boot (5–10 min) would break the per-user wait limit, the router hands the job to a serverless platform. Those platforms boot in about 2 min and scale out in parallel.

Serverless workers are **not one container per job**:
- the first job pays boot plus model load;
- the worker then stays warm with the model in VRAM and runs the following jobs back to back;
- the platform scales it to zero only after an idle timeout (5–10 min).

Modal sandboxes already behave this way (they pull jobs until idle). On RunPod Serverless, one worker process handles sequential calls with the WanGP session loaded once. "Submit" means "hand this job to the lane", not "start a container for it".
- One small interface per platform:
  - `submit(job, input_urls, output_put_url) -> call_id`
  - `poll(call_id) -> queued | running | done(result) | failed(code)`
  - `cancel(call_id)`
  - `price(call)` (seconds × rate)
- The job contract is the same as the pod worker's: signed input URLs in, one presigned R2 PUT out, plus a receipt (seconds, GPU, attention, bytes).
- The idempotency key is the job attempt ID. A lost submit reply is reconciled by listing calls by that key, never by resubmitting blindly.

**Router.** One pure function over a snapshot (queued jobs and ages, warm idle pods, booting pods, serverless calls in flight, prices). It returns `{pod_starts, serverless_submits}`.
- Today's rules become one table:
  - a warm idle pod takes the job;
  - otherwise serverless first, so there is no cold-start wait;
  - when the lookahead says enough work remains for a pod to pay off, start pods (the #688 lookahead);
  - when a job has waited past the limit, take any lane that has capacity.
- Hard gates (budget, account, provider limits) stay where they are, and the router never bypasses them.

**One worker image, three entry points:** the dstack task, the Modal function and the RunPod Serverless handler. They share the WanGP session code and attention/device resolution (#839).

## Migration order (each step keeps production working)

1. **Serverless lane interface plus RunPod Serverless for enhance.**
   - Enhance is new, so nothing existing can break.
   - Add `studio_platform/serverless_lane.py` (interface, call ledger rows, idempotent submit/poll/cancel) and a RunPod adapter. Route enhance jobs through it.
   - Accept on staging with frame checks.
2. **Generation handler in the serverless image.** The same worker entry runs one generation call (input URLs → R2), behind the interface. Accept a v8 FL/REF run on RunPod Serverless (5090/PRO 6000).
3. **Modal as a serverless adapter.**
   - Re-implement the Modal backstop as Modal **functions** behind the same interface.
   - Run both paths on staging, then retire the sandbox-host modules (`modal_host`, `modal_lifecycle`, `modal_worker_binding`, `modal_capacity` …) once production has been on the function path for a week.
4. **Router consolidation.**
   - Move `start_after_seconds`, `vast_share` and the lookahead knobs into one rules table and one pure function, with table-driven tests that reproduce today's decisions before changing any of them.
   - Steps 1–3 already use it for serverless submits.

Money paths (submit, budget and settlement of serverless calls) are Tier 2 and get one Codex review per step.

## Open-source options considered (2026-10-30)

| Project | What it does | Why not, as our router |
|---|---|---|
| dstack (already used) | Multi-provider pod provisioning, fleets, offers | Already our pod lane. It does not route to Modal/RunPod Serverless, and its service autoscaling scales HTTP replicas by request rate, not our pull queue. |
| SkyPilot (~10.8k★) | Multi-cloud jobs/serving, spot recovery, failover | Same layer as dstack, and a weaker fit for Vast/Lium. Switching gains nothing at this layer. |
| KEDA (~10.7k★), Kueue (~3.1k★) | Queue-length autoscaling and job queueing | Kubernetes-only. We run no cluster, and rented GPUs are not k8s nodes. |
| Modal / RunPod SDKs | Per-call execution with platform autoscaling | Used *inside* the serverless adapters, not as the router. |

**Decision.** The heavy parts stay open source:
- provisioning: dstack;
- queue and workflows: Postgres/DBOS;
- execution: the platform SDKs.

The router itself is our own small pure function. It encodes business policy: credits, budgets, provider price ceilings, and which lane pays off for a given queue. Existing tools don't cover that across pods plus serverless.

We adopt the KEDA pattern (scale from queue length with a lookahead) and SkyPilot's ordered failover idea, without the dependency.

## Not changing

- The queue/DBOS ownership.
- R2 delivery.
- The pod lane's reconciliation and idle stop.
- The prices users pay.
- dstack self-hosted vs Sky is independent (staging trial 2026-10-30) and affects only the pod lane's control plane.

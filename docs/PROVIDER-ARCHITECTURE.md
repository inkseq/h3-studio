# Provider architecture: one scheduler, two execution kinds, one capacity interface

Status: proposal for owner review, 2026-11-01. It extends [CAPACITY-LANES.md](CAPACITY-LANES.md) (owner approved 2026-10-30) with what the dial-out work (#859) and the hosted-API backstop (#918) have since shown. Nothing here changes production until its step lands.

## Why now

Each lane was added as its own slice. The cost is now visible:

- **Adding a provider touches ~10 files.** `modal-dialout` (#928) had to be added to hard-coded sets in `capacity_settings`, `control`, `generation_diagnostics`, `modal_admission`, `operator_billing`, `repository` (twice), `stage_metrics`, `telemetry`, `dstack_inventory`, `dstack_operator` and `dstack_runtime`. EveryFrame (#919) repeats the pattern in `control`, `dbos_transport`, `fleet` and `execution_policy`. 28 modules name a provider literally.
- **Scheduling rules live in four places:**
  - `dstack_demand_start` (profiles, `vast_share`);
  - `modal_admission.start_after_seconds`;
  - `capacity_lookahead`;
  - EveryFrame's always-on CPU slots.

  None of them sees the others' options, so "which lane serves this job" has no single answer.
- **Failure handling is global.** Two failed Modal starts tripped the demand safety brake and stopped *all* automatic starts, though Vast and Lium were healthy. A job also stays bound to one dead Modal attempt.
- **Two generations of each lane coexist:**
  - the old Modal lane (14 modules, 2,892 lines, its own admission, budget, host, lifecycle and exec transport) next to `modal_dialout` (180 lines);
  - SSH transport next to dial-out.

## Three layers

```
                ┌────────────────────────── scheduler (pure decide + executor) ──────────────────────────┐
 queue (DBOS) ─►│ snapshot: queued jobs, warm slots, booting machines, lane health, prices, budgets       │
                │ decide(): place on warm slot | start machine on lane L | offload to hosted API H | wait │
                └───────────────┬───────────────────────────────────────────────┬────────────────────────┘
                                │                                               │
                 execution kind A: our worker pool                execution kind B: hosted per-job API
                 (WanGP on a GPU we rent; worker pulls jobs)      (vendor runs the render; we submit once)
                                │                                               │
                 capacity providers (one interface)               hosted backends (InferenceBackend)
                 dstack: Vast · RunPod pods · Lium · CloudRift    EveryFrame · RunPod enhance · Boyesir · later fal/…
                 RunPod Serverless · Modal sandbox
                                │
                 worker link (shared): boot exchange · chisel dial-out · boot status · model cache
```

**Kind A, worker pool.** The worker runs our image and pulls jobs. Machines come from a capacity provider. Billing is per second of machine time, whether or not it is working. The money risk is an idle or orphaned machine.

**Kind B, hosted per-job API.** The vendor runs the job. It is billed per render at submit time, and the vendor refunds failures itself. The money risk is a double submit. The rule: never resubmit on an uncertain answer, reconcile instead (already in `everyframe_backend`).

The two kinds stay separate. They have different money semantics, and merging them into one abstraction would hide the difference that matters. They share the scheduler, the registry, cost records and telemetry.

## Shared components

| Component | Today | Target |
|---|---|---|
| **Provider registry** | literal sets in 28 modules | One table, `providers/registry.py`: id, kind (`pool` / `hosted` / `cpu`), billing name, capabilities, inventory reader, adapter factory. Every current set (`REAL_GPU_BACKENDS`, `LOCAL_BACKENDS`, the `{"vast","runpod",…}` sets, `PROVIDERS` maps, telemetry labels) is derived from it. Adding a provider means one adapter file plus one row. |
| **Capacity provider interface** | `RunPodServerlessClient` / `ModalDialoutClient` imitate dstack's run API; `RoutedClient` dispatches by `backends[0]` | Our own small interface (below). dstack becomes one implementation, not the shape every other lane has to imitate. |
| **Create-only journal** | inside `RunPodServerlessClient` (Modal subclasses it) | `providers/journal.py`, used by every non-dstack capacity provider and by hosted backends for their attempt records. |
| **Worker link** | `worker_boot`, chisel sidecar, boot script | Unchanged and shared by every kind-A lane. SSH transport is deleted after production switches (WORKER-DIALOUT step 5). |
| **Worker image and model cache** | one image; `INKSEQ_MODEL_CACHE` (#924) | One image. The cache source is a capability of the lane: Modal Volume, RunPod network volume, or none (download). |
| **Ownership and orphan sweep** | per-provider readers in `dstack_inventory`; Vast read in `dstack_capacity` | One rule borrowed from dstack's `provider_ref`: every resource we create carries `inkseq-<env>-<intent>` in its name or tag. `inventory()` per provider returns `(id, name, labels)`. One sweep compares against intents and alarms or terminates unknown resources. |
| **Lane health** | global demand safety brake | Per-lane backoff (below). The global brake stays only as the last resort for a broken controller. |
| **Cost records** | `operator_billing` per provider name | One cost row per machine-second (kind A) or per vendor render (kind B), with the price from the registry or offer. |
| **Budgets and spend gates** | existing ledger transactions | Unchanged. The scheduler never bypasses them; the executor calls the same admission transactions. |

### Capacity provider interface

The provision API in SkyPilot (`run_instances` / `query_instances` / `terminate_instances` / `get_cluster_info`) and dstack's `Compute` (`get_offers` / `run_job` / `terminate_instance`, with `ComputeWith*` capability mixins) converge on the same small surface. Ours:

```python
class CapacityProvider(Protocol):
    id: str                                   # registry id, e.g. "vastai", "modal-sandbox"
    capabilities: Capabilities                # model_cache, per_second_billing, max_lifetime_s, regions, gpu_names
    def offers(self, req: MachineRequest) -> list[Offer]: ...         # read-only; price, gpu, est_boot_s
    def launch(self, name: str, offer: Offer, boot: BootEnv) -> Launch: ...  # once; never retried on unknown
    def status(self, name: str) -> MachineStatus: ...                 # starting | running | exited(code) | gone
    def terminate(self, name: str) -> None: ...                       # idempotent
    def inventory(self) -> list[Resource]: ...                        # complete or raise; feeds the orphan sweep
```

- `BootEnv` is the three dial-out variables, nothing else. That is the same rule the RunPod handler already enforces.
- dstack-backed providers implement this by building today's run spec internally. The dstack run spec stops being the cross-lane language.

### Hosted backend base

`InferenceBackend` (prepare / submit / reconcile / cancel / fetch) stays the contract. Shared helpers move into `providers/hosted.py`:

- credentials file loading;
- presigned media staging;
- refused (4xx: no charge, no retry) vs uncertain (reconcile only);
- attempt journal keyed by attempt id;
- per-render price lookup;
- prompt suffixes such as the EveryFrame no-cut clause (#918).

`enhance_backend` and `everyframe_backend` already follow this shape and become thin adapters.

## Scheduling

The owner asked for this part to be designed carefully, so it gets the most detail.

### Goal, in order

1. **Wait limit.** No job waits longer than its tier's wait limit (Fast draft ~30 s, as #918 sets for cold start).
2. **Cost.** Within that, choose the cheapest option.
3. **One executor per job.** Never two attempts in flight. Never an attempt bound to a dead lane.
4. **Fail by lane.** A failing lane degrades only itself.

### Options the scheduler weighs for a queued job

| Option | Expected start | Cost to us |
|---|---|---|
| Warm idle slot (any lane) | ~0 s; the worker pulls it | marginal; the machine is already paid |
| Machine already booting | its remaining ETA | already committed |
| New machine on lane L | `boot_p50(L)` + model load (cache hit: seconds; miss: ~45 GB download) | hourly × (boot + work + idle tail) |
| Hosted API H | `submit_p50(H)` (EveryFrame 14–37 s to video) | vendor price per render (#918 table) |

`boot_p50` and `submit_p50` come from our own recent records per lane. The staging rounds so far:

- RunPod pods: 13 min.
- Lium: 23 min.
- Vast: 33 min.
- RunPod Serverless: 18 min without a cache.

These are what the scheduler reads. Nothing is hard-coded.

### Decision per controller tick: `decide(snapshot, rules) -> decisions`

`decide` is a pure function. It is table-driven and tested with recorded snapshots, the shape CAPACITY-LANES step 4 already asked for.

1. **Hard filters per job.** For each (job, option), drop the option if:
   - it lacks a capability the job needs (runtime profile v9, mode FL/REF, resolution, audio, input kind);
   - it fails the content constraints in the registry (from #929: user photos never go to Seedance; IP characters never to Seedance or Kling);
   - its lane is in backoff;
   - its price is above the profile's `max_price`;
   - the budget can't cover it.
2. **Warm first.** Jobs that a warm or booting slot can start within the wait limit stay queued for it. No action is needed: workers pull.
3. **Offload for the wait limit.** For each remaining job, in queue order, offload to the cheapest hosted option, but only when all three hold:
   - the job has no attempt;
   - no warm or booting slot can take it within the wait limit;
   - the tier is enabled for hosted routing (owner switch per tier and per environment).

   This replaces EveryFrame's always-on slots: offload becomes a decision, not a standing pool.
4. **Start machines for the backlog.** Start machines only when the lookahead says the backlog still pays for a machine after its boot (today's #688 lookahead, unchanged in meaning). Pick the lane with an expander chain, the cluster-autoscaler idea:
   - owner priority order;
   - then the lowest expected cost per job;
   - then the shortest boot;
   - then random.
5. **Replace stuck starts.** A machine that does not check in within `max_provision_s(L)` (default 20 min, like cluster-autoscaler's 15 min) is terminated. Its lane goes into backoff, and the next tick may pick another lane. A job is never bound to a machine; it stays queued until a worker claims it. That removes "one Modal attempt per job".
6. **Idle stop** per lane:
   - pods keep a warm tail (`keep_warm_s`);
   - Modal and serverless stop after their idle window;
   - the existing idle proof decides.

### Lane health and backoff

Per lane, keyed by registry id plus runtime profile:

- each failed start doubles a backoff, from 5 min up to 30 min, reset after 3 h without failures (cluster-autoscaler's defaults);
- a stuck start counts as a failure.

Hosted APIs use the same structure with a LiteLLM-style cooldown:

- a generic vendor `failed` puts that model into cooldown for this input kind and lets the job go elsewhere;
- this follows #929: a generic failure needs a different model or a stop, not a blind retry.

The global demand brake remains only for "the controller itself keeps failing".

### One executor per job

The rules:

- A job has at most one live attempt. A worker claim and a hosted submit both go through the existing attempt fence. Whoever writes the attempt first wins, and the loser does nothing.
- If a machine becomes ready after a job was offloaded, it takes the next job. The offloaded job is never pulled back.
- Hosted uncertainty is resolved by `reconcile` on the same attempt, never by a second submit. Machine uncertainty is resolved by `status`/`inventory`, never by a second launch.

### Rules table (operator config, audited)

```json
{"tiers": {"fast_draft": {"wait_limit_s": 30, "hosted": ["everyframe:h3-max-turbo-480"]},
           "standard":   {"wait_limit_s": 60, "hosted": ["everyframe:h3-max-480"]},
           "refine":     {"wait_limit_s": 120, "hosted": ["everyframe:h3-max-768"]}},
 "lanes": {"priority": ["vastai", "lium", "runpod", "cloudrift", "runpod-serverless", "modal-sandbox"],
           "max_provision_s": 1200, "backoff": {"initial_s": 300, "max_s": 1800, "reset_s": 10800}},
 "lookahead": {"version": 1}}
```

This absorbs:

- `modal_admission.start_after_seconds`;
- `dstack_demand_start.vast_share`;
- the profile `fallback_backends`.

Each moves into the table with a test that reproduces today's decision before any behaviour changes.

## Separate by design

- **Provider adapters.** One file per provider under `providers/`. Only these files know a provider's HTTP API or SDK, error codes and naming limits. No provider name appears outside `providers/` except in config.
- **Credentials.** One protected file per provider, read by its adapter only. Same as today.
- **The money paths of the two kinds.** Kind A reserves machine budget. Kind B reserves per-render cost. They share the ledger, not the code path.
- **Production and staging.** Same code, different rules tables and registry enablement.
- **Legacy, to delete rather than unify:**
  - the old Modal lane (`modal_host`, `modal_lifecycle`, `modal_worker_binding`, `modal_capacity`, `modal_admission`, `modal_budget`, `modal_dispatch`, `modal_transport`, `modal_runtime`, `modal_entry`, `modal_launch`, `modal_client`, `modal_controller`, `modal_credentials`), after `modal-sandbox` has run production for a week;
  - SSH transport and host-key pinning, after production dial-out.

## Research before building (cannot be unified blindly)

| Item | Question | Why it matters |
|---|---|---|
| RunPod network volume | Does pinning an endpoint to a volume's data center cut PRO 6000 / 5090 availability too much? | Cache vs availability for serverless cold start |
| Vast model cache | Vast volumes or a pre-seeded host disk: is there a supported way to keep 45 GB across rentals? | Vast is the primary lane and the slowest boot (33 min) |
| Modal | Sandbox max lifetime (24 h), Volume read throughput for 45 GB, RTX PRO 6000 stock by region | Modal is the backstop; its boot time sets the wait limit we can promise |
| EveryFrame | Rate limits, concurrency, SLA, account balance API, refund timing, and how a vendor outage appears | Offload must detect "vendor down" quickly and fall back |
| Hosted content constraints | Keep the #929 matrix current as vendors change moderation | The hard filter is only as good as this table |
| CloudRift | No labels in its listing; Blackwell stock | Orphan sweep must match by id only |
| AWS g7e (#837) | On-demand vs capacity blocks; image pull time from ECR in-region | Possible reliable floor lane |

Each row becomes its own Backlog card, and its findings go into `docs/EXPERIMENTS.md`.

## Target code layout

```
studio_platform/providers/
    registry.py          # the one table; derived sets for legacy callers
    journal.py           # create-only journal
    capacity.py          # CapacityProvider, Offer, MachineStatus, Capabilities
    hosted.py            # hosted backend helpers
    dstack_lanes.py      # vastai · runpod · lium · cloudrift through dstack
    runpod_serverless.py
    modal_sandbox.py     # today's modal_dialout
    everyframe.py · runpod_enhance.py · boyesir.py
studio_platform/scheduling/
    snapshot.py          # read-only state build
    decide.py            # pure rules → decisions
    health.py            # backoff / cooldown
    executor.py          # applies decisions through the existing ledger transactions
studio_platform/worker_link/   # worker_boot, tunnel authfile, boot status
```

The dstack ledger modules (`dstack_capacity`, `dstack_operator`, `dstack_controller`, …) keep their ledger role. They lose provider knowledge to `providers/` and lose start decisions to `scheduling/`. This feeds the size work in #899 rather than competing with it.

## Migration order (one PR each, staging first, production unchanged until named)

1. **Registry.** Replace the literal provider sets with registry-derived ones. Zero behaviour change, checked by the existing tests.
2. **Per-lane backoff.** Replace the global brake for start failures. Stuck starts are replaced on another lane.
3. **`decide()` extraction.** Move `start_after_seconds`, `vast_share`, the lookahead and the fallback lists into the rules table. Add table-driven tests that reproduce today's decisions first.
4. **Capacity interface.** Formalise `CapacityProvider` and move the serverless and Modal clients onto it. `RoutedClient` becomes a registry lookup.
5. **Hosted offload.** EveryFrame becomes a scheduler option (#918 cold-start backstop) instead of standing slots. #919 is adapted to this before merging. Owner decisions still open on #918: face photos, and the look difference.
6. **Production dial-out.** Then delete SSH transport.
7. **Production on `modal-sandbox`.** After a week, delete the old Modal lane.

## Open-source references (ideas only, no code copied)

| Project | License | Taken | Left |
|---|---|---|---|
| SkyPilot `sky/provision` | Apache-2.0 | Small per-provider lifecycle surface; provider dispatch by name; failover to the next cloud | Its cluster and SSH model (we run single-GPU dial-out workers) |
| dstack `backends/base/compute.py` | MPL-2.0 | Capability mixins instead of per-provider `if`s; `provider_ref` in names or tags for orphan cleanup; cached offers | Its run spec as our cross-lane language |
| Kubernetes cluster-autoscaler | Apache-2.0 | Expander chain (priority → least-waste/price → random); node-group backoff 5 m → 30 m, reset 3 h; `max-node-provision-time` | Pod-scheduling simulation (our jobs are one GPU each) |
| LiteLLM Router | MIT | Per-deployment cooldown on failures; ordered fallbacks; per-error retry policy; per-deployment limits | Request-level retries on the same vendor (refunded generic failures need another model, #929) |
| GitHub Actions JIT runners / Buildkite agents | MIT | Already used for dial-out: one-time registration token, worker pulls work | — |

# Provider architecture: one scheduler, two execution kinds, one capacity interface

Status: owner approved 2026-11-01 ("能"). It extends [CAPACITY-LANES.md](CAPACITY-LANES.md) (owner approved 2026-10-30) with what the dial-out work (#859) and the hosted-API backstop (#918) have since shown. Nothing here changes production until its step lands.

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
                 dstack: Vast · RunPod pods · Lium · CloudRift    EveryFrame · RunPod enhance · later fal/…
                 RunPod Serverless · Modal sandbox
                                │
                 worker link (shared): boot exchange · chisel dial-out · boot status · model cache
```

**Kind A, worker pool.** The worker runs our image and pulls jobs. Machines come from a capacity provider. Billing is per second of machine time, whether or not it is working. The money risk is an idle or orphaned machine.

**Kind B, hosted per-job API.** The vendor runs the job. It is billed per render at submit time, and the vendor refunds failures itself. The money risk is a double submit. The rule: never resubmit on an uncertain answer, reconcile instead (already in `providers/everyframe`).

The two kinds stay separate. They have different money semantics, and merging them into one abstraction would hide the difference that matters. They share the scheduler, the registry, cost records and telemetry.

## The generation gateway (owner question, 2026-11-01)

The owner asked whether a layer belongs between the frontend and model calls. It does, and most of its parts already exist, spread out:

- `quality_tiers`;
- `credits_pricing`;
- `execution_policy`;
- the queue;
- `InferenceBackend`.

The gateway names them as one layer with one contract. The frontend and Inky talk only to it, in tiers and shots. Nothing above it names a model.

| Part | Today | Gateway |
|---|---|---|
| Shot spec | `job.request` carries H3-specific fields (model, steps, seed, attention) | A route-neutral **shot spec**: prompt, lines with speakers, character pictures, first/last frame, audio, duration, aspect, resolution, tier. Each route compiles it itself: WanGP compiler, EveryFrame compiler (no-cut clause, `<Picture N>`, quoted lines), image compiler |
| Checks | Spread over preflight and policy | **Hard checks** in the gateway for every job (see below) |
| Quote and credits | `credits_pricing` by tier | Unchanged. Price depends on tier and length, never on route |
| Queue | One DBOS queue | One queue with priority classes `interactive` / `economy`. No second queue system; vendor limits are limiters in front of the adapters |
| Route | **Bound when the plan is created**: `execution_plan.backend/pool/configuration_id`, and a worker claims only an exact match | **Late binding**: a job carries its tier and shot spec, and `decide()` picks the route at dispatch (own fleet, hosted chain, economy chain) |
| Execution | `InferenceBackend` per backend | Unchanged contract. Route adapters live in `providers/` |
| Output | Per backend | One normalisation and delivery step: size, fps and duration checks, audio policy, store, one cost row per take |

**Late binding is the one structural change.** Today the route is frozen into the plan. #919 had to decide "hosted or not" when the job was created, from a snapshot that is stale by dispatch time. A job instead keeps its tier and shot spec, and the scheduler binds the route when it is about to run. Credits stay with the tier, so a late route change never changes what the user pays.

**How: rebind, not a new claim model.** Workers today claim jobs whose `execution_plan` backend, pool and configuration match their slot exactly (`control._claim_filter`). That stays. Instead:

1. **Choose at dispatch, from the catalog.** Inside the demand start's capacity-locked transaction, `scheduling/rebind.offload` takes the jobs that `eligible_jobs` has already confirmed as queued and unsubmitted. A job that has waited past its tier's limit (`decide.hosted_offload`) goes to the first model in `hosted_models.chain()` that has a live slot.
2. **Rebind.** It rewrites `execution_plan` and `jobs.pool`, bumps the fence, and emits `capacity.hosted_route`. The update only applies while the job is `queued`, has no lease and has the fence that was read. The original plan is kept as `primary_plan`. A submitted hosted attempt is never pulled back.
3. **Money.**
   - Route fields (`dispatch_backend`, `output_delivery`, the manifest digest) go.
   - The frozen `credit_quote`, the budget accounts and the spending snapshot stay.
   - The USD reservation is only an admission gate: `_settle` records the actual vendor cost whatever was reserved, so a rebind doesn't touch it.
4. **Switches.** Nothing moves unless the credential is installed (`everyframe_enabled`) and `runtime-secrets/everyframe-routing.json` lists the tier and this environment's share of the vendor window, e.g. `{"tiers": ["fast"], "per_window": 5}`. Staging and production share one vendor key, balance and limit (owner 2026-11-01), so staging keeps a small window, and spend is attributed from each take's recorded cost.
5. **Unchanged.** Claims, fences, settlement and jobs that never wait past the limit behave exactly as today. DBOS `turn` re-reads `job["pool"]` each turn, so a rebound job reaches the hosted slot with no new wake.

**Not yet:** a job with an explicit seed should stay on our fleet, but the compiler fills a seed for every job, so explicit seeds can't be told apart yet. Creators no longer set seeds; this only matters for API jobs.

**Inky is beside the gateway, not in front of it.**

- Inky judges and edits cards: the Jev verdict, fix-all and suggestions. It never generates.
- A user's click goes straight to the gateway.
- Shots Inky fixed go through the same gateway as any click, so "Inky advises, the user decides" can't be bypassed.

### Two kinds of checks

| | Hard checks | Jev verdict |
|---|---|---|
| Where | Gateway, every job, no model call | Inky, precomputed when a card is saved |
| What | Required input missing for the mode, model limits (duration, aspect, reference count), brand/IP lint, credits | Vague action, speech without written lines, recurring person without a picture |
| Clicked "generate" | Refuses with the reason | Read from the card's cached verdict, never computed on the click path. If flagged, one prompt: [仍然生成] [先改一下] [让 Inky 修]. Not yet computed: no prompt. "Don't ask again for this project" is allowed |
| Economy auto-queue | Must pass | **Must pass.** Nobody clicked, so the system doesn't guess for the user |

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

`enhance_backend` and `providers/everyframe` already follow this shape and become thin adapters.

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
- A hosted attempt past its deadline closes and the job moves to the next model. The vendor has no cancel and quotes the cost when it accepts a render, so that cost is recorded on the attempt at the move, and a late output is not collected. If the render later fails and is refunded, the record overstates that one render; no reconcile keeps running for it.

## Hosted video routing (EveryFrame, #918)

The owner decisions of 2026-11-01 and the evidence in #918/#929 fix the hosted half of the scheduler. EveryFrame is one account behind one endpoint, but each **hosted model** is its own registry row. Each row records:

- capabilities;
- a price function;
- a prompt adapter;
- a concurrency limit;
- a deadline.

### Before any route: the material gate

Jev (Inky's judgement model, #679) checks every shot before it becomes a job, on both routes, because our own v9 has the same weakness. It is a gate for the economy auto-queue and a one-time prompt for a click (see "Two kinds of checks").

| Shot | Needs |
|---|---|
| A recurring person on screen | A character picture or a first frame |
| A speaking part | Written lines in quotes, with the speaker named |
| Otherwise | An explicit "no dialogue" |

If something is missing, Inky tells the user what to add. Text-only shots invent a new person every render, and unscripted speech comes out as gibberish.

### Classify the job

The job's **input kind** comes from the compiled request:

- `text`
- `first-frame`
- `first-last`
- `reference` (reference people, or a voice track)
- `2k`

It is combined with resolution, duration (hosted: 5–15 s) and whether audio is wanted.

### Hosted chain per input kind

The chain is ordered by the registry price for this exact request (mode × resolution × duration). A price change reorders it automatically, and the owner's order is today's result.

| Input kind | Chain (cheapest first) | Notes |
|---|---|---|
| text / first-frame / first-last | `h3-max-turbo` → `pika-h3-flex` → `h3-max-fast` | Turbo $0.038, Pika $0.058, fast $0.076 per 5 s at 480p |
| reference / voice | `h3-max-fast` → `pika-h3-flex` | Turbo refuses these (400 `bad_attachment`). Pika omni is 3× its plain price and added an extra person with three references |
| 2k | `pika-h3-flex` | Native 2560×1440 |
| draft (cheapest preview) | `vivix-w1` with a short deadline → `h3-max-turbo` | Vivix runs one render at a time across all EveryFrame customers |

- **Excluded entirely** (owner: "只要挡的都完全剔除"): all Seedance, `kling-4-0`, and `flux-3` for images.
  - They are absent from the registry, not just ranked low.
  - Re-adding one needs a new moderation round in `EXPERIMENTS.md`.
- **Candidate behind a flag, untested:** compose the first frame of a reference shot with an image model (`nano-banana-2-1`, $0.036, takes several references), then run turbo first-frame ($0.038). That is about $0.074 against fast's $0.15. It becomes a chain entry only after a staging round passes.

### Fallback, deadlines and refusals

- **Deadline per model.** Fixed in the catalog for now: 300 s for turbo, fast and Pika, and 30 min for Vivix. A move pays for a second render, so a deadline must outlast the vendor's measured batch queue (about 250-290 s). Adapting it from our own latency records can come later, once there are records.
- **Moving down the chain:** a refusal (`failed`, refunded) or a missed deadline moves the job to the **next model in its chain**. A job never retries the same model, because refusals are model-specific (#929).
- **Retry exceptions:** two cases may retry on the same model, once:
  - a 4xx caused by our own request (validation) fails fast with a code;
  - a vendor 429 or 5xx before acceptance retries once with backoff.
- **Chain exhausted:** the job falls back to our own fleet (queued, counted as demand). If our fleet can't start within the tier's hard limit, the job fails with the credit hold released.
- **Health:** each model also has the per-lane cooldown from "Lane health". A run of refusals or deadline misses takes the model out for a while, so new jobs skip it.

### Hosted versus our own fleet

| Signal | Effect |
|---|---|
| Warm or booting own slot can start within the wait limit | Own fleet. It is about 10× cheaper per clip on a warm 5090 |
| No such slot | Hosted chain, as above |
| Job sets an explicit seed (API and admin tools only; owner 2026-11-01 removed seeds from the creator UI) | Own fleet only, since hosted models don't honour seeds |
| Hosted spend rate over the last 30 min would pay for a machine (break-even ≈ 10% utilisation, ≈ 410 480p clips/day vs turbo) | Counts as demand in the lookahead and starts our own machine. New jobs return to it once it is warm |
| Vendor unavailable: balance below reserve, rate-limit bucket empty, or every model of the chain in cooldown | Hosted is not an option this tick; own fleet only |

So hosted absorbs cold starts and spikes. Sustained hosted volume warms our own fleet, and from then on our fleet serves the volume.

### Vendor limits and money

- **Rate limit.** One token bucket per vendor account: 40 renders per 15 min. A submit takes a token first, and with no token the option disappears for this tick. A per-user share (start: 10 per 15 min) stops one user draining the bucket.
  - Staging and production use **separate vendor keys**. If the vendor allows only one account, staging gets a fixed small share.
- **Model concurrency.** Vivix: 1 globally. Others: unlimited until the vendor says otherwise.
- **Balance.** Read `GET /key` every 5 min. Below a warning floor (default $10), alert the owner to top up with SN117 alpha. Below the reserve (cost of in-flight renders plus one batch), hosted is disabled until topped up. Nothing is charged to users differently.
- **Credits.** The user pays the tier price whatever the route (#918: routing is invisible). The take's cost row records:
  - route and model;
  - vendor cost;
  - latency;
  - refusals and abandoned attempts.

  Margin is a report, never a user-visible price.

### Request and output handling (the adapter)

- **Prompt.** `<Picture N>` / `<Audio N>` tokens. Dialogue in quotes with the speaker named. Turbo and fast get the no-cut clause appended: "Single continuous shot with no cuts, no scene changes and steady lighting." Pika does not need it.
- **Attachments.** Upload raw bytes once per object hash per account, and reuse the `attachmentId` across retries and chain steps.
- **Outputs.**
  - Download immediately into our store. A lost hosted output cannot be re-rendered identically.
  - Verify fps (24), size and duration.
  - Map 1920×1088 to 1080 and keep the native 2560×1440.
  - Drop the always-on AAC track when the shot wants no audio.
- **Brand safety.** Neither the vendor nor H3 filters well-known IP. Our story risk lint (`STORY-RISK-LINT.md`) is the only check, and it runs on both routes.

### Rules table (operator config, audited)

```json
{"tiers": {"fast_draft": {"wait_limit_s": 30,  "hosted": true},
           "standard":   {"wait_limit_s": 60,  "hosted": true},
           "refine":     {"wait_limit_s": 120, "hosted": true}},
 "hosted": {"chains": {"text":      ["h3-max-turbo", "pika-h3-flex", "h3-max-fast"],
                       "reference": ["h3-max-fast", "pika-h3-flex"],
                       "2k":        ["pika-h3-flex"],
                       "draft":     ["vivix-w1", "h3-max-turbo"]},
            "order": "price",
            "deadline_s": {"h3-max-turbo": 300, "h3-max-fast": 300, "pika-h3-flex": 300, "vivix-w1": 1800},
            "bucket": {"renders": 40, "per_s": 900, "per_user": 10},
            "balance_usd": {"warn": 10, "reserve": 3}},
 "lanes": {"priority": ["vastai", "lium", "runpod", "cloudrift", "runpod-serverless", "modal-sandbox"],
           "max_provision_s": 1200, "backoff": {"initial_s": 300, "max_s": 1800, "reset_s": 10800}},
 "lookahead": {"version": 1, "count_hosted_demand": true}}
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
| EveryFrame | Known: 40 renders / 15 min, Vivix 1 concurrent globally, `GET /key` balance, refunds on `failed`. Open: is there a cancel call? Separate staging key? How does an outage appear (429, 5xx, or silent queue)? | Deadline fallback must know whether an abandoned render keeps charging |
| First-frame composition | nano-banana first frame + turbo, against fast with references: identity, extra people, cost | Halves reference-shot cost if it holds |
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
    everyframe.py · runpod_enhance.py
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
3b. **Late binding by rebind.** `offload` rebinds a queued, unsubmitted job to a hosted model at dispatch, behind the credential and per-tier switches. It needs the hosted adapter (5a), so it lands right after it. Moving the H3-only fields into the WanGP compiler follows separately.
4. **Capacity interface.** Formalise `CapacityProvider` and move the serverless and Modal clients onto it. `RoutedClient` becomes a registry lookup.
5. **Hosted offload (#918).** #919's backend core is kept: refused vs uncertain, never resubmit. Its standing slots and hard-coded model go. The sub-steps:
   - **5a.** Registry rows per hosted model; an adapter with attachments, the prompt rules, output verification and the balance read.
   - **5b.** Chains, deadlines and abandoned-attempt reconcile, with table-driven tests from the #929 renders.
   - **5c.** Scheduler offload on staging, with the token bucket and per-user share.
   - **5d.** Jev material gate in the card flow (#679). It is independent of routing and can ship first.
   - **5e.** Production enable per tier by owner switch.

   Owner decisions, 2026-11-01:
   - user-uploaded face photos may go to the vendor; the terms name third-party processing;
   - the look difference is accepted, given a reference or first frame;
   - EveryFrame models that carry traffic are chosen after a staging round through this path.

   Economy (auto-queue) jobs follow [STORY-AUTO-QUEUE.md](STORY-AUTO-QUEUE.md): Vivix W1 first, then turbo, then our fleet.

   Card pictures stay on Google's Nano Banana 2.1 directly. It is about $0.0336 per 1K image, and EveryFrame's resale is 7% dearer.
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

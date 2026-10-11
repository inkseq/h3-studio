# dstack, Hatchet and Grafana Cloud migration

Accepted direction: 2026-10-11. Delivery and verification are tracked in
[#132](https://github.com/inkseq/h3-studio/issues/132), on the existing Inkseq Project.
This specification is not a deployment receipt or a second task database.

## Authority and boundaries

Retain the current business API, PostgreSQL ledger, owner isolation, assets,
immutable jobs, attempts, cancellation intents, output receipts and budgets.
WanGP remains the generation engine. Quick Chat and Agent clients retain their
creation, availability, history and download contracts.

| Component | Responsibility | Authority it does not acquire |
|---|---|---|
| Business API and ledger | Authentication, admission, immutable request/route, attempts, obligations, final output | Provider lifecycle or Hatchet's execution state |
| Hatchet | Durable dispatch, worker assignment, dispatch concurrency, bounded step timeouts | Permission to regenerate an uncertain attempt |
| CPU execution worker | Atomic claim, original-attempt reconciliation, SSH WanGP calls, artifact validation/collection | Rental authority, budget increases, changing models |
| dstack 0.22.3 | Provider offers, long-lived GPU runs, machine observations and stop requests | Native model readiness or final provider billing |
| Thin capacity policy | Owned run binding, resource floors, readiness, idle/hold and preparation deadlines | A parallel generation queue or account-wide cleanup |
| Grafana Cloud | OTLP metrics, logs and traces; operator dashboards | Business state, accepted work or cost settlement |

Hatchet and business PostgreSQL may share a server but use separate databases,
roles and migrations. dstack's own state is subordinate provider-control state.
No API tokens, business database credentials or Hatchet credentials go onto
leased GPU hosts. Reuse the existing CPU-to-GPU authenticated SSH tunnel and
private native runtime protocol.

## Current provider identity and qualification scope

Use the explicitly selected central profiles
`vastai/vastai--sixnine-dstack-20261011` and
`runpod/runpod--sixnine-dstack-20261011` with their original protocols/endpoints
and the pinned dstack clients. Preserve the older `--rig-root` profiles; do not
fallback to them, overwrite them, or copy secrets into this repository. Loading
a profile is not authentication proof. Read-only authenticated account/stock
checks cover both providers; the currently authorized paid P2 qualification
uses **Vast only**. RunPod is read-only in this batch. Adapter support alone is
not permission to rent, cache, or enable a provider in production.

The immediate acceptance uses **two separate single-GPU Vast allocations**:
one FL2VA and one Ref2VA. Complete isolated P2 warm successive jobs, truthful
readiness, keep-on within the original deadline, and owned idle stop before any
P3 production activation. CPU configuration/build/proof can proceed beforehand,
but must not switch public admission or the production namespace as a substitute
for P2. Record exact identities and outputs on #132; failures remain blockers.

## Admission and at-least-once execution

Freeze `dispatch_backend` in the accepted execution plan: `legacy` (also the
interpretation of missing historical values) or `hatchet-v1`. Changing the
operating flag affects future admission only. Both claim and recovery filter
the original route, exact configuration, model, manifest and output policy.
Business owners cannot supply an execution route.

Publish a dedicated dispatch event transactionally with runnable business state
through the existing outbox. Publish acknowledgements are transport receipts,
not new jobs. A lost publish response can produce duplicate Hatchet deliveries;
the existing atomic business claim, physical slot binding and fencing prevent
duplicate inference. Never implement a check-then-submit lock in application
memory. A dispatch consumer must not consume unrelated outbox events.

Generation tasks set `retries=0` and explicit execution/schedule timeouts, rather
than Hatchet's short defaults. Worker disappearance, task timeout or cancellation
does not prove that GPU execution stopped. Preserve physical slot obligations
and the original attempt. Reconcile the durable submission intent and upstream
task identity before any further action. An uncertain submission stays unknown;
do not create a new attempt to investigate it. Collection and conditional receipt
writes can retry safely without regenerating video.

Required worker labels express route, configuration/manifest and supported mode.
An existing attempt's recovery additionally requires its original immutable
physical worker identity, proven from the attempt and still-bound registration.
Shared workflow defaults never pin unrelated new jobs to one replica. A busy,
incompatible or non-winning callback promptly yields its broker slot. The original
business scheduler chooses among all compatible jobs under its pool lock, retaining
owner fairness and FIFO aging; an identity delivery can claim only if its exact job
wins that choice. Yielding creates no attempt and never authorizes another inference.
Do not claim that Hatchet sticky assignment batches unrelated jobs by mode; it
addresses related workflow steps. Optional loaded-mode affinity must never defeat
resource safety, fairness or the exact model request.

## Long-lived GPU runs

Use one long-running dstack task per GPU allocation, keeping the existing WanGP
session alive for successive videos. Initially request exactly one GPU per run.
Multi-GPU hosts become selectable only when every rented physical GPU has its
own registered independent slot and lifecycle evidence. Do not rent eight cards
while using one, or partition pipeline components across cards without measured
support. A logical operator group can contain heterogeneous runs.

Each run binds a stable project/intent-derived name, ownership tags, immutable
dstack run ID, provider allocation identity and native incarnation. Journal the
apply intent before the API call. After a lost response, get the original run;
do not apply again. A name collision with different identity is an error.
Stop only positively owned resources and keep unknown stop/billing obligations.
An account's other projects must never be included in cleanup.

For Vast and RunPod container backends, keep the run alive for reuse. dstack VM
`idle_duration` is not business-queue idle shutdown. The thin policy stops a run
only after the configured idle interval, no active/unknown/collection obligation,
and expiry of explicit manual holds. Finite preparation and absolute authorization
deadlines also apply. `max_duration` starts at RUNNING and excludes provisioning
and image pulling; it is not a rental cost cap. Estimate all billable phases and
retain reservations until actual supplier evidence or explicit review settles them.

RUNNING means a machine exists. Native endpoint readiness proves the exact
manifest/profile/controls and a usable session; it does not prove transformer
weights are already resident. Expose `model_load_state=not_observed` until load
evidence exists, and a cold-model hint on the first task. Record loaded mode and
mode-switch measurements rather than promising zero latency.

## Runtime, resources and caching

Reuse the pinned native launcher, compiler, output policies and offline wheel
bundle. Build a digest-bound CUDA/PyTorch/WanGP runtime with ffmpeg >=5.1 and
check `-fps_mode` support. Do not install unpinned packages on every boot. Weights
remain separately hash-bound; do not bake provider tokens into an image.

Historical #129/#130 evidence can recommend hardware and expected timings, but
cannot whitelist user inputs. Actual model/API support governs FL/REF mutual
exclusion and first-only, last-only and both-frame input. Resource floors, pricing,
permissions and budgets remain real constraints. Listed host memory and measured
cgroup limits must be distinguished. The pruned INT8 short-video matrix is not
proof for full weights, long clips or all hardware.

For this qualification use the additive RTX 5090 profile
`h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2` for both modes.
Its allocation floor and separate native available-headroom gate are **29 GiB**,
keeping the same pinned pruned INT8 weights, Qwen/VAEs, MMGP profile 4 and controls.
#130 measured a 29.2 GiB total cgroup limit on standalone RTX 5090 for short
20-step FL/REF clips at 480p and 768p, including mixed REF inputs, with the old
RAM admission gate disabled. That is operating-floor evidence, not proof of
29 GiB available headroom after torch/MMGP imports. A 29.2 GiB allocation may
fail the retained headroom gate even though the historical standalone run worked.
P2 must record actual total/cgroup limits and effective available headroom after
imports before loading. Do not invent a lower threshold or claim a universal
minimum or proof for 50 steps/15 seconds. Keep free-VRAM, disk, parameter, price
and budget gates. The historical 96 GiB profile and its FL/REF manifest identities
remain unchanged, as do the unpruned/PRO 6000 gates. Select the new ID explicitly
in both source bundles, protected capacity profiles, execution bindings and the
default profile; use new configuration identities. No historical accepted job
is retargeted and no timing sample is fabricated for the new integration.

Vast local persistent volumes are host-bound; dstack 0.22.3 does not expose them
as reusable Vast volumes. RunPod network volumes can cache weights but constrain
region/stock and incur storage cost. Measure complete preparation and per-delivered
video cost before choosing a cache. Avoid claiming machine-RUNNING time is total
cold-start time. Target adapters include Vast and RunPod, subject to the current
qualification scope above; Lium/Targon original jobs,
leases and historical reconciliation remain supported on the legacy path. Custom
provider plugins are deferred until measured value justifies their maintenance.

## Grafana Cloud telemetry

Use the existing Cloud stack for metrics (Mimir), logs (Loki) and traces (Tempo),
via HTTPS OTLP. Do not add a self-hosted Grafana, telemetry SQL table or long-term
local telemetry store. Business attempts, receipts and cost obligations remain
durable in the current ledger. Direct SDK export is the initial low-volume choice;
Alloy is a later delivery improvement if measured loss or scale warrants it.

Emit a structured stage-start event immediately, then a completion event and
monotonic duration. A span exported only on completion cannot reveal a stalled
start. Begin/end exports are best effort with bounded queues and retries; Cloud
failure must not fail generation, block acceptance or trigger another paid task.
Never export prompts, original filenames, media, signed URLs, authentication
headers, exception bodies or raw HTTP diagnostics.

Stages include queue wait, provision, image, weights, runtime readiness, model
load, mode switch, asset transfer, inference, collection/validation, result upload
and final business commit. End-to-end latency ends when validated downloads are
available. Correlate job/attempt/Hatchet/dstack identifiers in logs and traces;
never use per-job IDs as metric labels. Keep metric dimensions bounded to stage,
provider, profile, mode, GPU, cold/warm and outcome. Compare width/height, native
frames, steps, reference counts, actual memory limits and versions in structured
events so unlike requests are not presented as comparable benchmarks.

Use a stack-scoped write-only telemetry credential. Dashboard management and
telemetry query credentials are separate if needed. Keep secrets in existing
protected runtime mounts/managed secret mechanisms, never source, `.env`, issue
comments or process arguments. Version dashboard JSON and non-secret settings.
Monitor free-tier/trial limits without automatically upgrading the account.
Show estimates separately from supplier-confirmed charges; duration times hourly
price excludes storage/download charges and is not a bill.

## Delivery gates and rollback

1. P0: reproducible runtime, Cloud configuration and safe stage instrumentation.
   Record observed timings; a promised three-minute improvement is not a gate.
2. P1: simulated long jobs, duplicate/concurrent dispatch, outbox crash recovery,
   dropped submit response, worker loss, collection/write interruption and cancel
   recovery. Prove no duplicate submission and no lost output before enabling.
3. P2: two isolated owned single-GPU Vast runs, one FL and one REF using the
   explicit 29 GiB revision; warm successive jobs, independent slots, honest
   readiness, idle/keep-on and stop observations. Reuse verified media fixtures
   rather than rerunning the full unrelated qualification matrix. Both modes
   and lifecycle evidence must pass before production activation.
4. P3: only after P2 passes, independently reviewed CPU services and an explicit
   qualified profile rollout; real
   Quick Chat/Agent accepted job through validated video and independent audio
   download. Verify compatibility and owned idle cleanup.
5. P4: focused group/machine lists, details, start/stop/hold/extension, truthful
   costs and capacity availability. RunPod rental/cache is deferred; qualify it
   only under separately authorized scope, never as an implicit P2 fallback.
6. P5: remove replaced responsibilities only after >=14 stable days, legacy
   accepted work is finished and outstanding supplier obligations are handled.
   Archive provider notes and an exact pre-retirement Git tag first.

Routine implementation proceeds under the user's autonomous authorization.
Record unresolved blockers and acceptance gaps on #132 or linked bounded issues;
do not declare deferred stability or unobserved production criteria complete.
Rollback disables new-route admission only. Original Hatchet jobs continue on
their route through reconciliation/collection. Keep legacy code until P5;
schema additions and receipt evidence are never destructively rolled back.

## Operating the implemented boundary

The existing cookie-authenticated operator API adds `/v1/operator/dstack/state`,
`catalog`, `previews`, `starts` and `nodes/{id}/stop` or `hold`. Start/stop/hold
commands retain the original idempotency key through response loss. An idle hold
does not extend a rental: it stays inside the original absolute deadline and
adds no reservation. Extending paid lifetime is not implemented by this hold.
Native-ready and admission-ready are separate fields. Admission also requires
the exact authenticated Hatchet consumer, labels, workflow and fresh heartbeat.
Both website availability and atomic claim use the same original-ledger gate.

The CPU controller has an exclusive local supervisor lock and owns only its
child consumers. Closing it reaps CPU processes/tunnels; it never translates
CPU shutdown into a GPU stop or a replacement rental. Bootstrap credentials and
source receipts are staged before the launch journal. Only proof that the
launch never began permits resuming a prelaunch failure. Unknown launches are
observed using their original identity. Bounded stop retries require the exact
still-running owned allocation and do not settle its invoice.

Use [deploy/dstack/README.md](deploy/dstack/README.md) and the additive private
CPU overlay. The read-only deployment checker validates isolation, memory caps,
protected mounts and optional health/auth probes; it never starts services.
The native CUDA image and CPU dispatcher are separate images. The native base
is about 24.5 GB uncompressed, so do not pull it onto the small AWS CPU disk.
Build/import/fixture decoding is not a real H3 qualification. Versioned source,
local simulation, Cloud readback, published image and live release receipts must
be reported separately in #132.

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
| Thin capacity policy | Owned run binding, resource floors, readiness, idle/keep-on and funded spend runway | A parallel generation queue or account-wide cleanup |
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
readiness, configurable keep-on, and owned idle stop before any
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
only after the configured idle interval, no matching accepted queued work or active/unknown/collection obligation,
and expiry of explicit manual holds. New lifecycle-v2 runs have no fixed hard
deadline at start; render dstack `max_duration: off` explicitly. Keep-on supports
idle policy, a chosen hold duration, or until the operator stops the machine.
Matching queued, active, unknown and collection work prevents idle cleanup. Existing lifecycle-v1
rentals retain their original finite deadline and immutable runtime specification.

Cost safety is the hourly `max_price`, original cumulative budget accounts
including an owner-scoped spend cap, and idle stop. Reserve a bounded initial
money chunk plus shutdown margin; the controller funds further runway atomically
in the same reservation rows and account counters before admitting more work.
No additional rental or billing ledger is created. Insufficient funds drains new
work, collects existing results and requests stop when safe. Keep-on does not
bypass the spend cap. Charge estimates include provisioning/download and are not
invoices; manual operational review never releases or settles unknown money.
An exact failed native submission with complete typed rejected/no-contract
attempt evidence releases its original reservation at zero. Older starts the
owner explicitly confirms never allocated can use a trusted one-shot review
with fresh exact-run and complete empty account checks; record the review on
the same intent. Unknown PUTs/contracts and incomplete evidence remain held.

With no live capacity, tasks or unresolved outcomes, controller/dispatch block
on committed PostgreSQL wake hints, then re-read these original authorities.
Only a rare15–30minute read-only provider safety sweep runs while idle. Active
capacity retains its existing timely observation/idle-stop cadence. The pinned
CPU dstack safety derivative disables idle catalog preload and retains native
offer allocation, adding bounded Vast429 cooldown and explicit outcome evidence.

Under the existing capacity lock, new-path limits count original owned live or
uncertain dstack runs and actual GPU allocations. Legacy Lium/Targon intents do
not count toward this path. An exact owned terminal run with fresh authenticated
complete absence from its configured provider account does not count, regardless
of retained historical intent/bill state. This is a transient capacity projection;
do not add a physical-removal reconciler or overwrite historical ledger rows.
Unknown identities or incomplete provider reads remain counted. Provider account
reads use fixed supported endpoints and the same dstack backend credential in
memory; neither raw credentials nor provider configuration enters public state.

An authorized operator can mark a legacy Lium/Targon record as already manually
stopped after confirming account absence and no continuing charge. Preserve the
exact nullable provider identity, original creation/stop uncertainty, task history
and budgets. Active workers, unresolved attempts and collection block this action.
The record moves to history; it is not a provider deletion or invoice receipt.

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
`h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3` for both modes.
Its provider allocation floor and native effective-total-capacity gate are **29 GiB**,
keeping the same pinned pruned INT8 weights, Qwen/VAEs, MMGP profile 4 and controls.
#130 measured a 29.2 GiB total cgroup limit on standalone RTX 5090 for short
20-step FL/REF clips at 480p and 768p, including mixed REF inputs, with the old
RAM admission gate disabled. Effective total capacity is the minimum of host
MemTotal and all visible finite cgroup-v2 ancestor limits; invalid constrained
scope fails closed. Available headroom is a separate observation, not a second
29 GiB gate. Preserve the issued v2 available-headroom contract and every older
manifest rather than rewriting an existing attempt's identity.
P2 must record actual total/cgroup limits and available/anonymous/cache/peak
memory observations. Do not invent a lower headroom threshold or claim a universal
minimum or proof for 50 steps/15 seconds. Keep free-VRAM, disk, parameter, price
and budget gates. The historical 96 GiB profile and its FL/REF manifest identities
remain unchanged, as do the unpruned/PRO 6000 gates. Select the new ID explicitly
in both source bundles, protected capacity profiles, execution bindings and the
default profile; use new configuration identities. No historical accepted job
is retargeted and no timing sample is fabricated for the new integration.

An explicitly stopped cold preparation may be terminated without finishing
model loading only if the exact owned instance has fresh observations, no
worker registration history, and no native incarnation/first-ready latch.
Recheck this under the original capacity lock before journaling stop. Previously
admitted or uncertain inference still requires the original drain/collection
and native-idle proof; task termination does not settle supplier billing.

Vast local persistent volumes are host-bound; dstack 0.22.3 does not expose them
as reusable Vast volumes. RunPod network volumes can cache weights but constrain
region/stock and incur storage cost. Measure complete preparation and per-delivered
video cost before choosing a cache. Avoid claiming machine-RUNNING time is total
cold-start time. Target adapters include Vast and RunPod, subject to the current
qualification scope above; Lium/Targon original jobs,
leases and historical reconciliation remain supported on the legacy path. Custom
provider plugins are deferred until measured value justifies their maintenance.

## Physical host history and future selection (#177)

Keep Vast physical `machine_id`, native bundle/offer ID, rented instance ID and
Sixnine node/run ID distinct. Record a closed, time-stamped physical-host
observation on the original node at allocation, with source identity, provider
quote, region, GPU, allocated RAM/VRAM/CPU and reliability. Physical machine RAM
is not silently equated with the rented VM allocation. Missing historical
identity remains unknown; never fabricate a backfill from a coincidental account
list or turn an old cancellation into a confirmed supplier failure.

`GET /v1/operator/hosts` is a human-operator, owner-scoped scorecard.
`GET /v1/operator/admin/hosts` is a human-admin, same-tenant aggregate with no
other-owner prompts, job payloads, credentials or SSH coordinates. Both use
bounded `limit`/`offset` pages. Admin `machine_id` lookup can return a policy-only
row for an explicitly entered numeric ID: zero measured rentals, null timings
and `operator_supplied_numeric` provenance. It does not claim a provider has
observed that host. `POST /v1/operator/admin/hosts/policy` requires the closed
default/prefer/deny choice, reason, expected policy revision/snapshot and original
Idempotency-Key. The existing policy, command and audit authority owns the
change; `GET .../admin/hosts/audit` is a bounded admin read. PATs have no host
policy authority. Recover an unknown response with the same key/body after
checking current identity, never issue a second policy command blindly.

Manual denial applies to new allocation decisions; it does not terminate or
rebind an already accepted rental/job. Two explicitly qualified provider
startup failures within 24 hours produce a seven-day temporary denial, expiring
by the ordinary policy read clock without a new polling/reconciliation loop.
Manual preference cannot override an active temporary denial. Owner stop,
business cancellation, unknown model loading, unrelated transport uncertainty
and missing measurements do not count as confirmed provider startup failures.

Use dstack 0.22.3's supported `profile.instance_types` for selected Vast bundle
IDs. An authenticated fresh raw offer mapping must prove each bundle's physical
host and current policy; unknown/denied hosts cannot pass enforced selection.
Carry this selection through preview, start, frozen run spec/hash, ownership
checks and normal apply. The pinned native reuse path skips its type gate when
provisioning metadata is absent, so recheck the complete reusable-instance view
and reject missing/conflicting identity rather than claiming the offer filter
alone is sufficient. Preserve dstack's native allocation/fleet/stop operations,
the original capacity lock, command replay, hourly caps and reservations. No
regional restriction, direct provider rental, new physical-removal reconciler,
unsupported create-only mode or parallel scheduler is introduced.

The scorecard derives measured samples from original allocations and authorized
successful attempts plus bounded Grafana Cloud stage evidence. Compare model,
mode, dimensions/duration, steps, reference counts and cold/warm controls before
ranking. Null timing is not zero; observed sample count and basis accompany each
median. The original create-request to first native-worker-ready interval is
explicitly `bootstrap_to_native_worker_ready`; it excludes first-job model
loading. The generate interval includes submission, loading and terminal polling,
so do not count that model loading twice in per-video cost. Native model-load or
network download timing needs its own qualified same-clock evidence; completed
file counts can include cache hits and alone do not prove download bandwidth.
Price × generation plus measured amortized bootstrap is a quote-based estimate,
never settlement of an unknown invoice. `machine_id` belongs in safe
stage logs, not permanent high-cardinality metric/index labels. The existing
Cloud pipeline and the additive `sixnine-host-scorecard.json` dashboard share
the original telemetry; no long-term local metrics database is added.

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
on a lifecycle-v1 rental stays inside its original absolute deadline. On a v2
run, keep-on is idle, until a chosen time, or until manually stopped, with funding
through the original reservation and owner spend cap. It creates no fixed deadline.
Native-ready and admission-ready are separate fields. Admission also requires
the exact authenticated Hatchet consumer, labels, workflow and fresh heartbeat.
Both website availability and atomic claim use the same original-ledger gate.

### Approved P4 workspace

Startup progress is an additive `preparation` object on each owned workspace
node, derived from the existing dstack observation and native setup receipt.
List and detail share the same phase, elapsed/stage seconds, last observation
age, bounded download/image counters and static diagnostics. Weight bytes count
completed files, not an invented in-flight transfer rate; image percentages
use downloaded plus extracted work against dstack's explicitly final total,
with downloaded/extracted bytes shown separately. Missing totals and old-host telemetry remain null.
Two minutes without a native receipt update is described as no new progress,
not proof of a failed download. Provider observation freshness is unchanged.

Native model state is separate from service readiness. The pinned upstream
Session's existing progress callback reports actual `loading_model`; only an
observed matching native model reports loaded. A ready HTTP process can still
wait for its first job to load weights. These presentation fields do not grant
worker admission, reset policy/cost counters, retry setup or create a second
reconciliation loop. New native source bundles carry the instrumentation;
existing hosts lacking it explicitly show model progress not observed.

The cookie API adds `GET /v1/operator/dstack/workspace` for owned groups, nodes,
waiting FL/REF jobs and usage. Groups are account-owned presentation metadata in
the same database; they do not schedule jobs or duplicate rental authority.
`POST /groups` creates a named group, and `POST /nodes/{id}/group` moves a node
with `group_id: null` meaning Ungrouped. Preview binds the optional group before
start; starts from Find/Queue are Ungrouped by default. Group Add defaults to one
machine; a typical FL/REF pair remains an explicit batch choice.

Every batch child obtains a fresh single-use preview and independent original
start/idempotency journal. dstack plans and allocates each run. A normalized Vast
offer has a native bundle/offer identity; it is not the physical `machine_id` or
the rented instance ID. The #177 selection path joins a fresh authenticated
provider mapping before freezing eligible `profile.instance_types` in the normal
dstack run specification. Public quotes do not guarantee quantity; displaying one
quote does not assign it repeatedly to several machines.
Partial results are shown per machine; an unknown response stops subsequent batch
children until the original command is recovered. No new allocation reconciler
or browser-controlled provider request is introduced.

Node and group `GET .../stop-impact` compute capacity loss using the frozen job
profile, original owner/project and real worker eligibility. Stop rechecks under
the existing capacity lock and returns `capacity_loss_confirmation_required`
with warnings/fingerprint unless the caller acknowledges that exact impact.
Queued tasks stay intact. Qualified busy machines count as future compatible
capacity; unready/stale machines do not. Group stop/hold reports each ordinary
child command rather than promising atomic provider side effects.

The list retains renting, observed weight download, observed model load,
ready/running and stopping/stopped. Process startup is not model-load evidence;
lazy-loaded weights remain explicitly unobserved until proven. Stop intent does
not remove a row or zero its hourly estimate. The first terminal observation is
latched in the original dstack row so ended runtime does not grow on later polls.
Keep-on remains visible at intermediate widths. Ended/detail projections derive
runtime, cost and validated successful-video counts from original receipts.
Today uses an explicit Sydney day boundary; modeled/ceiling estimates are labeled
separately from settled charges. Missing evidence is null, not zero. Exact average
generation stage/cost requires Cloud query evidence; the available ledger fallback
is labeled average submission-to-validated-result, including collection/upload.

`GET /nodes/{id}/metrics` authorizes the owned node (or an explicit administrator)
before reading optional Grafana Cloud logs. `SIXNINE_STAGE_QUERY_CONFIG_FILE`
selects a separate `logs:read` mount, never the OTLP write credential. The existing
managed migration secret accepts optional `grafana_stage_query`; ordinary hydration
creates only its missing `10001:10001`, `0600` mount and preserves all existing
secret bytes and identities. Hosts without this field remain compatible.
The reader checks exact original successful video/job/attempt/node/profile/mode,
reads at most seven days/32 attempts/256 rows, times out after one second and
caches for 60 seconds. Missing, conflicting or inaccessible measurements stay
unavailable. The mean includes remote submission/terminal polling, not only GPU
kernels. Generation cost uses the original machine quote or clearly labeled
approved ceiling; it excludes startup, idle, storage and transfer. Credential
expiry/renewal is an operator obligation, not a reason to fabricate samples.

`GET /admin/workspace` and stopping another account's machine require explicit
`SIXNINE_OPERATOR_ADMIN_OWNERS`, an empty-by-default subset of the existing
capacity accounts. Rental privilege does not grant cross-account visibility.
The authenticated administrator remains the command actor; machine ownership,
accepted tasks and budget obligations are retained. Creator views stay concise;
historical reconciliation and billing remain in the administrator area.

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

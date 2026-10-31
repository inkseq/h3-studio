# Pinned WanGP H3 FL2VA execution recipe

This is the first deliberately bounded **BF16 Base, 50-step** path, not full H3
control parity or a measured performance claim. The business ledger, leases,
owner isolation, attempts, budgets, media validation and publication remain in
the existing platform. The upstream Session is private to a one-slot host.

## Reproduction and qualification status

- Source: `deepbeepmeep/Wan2GP@0e58385fbde7ff102d276e4a9e490845de76b4ea`.
- Components: `manifest.json` pins the HF repository revision, exact filenames,
  precision, byte sizes and SHA-256 (LFS), or Git blob SHA-1 (small JSON files).
  Metadata was read from the pinned [HF model API](https://huggingface.co/api/models/DeepBeepMeep/MiniMax-H3/revision/adc81ccb71352192214d83d5fafb9487e860be39?blobs=true)
  on 2026-10-07. This is source/metadata evidence, not proof of installed weights.
- Model files total **124,300,443,428 bytes**. Transformer: 66,280,486,944;
  Qwen layer-50 BF16: 51,506,305,568; video VAE FP16: 5,207,806,512;
  audio VAE FP32: 605,429,308; latent upscaler BF16: 690,592,992;
  the balance is tokenizer/configuration files. Disk size is not a VRAM estimate.
- `runtime-recipe.json` is a candidate installation recipe, **not a complete
  dependency lock or a qualified container image**. `runtime_digest` identifies
  the exact upstream requirements source, not a fabricated installed-image hash.
  Transitive wheels, OS packages, driver compatibility, image digest and measured
  memory/cold-start/inference results must be recorded on the authorized host.
- The candidate uses Python 3.11.14 and PyTorch 2.10.0/CUDA 12.8 wheels. The
  [pinned Dockerfile lines 1–43](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/Dockerfile#L1-L43)
  specifies these Torch wheels; the [manual guide lines 5–25](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/docs/INSTALLATION.md#L5-L25)
  recommends Python 3.11.14 and a different CUDA 13.0 recipe for modern RTX.
  These are documented alternatives, not evidence of a universal CUDA minimum.
  This first recipe selects dense SDPA and does not require building optional
  Sage/Sol kernels. Do not substitute CUDA wheels silently to make a host pass.

## Explicit startup interfaces

### Platform cold-start integration

The existing production controller selects `execution_backend=wangp-worker` and
the frozen `engine_manifest_digest`; it still owns the original PostgreSQL
capacity, rental and job ledger. Do not start a second controller or reuse a
Comfy approval for this runtime. The cold path uses the real queued-task profile;
boot readiness never fabricates an inference result.

The initial host-managed rollout uses a separately staged, root-owned dependency
archive at `gpu-scaler/public-source/wangp-dependencies.tar.gz`. The host verifies
its hash from the small runtime configuration and mounts it read-only into the
CPU controller. The controller streams it in bounded chunks to the GPU's
`/root/sixnine-cache`, resumes only a matching prefix, and publishes it only after
the complete hash matches. Large wheels and weights are never inserted into the
controller's in-memory source map. Other bootstrap package modes are manual
interfaces, not alternative enabled production configurations.

The PyTorch template starts bootstrap through `/opt/conda/bin/python`; its
observed Python patch version must match the captured lock. The private client
configuration requires the original 32-hex `runtime_incarnation`. Missing/null
identity cannot configure a production slot. Same-instance SSH recovery keeps
the listener and worker identities while checking the retained host key; full
controller-process restart recovery is a separate C2 gate.

Host installation is separate from the application release. Install the reviewed
`deploy/platform/gpu_scaler.py`, `deploy/platform/release.py` and the pure
`studio_platform/service_policy.py` in their existing protected host locations;
the application bundle does not automatically replace `/opt/sixnine-release`.
Archive a verified finished lifecycle before constructing new inputs, preserve
the SSH identity and database, and never reset accounting or old approvals to
make the new configuration pass.

### Package preparation and one-shot bootstrap

`package_tool.py` is an explicit build command, never an application import hook.
`prepare --upstream-root CHECKOUT --output NEW_DIRECTORY --base-image IMAGE@sha256:DIGEST`
runs on Linux x86-64/Python 3.11.x and records the exact patch version. It checks the upstream revision and requirements
hash, resolves the complete upstream import surface plus `runtime-host.in`, builds
a wheelhouse, verifies an offline hash-locked installation in a fresh venv, and
records every installed Python distribution, system package, source file and wheel.
The resulting `wangp-dependencies.tar.gz` can be several GB; stream it to the GPU
host separately. Never put it or the 124 GB model files in the controller's
in-memory small-source map. Failed resolution does not produce a qualified lock.

`--wheelhouse EXISTING_DIRECTORY` reuses already acquired wheels offline. Retain
the exact additional/updated native `.deb` files and pass `--system-debs DIRECTORY`
(default `/var/cache/apt/archives`). Their package/version/architecture, sizes and
hashes are included in the environment lock and artifact. Bootstrap verifies these
archives and uses offline `dpkg --install`; it never runs apt update/latest on a
production cold start. Missing dependency closure fails the install. Python
distributions must match the whole lock exactly. OS packages listed in the lock
must be present at their exact recorded versions; extra provider packages such as
SSH may exist and are recorded in the observed receipt. They cannot replace or
upgrade a recorded library silently.

For a pre-existing provider image, the concrete sequence is:

1. Run its exact observed image digest locally, add required native packages,
   and prepare a fresh isolated venv using the retained wheels and `.deb` files.
   Capture Python, wheel and native-package identities on that target image.
2. Bind a new manifest and stage the protected package/identity. The controller
   must pin and verify the provider template's actual Docker digest separately.
3. On the authorized GPU, the bootstrap installs that artifact and invokes
   `probe_gpu.py` in a bounded child process before downloading models. It verifies
   source/environment, real CUDA visibility, and imports `wgp` plus the H3 pipeline
   with outbound socket connections refused. Import failure names the missing
   module in the protected `gpu-import.json`; it does not auto-install anything.
4. Keep this frozen environment identity for the explicit model/bootstrap and
   first accepted task. `imports_verified` is not inference qualification. A
   dependency repair creates a new captured lock/manifest before another lease.

`Dockerfile.build` pins an official Python base and captures the packages actually
installed by its initial build. Its apt operation alone is not a reproducible
lock. Retain the resulting image digest. `Dockerfile.runtime` installs the recorded
wheels offline on that exact base, without starting a server or fetching models.
A container image/venv installation still does not prove NVIDIA driver support or
successful H3 inference. A different provider template requires its own explicit
environment capture and new manifest identity; the builder's Debian package list
is not a universal requirement for every GPU provider.

`source-bundle --output NEW_FILE` creates only the explicitly enumerated private
host Python files, under 16 MiB. No credentials, media, settings, weights, runtime
state or dependency wheel is included. `bind-manifest --manifest manifest.json
--environment-lock ENVIRONMENT_JSON --image IMAGE@sha256:DIGEST --output NEW_FILE`
creates a new identity bound to the measured full lock. It does not overwrite the
historical source-only candidate and leaves inference qualification false.

The controller stages the four small files (`wangp-bootstrap.py`,
`wangp-runtime.json`, `wangp-manifest.json`, `wangp-package.tar.gz`) and, for the
archive mode, streams the separately hash-bound `wangp-dependencies.tar.gz`.
Copy `runtime-config.template.json` and fill measured hashes/paths; placeholder
values deliberately fail validation. Exactly one dependency source is selected:
an already-staged local artifact, an unsigned HTTPS artifact URL, or an immutable
image's `prepared_root` containing `dependencies/` and `venv/`. URL credentials and
query parameters are refused. API credentials and model credentials are not
accepted by this bootstrap.

The explicit remote command is:

```sh
python wangp-bootstrap.py --config wangp-runtime.json --slot-key APPROVED_INTENT_ID --token-file /protected/private-runtime-token
```

The token remains a protected mode-0600 file and is used only in process memory.
The bootstrap's exclusive `wangp-bootstrap-started.json` is separate from the
controller's start identity. A repeated invocation returns `reconcile_required`
without changing status, launching a second process, or resetting the journal.
After downloading pinned public components, bootstrap starts the existing
launcher once. The launcher acquires exclusive journal/slot ownership before
checking source, configuration, dependencies and every model size/hash. Only then
does it initialize the Session and expose its loopback listener. Bootstrap checks
both the private verification receipt and authenticated `/v1/readiness` for the
same manifest, slot and live incarnation; readiness must report idle.
`setup-status.json` records phases and safe error codes; successful readiness has
`state=ready`, `runtime_verified=true`, source/manifest identity, observed GPU UUID
and bytes, and always `inference_verified=false`. Any uncertainty after process
creation remains `unknown`; the controller must reconcile it before another start.
The first accepted queued task, not a hidden smoke generation, proves inference.

### Bounded public model transfer

After the locked environment and import probe pass, bootstrap invokes
`studio_platform.runtime_hosts.wangp_download` in one owned Linux child. It uses
the **existing locked** `huggingface_hub`/`hf_xet` packages; it does not install an
SDK or snapshot a repository. Each call names an exact manifest file and full
repository revision, with the official Hub endpoint and literal `token=False`.
Inherited Hub identity, endpoint and debug settings are removed for this child.

Normal future boots of the existing `29gib-total-v3` pruned profile use an
explicit content-preserving download locator in `wangp_download.py`. Its original
FL and REF manifest digests and all eleven selected file identities are bound to
the immutable transport revision `8d9093438ffb3e33d6a058cc20fa3b7ca31f5752`.
Official immutable metadata matched every original size and LFS SHA256/Git blob
SHA1 (response SHA256 `df25843addd3d71ebbc2041e0900b3a7185a4a9fb93a4a1c4c5f488d7d73041b`).
The manifest still names original revision `adc81ccb71352192214d83d5fafb9487e860be39`;
only the SDK download revision changes before transfer. No 404/latest fallback,
model/profile substitution or verifier bypass is permitted. Other profiles retain
their existing locators. The new source-package epoch is for future starts;
already bound bootstrap inputs and journals must remain unchanged. Source checks
and metadata equivalence do not certify a released cold start or video inference.

At most two files transfer concurrently, with at most two SDK invocations per
file. The SDK owns its partial files and transfer retries; this helper does not
implement HTTP Range or replay bootstrap. Existing SDK partial files are retained
within that preparation. HTTP fallback supports SDK resume; native Xet byte reuse
across interruption is **not qualified by the fake SDK tests**. New ephemeral
GPU instances do not gain a persistent model cache from this change.

The child sets `HF_HUB_DISABLE_XET=1`, so the SDK downloads over plain HTTPS
from the Hub CDN even though `hf_xet` is installed. On 2026-10-31 a staging Lium
host on native Xet averaged ~8.5 MB/s (28 GB in ~55 min) and hit
`capacity_startup_timeout`; the next Lium host over HTTPS sustained ~38 MB/s.
The completed-file counter in progress receipts does not move while one large
file transfers, so it is not a stall signal on its own.

Download speed gate (#885). With Xet disabled, the child also reports
`bytes_received`: completed files plus the size of the SDK's `.incomplete` HTTP
partials under `<model-root>/.cache/huggingface/download`. A ticker rewrites the
receipt every 10 s. The field is omitted until a partial is seen, and whenever
Xet is enabled. The controller latches it on its own clock:

- After a 180 s measurement window, a host averaging under 50 MB/s is stopped as
  `dstack_download_too_slow`. A bound Vast `machine_id` is then set to manual
  `deny` with reason `automatic_slow_download`. It stays denied until an admin
  sets it back to default on the operator hosts page; the change is audited.
  Hosts with no bound physical identity are stopped but not denied.
- Download time with byte progress is credited against the fixed cold-start
  window (`capacity_startup_timeout`), up to 1800 s. A stall or a slow rate
  earns no credit.
- Byte progress also feeds the existing no-progress timebox.

The parent watches a two-hour model-transfer deadline, free disk headroom and the
isolated Hub/Xet cache (a watched 1 GiB threshold, not a filesystem quota, with
chunk/shard caches disabled). The child
also arms an independent Linux OS timer and parent-death termination before SDK
import. Failure stops and reaps that exact child; an unconfirmed stop remains an
obligation. Preparation-idle checks count the download child. SDK/native stdout,
stderr and Xet file logs are suppressed; protected receipts contain only static
error codes and completed-file/byte totals. Those totals are not network progress:
Xet may preallocate files. The initial disk check conservatively requires all
missing model sizes plus 10 GiB free, even if SDK partial files already exist.

`downloaded_unverified` is transfer completion only. The existing complete
size/hash checks run once inside the owned launcher, before Session initialization
or readiness. Normal bootstrap does not first run a duplicate `--verify-only`
process. Standalone `--verify-only` remains available for an explicit inspection.
This source change neither modifies the model manifest nor enables a new runtime
configuration. Offline fake SDK and inert Linux process tests establish these
guards; actual download throughput, interruption reuse and cold-start duration
need a separately authorized future host measurement.

#### Explicit original-download continuation

`tools/resume_wangp_download.py` is a separately staged, Linux-root operation
for a confirmed stopped `model_download_failed` or `model_download_timeout`.
It supports only the two exact bootstrap SHAs in `SUPPORTED_BOOTSTRAP_SHAS`
(the retained `2620c95b...` and preparation-progress `b4647703...`) and Python
**3.12.14**. It does not replace the original bootstrap or
native bundle, rerun initial bootstrap, or run automatically from the controller.

Before execution, independently approve a protected one-use authority with
purpose `resume-original-model-download-v1`. Its `input_sha256` binds all nine
original inputs: `config`, `token`, `package`, `manifest`, `bootstrap`, `identity`,
`marker`, `status`, and `failure`. Bind the exact original slot, dstack run,
provider instance, hard deadline and recovery-tool SHA. `parent_metadata` must
exactly describe every original install/status directory ancestor, with root
UID/GID and observed mode 0700, 0755 or 0775. This permits only those pinned
existing parents; it does not relax unrelated paths or require chmod of originals.
Stage the tool and authority in a new root-owned 0700 directory, with 0600 files.

If the original immutable repository revision becomes unavailable, an explicit
version 2 authority with purpose `resume-original-model-download-v2` may bind a
content-preserving `download_alias`: exact repository, original and alternate
immutable revisions, all selected original paths/sizes/SHA or Git-blob hashes,
and the independently approved metadata-evidence SHA. Only the SDK revision
locator changes; the original manifest, source and final full-byte verifier remain
unchanged. There is no automatic fallback to a repository's current revision.
`previous_intent_sha256` must pin the original journal's confirmed terminal
download failure, the same slot/deadline and eight unchanged non-status input
hashes. A freshly observed failed status may be rebound in one new intent; the
prior journal and consumed intent remain retained and cannot be replayed.

For an original download that already has a terminal `downloaded_unverified`
receipt, version 3 (`resume-original-model-download-v3`) adds `completed_download`
to the same nine-input authority. It pins the exact original progress-file SHA,
all original manifest file records sorted by path, and the four complete file/byte
counts. `completed_download.keep_until` must equal the authority `hard_deadline`.
For the new dstack path, independently obtain that finite value from the currently
accepted machine keep-until policy; do not infer a fixed deadline from historical
ledger intent or rewrite the original config. An indefinite policy or unknown
current lifetime does not authorize this bounded continuation route.

Version 3 requires no earlier continuation journal. Under the original owned
cache lock it rechecks the unchanged original receipt, exact manifest identity,
counts and safe exact-size file metadata. It skips SDK/download-child execution
and preserves the original downloaded bytes and failure receipts. The original
remaining setup and launcher still perform mandatory full content verification
before loading the model or reporting ready; completed receipt/size metadata is
never evidence of inference qualification. Both approved bootstrap versions reuse
their exact original status callback; the progress version receives its required
closure state and retains the original overall start timestamp.

The authority expires within 300 seconds of issuance and within its independently
approved finite lifetime bound (the original hard deadline for older paths, or
the current finite keep-until policy for the new path). More than 900 seconds must remain for runtime verification/start;
download and cache-lock waits are bounded to preserve that margin. Matching
failure receipts must confirm the download child stopped. Complete, stable
process visibility must show no bootstrap/download/launcher child, the original
loopback port must refuse connections, and no runtime configuration, slot state
or runtime-start marker may exist. Unknown process or startup outcomes refuse
continuation.

Use the original config and token file; `--receipt` names the approved authority,
not an ordinary setup or verification receipt. Paths below are placeholders:

```sh
/venv/main/bin/python /protected/recovery/resume_wangp_download.py \
  --config /original/slot/wangp-runtime.json \
  --token-file /original/slot/private-runtime-token \
  --receipt /protected/recovery/authority.json
```

The tool durably journals `continuation_started` before effects and saves the
original failure/status evidence. It retains the original model directory and
SDK `local_dir` bytes, while the new attempt uses an isolated `HF_HOME`; the old
attempt/cache evidence is retained. This does not certify Xet-cache reuse or
avoided network transfer. After download it uses the SHA-pinned original remaining
setup, verification and launcher. A consumed intent or unknown result means
reconcile the original journal/process; never replay the command or remove its
marker to retry. Successful native readiness remains separate from actual video
generation. There are no provider, job, ledger, budget or rental-extension
operations in this tool.

The normal launch receipt is `slot-state/runtime-verification.json`, written
atomically with mode 0600 after full verification. It binds the manifest, slot,
process ID and randomly generated host incarnation, and explicitly does not
certify inference. It is evidence for that process, not a reusable hash cache:
every new launcher repeats the complete verification, including on recovery.
An old receipt, HTTP response alone or child creation cannot establish readiness.
The existing 900-second launch/readiness deadline includes this verification;
status distinguishes `runtime_verification` from `runtime_start`. A failed receipt
write prevents Session initialization; any bootstrap uncertainty after process
creation retains the original start marker and requires reconciliation, never
automatic relaunch. Non-launch inspection writes its separate root-level
verification receipt and returns `verified_not_started` without creating a slot.
Completed-file/byte counts remain protected setup/progress evidence; the current
controller/API exposes preparation phases rather than those transfer counters.

Environment attestation of an extracted package uses its hash-bound
`.sixnine-environment.json`; the original git-checkout path remains available for
the historical candidate. Both paths retain core-version and complete model-file
checks. A guest cannot prove its own OCI identity: the controller/provider must
verify that separately. Native libraries and model execution still need the
authorized GPU test and exact release evidence.

The launcher owns the listener, loopback authentication token, process lifetime,
journal, immutable input storage and Worker transport. These helpers do not
start anything merely by being imported:

1. `config_for_model_root(model_root)` produces a non-secret `wgp_config.json`.
   Write that exact filename in a protected configuration directory. Its single
   absolute `checkpoints_paths` entry is the same directory verified below.
   Do not add a `.` fallback, UI plugins, other accounts or saved GUI presets.
2. `verify_runtime(runtime_root, config_path, manifest_path, model_root)` checks
   source HEAD, tracked modifications, untracked executable source, core package
   versions and every model file's size/hash. It returns dated-run evidence for
   the caller to save and explicitly reports `inference_verified=False`.
   The source/config/weight tree must remain private and immutable after this
   check. A full dependency lock and container attestation remain separate gates.
3. `create_session(runtime_root, config_path, output_dir)` explicitly imports
   `shared.api.init(root=..., config_path=..., output_dir=..., cli_args=["--attention",
   "sdpa", "--profile", "4"], console_output=False, webui_state=None)`.
   This call initializes the runtime; it is not an offline test. The directory
   must already exist. Model loading/readiness does not qualify inference.
4. Wrap the facade in `WanGPHost` with durable receipts. Inputs are immutable
   `InputDescriptor` values. The host resolver replaces `image_start`/`image_end`
   opaque handles only with its own hash-verified private files. No client path,
   URL, arbitrary workflow or Python code is accepted.
5. Construct `H3FL2VACompiler(manifest, transport.stage_input)`. Its callback
   receives `(descriptor, source_stream, heartbeat=...)`, must verify both digest
   and size, and return the same descriptor. The worker retains the same job ID,
   request hash, attempt tag, manifest binding and source asset ownership.

Upstream `Session.close()` waits for its generation lock and unloads the model;
it is not a cancellation acknowledgement. Do not release host ownership while
a live GPU thread can still execute. Host/process replacement must preserve and
quarantine nonterminal receipts rather than infer the old attempt stopped.

## Controls and exact mapping

The public schema comes from `wangp_compiler.control_schema()`, and the machine
envelope is also recorded in `manifest.json`. New requests are normalized before
admission; old Comfy snapshots are never rewritten. A rejected control produces
a stable error before staging/submission.

| Platform intent | Upstream setting / behavior | First recipe |
| --- | --- | --- |
| BF16 Base | `model_type=minimax_h3_fl2va`, config `bf16,bf16`, startup transformer/text quantization `bf16` | Fixed; no pruned, turbo, INT8, FP8, LoRA or VDN substitution |
| Steps | `num_inference_steps=50` | Fixed 50; handler's default 20 is explicitly overridden |
| Prompt | `prompt`, `multi_prompts_gen_type=FG`, enhancer disabled | One prompt including all lines; exactly one task/output |
| Seed | Decimal unsigned 32-bit value (`0`–`4294967295`) converted to Python integer | Exact value; omitted seeds are generated once within this domain. Reject explicit out-of-range seeds before dispatch; never truncate accepted requests |
| Resolution | Exact `WIDTHxHEIGHT` from the accepted native output spec | Existing 480P/576P/768P/custom area and aspect rules |
| Duration | Integer `video_length` on native `17*k+5` grid, `force_fps="24"` | 107–362 native frames; preserve both native and requested durations |
| First/last image | `image_start`, `image_end`; explicit `S`, `TE`, `SE` flags | Zero, one or two image assets; no references/video/audio inputs |
| Reference sizing | Startup `fit_canvas=2` | Explicit center crop to the requested canvas; source images cannot silently alter resolution |
| Sampling | `sample_solver=euler`, `flow_shift=12`, `guidance_phases=1` | Native scheduler only; audio sigma shift is fixed 3; guidance fixed 1 |
| Decode | Video tiled 256, overlap 64; native temporal clip length 17; audio normal | No normal video decode, adjustable temporal tiles or forced CPU encoder |
| Sound | Native joint generation; `_api.return_audio=True` | `generate_audio=True` only for this recipe; false is rejected, not ignored |
| Export | Native MP4 + exact generated audio tensor, then existing platform export | Raw H.264 CRF10/AAC; delivered H.264 CRF18 + separate FLAC |

### Why some familiar options are rejected

- Default quantization is INT8 in [wgp.py lines 2647–2675](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L2647-L2675).
  The BF16 transformer choice is explicit in [model selection lines 3009–3071](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L3009-L3071).
  The Qwen and video VAE configuration groups are [handler lines 669–689](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py#L669-L689).
  The video VAE menu calls its option BF16, but the actual selected file is FP16;
  this manifest records its real precision.
- [Handler lines 1204–1220](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py#L1204-L1220)
  defaults to 20 steps. The recipe overrides it. [Pipeline lines 743–775](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L743-L775)
  consumes solver, steps, shift and frames, but does not consume the generic
  `guide_scale` keyword forwarded by the outer UI. Guidance is not advertised as
  an adjustable control. [Lines 1148–1151](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L1148-L1151)
  show the independent fixed audio shift of 3.
- [Pipeline lines 499–500](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L499-L500)
  ignores the requested VAE tile size and always enables 256-pixel tiles.
  [Video autoencoder lines 691, 748–779](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/components/video_autoencoder.py#L691-L779)
  sets clip length 17 and overlap 64. Passing Comfy's temporal tile controls would
  therefore be misleading; they are rejected rather than discarded.
- Resolution follows input images by default in [wgp.py lines 7380–7390 and
  7739–7767](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L7739-L7767).
  Explicit crop-to-canvas configuration preserves the accepted resolution.
- The pinned [video codec table](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/utils/video_codecs.py#L46-L77)
  maps `libx264_8` to CRF10; `libx264_18` is not an actual selectable CRF18 codec.
  The existing platform worker, not a guessed upstream option, applies the
  requested final export CRF. Native frames are retained in the raw receipt;
  the platform's existing explicit requested-duration export can shorten delivery.

## Result and cancellation semantics

`SessionJob.cancel()` only sets cancellation intent. `done`/`result()` are the
completion evidence ([shared/api.py lines 511–544](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api.py#L511-L544)).
The headless runner normally completes its generation worker, clears `active_job`,
then sets the result in [shared/api_cli.py](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api_cli.py).
Its outer exception path can publish a result before that daemon worker stops.
The facade therefore also checks the pinned `wangp-session-worker` thread and
requires successful CUDA synchronization before reporting terminal stop proof.
Acknowledgement, timeout and an empty queue are insufficient. A runtime exception
is sanitized; partial files are not success.

Success requires one successful task, no error, exactly one MP4, and the matching
audio artifact. H3 returns sample-major stereo float32 NumPy samples at 32kHz
([pipeline lines 1695–1721](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/pipeline.py#L1695-L1721)).
`_api.return_audio=True` retains those samples in `GeneratedArtifact`
([shared/api.py lines 319–339](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/shared/api.py#L319-L339);
[wgp.py lines 8671–8678](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/wgp.py#L8671-L8678)).
The facade writes IEEE float32 WAV without reconstructing it from the lossy AAC
track, preserves the native MP4, and hands both to the host for checksum sealing.
Failure during packaging retries the same completed result; it never regenerates.
A crash before durable sealing remains unknown for reconciliation, not safe retry.

Legacy/default worker export uses the requested integer duration (for example
120 frames for 5 seconds), while the native H3 sample can have 124 frames. D3 adds
the opt-in `native-frames-v1` delivery contract to preserve all native frames and
the complete generated waveform for new separately qualified WanGP plans.
It requires matching operator-policy, cold-approval, boot and worker capability
identities; an existing configuration or accepted task is not silently upgraded.
See [the output contract](../../docs/GENERATION-CONTRACT.md#6-output-contract) for
duration/audio validation and compatibility. Current first-proof configuration
remains on its original export behavior. CPU numbered-frame evidence establishes
export behavior only: B3 should first prove text-to-video, and a real first/last
example is still needed before claiming qualified last-frame fidelity.

## Checks and remaining gates

### Repair preparation while preserving the accepted job

The protected host entry `deploy/platform/preparation_hold_recovery.py` supports
an explicit source-only repair of a retained `bootstrap_failed` hold. All pool
rentals must be destroyed with settled billing, with no inference attempt or
fleet start. Engine, model, dependency archive, policy, budget and original
waiting deadline remain unchanged. Expired/cancelled jobs cannot be restored.

After independent source/image review and exact-version host approval, stage
the target configuration and only the changed public source files under
`gpu-scaler/operator/preparation-repairs/<operation-uuid>`. Ordered actions are
`freeze`, `prepare --job-id <original-id>`, `retire`, `stage`, then an explicit
root-supervised `resume`. Each action holds the host release lock. Resume
activates the existing provider-enabled controller; staging does not. The
existing next-cycle transfer keeps the original request, reservation and deadline.

An uncertain response or durable `*_started` phase requires reconciliation;
never repeat it automatically. Exact controller ownership, the database fence
and retirement evidence must agree. This helper cannot replace the accepted
runtime or clear unknown execution. Its docstring/tests specify file modes and
supported boundaries. Staging failures and attempted inference use separate
recovery paths.

### Optional repair for measured provider SSH package drift

The provider's [pinned SSH bootstrap](https://github.com/Datura-ai/lium-io/blob/ec31b1ecd9b5f4d594d88d7c1c8ecbfa7cdd7228/neurons/validators/src/services/assets/sshd_bootstrap.sh#L140-L175)
can install `openssh-server` after the base image starts. A disposable CPU replay
of that operation on the pinned PyTorch base upgraded `libsystemd0` from
`249.11-0ubuntu3.12` to `249.11-0ubuntu3.22`; replaying the original 199 dependency
archives left this one mismatch against the unchanged 425-package environment.
This reproduction does not establish the contents of an already deleted host.

For this exact side effect, an operator can include a finite local repair kit:

```sh
python deploy/wangp/package_tool.py source-bundle --output wangp-package.tar.gz \
  --os-restore-kit /path/to/verified-kit --environment-lock /path/to/original-environment-lock.json
```

The kit contains `manifest.json` and six official amd64 archives from the signed
[Ubuntu snapshot](https://snapshot.ubuntu.com/ubuntu/20250101T000000Z/):
`libsystemd0`, `systemd`, `libnss-systemd`, `libpam-systemd`, `systemd-sysv`, and
`systemd-timesyncd`, all at `249.11-0ubuntu3.12`. Their exact names, sizes and SHA256
values are pinned in `wangp_system_restore.py`. The kit is private deployment
material; do not commit binaries. Its manifest binds the original environment
digest and base image, and the approved source-bundle SHA binds the complete kit.

Bootstrap accepts only the measured `.22` family or a partially restored `.12`
family, rejects unrelated environment differences, and validates every archive
before installing the complete dependency closure. It does not fetch packages
or run `apt install` on the GPU. It then requires the original environment pins,
`dpkg --audit`, dependency consistency, and `sshd -t`. A matching original
environment is unchanged. This is an exact repair, not a weaker lock, new model,
new environment identity, or proof of GPU inference. A different drift requires
diagnosis and a separately reviewed repair or a new qualified runtime identity.

`test_platform_wangp_compiler.py` and `test_platform_wangp_session.py` use isolated
fake Session objects/files only. They cover control rejection, immutable identities,
role mapping, unsigned 32-bit WanGP seeds, cancellation intent, both outputs, collection retry,
lookup-root binding and escaped output rejection. They do not import WanGP/torch,
download weights or assert real video/audio quality. Real media validation is the
Worker's existing responsibility.

Before enabling this recipe publicly: verify the installed runtime and files,
qualify a real accepted job through the complete API-to-download path, record the
actual native/delivered media specs and audio stream, then reconcile cost/lease
evidence. Different weights/runtime configurations require an explicit declared
binding. Supported input combinations do not require a separate qualification
run merely because they were not measured; retain the immutable manifest and
request binding of existing jobs.

### Native deployment-profile support (current API)

The native `deployment_profile_id` compiler separates implemented model/API
support from historical measurements. `verified_cases` / `qualification_cases`
and public `joint_cases` are evidence/examples, not admission allowlists. Timing
hints remain exact-case-only and absent for unmeasured combinations. No extra
qualification confirmation is required for a supported request.

FL accepts text-only, first-image-only, last-image-only, or both. REF maps ordered
image lists (up to nine), up to three silent video references through
`video_guide` / `video_guide2` / `video_guide3` (`V-U`, `V+-U`, `V+*-U`), and up to
three independent audio references (`A`, `AB`, `ABD`). It requires references,
visual reference count at least audio count, at most twelve files, and explicit
aggregate bounds that prevent upstream trimming. Audio is 2–15 seconds per clip
with at most 15 seconds combined. Video uses the asset service's 24 fps native
17k+5 grid, at most 362 frames combined, and explicit 2–15 second source
selections. Host staging rechecks actual typed bytes and aggregate lengths.

These profiles map 1–100 integer steps (the existing public API range), Euler,
RES Multistep or Ralston 2S, and integer output duration 4–15 seconds. Output
canvases use the shared native presets or custom 32-pixel grid, side/area/aspect
validation; frame snapping and original controls stay visible in preflight.
Decoder/encoder configuration, precision, memory profile, attention and weight
identity remain pinned. Unmapped controls receive a specific diagnostic instead
of being silently ignored or described as untested model limitations.

Sixnine's current FL/REF mode contract remains separate. Upstream offers other
controls, including Ref2VA timeline anchors, soundtrack extraction and injected
frames; those are concrete adapter gaps rather than universal H3 exclusions.
The [pinned upstream handler](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py)
documents optional FL start/end images, the reference counts/audio-visual
relationship, multi-guide selectors and parity of pruned inputs/settings.

Current protected execution policy still imposes its explicit resource limits,
serving window, budget, exact model/manifest and capacity checks. A source change
does not rewrite that policy, extend a rental, enable a worker or authorize paid
inference. Public `generation_support` distinguishes `supported_constraints`
from `execution_policy_constraints`; always inspect the actual preflight. The
legacy BF16 candidate below remains its own frozen compiler/manifest contract.

### Offline Ref2VA candidate

`manifest-ref2va-candidate.json` is an unqualified source/weight manifest for
the separate Ref2VA BF16 transformer. It is not a cold-bootstrap environment
lock or a production profile. The transformer is 66,280,486,944 bytes with SHA256
`ca877ed2b1bf72cfda76fe38117832544d6c0530abcfb9463e114cd767a39516`, from the pinned
[HF revision](https://huggingface.co/DeepBeepMeep/MiniMax-H3/tree/adc81ccb71352192214d83d5fafb9487e860be39).
It shares the pinned companion components with FL; retaining both transformers
requires this extra storage. It does not establish enough disk headroom or
peak RAM/VRAM for mixed inputs on the intended single B200.

The first probe contract accepts one image, one silent selected video and one
audio at most. Image/video geometry is 256--832 px per side, at most 832x480
pixels and aspect 0.4--2.5. Videos select 2--3 source seconds and use the asset
service's 24 fps/56-or-73-frame normalization; audio is 2--3 seconds at 32 kHz,
stereo PCM16 WAV, with a visual reference present. Output is 832x480/124 frames,
Base/BF16/50 steps. Oversized media and unsupported roles are rejected rather
than handed to upstream's aggregate trimming. Actual video stream inspection
must prove silence, even when the immutable metadata says `has_audio=false`.

The pinned [handler](https://github.com/deepbeepmeep/Wan2GP/blob/0e58385fbde7ff102d276e4a9e490845de76b4ea/models/minimax_h3/minimax_h3_handler.py)
exposes broader source counts (9 images, 3 videos, 3 audio, 12 total) and
trims overlong combined references. These are not our qualified input limits.
`I` selects explicit image-reference dimensions; `V-U` supplies the silent
video reference, and `A` the standalone audio. No `K` soundtrack extraction,
additional guide, first/last mixing or VDN/pruned replacement is introduced.

`test_platform_wangp_ref` and `test_platform_wangp_ref_api` exercise owned
upload/selection, actual CPU media, strict mapping, explicit confirmation,
cold waiting, private fake-Session delivery and native downloads; they import
no model. Public plans now support the existing REF recipe ID, but only an
explicit singleton REF policy with native delivery and the bounded envelope
can admit it. Set `execution_recipe_id: h3-base-ref2va-v1` in a **new** controller
configuration; its absent default retains FL and historical fingerprints.
Boot requires the matching Ref manifest/compiler. Existing FL or legacy
multi-recipe policies cannot silently route REF to their current weights.

Before a live probe, bind a newly locked environment manifest to a reviewed
configuration/policy and original capacity/budget authority. This source slice
changes no enabled configuration. Upload via the existing owned asset endpoint,
then pass `images: [image_asset_id]`, `videos: [{asset_id: video_asset_id,
include_audio: false}]`, and `audios: [audio_asset_id]` in the normal generation
plan inputs. Use recipe `h3-base-ref2va-v1`, duration 5, resolution `480P`,
aspect `16:9`, and an explicit seed; omit an unused input kind. Inspect the
preflight before explicitly confirming its plan ID. A first live sequence
should qualify image, silent video, image+audio, then all three, preserving
original jobs, measured memory and actual downloaded video/audio evidence.

## Advisory subtitle inspection (#575, CR-20261026-66)

The GPU delivery host runs CPU-only RapidOCR after validating the exported MP4
and before upload. The native dstack image packages a separate
`/opt/inkseq-ocr` environment from the hash-pinned
`deploy/wangp/requirements.ocr.txt`; RapidOCR's wheel includes its OCR models.
It does not change the inference profile's environment. Existing images and the
older wheelhouse runtime without OCR dependencies return `unavailable`; do not
claim those tuples have been inspected. Runtime never installs or downloads OCR.

Two bottom-third samples per second (at most 120 / 60 seconds), one inference
thread, 45-second child wall timeout, and Linux CPU/address-space bounds keep
inspection finite. A warning requires exact normalized dialogue text, bottom
centre placement, stable geometry across at least one second, and changing
background. The journal retains only SHA256 dialogue digests from immutable
`<d>[language] ... </d>` inputs, never a second prompt or OCR transcript.
This deliberately favours precision: short/partial/incorrectly recognized lines,
stationary scenes and durations over the bound can be missed. No flag is not a
guarantee that a video has no subtitles.

The original delivery receipt and video artifact metadata retain `quality_flags`
and `quality_checks.burned_subtitles` (status, detector, and, when flagged,
confidence and times_s). The shot versions API exposes the same fields.
Legacy takes return `not_checked`; unavailable/timeout/failed are explicit.
Prepared delivery replay reuses the original decision. A detector error cannot
discard a valid video, rent a replacement, regenerate, change its seed, or affect
charging. Gemini verification is off. Claude owns warning UI, exact-tuple staging
acceptance, image/source qualification and production promotion.

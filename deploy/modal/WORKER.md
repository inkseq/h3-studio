# Modal worker host — staged implementation, default OFF

Refs #688 / roadmap #692 G2. These boundaries are not yet an activated provider.
No image, Volume, app, credential, budget, queue or running host is created by
importing them. `h3_reference.py` is a benchmark, not an application worker.

## Implemented boundary

- `ModalHosts` consumes a funded original `instance_intents` row and journals
  the sole external create/stop in existing `scaler_actions`/`scaler_receipts`.
  A process crash or missing response never authorizes another create. Unknown
  and stopping hosts consume the same two-host ceiling across pools.
- Admission requires staging, explicit enablement, a pinned app/image/Volume,
  full finite-life funding plus shutdown margin, and healthy original budget
  reservations. Exit proves removal only. It does not invent a zero invoice or
  release money. Default and production admission refuse before provider I/O.
- The SDK boundary is pinned to Modal 1.6.3. It resolves the original app and
  immutable image/Volume IDs without implicit creation/builds. Limits: one L40S,
  41 GiB memory reservation/limit, 2 CPU reservation/4 CPU limit, two hosts.
  The all-resource hourly ceiling must be qualified before live adoption.
- Sandboxes are process hosts; they do not introduce a second generation queue.
  `ModalHTTPTransport` carries the existing private WanGP protocol over SDK
  exec, including inputs, incarnation fences and GPU/R2 output delivery. Tokens
  and request bodies use stdin, not command arguments, logs or public tunnels.
- Modal's SDK `idle_timeout` does **not** track asynchronous inference in the
  entrypoint, so it is disabled. The qualified entrypoint must use the native
  runtime hooks: atomic admission close after 60 seconds of verified business
  idle, no in-flight transfers, no journal obligations and no undelivered
  successful output. Readiness probes do not extend idle. The provider timeout
  remains the crash fallback. A qualified image must wire these hooks before
  any live adoption; the boundary alone is not a ready worker image.
- L40S is an explicit launcher-only hardware variant for v7: exact L40S name,
  SM 8.9, 44–49 GiB observed VRAM, one physical GPU lock. The default launcher
  remains RTX 5090. Model files, five steps, compiler and immutable accepted
  manifests are unchanged. This is a candidate needing real GPU acceptance,
  not a claim of inference qualification.

## Native entrypoint and image context (not an image-build receipt)

`tools/build_modal_sources.py --output NEW_DIRECTORY` copies only the finite
existing private-runtime allowlist plus the Modal entrypoint into a clean build
context. It preserves `deploy/dstack/Dockerfile.wangp`'s pinned public native
image and v7 dependency checks. The benchmark's Python 3.11 plus unconstrained
requirements resolver is not substituted for the accepted native environment.
The Volume model-cache pattern is retained; journals and outputs are private.

The image CMD waits at most 60 seconds for the CPU controller's one private
launch document, bound to original intent, mode, manifest and protocol token.
No business request or provider credential is accepted there. The entrypoint
uses the normal full source/dependency/weight verification before loading and
then the receipt-aware idle hooks. Missing/mismatched cache bytes refuse; live
containers never concurrently download/write the shared model cache. A restart
with an existing private state directory refuses rather than replaying work.

`modal_launch.initialize` installs the launch binding on an already journaled
Sandbox via private stdin. The native installer publishes a complete private
directory atomically; the same binding is read back without rewriting, changed
bindings or interrupted partial installs refuse. No provider credentials or
business input are included. A missing SDK response stays unknown and is never
retried automatically. The existing CPU backend factory can accept this private
HTTP transport while retaining its original manifest, profile, token and runtime
incarnation checks; that injection does not change worker claim/settlement logic.

## Remaining activation slice (do not enable this boundary alone)

Build and pin the image, qualify/populate the existing Volume with the exact
hashed v7 model files under one recorded preparation operation, and verify GPU
startup. The context receipt is only local source evidence, not a built image
or runtime qualification. Keep SQLite operation journals private to each
container; the shared Volume is a model cache, not a concurrent receipt database.

Wire the existing CPU WorkerRunner and DBOS registry, original budgets and the
CR73 combined-task projection. Freeze the configured profile and funded intent
before dispatch; choose a compatible ready dedicated worker first, otherwise
admit Modal promptly while the independent Vast lookahead rule continues.
Complete readiness/incarnation recovery and actual CPU collection tests before
one reviewed staging end-to-end job. No accepted job may be rewritten to obtain
an alternative profile. Only then can the item be deployed/adopted and Done.

Existing staging credential: `/sixnine/staging/modal`, loaded only by the
existing authorized role. Never import the obsolete secret-drop file or pass
provider/database credentials into the GPU image. Production remains OFF.

## Shared test-budget admission

`modal_budget.bind_test_guard` records the immutable task/spec binding in the
original scaler receipts, in the same capacity-locked transaction as the
original instance reservation. It must be called before committing a new Modal
intent. It creates no table, budget account, rental or job. Both providers use
`dstack_test_budget.projected_cost`: the shared-account layer is counted once,
including Modal commitments and authoritative settlements. Unknown billing and
confirmed process exit retain the original commitment; an unconfirmed running
host beyond its expected lifetime increases the conservative projection.

ModalHosts rechecks this binding and the combined task projection before its
sole create when the shared staging account funds the intent. An unattributed
or changed binding refuses; repeated binding only observes identical evidence.
The task ceiling cannot exceed CR73's USD50, and original account/pool limits
still apply. The caller must catch refusal outside the reservation transaction
so an unfunded intent cannot remain committed. Old daily guards cannot silently
reinterpret a Modal task. This boundary is tested with fake clients; controller
routing, real image qualification and activation remain unfinished above.

Readiness, claim and the original late-submit guard recheck the same Modal
binding, current shared account limit and task projection. A stopped, expired,
unfunded or unattributed host cannot take new work. Existing recovery/collection
retains its original attempt path. Within the existing DBOS slots, new work tries
dedicated hosts first and falls back to Modal only when the original dedicated
runner cannot claim it; existing attempts retain their original worker affinity.
This preference creates no provider resource, alternate queue or new job.

## Primary implementation references

- [Modal official examples](https://github.com/modal-labs/modal-examples)
- [Function crash/retry semantics](https://modal.com/docs/guide/retries)
- [Sandbox lifetime and activity definition](https://modal.com/docs/guide/sandboxes)
- [Pinned SDK-compatible Sandbox interface](https://modal.com/docs/sdk/py/latest/Sandbox)
- [Image CMD used by Sandboxes](https://modal.com/docs/sdk/py/latest/Image)
- [NVIDIA L40S hardware](https://www.nvidia.com/en-in/data-center/l40s/)
- [NVIDIA compute capabilities](https://developer.nvidia.com/cuda/gpus)

These references inform the implementation; they do not grant spend or change
the current owner budget. Live proof remains a separate original receipt.

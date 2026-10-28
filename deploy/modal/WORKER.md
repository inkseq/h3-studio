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

## Remaining activation slice (do not enable this boundary alone)

Build and verify a native image using `deploy/modal/h3_reference.py`'s CUDA
12.8/Python 3.11/WanGP revision and Volume pattern, the exact v7 dependencies and
hashed model files. Keep SQLite operation journals private to each container;
the shared Volume is a model cache, not a concurrent receipt database.

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

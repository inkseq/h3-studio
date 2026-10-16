## PostgreSQL queue wake migration (#217)

New artifacts use manifest version 2, layout `postgres-notify-v1` and omit
Hatchet templates, broker services, broker mounts and the dispatcher. The
existing fenced PostgreSQL queue remains authoritative. Immutable `hatchet-v1`
route values are historical identity, consumed by the new per-slot runner.

Install the independently reviewed helper/checker closure first. Deploy with
the existing approved activation path and retain paths.json version 1, its
active hash and managed secret unchanged: old path references are unused by
the new overlay. No manual marker edit or credential migration is required.
Future version 2 paths omit broker references; secret hydration can project the
old v1 secret without changing credentials. Preserve all old secret files.

The protected release lock covers stopping exactly verified CPU containers
(dispatch, hatchet, hatchet-db), then recreating app/dstack/controller and
checking exact images, namespace, mounts, provenance and health. After health,
remove only those verified old containers without volumes; ready retries finish
cleanup idempotently. Failure retains pending state for reconciliation.
Rollback requires the independently approved original v1 artifact with retained
named volumes/secrets; no down/remove-orphans, new broker bootstrap or GPU
operation. Jobs, attempts, leases, budgets and native runs remain unchanged.

Accept on staging first, including queue wake/reconnect and idle CPU evidence;
production promotes the same artifact bytes, without rebuild.

# dstack capacity boundary

`studio_platform.dstack_capacity` targets dstack **0.22.3** using its REST API.
The pinned CPU server in `compose.yml` owns Vast/RunPod credentials and a separate
dstack database/user. The business database retains original capacity intents,
reservations, accepted jobs, attempts and output obligations. The dstack database
is infrastructure metadata, not another business queue or telemetry archive.
The integrated app accesses loopback port 3000 through the shared namespace
described below; a container's own loopback cannot reach a standalone host port.
Do not publish the administrator endpoint.

Current #132 qualification explicitly uses central
`vastai/vastai--sixnine-dstack-20261011` and
`runpod/runpod--sixnine-dstack-20261011` with unchanged pinned-client protocols.
Preserve old profiles. Authenticated stock checks are read-only for both; paid
P2 is Vast only and RunPod remains read-only. Before production activation,
qualify two independent single-GPU runs (one FL, one REF), warm successive jobs,
business idle stop and keep-on within the original deadline.

Bind both source selections and protected execution/capacity configuration to
`h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3`, with new configuration
identities and an explicit default. This additive revision has a 29 GiB
allocation floor and a 29 GiB native total-capacity check, using the minimum
of host MemTotal and all visible finite cgroup-v2 ancestor limits. Invalid
constrained scope fails closed. #130 supports total allocation, not a 29 GiB
available-headroom requirement. Record available/anonymous/cache/peak memory
separately. Preserve the issued v2 headroom contract, original 96 GiB manifests and
unpruned/PRO 6000 floors unchanged; model support is not a historical-case
whitelist and this profile is unqualified until its own integration evidence.

The integration owner supplies `DstackStore` over the existing ledger. Its
`begin_apply` transaction commits the immutable run spec and original reservation
once before `/runs/apply`. It returns false for any previously started operation.
A lost response uses exact `/runs/get` with run UUID or stable owned name; absence
is unknown and does not permit another apply. `force:false/current_resource:null`
also rejects a concurrent existing dstack resource. `record` retains immutable
run/provider/incarnation identity. `begin_stop` locks and checks jobs, unsafe
attempts, collection holds and retired workers before committing stop intent.
Cold preparation with no worker history or native incarnation/first-ready latch
may be stopped during model download after fresh exact-instance checks. A
previously admitted runtime still needs native idle and original obligations
resolved; disappearance is not final supplier-bill settlement.
Do not provide a file-based substitute ledger in production or the operations CLI.

Each new run requests exactly **one GPU**, explicit backend, image digest,
supported model/profile/mode and resource floors. A container whose allocation
reports more than one GPU cannot become ready. Additional GPUs need independently
registered native slots and proof; a whole multi-GPU price must not hide idle GPUs.
`get_plan` is advisory inventory and never rents. A failed provider does not
silently substitute another model, GPU, backend or precision. Historical test
cases are timing evidence, not an input whitelist.

The run command remains alive (`sleep infinity`) while the CPU controller uses
the existing SSH transport to upload the exact native profile bundle/manifest,
private loopback token and `native_bootstrap_config`, then invokes once:

```sh
/venv/main/bin/python /opt/sixnine/wangp-bootstrap.py \
  --config /workspace/h3-studio/profile-slot-0/wangp-runtime.json \
  --slot-key ORIGINAL_INTENT_UUID \
  --token-file /workspace/h3-studio/profile-slot-0/wangp-token
```

This reuses `wangp_profile_bootstrap`, the hash-checked model downloader and
`wangp_launcher`'s persistent `PinnedWanGPSession`. Dependencies belong in a
captured image (`Dockerfile.wangp`), not fresh apt/pip operations on each rental.
The derivative requires a digest-pinned **native** image with exact Python3.12.14
and profile core versions; the older BF16 resolver image is not interchangeable.
Pin the derivative's published digest in `CapacityRequest`. An image build alone
does not establish NVIDIA compatibility or real inference. First startup hashes
and loads public model files; cache availability depends on the backend/region.
RunPod network volumes have separate storage cost and locality restrictions.
Vast's local volume support is not automatically exposed by dstack.

dstack `running` means the command is running. Native readiness separately binds
the exact manifest, slot, provider instance and runtime incarnation. Even a native
ready Session does not prove the transformer has loaded: `model_load_state` stays
`not_observed` until trusted runtime evidence observes it. The first accepted
task may therefore incur model load. Later jobs reuse the same Session; profile
offloading can still occur inside WanGP. No extra paid warm-up test is fabricated.

The controller measures **business** idle time. It stops only after 600 seconds
without queued demand, running/unknown attempts or collection holds, subject to
configured manual hold and the original absolute deadline. `max_duration` is a
runner fallback measured from dstack running; it excludes provisioning/image
pull and is not an absolute billing cap. Deadline admission must leave time to
finish/collect before that fallback. Deadline plus outstanding work means drain
and reconcile, not erase obligations. A stop response is not a zero invoice;
dstack cost is explicitly an estimate until supplier billing settles.

The trusted CPU CLI uses a service factory over the original ledger:

```sh
python -m studio_platform.dstack_capacity spec --request /protected/request.json --ssh-public-key /protected/controller.pub
python -m studio_platform.dstack_capacity bootstrap-config --request /protected/request.json --source-bundle-sha256 HASH
python -m studio_platform.dstack_capacity plan --request /protected/request.json --ssh-public-key /protected/controller.pub --factory studio_platform.dstack_operator:create_capacity
python -m studio_platform.dstack_capacity start --request /protected/request.json --ssh-public-key /protected/controller.pub --factory studio_platform.dstack_operator:create_capacity
python -m studio_platform.dstack_capacity observe --request /protected/request.json --factory studio_platform.dstack_operator:create_capacity
python -m studio_platform.dstack_capacity stop --request /protected/request.json --factory studio_platform.dstack_operator:create_capacity
```

The integration factory is supplied by the main application integration; it must
never auto-create a replacement store. The first two commands are inert and do
not contact a provider. Remaining commands obey existing authority and journals.
Keep manifests, request files and protected tokens outside the public checkout.
No secret is accepted in a CLI argument or serialized into a GPU run spec.

Grafana Cloud receives allowlisted business stage events from the CPU telemetry
adapter. dstack's automatic HTTP/log instrumentation is disabled here because raw
request bodies, provider responses and media URLs must not enter Cloud telemetry.
Short bounded Docker logs remain operational transport buffers, not long-term
storage. The wider rollout/retirement gate is tracked in issue #132.

## Existing platform Compose integration

Merge `platform-overlay.yaml` **after** `deploy/platform/compose.yaml`, in that
order. Relative overlay paths resolve against the first Compose file's directory.
It preserves the app, database, assets, frontend and Caddy ingress. CPU dstack,
The controller and dstack server join `network_mode: service:app`; their private
dstack loopback endpoint is `127.0.0.1:3000`.
The controller creates the native SSH tunnels in this namespace, so its child
slot workers can use their original `127.0.0.1:19100...` endpoints. None of
these management ports is published or added to Caddy. There is no GPU device
request in this CPU deployment.

Supply only nonsecret paths, volume names, reviewed image digests and existing
operator owners through the established protected host loader; do not make a
project `.env`. `SIXNINE_IMAGE` and `SIXNINE_DISPATCH_IMAGE` must identify the
exact reviewed source and matching dispatch dependencies. This overlay uses
`pull_policy: never`, so separate image verification must precede service startup.
No image is built, pushed or deployed by the checker.

All app/controller processes use the **same** mounted
`app_database_url` and `/srv/sixnine/platform-data`. Historical Hatchet volumes
and protected credentials remain untouched for approved v1 rollback; they are
not mounted or started by the active overlay. Never run `down -v` or reset data.

dstack uses an independently provisioned infrastructure database/role on the
private original PostgreSQL server: the protected DSN must select driver
`postgresql+asyncpg`, host `db`, role `dstack_control`, database `sixnine_dstack`.
It never receives the business DSN or DB administrator password. Provision that
least-privilege role/database separately under the release owner; this overlay
does not create it, budgets, pools or approval policies. dstack's startup manages
only its own infrastructure schema. Preserve `SIXNINE_DSTACK_STATE_DIR` (SSH/server
state) and the existing dstack token. The pinned server seeds an initial token
only when its admin user does not exist; changing the environment is **not** an
existing-token rotation. `--log-level WARNING` prevents the upstream INFO startup
message from printing that token. The automatic OTel enable variables must be
absent: even the value `"0"` enables them in 0.22.3.
Its CLI also writes a daily update-check marker in `/root/.cache` before starting
the server. A 1 MiB private tmpfs provides that cache on the read-only container;
the entrypoint seeds only this nonsecret marker to prevent startup update probes.

Provision existing protected files outside the checkout, readable by their
intended CPU UID. App/controller-only files belong to UID 10001 with mode 0600;
the dstack infrastructure DSN and persistent state belong to root. Shared dstack
API token/public-key files belong to `root:10001` with mode 0640, so both intended
CPU identities can read them. Group write/execute and all world permissions stay
disabled. Docker's local-file secret mount does not repair host ownership, and
root with `cap_drop: ALL` cannot read another user's mode-0600 files. Do not add
DAC override capabilities or weaken the protected loader. The original title
config and app DB mounts remain attached. Additional file bindings are:

| Protected source variable | Purpose and in-container path |
| --- | --- |
| `SIXNINE_EXECUTION_PROFILES_SECRET_FILE` | Original admission profiles, `/run/secrets/execution_profiles`; accepted jobs retain frozen routes. |
| `SIXNINE_GRAFANA_CONFIG_FILE` | Existing Grafana Cloud safe exporter configuration, `/run/secrets/grafana_cloud`; Cloud is the telemetry store. |
| `SIXNINE_DSTACK_OPERATOR_CONFIG_FILE` | Original-ledger capacity policy, `/run/secrets/dstack_operator_config`; endpoint `http://127.0.0.1:3000`, token/public-key paths below. |
| `SIXNINE_DSTACK_RUNTIME_CONFIG_FILE` | Native factory config, `/run/secrets/dstack_runtime_config`; work `/dstack-runtime`, source index `/dstack-sources/index.json`. |
| `SIXNINE_DSTACK_TOKEN_FILE`, `SIXNINE_DSTACK_DSN_FILE` | CPU-only API token `/run/secrets/dstack_api_token` and infra DSN `/run/secrets/dstack_database_url`. |
| `SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE`, `SIXNINE_GPU_SSH_KEY_FILE` | Original controller key pair, `/run/secrets/dstack_ssh_public_key` and controller-only `/run/secrets/gpu_ssh_key`. |
| `SIXNINE_HATCHET_BROKER_CONFIG_FILE`, `SIXNINE_HATCHET_TOKEN_FILE` | Broker config `/run/secrets/hatchet_broker` using the loopback HTTP/gRPC addresses above and token `/run/secrets/hatchet_api_token`. |
| `SIXNINE_HATCHET_SECRET_DIR` | Existing `database-password` and `administrator-password`; no new default credentials. |

The native runtime config uses `known_hosts_file: /dstack-runtime/known-hosts` as
a prefix. Each immutable intent gets `/dstack-runtime/known-hosts.<intent UUID>`;
old IP pins are preserved and never shared across new leases. The default
`trust_first_host_key: false` needs an explicitly established per-instance pin.
An approved initial TOFU setting is an explicit protected policy, not a bypass.
Generate only selected source sets with `tools/build_dstack_sources.py` and mount
their parent as `SIXNINE_DSTACK_SOURCE_DIR` read-only. Model and provider secrets
never belong in that four-file bundle. The example runtime JSON must use the
container paths in this section rather than the standalone host paths.

The aggregate service memory **cap**, including the one-off existing db-init,
is 2528 MiB, leaving 1312 MiB on a 3840 MiB host. This is a configuration budget,
not a measured resource guarantee. dstack receives 768 MiB for server startup;
the 512 MiB cap caused repeated kernel OOM kills even when Docker's latest
`State.OOMKilled` was false after a restart. Check kernel OOM records and restart history as well as
current container state when qualifying these caps. The controller's 640 MiB
includes its native CPU worker children; qualify at most two concurrent slots initially and measure
actual RSS, swap/OOM and media collection before expanding. Existing services
outside this Compose project also consume host memory and must be counted.
The overlay does not enable generation by default. Explicit existing release
authority may set `SIXNINE_MIGRATION_GENERATION_ENABLED=1` together with
`SIXNINE_MIGRATION_EXECUTION_BACKEND=wangp-worker` after acceptance; no budget or
capacity authority is inferred from these switches.

Run the read-only checker after the nonsecret variables are hydrated:

```sh
python tools/check_migration_deploy.py --local-inputs
```

It suppresses automatic dotenv discovery, captures Compose rendering in memory,
and prints only a bounded structural receipt. It never prints resolved config,
credentials or Docker stderr and never executes `up`, `pull`, `stop` or `create`.
After the release owner starts the existing services, `--probe` executes only
read-only private health/auth requests through app: dstack must reject an
unauthenticated user read and accept the configured admin token. Health alone
is not native GPU or slot-consumer readiness; verify the actual consumer fence
and heartbeat separately.

An opt-in isolated CPU proof runs the exact rendered protected-file entrypoint
against a fresh non-admin PostgreSQL database, with synthetic credentials and
provider configuration/background provisioning disabled. It requires Linux root
to provision the same root-owned mount permissions; both pinned images must
already exist locally. It creates only uniquely tagged temporary CPU containers
and a private network, then removes only those owned resources in `finally`.

```sh
SIXNINE_DSTACK_CPU_PROOF=1 python3 -m unittest test_migration_deploy.DstackCPUProof -q
```

This opt-in is separate from the read-only deployment checker and does not use
the live business database, Hatchet volumes, provider accounts or cloud hosts.

**Shared namespace release boundary:** recreating `app` also replaces its network
namespace. Coordinate recreation/reconnection of all four namespace members in
one reviewed release; an app-only `up` can leave them attached to an obsolete
namespace. Retain original jobs, attempts, broker/transport state and budgets,
reconcile unknown work and reconnect original native runtime incarnations.
Never treat stopping a CPU container as provider shutdown or zero billing.
Rollback changes admission for future jobs; accepted Hatchet/legacy jobs keep
their original execution route and recovery services. The root release owner
records the exact deployment and recovery evidence; a successful Compose check
is not publication or public video acceptance.

References: [pinned run schemas](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/server/schemas/runs.py),
[pinned configuration models](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/core/models/configurations.py),
[run API](https://dstack.ai/docs/reference/api/runs/),
[backend boundaries](https://github.com/dstackai/dstack/blob/0.22.3/contributing/BACKENDS.md).
Deployment behavior was checked against the pinned
[server image recipe](https://github.com/dstackai/dstack/blob/0.22.3/docker/server/release/Dockerfile),
[server CLI](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/cli/commands/server.py),
and [startup/authentication](https://github.com/dstackai/dstack/blob/0.22.3/src/dstack/_internal/server/app.py).

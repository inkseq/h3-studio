# Sixnine CPU dispatch

This pinned authenticated Hatchet Lite deployment is for the current low-volume
CPU control host. It uses PostgreSQL for the Hatchet engine/message queue and
does not require RabbitMQ. Hatchet's database is separate from the business
ledger. Existing jobs, attempts, media and budgets never migrate into Hatchet.
The container pair is limited to 1712 MiB; this is a cap, not measured steady use.
The Python execution worker requires Linux (the upstream SDK uses Unix signals).
On Windows, run CPU qualification in a local Linux container or WSL runtime.

Both HTTP and gRPC are published on loopback only. Do not expose the cleartext
loopback ports publicly. Operators can use a protected SSH port forward. Changing
ingress requires its own authenticated TLS/proxy design. The image is the normal
authenticated Lite image, not `hatchet-lite-dev`; no fixed development token or
default administrator password is used.

Provision `database-password` and `administrator-password` in an explicitly
protected directory outside the repository and supply its **nonsecret path** as
`SIXNINE_HATCHET_SECRET_DIR`. This compose file reads mounted Docker secrets and
the wrapper injects only into the intended service. Do not create a project
`.env`, paste credentials into arguments, inspect container environment dumps,
or commit `/config` (it contains private signing/encryption keys). Preserve the
named volumes and back them up before engine upgrades; a source deploy never
resets them. Set the initial admin password before first boot, not after seeding.
The database password must use random URL-safe characters; the pinned migration
binary requires a DSN, so the wrapper constructs it internally without printing.

On the Linux CPU host, initialize the protected mode-0700 directory and mode-0600
files, then start Compose explicitly and capture the authenticated token:

```sh
python deploy/hatchet/bootstrap.py init --secret-dir /var/lib/sixnine/hatchet-secrets
export SIXNINE_HATCHET_SECRET_DIR=/var/lib/sixnine/hatchet-secrets
docker compose -f deploy/hatchet/compose.yaml up -d
python deploy/hatchet/bootstrap.py token --secret-dir /var/lib/sixnine/hatchet-secrets
```

The bootstrap utility preserves existing initial passwords, refuses to overwrite
an existing API token and captures token output in memory rather than the
terminal. The 2160-hour token needs a planned rotation before expiration.
Provision ownership for the dedicated CPU dispatcher UID before its token mount;
keep directory 0700 and token 0600 rather than relaxing group/world access.

The pinned engine binds API tokens to its configured `SERVER_URL` claim. A token
created by the standalone loopback deployment (`http://127.0.0.1:18888`) cannot
be reused unchanged when the migration engine uses `http://127.0.0.1:8888`:
overriding the SDK's endpoint does not change the signed token, and the engine
can return HTTP 403 even when the tenant and expiration are correct. Before
switching deployments, check the URL binding in memory against the actual
engine configuration. An authorized rotation creates a new protected token with
the same intended tenant/permissions and bounded expiry; preserve the old file
and engine volumes until readback succeeds. Do not disable authentication,
broaden privileges or print token claims/contents to diagnose the mismatch.
Readiness now distinguishes safe HTTP rejection categories from an oversized
response; all of these remain negative consumer proof.

The default tenant UUID is `707d0855-80ab-4e1f-a156-f1c4546cbf52`. An operator can
create a worker token with the bundled `/hatchet-admin token create --config
/config --tenant-id ...` command, capturing stdout directly into a protected
file. Never print the token. `broker.example.json` is only a nonsecret path and
endpoint example; copy to the protected runtime configuration directory, not a
second credential store. Python's explicit client configuration disables SDK
dotenv discovery and broad log capture.
Automatic Hatchet OpenTelemetry task attributes are excluded; platform telemetry
uses its separate safe attribute allowlist and never exports task payloads.

Install the dispatch-service dependencies from
`requirements.dispatch.lock.txt` with hash checking. The GPU image does not
install Hatchet or receive database/object-store/provider credentials.

Run the existing CPU runtime settings loader with:

```sh
python -m studio_platform.hatchet_dispatch dispatcher --broker-config /absolute/broker.json
python -m studio_platform.hatchet_dispatch worker --broker-config /absolute/broker.json \
  --fleet-config /absolute/fleet-v2.json --worker-id existing-physical-gpu-slot-id
```

Every new fleet v2 slot must explicitly bind `dispatch_backend: hatchet-v1` and
one physical GPU. The matching accepted `execution_plan` freezes that same route.
Absent route remains `legacy`. A worker is advertised through the existing
runtime readiness proof, not because the Hatchet service or GPU VM is running.

These client endpoint examples assume the Python processes run on the CPU host.
Container clients need a shared Hatchet network namespace or an authenticated
TLS endpoint; their own `127.0.0.1` does not reach another container or the host.
The private business DB remains a separately configured protected connection.

Each worker uses one Hatchet execution slot and required immutable binding,
profile and FL/REF labels. Hatchet retries are zero; the business adapter records
submission intent before GPU POST. A lost response reconciles the original
attempt tag. Broker timeout/disconnect cannot release the GPU or requeue paid
inference. The recovery dispatcher only wakes the original job after the broker
run ends, and collection reuses durable `ArtifactWriter` receipts. Published
outbox deliveries are not re-enqueued per GPU status poll.

Preserve queued work during rollback: switch admission only for future jobs;
continue the original Hatchet worker/dispatcher for accepted Hatchet jobs and the
legacy route for legacy jobs. Do not rewrite immutable job routes or kill the
engine/volumes to roll back an app release. Parent migration #132 owns live
qualification and the 14-day retirement gate.

Official references: [Lite deployment](https://docs.hatchet.run/self-hosting/hatchet-lite),
[engine configuration](https://docs.hatchet.run/self-hosting/configuration-options),
[Python SDK](https://docs.hatchet.run/reference/python/client),
[idempotency](https://docs.hatchet.run/v1/idempotency).

Local qualification is opt-in: point `HATCHET_PROOF_CONFIG` at an authenticated
isolated loopback engine configuration and run `python -m unittest
test_hatchet_service`. This uses a temporary original SQLite ledger, CPU-labelled
simulation video/audio and no GPU/provider network calls. Set
`HATCHET_PROOF_LONG_SECONDS=610` to exercise a task beyond ten minutes. The test
injects broker-response loss and inference-response loss, checks the original
attempt/submission/fetch count and leaves an older planned draft untouched.
The fixture follows the production dispatcher recovery cadence from its protected
broker configuration. If a callback yields during collection backoff, another
wakeup event may resume the same original job and attempt. The receipt separates
the original lost-response retry from subsequent recovery event/run counts and
records the recovery interval; it does not require one broker event for the whole
business lifecycle. A failure preserves a mode-0600 SQLite diagnostic snapshot
beside the protected broker configuration, for investigation only.

Engine v0.110.5 observations: the steady registered-workflow proof returned the
same accepted run ID for duplicate event delivery. One first-boot round accepted
two broker run IDs; its business ledger still submitted inference once. Therefore
Hatchet TTL deduplication is an optimization, never an exactly-once guarantee or
permission to remove the original ledger's fences. A broker run ending remains
only a reconciliation wakeup. Issue #132 records final qualification evidence.

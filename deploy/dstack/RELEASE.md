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

# Approved CPU migration release

This route runs the dstack server and PostgreSQL queue controller alongside
the existing `sixnine-platform` Compose project. It does not rent a GPU, stop a
provider instance, settle a bill, reset accepted jobs or delete storage. Native
GPU images are published separately and are never downloaded to this CPU host.

## Finite GPU/R2 qualification (#174)

Qualification uses the normal owner-confirmed job, registered slot, PostgreSQL queue
worker, immutable native operation and artifact writer. It does not run a
parallel collector, replay a settled write, or regenerate an old result to
repair storage. Keep the original CPU delivery route available for retained
jobs and rollback; a qualification release is not a default-route switch.

The normal CPU worker has one deliberately disabled acceptance seam: an absent
`qualification-drop-delivery-ack.json` in that worker's existing work directory
means no injected fault. A qualification operator may install one bounded,
root-owned control file after the exact test attempt is claimed and before
collection. It contains only closed identity fields, a versioned mode and an
expiry of at most one hour: owner, job, attempt, request hash, deployment profile,
manifest, slot and native incarnation must match the same uploaded receipt.
No bearer URL or credential belongs in the control file. The file is not an
HTTP/request option; an owner cannot request it through a generation parameter.

The injected fault discards only a validated, complete upload acknowledgement,
after both original output objects are uploaded with stop proof. It is consumed
in memory before raising the fixed error. The existing unknown-write branch
then verifies those same immutable R2 keys and commits the original job. On a
worker restart, matching objects are read before another upload is considered.
Do not inject midway through a PUT or invent an upload-stop fact. Invalid,
expired, foreign, linked or writable-by-others controls cannot inject a fault;
ordinary jobs retain their existing behavior. Remove the disposable control
after recording the actual injection and recovery evidence.

Record collection-scoped controller/cgroup and CPU-host memory, R2 descriptors,
native stage durations, normal website/PAT downloads and full decode/hash
checks. Whole cold-start windows are not collection benchmarks. Expired and
wrong-key grants must be tested against R2 without exposing permanent storage
credentials to the GPU; wrong-size/hash rejection must retain the original
write and job identities.

For a matched media-processing baseline, wait until the new job succeeds and
its normal collection measurement ends. Read its original sealed native files
through the existing authenticated, size/hash-bounded artifact transport into
a protected benchmark fixture. Run only the existing `GPUDelivery._export`
export/validation code in a finite isolated CPU container using the exact CPU
image, a 640 MiB limit, no network, no secrets/database/active volumes, read-only
fixture input and a separate writable benchmark output. This is media-only
measurement, not a second business collection or publication. Compare its
encoding/validation time and cgroup peak with the same job's GPU receipt and
normal controller observation; report fetch, upload and commit separately.
An OOM is a ceiling hit with censored timing, not a successful baseline. Keep
historical unknown measurements explicit and stop all qualification machines
before accepting the issue.

The ordinary platform bundle remains unchanged. The optional migration bundle
contains the exact tested dispatch image archive, the reviewed overlay/checker
and maintenance source, nonsecret examples, and selected public four-file WanGP
source sets. It contains no API token, SSH private key, Grafana credential, model
weight or local `.env`.

The #193 migration also includes the exact CPU dstack safety image and installed
provenance. It derives from the pinned 0.22.5 base and verifies six upstream
source hashes before applying the bounded Vast fixes: qualified-offer fallback,
shared 429 cooldown, explicit no-contract evidence, and disabled idle catalog
preload. The original native allocator and GPU image/source bundles remain in
use. The protected migration manifest binds the derivative image, helper and
installed source hashes before publication/loading; old approved artifacts keep
their original pinned dstack image for rollback.

#517 B1 trusts only the complete 0.22.3 and 0.22.5 stock tuples: exact version,
upstream commit, base-image digest and all six original source hashes. Mixed tuples
are rejected before loading. The installed package version and every patched file
are then checked inside the derivative. Build publication still requires the
current checkout's exact provenance/installer/helper bytes; accepting the retained
0.22.3 tuple does not permit building an old image as the current release. Artifacts
that predate derivative images retain their original 0.22.3 stock fallback.

Before replacing the CPU server, qualify the same isolated PostgreSQL state and
synthetic administrator token through 0.22.3 → 0.22.5 → 0.22.3, including project
identity, schema head and anonymous/invalid-token refusal. This fixture has no
provider configuration or work. Then deploy normally to staging and run one bounded
Vast cold cycle with the original task budget and removal evidence. Preserve the
actual retained helper/checker pairs and rollback artifact; this proof does not
cover new-provider data created by later B2/B3 work.

Controller and slot workers subscribe to committed PostgreSQL wake hints before
scanning the original ledgers. With no work or unresolved outcome they block;
only the controller performs a read-only account safety sweep every15minutes.
Active observations/idle-stop cadence is unchanged. Notifications do not grant
permission to rent or replay a task.

A failed start releases its original reservation automatically only when its
exact native submission carries the complete typed no-contract rejection proof.
Unknown PUTs, returned contracts and incomplete/legacy summaries retain their
obligations. A historical start the owner explicitly confirms was unallocated
can use the trusted one-shot `review_unallocated_start` operation: it re-reads
the exact failed run and a fresh complete empty same-provider account, then
commits the original row/zero settlement/review receipt atomically. This is not
a public endpoint or a background settlement rule. Use it only for the exact
owner-authorized record; do not rerun consumed operational scripts.

## Build and independent approval

Build the tested app and dispatch images with the complete source commit label.
Use `tools/build_platform_release.py COMMIT PLATFORM_DIRECTORY` for the ordinary
bundle. Use its `--migration-destination MIGRATION_DIRECTORY` and repeated
`--migration-selection PROFILE:MODE` arguments for the additive bundle, or call
`build_migration(COMMIT, PLATFORM_DIRECTORY, MIGRATION_DIRECTORY, selections)`
when an already tested ordinary artifact has the same exact commit.

The platform preparation workflow includes Pruned FL, Pruned REF, and the
existing `h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1:fl` selection
for storyboard Final. Each dstack source entry contains one GPU and slot zero;
this does not change the original native profile's independent-process model
or its resource admission. With an explicit prior preparation run/commit/index
binding, adding a catalog selection preserves the old selections in their
original order and every old four-file byte/hash. The new selection uses the
verified retained package whose members still equal current native source,
plus its current canonical manifest/runtime. The resulting archive and index
are new artifacts; the existing GPU bootstrap receipts still match their old
entries. Dropping/reordering old choices, changed native source, tampered
archives, or missing provenance fails the preparation. No native runtime is
restarted or re-uploaded by this build step.

The nonsecret `base-fl.operator-profiles.example.json` and
`base-fl.execution-policy.example.json` are reviewed configuration fragments,
not automatically activated release inputs. They bind Base FL to the existing
native image digest, the explicit `sixnine-dstack-base-fl-20261014` pool and
`sixnine-h3-base-int8-fl-20261014` configuration. The operator fragments keep
owner/project/budget isolation and request one physical GPU, 256 GiB total RAM,
350 GiB disk and 90 GiB GPU memory, with a US$2.00/hour maximum. These stricter
host requirements preserve the catalog's original 256 GiB host evidence and
128 GiB available-headroom check; no 29 GiB Pruned bound is applied to Base.
Only pinned scheduler names `RTXPRO6000S` and `RTXPRO6000WS` map to the native
Server/Workstation identities. Max-Q and generic PRO6000 names are excluded;
CUDA still independently verifies the actual full physical name, capability
12.0 and 90–100 GiB VRAM.

Install the two owner-scoped operator fragments through the existing protected
configuration process, without changing global capacity, lifecycle or budget
limits. Replace the one existing Base FL execution-profile record with its
new explicit `hatchet-v1` route after review; do not append a duplicate
profile/recipe. Preserve the old Base REF record and every accepted job's
frozen execution route. The example execution policy is disabled and marked
pending acceptance; refreshing its evidence/authority and enabling it requires
the authorized release owner's bounded real acceptance. The runtime configuration
keeps the existing source-index path; the new approved index supplies Base FL.
Neither these examples nor preparation of an archive rents a GPU or certifies
Final generation success.

`tools/publish_aws_release.py COMMIT PLATFORM_DIRECTORY --migration-directory
MIGRATION_DIRECTORY` performs conditional immutable uploads under
`releases/COMMIT/` and `releases/COMMIT/migration/`. Publication never approves
or deploys either bundle. A retry accepts existing objects only when their
stored hash and byte count match; it cannot overwrite a published object.

Independent root review installs the exact reviewed code, rather than executing
code obtained from an incoming bundle:

| Reviewed source | Fixed root installation |
| --- | --- |
| `deploy/platform/release.py` | `/opt/sixnine-release/release.py` |
| `deploy/platform/deploy_approved.py` | `/opt/sixnine-release/deploy_approved.py` |
| `deploy/dstack/maintenance.py` | `/opt/sixnine-release/migration_maintenance.py` |
| `tools/check_migration_deploy.py` | `/opt/sixnine-release/migration_check.py` |

Preserve bytes when installing; the helper and checker must match their hashes
in the separately approved migration artifact. The existing trusted
`fetch_release_s3.py`, Docker executable/configuration, SSM document and release
lock remain in use. Both root approval files are required:

- `/srv/sixnine/approved-releases/COMMIT.sha256`: ordinary manifest hash.
- `/srv/sixnine/approved-migrations/COMMIT.sha256`: migration manifest hash.

The migration manifest also binds the ordinary manifest hash and exact dispatch
image. Fetch checks the independent approval before transferring the image.
Source extraction rejects links, devices, duplicate paths, traversal and
unexpected files; interrupted staging is unpublished and a same-commit retry
can resume using the original immutable objects.

## Protected host preparation

Create root-owned, non-writable-by-others directories `incoming-migrations`,
`approved-migrations`, `migration`, and `migration/releases` under
`/srv/sixnine`. Keep the existing business data, PostgreSQL, release state,
legacy operator records and budget history intact.

Prepare protected credentials separately using the existing credential
delivery process. Do not put secrets in arguments, Compose environment or a
release artifact. The helper reads only path references from
`/srv/sixnine/migration/paths.json`; this document has exactly:

```json
{
  "version": 1,
  "host_memory_mib": 3844,
  "environment": {
    "SIXNINE_EXECUTION_PROFILES_SECRET_FILE": "/run/sixnine-secrets/execution_profiles",
    "SIXNINE_GRAFANA_CONFIG_FILE": "/run/sixnine-secrets/grafana_cloud",
    "SIXNINE_DSTACK_OPERATOR_CONFIG_FILE": "/run/sixnine-secrets/dstack_operator_config",
    "SIXNINE_DSTACK_RUNTIME_CONFIG_FILE": "/run/sixnine-secrets/dstack_runtime_config",
    "SIXNINE_DSTACK_TOKEN_FILE": "/run/sixnine-secrets/dstack_api_token",
    "SIXNINE_DSTACK_DSN_FILE": "/run/sixnine-secrets/dstack_database_url",
    "SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE": "/run/sixnine-secrets/dstack_ssh_public_key",
    "SIXNINE_GPU_SSH_KEY_FILE": "/run/sixnine-secrets/gpu_ssh_key",
    "SIXNINE_HATCHET_BROKER_CONFIG_FILE": "/run/sixnine-secrets/hatchet_broker",
    "SIXNINE_HATCHET_TOKEN_FILE": "/run/sixnine-secrets/hatchet_api_token",
    "SIXNINE_HATCHET_SECRET_DIR": "/run/sixnine-secrets/hatchet",
    "SIXNINE_DSTACK_STATE_DIR": "/srv/sixnine/dstack-state",
    "SIXNINE_DSTACK_SERVER_CONFIG_FILE": "/run/sixnine-secrets/dstack_server.yml",
    "SIXNINE_DSTACK_RUNTIME_DIR": "/srv/sixnine/dstack-runtime",
    "SIXNINE_OPERATOR_CAPACITY_OWNERS": "EXISTING_AUTHORIZED_OWNER_ID",
    "SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID": "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1",
    "SIXNINE_HATCHET_POSTGRES_VOLUME": "EXISTING_HATCHET_POSTGRES_VOLUME",
    "SIXNINE_HATCHET_CONFIG_VOLUME": "EXISTING_HATCHET_CONFIG_VOLUME",
    "SIXNINE_MIGRATION_GENERATION_ENABLED": "0",
    "SIXNINE_MIGRATION_EXECUTION_BACKEND": "disabled"
  }
}
```

Replace the memory amount, owner and volume names with observed/reviewed values.
The explicit default must be included in the approved source-set index and be
the same in app and controller. Historical volume names remain unchanged for
v1 rollback; no broker bootstrap is performed. Use generation `1`/`wangp-worker` only for
an explicitly authorized acceptance deployment with its exact existing profiles.
The activation marker freezes the complete path/configuration document hash;
later configuration changes require reviewed maintenance, not an app release.
The optional nonsecret `SIXNINE_STORYBOARD_OWNERS` field in that same environment
document selects the new storyboard UI for explicit existing browser accounts.
Omit it, or set it to an empty string, to keep the UI disabled. It is passed only
to `app`, not to controller or dstack; it grants no operator,
asset, job or budget permissions. Allowed nonempty values contain unique account
IDs from `superdan` and `supervan`. Do not inherit this flag from a shell or
project `.env`, and do not edit an active frozen configuration during a source
release. Enablement remains a separately reviewed maintenance change after the
exact source release and acceptance gates in [#161](https://github.com/inkseq/h3-studio/issues/161).
Independently review/install the matching exact maintenance helper and checker
before the approved source deployment. Invoke flag maintenance only after that
current source release is healthy. Read the current
full commit and SHA256 of `paths.json` and `active.json`, then use only:

```text
/usr/bin/python3 /opt/sixnine-release/migration_maintenance.py storyboard-owners CURRENT_COMMIT superdan,supervan EXPECTED_PATHS_SHA256 EXPECTED_ACTIVE_SHA256
```

Use `superdan` or `supervan` for one account, or `-` to disable both. This is not
a general configuration override. The command takes the existing nonblocking
`release.lock`, requires the exact healthy current approved platform/migration
artifacts and installed helper hashes, and rejects stale original bindings.
It saves the exact old/candidate configuration and marker bytes in the protected
`migration/storyboard-maintenance/OPERATION_ID/` directory before any mutation.
The durable `migration/storyboard-maintenance.json` records the pending intent.

The app reads this environment flag at startup. Because dstack and controller share its container network namespace, updating it
reconnects **app and those four CPU members** using their current approved images.
It does not load images, initialize/restart either database, change the frontend
pointer, alter lifecycle policies, rent/stop a provider GPU, or write jobs and
budgets. Original runtime environment and persistent mount signatures are
checked; only the app's storyboard flag may differ. Once CPU health, namespace,
runtime mounts, authenticated probes and the live app flag pass, the command
atomically replaces each configuration/marker file and updates their frozen
binding. The current/previous source release records remain unchanged.

If interrupted or failed, keep the receipt and backups. Ordinary migration
deploy/activation and another flag update are fenced; do not repeat activation.
Read the exact current SHA256 of `storyboard-maintenance.json` and explicitly
restore that same intent:

```text
/usr/bin/python3 /opt/sixnine-release/migration_maintenance.py restore-storyboard-owners CURRENT_COMMIT EXPECTED_RECEIPT_SHA256
```

Restore verifies unchanged approved source, backup hashes, the flag-only diff,
and that current configuration/marker are one of that intent's before/after
states; unrelated changes are never overwritten. It reconnects the original
CPU configuration and restores both original files byte-for-byte. Backups stay
as evidence. A lost success response is checked through the completed receipt,
current hashes and live flag; it is not permission for a blind replay.
The derived source-directory path is set by the helper for the exact artifact.

Pre-pull only the pinned public CPU infrastructure images in the overlay. Load
the two separately approved app/dispatch archives. Their exact loaded identities
must also be addressable by a digest reference on this Docker daemon; the helper
verifies that reference resolves to an archive-derived identity. A config hash
must not be presented as an unverified registry manifest digest.

## Activate and keep future application deployments safe

Before first activation, independently confirm the scoped legacy provider
account lists contain no allocations. Fence old public generation admission
before the final locked read-only business probe; a bounded outage stopping the
exact old app CPU container is acceptable. Require zero nonterminal jobs and
attempts, then stop only the exact old CPU operator process. Initial activation
independently verifies that the app CPU container remains stopped, preventing
new acceptance between the probe and namespace replacement. Preserve the
original ledger, budgets, reservations and all historical execution pins.

The ordinary safe-restore route remains available. When historical uncertain
bills prevent that route, separately approve the root-owned
`/srv/sixnine/migration/legacy-cpu-handoff.json` and put its SHA256 in
`approved-legacy-cpu-handoff.sha256` in the same protected directory. The helper
never writes either file or the original pins. This is a CPU authority handoff
only; an empty account list does not prove exact provider deletion, unchanged
credits or final settlement. Initial activation requires every observation to
be no more than 300 seconds old and not in the future. For example:

```json
{
  "version": 1,
  "purpose": "legacy-cpu-authority-handoff-only",
  "host_id": "i-03d81d2d153b5e2fd",
  "target_commit": "EXACT_40_CHARACTER_REVIEWED_COMMIT",
  "checked_at": 1791684000,
  "markers_sha256": {
    "operator-capacity/active.json": "EXACT_OBSERVED_64_CHARACTER_SHA256",
    "gpu-acceptance/active.json": null,
    "gpu-scaler/active.json": "EXACT_OBSERVED_64_CHARACTER_SHA256"
  },
  "providers": {
    "lium": {"account_scope_verified": true, "complete": true, "listed_allocations": 0, "checked_at": 1791684000},
    "targon": {"account_scope_verified": true, "complete": true, "listed_allocations": 0, "checked_at": 1791684000}
  },
  "business": {"nonterminal_jobs": 0, "nonterminal_attempts": 0, "admission_closed_before_probe": true, "checked_at": 1791684000},
  "preservation": {
    "original_markers": "unchanged", "ledger": "unchanged",
    "budgets_and_reservations": "unchanged", "provider_deletion": "not_asserted",
    "final_billing": "not_asserted", "legacy_retirement": "deferred"
  },
  "evidence_refs": ["ssm:EXACT_READ_ONLY_PROVIDER_AND_BUSINESS_PROBE_COMMAND_ID"]
}
```

Record every listed marker's exact hash or verified absence (`null`); preserve
their bytes even if their old `active` field is still true. Before each apply,
the helper independently refuses any live legacy operator, finite GPU
controller or worker process. The migration activation freezes the approved
receipt hash, so later app updates recheck original pin hashes and process
absence without requiring the new path's GPU accounts or jobs to become empty.
Unsettled historical billing alone does not require two live CPU controllers.
This authority receipt is distinct from P5 retirement and does not erase the
historical marker or settle any bill. A failed initial activation may be retried
at the same commit with a freshly independently approved observation while
`current` is still null; a healthy activation cannot change its frozen receipt.

Run the installed root helper under the existing release authority:

```sh
/usr/bin/python3 /opt/sixnine-release/migration_maintenance.py activate COMMIT
```

The helper locks the existing release lock, journals activation and the pending
commit, checks protected inputs and capacity, and updates the five members
sharing the app network namespace together: app, dstack and controller. It retains both named Hatchet volumes and the business database.
It verifies actual image identities, service labels, namespace joins, original
volume mounts, service health and private authenticated dstack access before
marking the release healthy. It never uses `docker compose down -v`.

Once `/srv/sixnine/migration/active.json` exists, the ordinary installed
`deploy_approved.py COMMIT` fetches both approved artifacts and routes through
the migration helper. Thus an ordinary application update cannot leave worker
members attached to the replaced app container or forget the overlay. Every
such release needs an approved additive artifact and matching installed helper;
a helper change is a separately reviewed root installation.

Failure preserves the original current commit and records the same pending
commit with `migration_failed_needs_reconciliation`. Reconcile and retry that
commit; do not delete markers, submit another provider run, or silently revert to
the legacy backend. Setting `active:false` is not a retirement procedure.
Retirement remains the separate P5 acceptance/rollback decision with all
original jobs, broker history, provider records and bill reservations retained.

## Verification scope

`test_migration_release.py` uses synthetic image archives, real public source
rendering and fake S3/Docker. It checks immutable publication, approval-before-
transfer, archive safety, interrupted-copy retry, installed-only execution,
namespace/volume identity, inactive-path fences and failure state retention.
Existing platform/operator release tests cover the unchanged legacy path.
These tests do not claim live deployment or real GPU acceptance; those require
the release receipt and the separately authorized Quick Chat/Agent videos.

## Demand start (CR43)

Production activation is a separate audited step after Claude accepts the
staging cold-start soak. Deploying the code does not enable automatic rental.
The original capacity controller, job ledger, operator commands and global
capacity lock are reused; there is no additional queue or scheduler.

Protected operator policy opts in explicitly with
`demand_start: {"profile_ids": ["EXISTING_FL_PROFILE", "EXISTING_REF_PROFILE"]}`.
Only these existing profiles may fund automatic capacity; their tenant, native
mode/configuration/manifest and shared pool remain exact. Job owners and signed
asset/result access are unchanged. Staging additionally supplies its current
`test_task_id` here and retains the task budget, idle stop and maximum runtime.
The operator policy and selected execution qualification/reservation may use
explicit `expires_at: null` for no authority expiry. Offers, previews,
confirmation plans, worker heartbeats and private member approvals remain
finite. Never replace expiry with a distant date or edit accepted job inputs.

Human-admin `GET /v1/operator/dstack/admin/workspace` adds `demand_start`:
`enabled`, `revision`, `hard_max_machines`, `running_machines`,
`starting_machines`, `uncertain_machines`, `queued_generation_jobs`,
`queued_by_mode` (fl/ref) and `blocked_reason`. These counts are advisory;
the original start transaction rechecks actual dstack/provider occupancy.
Manual and automatic starts together cannot exceed two machines or the lower
configured capacity. Each machine retains its 850000 microusd/hour ceiling;
normal audited operator price preferences may lower that price.

The human-admin runtime switch uses
`GET/PUT /v1/operator/dstack/admin/demand-start`. PUT requires an
`Idempotency-Key`, exact `expected_revision`, boolean `enabled` and a reason.
It defaults OFF in every environment. CAS, original command replay and an
outbox audit are committed together. OFF prevents new automatic commands;
it does not cancel accepted startup/running work or disable manual starts.
Automatic machines always use ordinary idle stop, never an indefinite hold.
Compatible queued/running work pauses that mode's idle timer; unrelated modes
do not keep an unused machine alive. Explicit manual holds are not rewritten.

Confirmed, active, unsubmitted native jobs trigger one decision per controller
tick. Preflight alone, private member-bound work, cancelled/terminal tasks and
unknown upstream submissions do not trigger rental. Account and job-row locks
fence cancellation/suspension/claim races before the original reservation.
Planning and 429 backoff use the existing operator-command journal. Response
loss preserves the same preview/start identity; replacement requires existing
positive no-allocation/removal evidence. No invoice is fabricated or forgiven.

Creator availability keeps `state`/`available` truthful and adds `can_queue`.
The normal preflight/confirmation is still required. Accepted job reads add
neutral `wait_diagnostic.stage`: queued_no_machine, machine_starting,
downloading, loading, awaiting_worker, running or unknown. The frontend owns
the copy; do not expose provider/model names or dollars in this hint.

Before production enable, use the existing audited settings to clear old
production USD daily/connection caps, retain credit and confirmation settings,
and make the fleet hourly setting at least twice the per-machine ceiling
(redundant under the two-machine limit). Existing retired account caps remain
accounting-only; actual spend, original reservations and unsettled invoices are
retained. Staging keeps USD20 per acceptance task without this relaxation.

Acceptance is two normal cold cycles within one USD20 task: confirmed QA job,
automatic allocation, native readiness, one original video plus separate audio
in R2, idle stop and positive dstack/provider removal. A natural unknown branch
not observed live must be reported as unexercised; do not induce paid failures.
After testing, stop test machines and switch demand start OFF. Never operate
owner production machines during this preparation or soak.

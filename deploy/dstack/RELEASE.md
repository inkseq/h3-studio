# Approved CPU migration release

This route adds the dstack server, Hatchet engine, dispatcher and controller to
the existing `sixnine-platform` Compose project. It does not rent a GPU, stop a
provider instance, settle a bill, reset accepted jobs or delete storage. Native
GPU images are published separately and are never downloaded to this CPU host.

The ordinary platform bundle remains unchanged. The optional migration bundle
contains the exact tested dispatch image archive, the reviewed overlay/checker
and maintenance source, nonsecret examples, and selected public four-file WanGP
source sets. It contains no API token, SSH private key, Grafana credential, model
weight or local `.env`.

## Build and independent approval

Build the tested app and dispatch images with the complete source commit label.
Use `tools/build_platform_release.py COMMIT PLATFORM_DIRECTORY` for the ordinary
bundle. Use its `--migration-destination MIGRATION_DIRECTORY` and repeated
`--migration-selection PROFILE:MODE` arguments for the additive bundle, or call
`build_migration(COMMIT, PLATFORM_DIRECTORY, MIGRATION_DIRECTORY, selections)`
when an already tested ordinary artifact has the same exact commit.

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
the same in app, dispatcher and controller. The volume names must already exist; changing them must not silently initialize
a blank broker. Initialize a new broker once through the separately reviewed
Hatchet bootstrap before activation. Use generation `1`/`wangp-worker` only for
an explicitly authorized acceptance deployment with its exact existing profiles.
The activation marker freezes the complete path/configuration document hash;
later configuration changes require reviewed maintenance, not an app release.
The derived source-directory path is set by the helper for the exact artifact.
For first initialization, a temporary private CPU bootstrap project may use
these same explicitly named Hatchet volumes and persisted infrastructure
credentials to capture the broker token. Stop only its bootstrap containers
before integrated activation; preserve both volumes and token identity. This
is one broker initialization, not a second broker or business ledger.

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
sharing the app network namespace together: app, dstack, Hatchet, dispatcher and
controller. It retains both named Hatchet volumes and the business database.
It verifies actual image identities, service labels, namespace joins, original
volume mounts, service health and private authenticated dstack access before
marking the release healthy. It never uses `docker compose down -v`.

Once `/srv/sixnine/migration/active.json` exists, the ordinary installed
`deploy_approved.py COMMIT` fetches both approved artifacts and routes through
the migration helper. Thus an ordinary application update cannot leave broker
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

# Production operation guards (#665)

This is the existing CR70 execution contract, not another approval stage. A
Tier2 PR receives one non-author review; the author adopts the reviewed behavior
after the operational checks pass. Claude alone promotes production releases.
Source tests never substitute for the actual host qualification or receipt.

## Required execution boundary

Every bounded production install, recovery or backfill needs a read-only
qualification of its **expected** host, current release, image and relevant
operation state. Reading whichever image happens to be current is not an exact
pin. The same checks must run again under the existing release/operation lock
immediately before execution; an earlier successful dry run cannot authorize
execution after promotion. Qualification must not create an operation intent,
modify business data, start a backup, upload objects or change services.

- Validate protected paths and original receipt/config/helper hashes. Bind the
  current/previous approved manifests and image identities when installing a
  helper needed by both releases. Reject symlinks, unknown tuples and a pending
  release; do not silently regenerate pins from the current host.
- Enforce the applicable zero-job/writer gate without cancelling jobs to make it
  pass. Hold #193 while the operation pins production; release only that hold
  after its completion is read back. Other operations' holds remain independent.
- Fence the original operation before the first write, preserve rollback and
  record unknown outcomes. Reconcile the original command/intent/object versions;
  never clear a fence, choose a new nonce, or rerun a completed/unknown operation.
- Report fixed diagnostic codes and nonsecret identities. Do not print configs,
  environment contents, credentials, prompts, customer data or raw error output.

Qualification may inspect a running container or use an offline verifier with
read-only payload mounts and no network. That is not a promise of zero temporary
Docker objects; name the actual reads and bounded verifier work in the receipt.

## Coverage and remaining work

| Entry | Current protection | Remaining boundary |
| --- | --- | --- |
| `business_backup_recovery.py` (#657) | Default qualification and explicit execute share the exact release/image/approval, zero-work and original-operation checks under `release.lock`. | One-off historical operation, not reusable authority. Entry-level regression tests now exercise drift and lock contention before the write boundary. |
| `business_backup_oom_recovery.py` (#668) | Default qualifier; fixed date/config/release/image/runtime/original nonce; exclusive recovery fence. | Exhausted operation remains exhausted. A new source fix does not rearm it. |
| #592/#524 helper installations | Bounded CAS installers qualify exact current/previous helper/manifest/runtime tuples, keep rollback, recheck under lock and preserve durable receipts. | These were operation-specific receipt artifacts; they are not yet a reusable tracked installation entry point. Do not present this audit as that implementation. |
| `deploy_approved.py` / `deploy/dstack/maintenance.py` | Approved artifact closure, release locking and operation-specific state checks exist. | No universal read-only dry-run CLI for all maintenance actions. Any addition must preserve installed-helper compatibility and receive the single Tier2 PR review. |
| `asset_recovery.py` / `rendition_backfill.py` | Read-only listing/plan and original asset/receipt CAS checks exist. | Domain-level CAS does not prove the invoking production host/release/image pin. A guarded host wrapper is still needed for production execution. |

Do not close #665's production-operation checkbox based on this inventory.
The next implementation slice should standardize a tracked installer qualifier
from the proven #592/#524 protocol, then cover maintenance/backfill entry points
without changing or replaying their historical operations.

## Shared read-only tuple qualifier

`deploy/platform/operation_qualification.py` now supplies that first shared
qualification boundary. It is source-only until separately adopted; no existing
host helper or release package installs or invokes it automatically. It does
not replace the operation-specific #592/#524 installers or recover an old intent.

The CLI accepts exactly one absolute path to a protected root-owned JSON binding:
`python3 -B operation_qualification.py /root/reviewed-operation-pins.json`.
There is no execute, capture-current-pins, force or skip-idle option. Bindings
must be prepared from the reviewed operation's read-only evidence, not inferred
from whatever host happens to be current at execution time. Version 1 requires:

- `environment`, its exact `instance_id`, 40-hex `current` and `previous` commits;
- `helpers`: filename-to-SHA256 pins for the installed helper dependencies;
- `files`: relative-path-to-SHA256 pins under `/srv/sixnine`, including release,
  migration and target markers, site config, both retained platform/migration
  manifests and their independent root approval records;
- `containers`: exact `id`, `image`, `started`, `running` pins for the app,
  controller, dstack, db and caddy compose services;
- `absent`: operation-specific relative paths whose prior existence requires
  reconciliation (for example a new installer intent directory).

It checks protected paths, independent manifest approval hashes, current app
image, live container identities, and zero nonterminal jobs under the existing
release lock, then rechecks the tuple before yielding. Its only subprocesses
are Docker inventory/inspect and a read-only SQL count inside the pinned app.
No intents, configs, bytecode files, containers or artifacts are created.
Diagnostics contain fixed error codes, never config contents or raw stderr.

A reviewed writer may use `locked_qualification(binding)` **inside the same
process** and must keep the context open through its own checks and write
boundary. CLI success is only a point-in-time observation. It cannot authorize
a later command, prove no new job can arrive, or replace admission quiescence,
writer/transport checks, retained artifact/helper compatibility checks,
operation-specific fences, backup/rollback or completion verification. Those
remain the writer's responsibility. This slice checks manifest pins and approvals;
it does not execute incoming artifact code or validate every archive payload.
The remaining installer/writer integration keeps #665 open.

## Offline regression contract

`test_business_backup_recovery.RecoveryEntryGuardTests` calls the real recovery
entry with synthetic host identity and mocked Docker/transfer boundaries. It
checks changed release/status/pending state, rejected approval, changed app image
or data mount, non-idle refusal, read-only default, drift between dry run and
execution, and lock ownership through the write boundary. Existing tests retain
original-operation fencing, unknown-transfer preservation and fixed-prefix proof.
Run on Linux to exercise real `flock`; Windows reports these lock tests skipped.

Backend maintainability/audit/coverage landed in #644 (`cf33227`). CI native
runner/DBOS observation handling landed in #666 (`778c97c`): macOS is advisory,
only the bounded DBOS observation timeout is advisory, and invariant assertions
still fail. The one infrastructure rerun is the CR70 operator policy; this audit
does not claim a new automated workflow rerun implementation.

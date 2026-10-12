# CPU host artifact retention

This policy reclaims reproducible packages and unused Docker cache on the
existing CPU host. It never resizes a resource, provisions a GPU, migrates user
media, settles bills, edits a database or removes a Docker volume/container.

Install the independently reviewed `release_retention.py` beside the existing
root helpers in `/opt/sixnine-release` (root-owned, not writable by others).
The application release artifact is not allowed to install or approve this code.
Run `python3 /opt/sixnine-release/release_retention.py` for a read-only plan.
`--apply` computes and applies the verified plan while holding the existing
`/srv/sixnine/release.lock`; never apply a stale saved plan or run concurrent
manual cleanup. Both modes produce a bounded, nonsecret JSON receipt.

## Kept dependencies

Keep the app's current and previous release, independent frontend current,
previous and retired platform-fallback pointers, migration current, active
legacy GPU execution pins, and managed release paths used by container binds
or running Compose working directories. Complete incoming duplicates of kept
commits can be removed after proving the installed manifest is identical. An unresolved pending release or unknown
execution marker stops retention. Keep every container image (including stopped
containers), the retained releases' full archive image identity closure, and
all other tagged infrastructure images. Independent rollback pointers may require
more than two commit directories; keeping only the newest two by date is unsafe.

Approval files, runtime secrets, job/lease/budget ledgers, backups, PostgreSQL,
Hatchet/dstack persistent state, `platform-data`, `upload-spool`, and shared
content-addressed frontend assets remain untouched. `upload-spool` is an app
mount, not disposable system temporary storage. Existing browser tabs can still
need older shared frontend assets.

## Removable artifacts

Only complete, type-safe approved app/migration/frontend bundles in the managed
release/incoming parents and complete private download staging can be removed.
Each local manifest must match its independent root approval. Every published
object must still exist in the fixed AWS release bucket, with the exact byte
count, publisher SHA-256 metadata and AES256 encryption. Publisher keys are
immutable conditional-create objects. Partial, unapproved, foreign or unexpected
directories remain on disk and appear as `unproven_or_partial_artifact`; a human
can review them separately rather than broadening an automatic path glob.

Deletion validates all trees first, rejects symlinks/hardlinks/devices/path
escapes, hashes each file, and uses Linux directory descriptors and no-follow
operations with inode/content rechecks. Pointer/in-use changes reject the plan.
Deletion failure may leave a partially removed reproducible old bundle; it
never changes a release pointer or deletes protected data.

Docker cleanup removes exact unreferenced old Sixnine image IDs backed by the
same restore proof, plus unreferenced dangling images. It never forces image
removal and never uses `image prune -a`, `system prune`, volume prune or stopped
container removal. Unused build cache older than 72 hours is pruned; current
images and their rollback dependencies stay referenced and usable.

## Regular release integration and logs

After a successful app or frontend install, the reviewed helpers optionally
apply retention under the same lock. Enable it with the independently installed
root-owned `/srv/sixnine/retention-policy.json`:

```json
{"version": 1, "enabled": true}
```

A cleanup error emits `retention_skipped_preserving_healthy_release` and leaves
the healthy install/status intact. Inspect the dry-run receipt; do not retry a
successful release or reconcile GPU/provider operations to repair cleanup.

The host journald policy keeps at most seven days and 128 MiB. Install the
reviewed host drop-in separately; no Docker daemon restart or app recreation is
required for this cleanup batch. Compose already limits app logs to three
10 MiB files and migration logs to two 5 MiB files. Never truncate active Docker
logs or remove logs containing unique task/billing evidence. Generic `/tmp`,
legacy scaler publication bundles and media migration are separate review
scopes; this helper does not recursively clean them.

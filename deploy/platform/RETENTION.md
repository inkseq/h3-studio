# CPU host artifact retention

This policy reclaims reproducible packages and unused Docker cache on the
existing CPU host. It never resizes a resource, provisions a GPU, migrates user
media, settles bills, edits a database or removes a Docker volume/container.

Install the independently reviewed `release_retention.py` beside the existing
root helpers (currently `/opt/inkseq-release-staging-v1` on both sealed hosts;
the retained `/opt/sixnine-release` entry remains compatible). They must be
root-owned and not writable by others.
The application release artifact is not allowed to install or approve this code.
Run `python3 /opt/inkseq-release-staging-v1/release_retention.py` for a read-only plan.
`--apply` computes and applies the verified plan while holding the existing
`/srv/sixnine/release.lock`; never apply a stale saved plan or run concurrent
manual cleanup. Both modes produce a bounded, nonsecret JSON receipt.

## Kept dependencies

Keep the app's current and previous release **plus the newest three additional
distinct installed commits**, independent frontend current,
previous and retired platform-fallback pointers, migration current, active
legacy GPU execution pins, failed-release evidence, and managed release paths used by container binds
or running Compose working directories. Complete incoming duplicates of kept
commits can be removed after proving the installed manifest is identical. An unresolved pending release or unknown
execution marker stops retention. Keep every container image (including stopped
containers), the retained releases' full archive image identity closure, and
all other tagged infrastructure images. Independent rollback pointers may require
more than five commit directories. Extra versions are ranked by protected
installed-directory modification time across platform/migration/frontend
families, with SHA tie-breaking. Incoming copies do not consume extra slots;
unknown or unapproved metadata remains on disk without consuming a slot.

Optional root-protected `release-retention-pins.json` has exactly
`{"version":1,"commits":["FULL_40_CHARACTER_SHA"]}`. Missing means no additional
explicit pins; malformed, unknown or duplicate fields stop cleanup. Existing
release-state, frontend, migration, execution and container pins remain mandatory.
Explicit pins do not expire when a newer release is promoted. If a successful
cleanup leaves many old versions, inspect this file and the receipt's
`kept_commits` before retrying cleanup. Retiring an obsolete explicit rollback
pin needs separate review of the exact commits, fresh release/execution/container
references, and a protected copy of the original pin file. Apply any approved
pin update under `release.lock` with an exact old-file hash check; then rerun the
normal planner. Never bypass the other dependency pins or delete a directory
directly to meet a disk-space target. A size estimate for pinned directories is
not a verified deletion plan or proof of reclaimed space.
`environment-release-intent.json` binds the complete existing publication tuple
through platform and frontend installation; an incomplete operation keeps its
commit, never creates approval and never permits another unknown deployment to
be silently replaced. Post-install retention waits for this tuple to complete.

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

The same controller also considers the exact historical publication namespace
`publication/<40-hex-commit>/<32-hex-nonce>/platform-release` and
`migration-release`. Current/previous, newest-three, explicit rollback,
in-flight and container bind pins take priority here too; publication copies
do not consume installed-version slots. A payload needs the original
root-protected nonce journal's terminal verified status, exact authority-file
SHA/binding/download receipts, and the original publisher's nonblocking nonce
lock in addition to the release lock. Unknown, busy, missing or partial
publication evidence is retained. Apply reacquires all selected nonce locks and
rechecks the original journal/authority/lock identities before any deletion.
Payloads still require the normal manifest, independent approval, image
validation and immutable S3 member-size/SHA/AES256 restore proof.

Only a corresponding **complete validated payload** can justify selecting its
exact `archives/platform-transport.tar.gz` or
`archives/migration-transport.tar.gz` file. The original authority must bind the
compressed size/SHA and every member to that payload. A bounded streaming
single-gzip/raw-TAR check rejects PAX/GNU extensions, links, duplicates, unknown
members, changed bytes, bad padding, trailing streams and excess decompression
before selecting the file. Its restore unit is the independently approved
release **members**, fetched/applied by the existing normal release tools from
immutable S3 objects. Retention does not promise byte-identical recreation of
the consumed gzip wrapper or replay a publisher intent. A partial payload
cannot qualify an otherwise intact transport archive by filename alone.

File removal is restricted to those two exact filenames under the validated
namespace, using the existing no-follow descriptor/inode/hash boundary. Never
remove the `archives` parent, nonce/SHA parents, publisher transport or source,
SDK, authority, journal, lock, recovery script or unknown file. These controls
remain the original evidence after cache deletion. Publication failure reasons
in the retention receipt stay bounded/static and never contain AWS error text.

Deletion validates all trees first, rejects symlinks/hardlinks/devices/path
escapes, hashes each file, and uses Linux directory descriptors and no-follow
operations with inode/content rechecks. Pointer/in-use changes reject the plan.
Deletion failure may leave a partially removed reproducible old bundle; it
never changes a release pointer or deletes protected data.

Docker cleanup removes only exact unreferenced managed commit **tags** backed by
the same restore proof, then prunes unused dangling images. It never removes an
image by ID or forces image
removal and never uses `image prune -a`, `system prune`, volume prune or stopped
container removal. It never prunes builder cache. Global dangling prune is
skipped whenever a protected current/rollback image has no tag, even if Docker
reports a digest alias; current images and rollback dependencies remain usable.

Containerd-loaded images may have local `sixnine-platform@sha256:...` or
`sixnine-dispatch@sha256:...` aliases. Such aliases are eligible only when their
repository matches the verified bundle kind and the digest equals the actual
inspected image ID, which must be in that approved archive's identity closure. Commit tags must match the same
bundle. Foreign repository aliases, unrelated digests and kept image IDs remain
protected; an arbitrary Sixnine-looking tag does not establish ownership.

## Regular release integration and logs

After a successful app or frontend install, the reviewed helpers optionally
apply retention under the same lock. Enable it with the independently installed
root-owned `/srv/sixnine/retention-policy.json`:

```json
{"version": 1, "enabled": true}
```

A compact SHA/path/byte receipt is written under `retention-receipts` before
mutation and completed after success. A started receipt is not proof all planned
bytes were freed. CLI dry-run emits the compact plan without deletion or receipt
writes. Each apply recomputes and rechecks all pins and trees under release.lock.

Install/enable the reviewed `inkseq-release-retention.service` and `.timer`
independently for daily execution. The service calls the same fixed helper with
`--apply --scheduled`, requires the exact protected enabled policy, and takes
the same release.lock. A scheduled run waits behind an active deployment; the
one-hour service timeout bounds that wait and cleanup together. Lock contention
is not reported as successful cleanup. Interactive dry-runs and applies fail
immediately with `retention_release_lock_busy` instead of waiting.
The service uses bounded memory/CPU and idle I/O. No application
bundle may install this helper, policy, service or timer.

A cleanup error emits `retention_skipped_preserving_healthy_release` and leaves
the healthy install/status intact. Inspect the dry-run receipt; do not retry a
successful release or reconcile GPU/provider operations to repair cleanup.

The host journald policy keeps at most seven days and 128 MiB. Install the
reviewed host drop-in separately; no Docker daemon restart or app recreation is
required for this cleanup batch. Compose already limits app logs to three
10 MiB files and migration logs to two 5 MiB files. Never truncate active Docker
logs or remove logs containing unique task/billing evidence. Generic `/tmp`,
legacy scaler publication bundles and media migration are separate review
scopes; this helper does not recursively clean them. The current production
`gpu-scaler/public-source` contains an unversioned dependency archive and
`publication` mixes SDK/authority/recovery files with SHA-named directories.
Neither is proven to be a closed per-release snapshot; both remain untouched.

The local ten-cycle fixture checks actual Linux deletion, duplicate removal,
bounded installed bundle size and current/previous rollback retention. It is
not ten real host deployments. Track the actual longitudinal observation and
one staging fresh deploy/rollback rehearsal separately in #286.

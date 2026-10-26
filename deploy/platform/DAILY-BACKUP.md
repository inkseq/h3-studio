# Daily business backup and isolated recovery

`business_backup.py` is a separate protected host helper under
`/opt/inkseq-business-backup-v1`, outside the exact release-helper closure.
Install the reviewed service/timer only after deploying and accepting the same
application image containing `backup_cli configured` and the R2 backup changes.
Installation and timer enablement are separate operational steps. Source tests
do not demonstrate a live backup or restoration.

The root-owned 0600 `/etc/inkseq/business-backup.json` has exactly:

```json
{"version":1,"environment":"staging","instance_id":"i-07805df7bd6984bd9","target":{"account_id":"829135631045","region":"ap-southeast-1","bucket":"REVIEWED_INDEPENDENT_BUSINESS_BACKUP_BUCKET","kms_key_arn":"REVIEWED_KMS_ARN","role_arn":"REVIEWED_BACKUP_ROLE_ARN"}}
```

Production binds `production` and `i-03d81d2d153b5e2fd`, retaining the original
backup bucket/key/role. Staging uses its independently created stack and host
role. These identifiers are nonsecret; no new static credentials are needed.
The application container must still lack IMDS/host AWS credentials. Only a
finite transport container has host networking and IMDS access; it runs the
approved image by ID, with a read-only root, no capabilities, no Docker socket,
no application database/secrets mounts, and only the private snapshot and output
paths. Its SDK requires `iam-role` and assumes the exact configured backup role.
No inference provider calls, workers or app servers start in that container.

The timer runs daily at 16:30 UTC, with up to five minutes jitter and a persistent
catch-up after downtime. One UTC-day directory in `/var/lib/inkseq-business-backup`
records intent before work. A completed same-day run does not recapture or upload
again. The recovery-window service invokes `--daily-preserve`, which also skips
local retention on both new and already-completed copies. The normal `daily`
invocation retains its verified local-retention pass. Any unfinished
prior intent blocks future prefixes until the original remote reconciliation
workflow resolves it. Do not delete intents or rerun a copy with a new prefix to
hide an unknown write. This file tracks backup operation outcomes, not tasks,
GPU costs or business ownership. No application accounting is changed.

During snapshot/copy, the existing release lock prevents image replacement.
The helper validates the protected accepted manifest, current app image ID and
`/data` bind, then executes a read-only consistent business snapshot in that app.
It captures snapshots under `/srv/sixnine/platform-data/business-backups/<uuid>`.
Before upload, Docker copies that snapshot into the root-controlled operation
directory without following symlinks. The helper rejects links/nonregular files
and gives only the transport UID (10001) read access: files 0400 and directories
0500. The enclosing operation directory remains root-owned 0700, so the app
cannot traverse or replace this host copy even though it shares that UID. The
transport receives only a read-only bind mount of the exact snapshot. The configured snapshot is limited to 2 GiB media plus
the existing 256 MiB DB limit; less than 5 GiB available disk refuses a new run.
When explicitly enabled (not `--daily-preserve`), local retention keeps the newest daily run's
application-side payload and removes older verified payloads plus their redundant
private transport snapshots. All operation intents, outputs, completion/transfer
receipts and S3 versions remain. Unknown, unmatched, manual or active copies are
never treated as disposable because of age or directory name. The service may run
up to 95 minutes (40-minute capture, 15-minute handoff, 30-minute transfer,
10-minute validation headroom); individual SDK
calls and child processes are bounded. An uncertain process/write preserves its
container and intent, with a fixed nonzero service result. No automatic restart.

The retention pass uses the same exclusive release lock and current independently
approved image. The existing read-only reconciliation transport reads the exact
pinned remote object versions and verifies every body/hash against the original
copy intent and receipt. The newest local payload must also match that inventory
before any older payload can be removed; each deletion candidate is independently
matched, bounded and checked for links, extra files or changes. A local cleanup
failure does not erase a completed backup or trigger another capture/upload.
Retention evidence is appended before removal; unknown outcomes remain explicit.
Only the exact `platform-data/business-backups` subtree is added to the backup
service's writable paths; the database and other application data are not writable
through the host helper.

The fixed `--retention-plan` and `--retention-apply` modes run this same path for
catch-up without taking a database snapshot, issuing a cloud write or changing
the UTC-day calendar. The plan does not delete payloads. After installation,
inspect the plan and original verification evidence before the first catch-up
apply. Existing80/90% alerts sample filesystem usage at `/srv/sixnine`; local
backup and transport payload bytes on that filesystem are included, not excluded
as release-cache accounting. Verify mount/device identity on both hosts rather
than adding directory sizes a second time.

After a successful first staging copy, explicitly run the protected helper as
root with `--restore-day YYYY-MM-DD`. This is allowed only on the staging host.
It verifies the root-pinned completion receipt and original approved image,
downloads the exact S3 versions, verifies every checksum and restores into a new
private `restore-YYYY-MM-DD/output/restored` directory. The restore container
mounts no live database or application data and publishes no port. Original
jobs, unknown upstream/cost evidence and reservations remain in the backup;
the new copy has disabled execution, recovery holds and no authentication.
No production PostgreSQL migration/failover or PITR is claimed. The restore
intent never replays and its data/container remain for review. A separate native
PostgreSQL dump drill may be performed using the retained pinned staging dump;
do not conflate that independent export with the portable snapshot.

Acceptance: exact stack/IAM/IMDS isolation readback; manual service success;
matching remote completion version/hash; isolated restore success and hold;
timer enabled/next trigger; a real scheduler-dispatch receipt; then same-byte
production promotion and separately bound installation. A bounded acceptance
transient timer may trigger the same backup service after that day's copy is
complete: require `already_completed` and unchanged operation UUID, local
receipt and remote version set. This validates scheduler dispatch and daily
idempotency without altering the daily calendar or taking another snapshot.
Report the manual copy, scheduler dispatch and the next cross-day fresh daily
backup separately; the latter remains pending until its actual scheduled run. A copied receipt is explicitly
`restore_verified:false` until the separate restore operation succeeds.

R2 has no native S3 bucket versioning APIs. [Cloudflare compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
and [bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)
describe different capabilities: locks can block live overwrite/deletion but
do not provide version history. This workflow therefore creates independent
versioned, encrypted S3 copies. It never silently enables a live R2 lock or
deletes source media. Incomplete upload staging/orphans remain outside the
published-reference snapshot and require their existing separate preservation.

## Explicit incomplete-media archives

Strict version 1 remains the default. A separately reviewed configuration may
set `version: 2` and add exactly `allow_missing_media: true`. Only a definitive
R2 HEAD `ObjectNotFound` may become a missing-reference record. Local misses,
permission/timeout errors, identity mismatches, corrupt bodies and GET failures
still stop the operation. Every available body must match its original size and
SHA. Original business data, receipts and financial unknowns remain intact.

Version 2 manifests, remote receipts, daily completion and subsequent
`already_completed` output retain exact available/missing object and byte
coverage. A successful copy or scheduler invocation with `coverage.status:
incomplete` is a verified incomplete archive, never a full media backup.
Changing the protected configuration requires a reviewed update and cannot
resolve or replay a previous failed operation.

An incomplete staging recovery also requires the explicit invocation
`--restore-day YYYY-MM-DD --allow-incomplete`. Configuration alone does not
authorize it. The isolated `RECOVERY-HOLD.json` retains missing references,
coverage and `media_recovery_complete: false`. Only available verified objects
are remapped to Local; absent references remain in the preserved DB. No
credentials, execution, live repair or production restoration are enabled.

For expected-deletion classification, a reviewed configuration uses `version: 3`
with exactly `allow_missing_media: true` and `classify_expected_deletions: true`
in addition to the original environment/instance/target fields. The original
target and credentials are unchanged. Versions 1 and 2 remain supported.
The helper carries the closed v3 coverage through copy, daily completion,
`already_completed` and isolated recovery summaries. A backup-loss observer
uses `coverage.loss_alert_required` (unexpected missing objects only), while
still disclosing expected missing objects/bytes. An older v2 missing count has
no verified classification and remains actionable. Expected deletion does not
make `coverage.status` complete or permit claiming full byte recovery.

The existing application alert monitor does not currently ingest backup
coverage. This format supplies the verified loss classification for the daily
backup observer; it does not claim a new SNS/CloudWatch alarm has been installed.
Helper/config upgrades and final candidate acceptance remain separately reviewed
operations. Source merge alone does not alter the installed scheduler or replay
any failed operation.

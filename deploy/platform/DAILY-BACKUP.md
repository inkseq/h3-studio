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
records intent before work. A completed same-day run is a no-op; any unfinished
prior intent blocks future prefixes until the original remote reconciliation
workflow resolves it. Do not delete intents or rerun a copy with a new prefix to
hide an unknown write. This file tracks backup operation outcomes, not tasks,
GPU costs or business ownership. No application accounting is changed.

During snapshot/copy, the existing release lock prevents image replacement.
The helper validates the protected accepted manifest, current app image ID and
`/data` bind, then executes a read-only consistent business snapshot in that app.
It retains snapshots under `/srv/sixnine/platform-data/business-backups/<uuid>`.
Before upload, Docker copies that snapshot into the root-controlled operation
directory without following symlinks. The helper rejects links/nonregular files
and gives only the transport UID (10001) read access: files 0400 and directories
0500. The enclosing operation directory remains root-owned 0700, so the app
cannot traverse or replace this host copy even though it shares that UID. The
transport receives only a read-only bind mount of the exact snapshot. The configured snapshot is limited to 2 GiB media plus
the existing 256 MiB DB limit; less than 5 GiB available disk refuses a new run.
Both local copies, outputs and S3
versions are retained: monitor space and plan reviewed archival/cleanup before
growth fills the host. The service may run up to 65 minutes; individual SDK
calls and child processes are bounded. An uncertain process/write preserves its
container and intent, with a fixed nonzero service result. No automatic restart.

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

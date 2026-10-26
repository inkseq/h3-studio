# Business backup observation and recovery (#655)

This slice prepares a fixed original-operation recovery, bounded capture wait,
diagnostics and alarms. Nothing here is an installation or successful-copy receipt.
Non-author exact-byte review, helper CAS adoption and zero-work gates remain.
Priority follows Claude review 6419059531: production recovery, alarms, staging
missing-local-media diagnosis, then Sentry.

## Read-only findings (2026-10-26)

Production's original 2026-10-19 operation
`55f97645-d81e-493a-9dd7-f45926c36f80` has a final local v3 manifest, but no host
handoff/transport receipt. Its manifest was written 1009.78 seconds after intent,
beyond the caller's 900-second `docker exec` wait. This strongly suggests the
snapshot outlived its caller; the original journal has rotated, so the original
exception cannot be proven. No backup CLI remains active. Timing is not remote
copy evidence, and increasing the wait alone would not reconcile this operation.

Staging's 2026-10-20 operation `6abd2467-226b-4092-a5b6-3bf38dbb31f5` has a database
snapshot, workflow sidecar and partial media, but no manifest. All 32 local
references in that original snapshot are absent at their source paths, without
explicit deletion evidence. The first uncopied reference is local; the existing
missing-media opt-in only accepts R2 misses. Missing local content must not be
silently classified as deleted, removed from the database or reported as backed up.

Version-aware S3 listing of both original operation prefixes returned **zero
versions and zero delete markers**, without truncation. This is bounded read-only
evidence for later reconciliation, not authority to create a replacement prefix.
Both historical intents and original snapshots remain intact. The historical
unfinished-operation guard still blocks subsequent scheduled copies.

Read-only qualification `a10f4e02-4c95-4130-9e0e-6f0a0f3ea46f` found the old
production image/release no longer retained. Do not fabricate their approval or
recapture the data. The then-approved production image
`sha256:375b439ff88697761abe1318871027d3a3e1976dfd014c7499a8dbc5100de705`
at `b78510e6991f5acccb0996981e8016be5434693f` retains the v3 verifier/copy protocol.
Offline command `b0a226cf-e533-4850-84a1-9c7b0bd50513` verified the original data
using that image, network disabled, read-only payload/root, uid10001, no credentials:
1937 files / 1,359,916,299 bytes; manifest SHA256
`37a5e05898b9288f58b7eaa802b6bcb880d3935054bcbe6ee51b401a363fa965`;
1935 available objects, two explicitly deleted misses, zero unexpected misses.
No upload, live DB write, retention or application restart was performed.

Production subsequently promoted `fd2ae3af0922395639f26a4718b1b5f33994b652`
(#193 comment6421118992). Following review6421689261, recovery now pins this
release and image
`sha256:99135c468f9ac92ba02a8daaed5efeceb814183511f423296ba2f735080cec7e`.
Read-only/offline command `db62dc4e-3ec1-4c98-bb85-d1d06c6a6abe` verified the same
original v3 payload with identical manifest, file/byte counts and coverage using
this image. Read-only command `461fac65-89ef-4fa5-9163-7dcc0a800303` also verified
the image's membership in the approved archive and matching revision label.
The original snapshot commit/image above remain historical identity;
only the separately recorded transport pin changes. Further production promotions
are held by Claude until this recovery; the helper still refuses any release or
image drift rather than silently choosing another image.

## Operator alerts

The existing minute monitor reads only the fixed backup systemd unit and root-pinned
copy receipts. It does not depend on the app, query the business DB, read media,
call S3 or run the backup service. It reuses the existing SNS outbox, persistence,
deduplication, retry bounds and destinations. No AWS resources or identities change.

- `business_backup_failed`: confirmed failed oneshot; unknown/running observations
  cannot resolve it.
- `business_backup_missing`: no validated receipt for the last due daily copy.
  The proposed deadline is **18:30 UTC**, from the existing 16:30 UTC timer, up to
  five minutes of jitter, 95-minute service budget and 20 minutes of grace. Before
  18:30, yesterday's copy is sufficient; after 18:30 today's copy is required.
  This is approximately 26 hours from the previous scheduled start, not an exact
  age-since-completion metric. Original snapshot day governs recovery freshness.
  Re-review this constant if the timer or service budget changes.
- `business_backup_unobservable`: protected receipt state cannot be read or is
  invalid. This is separate from missing, so unknown evidence never fabricates
  recovery of a missing-copy incident.

At most today's and yesterday's receipt pairs are examined. Completion identity,
format, version and receipt SHA256 must agree. Paths must remain root-owned,
no-follow, bounded regular files; metadata remains private, while the existing
nonsecret receipt may be 0644. Files outside these two daily directories cannot
make an overdue copy healthy. No customer identifiers or paths enter SNS messages.

`SnapshotHealthy` continues to mean freshness of the **operator ledger snapshot**,
not backup or restore health. A valid copy receipt establishes only that the
original transfer was acknowledged. It says nothing new about missing media,
current object retention or a successful restore drill.

## Failure diagnostics

New operations append a start/returned/unknown diagnostic for each snapshot,
handoff and transfer phase. Each file is exclusive, private and fsynced. A timeout
is explicitly `backup_phase_timeout_outcome_unknown`: killing the Docker client
does not prove the container-side work stopped. Diagnostic files never satisfy
`check_history`, authorize a retry, or mint a `completed.json`/restore receipt.
Only finite error codes are printed; subprocess output, arguments and arbitrary
exception text are omitted. A failure to write diagnostics preserves the original
exception and intent. Snapshot caller wait rises from 900 to **2400 seconds**,
over twice the observed 1010s duration. Handoff remains 900s, transfer 1800s;
systemd bounds the whole daily invocation to 5700s including 600s validation
headroom. A timeout remains unknown, never a retry permission. The service uses
`--daily-preserve` during this recovery window, so neither recovery nor subsequent
scheduled daily copies delete local snapshots. Explicit retention is separate.

The post-copy hosted-user check uses the existing bounded payload reader, because
the frozen snapshot intentionally belongs to uid10001 and a real manifest may
exceed the 64 KiB root-operation-metadata limit. This prevents a successful copy
from failing solely because the wrong reader was used. Hosted-user unavailability
still fails visibly after the original business-copy receipt is saved.

## Remaining gated recovery

1. Review the exact recovery/helper/unit bytes and the transport-image substitution
   above. Preserve installed helper/unit bytes as rollback with their hashes; adopt
   only against fresh matching installed hashes (production helper previously
   `ff671c65030b6a3c2de7fe590d29be40b20abaeeacc737f2254e58606dd6f676`).
   The fixed recovery helper is a new root-controlled file beside `business_backup.py`;
   it is not loaded by the timer. Hold the existing release lock during adoption,
   require an inactive backup service and a fresh zero-work observation. No app
   deployment, image pull, new AWS resource/credential or cleanup is involved.
2. Default `python3 -B business_backup_recovery.py` qualifies only the exact original
   operation. Its optional `--execute` invocation must be reviewed and bounded by a
   4800s service/caller deadline (600s source verification + 900s handoff + 600s
   frozen verification + 1800s transfer + validation headroom). It pins current
   release/image as well as original intent and manifest, checks zero jobs/no
   surviving snapshot process, and refuses any prior recovery/transfer output.
   An exclusive fsynced recovery intent is written before handoff. The existing
   copy protocol checks empty version history of the **original** prefix before
   conditional writes. Unknown/partial outcomes require read-only reconciliation;
   do not rerun this helper, remove its fence, or invent completion.
3. Recovery records original day/commit in `completed.json`, and actual transport
   image in `recovery-intent.json`. It does not satisfy today's freshness alarm.
   Recheck zero-work and history, then run the one bounded daily-preserve service;
   reconcile that exact service/SSM receipt, current-day completion and pinned S3
   object versions. Only that fresh capture meets the production recovery goal.
   Rollback restores helper/unit bytes only, never deletes an operation or undoes
   an acknowledged upload. Do not overlap recovery and the scheduled daily timer.
4. Determine how the absent staging local media can be recovered. If explicit
   incomplete capture is needed, review a contract change that lists unexpected
   loss and preserves it in copy/restore reports. No forced "expected deletion".
5. An isolated staging restore drill is separately gated, not a consequence of
   today's upload. Preserve its original receipts and report actual data dates.
6. Sentry wiring is a separate #655 follow-up through the existing deployment config
   path and acceptance gates. Charging/Checkout remain off.

## Confirmed fresh-capture OOM, 2026-10-26 UTC

The original 10-19 copy completed (SSM `e64cb9bb-020d-4391-83d3-491078b41004`).
Its S3 completion version is `I8UkNyXtrq._wi_MAdY_kVzbbKf6obrs`, SHA256
`e71866a66d2a15a7fdf4467197c1fb8aff5c0ef7131258f05de2f5076c158b33`.
This is an old snapshot copied today, not a fresh capture or restore proof.

The one subsequent daily invocation `3849a3e9-2d95-416c-b78d-f0c527aee51f`
passed zero-work and release checks but the kernel killed backup Python in the
application's 512 MiB memory cgroup at 18:25:30 UTC. Read-only evidence
`1f0b4d16-e0e2-4735-a57a-de6735c1724a` records 408116 KiB anonymous RSS.
Application health remained healthy, restart count zero, and jobs zero. Original
operation `9f8335d4-2da3-42ac-81c0-4f4104af2ee8` has only its intent, phase records
and partial SQLite/journal, no manifest or transfer. S3 version history for that
exact prefix was empty. The files and hashes are pinned by the new helper; none
was removed, renamed, reset or represented as a completed snapshot.

The source fix selects a table's export batch from its already measured largest
row: at most 200 rows and approximately 4 MiB serialized bytes, with a minimum of
one row. The existing 16 MiB individual-row / 256 MiB total / time limits remain.
Decoded JSON and serialized values are released before the next fetch. A single
large row can exceed 4 MiB; this is a batching bound, not an absolute process-RSS
guarantee. A local PostgreSQL export of 96 synthetic 1 MiB documents plus one small
document measured 397972 KiB peak RSS before, 112560 KiB after, in separate 512 MiB
containers with no swap. Nine real PostgreSQL backup regressions passed.

### One reviewed recovery, without promoting or raising memory limits

This changes the reviewed recovery behavior and needs one new Tier2 PR review.
The source fix goes through ordinary staging; Claude remains the production
promoter. Do not clear #193 hold until the fresh copy is reconciled.

1. Adopt only two **new** root-owned mode-0644 files, recording hashes and an
   exclusive install intent under the existing release lock: this PR's
   `deploy/platform/business_backup_oom_recovery.py` as
   `/opt/inkseq-business-backup-v1/business_backup_oom_recovery.py`, and the exact
   `studio_platform/backup.py` as `backup_capture_runtime.py` in that directory.
   Refuse either existing path or changed installed #657 helper/config/release
   hashes; retain rollback evidence. Do not alter the app image, app filesystem,
   credentials, memory limit, existing helpers, unit or timer. The runtime hash
   is pinned in the recovery script and tested against the tracked source bytes.
2. Run the helper without arguments to qualify only. It pins production fd2ae3af,
   its approved archive/image, original source hash, configuration, exact failed
   intent/partial-file hashes, zero jobs, no writer, failed/inactive unit and no
   original-nonce transport container. It requires at least 5 GiB free disk and
   the original UTC capture day; another date requires a reviewed adjustment,
   never silent redating. Independently recheck that original S3 prefix still has
   no versions or deletion markers before executing.
3. Invoke `python3 -B business_backup_oom_recovery.py --execute` **once**, with a
   5700-second outer bound. Snapshot/handoff/transfer bounds remain 2400/900/1800s.
   The exclusive fsynced recovery intent precedes all writes. A fresh private
   `-oom-recapture` directory receives a new read-only database snapshot. The
   previous partial directory stays untouched and its hashes are rechecked.
   The known-reviewed module is passed on stdin to a separate Python subprocess
   in the existing app container; source and runtime hashes must match. This
   replaces no running application module/file and uses only its existing
   explicit DB/storage identity. No generic script or destination argument is
   accepted. Capture failure/timeout preserves the fence and never retries.
4. Freeze a private host handoff and use the existing verified conditional-copy
   transport to the **original nonce**. This is the first remote write for that
   operation, not a replay or new upload prefix. Record the actual S3 receipt,
   root receipt hash and completion once. Hosted-user unavailability can still
   fail after a genuine business-copy completion; reconcile that receipt rather
   than rerunning. A copy is never represented as a verified restore.
5. Read back S3's exact completion version and local receipt/digests, report both
   dated copies on #655, then clear #193's hold for Claude. Keep daily-preserve;
   the old partial and new recapture paths are intentionally retained. Existing
   retention will refuse the old partial tree's inventory mismatch, not delete
   unverified files. Any later cleanup needs its own verified plan. Claude's
   normal promotion of this source fix makes subsequent daily captures bounded
   without using this one-operation helper again.

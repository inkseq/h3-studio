# Capture-only failure acknowledgment (#714, G0 / #655)

This mechanism lets a later scheduled day proceed after an operator proves that
an earlier **capture-only** attempt ended without uploading. It is never a
successful backup, a retry, a restore, or authority to remove the original data.
Only an actual off-device completion version/hash satisfies the daily backup gate.

## Scope and trust boundary

Version 2 moves operation-specific hashes from source into a root-owned 0600
`failed-capture-reconciled.json`. The timer only reads it. The separate
`business_backup_ack.py` operator tool creates it, exclusively, after validation
under the existing release lock. The original state directory must be 0700 and
its parents protected. The application cannot grant itself this exception.

The accepted evidence inventory is deliberately narrow: `intent.json`,
`phase-snapshot-started.json`, and `phase-snapshot-unknown.json`. Each must be a
protected regular file, bounded to 64 KiB, with stable identity and strict phase
semantics. The original day, UUIDv4 nonce, image and commit come from that intent.
Evidence hashes cover the actual bytes. Unknown extra files, recovery subtrees,
returned capture phases, handoff/transport files and completed records refuse.
Those cases need original-operation investigation, not a generic skip flag.
Existing reviewed version 1 acknowledgments remain on their unchanged reader.

## Reviewed adoption procedure

1. Reconcile the original failure and current production state. Post a #193 hold
   while pinning the release. Confirm this PR has its one Claude Tier2 review and
   the qualified release contains the underlying capture fix. Never stop customer
   work to force the gate. A board card or acknowledgment is not deployment permission.
2. Install the two reviewed helper files together in the protected backup helper
   directory, keeping rollback copies outside dated history. Do not change the
   timer/unit. Record reviewed hashes and read them back. This source PR does not
   automatically install either file or create an acknowledgment.
3. Read fresh release SHA, exact running Docker image ID, backup-config SHA256,
   release-state file SHA256, `business_backup.py` SHA256, and
   `business_backup_ack.py` SHA256. Supply these six nonsecret CAS pins with
   `--expected-release`, `--expected-image`, `--expected-config-sha256`,
   `--expected-release-state-sha256`, `--expected-helper-sha256`, and
   `--expected-operator-sha256`, plus the original `--day YYYY-MM-DD`.
4. Invoke the operator helper without `--execute` first. It validates the
   approved release manifest/archive and running image, exact pins, zero
   nonterminal jobs, no capture writer or nonce-related backup container, and an
   inactive/failed daily service. Plan writes nothing and does not contact S3.
5. With the same pins, invoke once with `--execute`. It repeats those checks under
   `release.lock`, then uses an ephemeral container from the already installed
   image and the existing instance-role/backup-role path. No app data or credential
   mount is supplied. The only S3 call is paginated `ListObjectVersions` for the
   original prefix with `ExpectedBucketOwner`; every version and delete marker
   must be absent, and the listing must finish. An access error is never absence.
6. Before the single exclusive/fsynced acknowledgment write, the tool repeats
   release/image/config/helper pins, zero-work, writer and evidence checks. The
   listing must start after the failure and be at most five minutes old when
   attested. A slow listing, changed pin or active job refuses. The original
   partial capture is never opened, moved, deleted or recaptured by this tool.
7. Read back the acknowledgment SHA and original evidence. Unknown execution or
   write outcomes require reading the original file/SSM receipt, never rerunning
   `--execute`. Existing acknowledgment files refuse; they are never overwritten.
   Clear the #193 hold only after actual receipts. The timer still rejects the
   same or an earlier day. Report the original day as failed/reconciled without
   upload, and observe the next scheduled day's real S3 completion separately.

All timestamps and hashes are protected operator evidence, not user-supplied
application metadata. Later timer reads need no network; they check that the
exact captured evidence remains unchanged and that the acknowledgment represents
a complete, fresh-at-adoption negative listing. They do not claim that any new
backup has happened. No automatic retry, retention, or silent deletion is added.

## Offline verification

Linux tests exercise future days with no source registry entry, preserved bytes,
wrong/transplanted identity, unsafe modes/links, altered/extra evidence, phase and
clock errors, original-prefix pagination, versions/delete markers, zero-work and
writer checks, stale qualification, changed CAS pins and exclusive-write refusal.
No tests invoke Docker, S3 or production. Existing version 1 backup/history,
recovery and retention tests remain required.

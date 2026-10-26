# Business backup observation and recovery (#655)

This first slice adds diagnostics and alarms. It does **not** reconcile an old
intent, adopt a host helper, rerun a backup, delete data, or assert a new restore.
Normal non-author review, exact-byte helper adoption and zero-work gates remain.

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

## Operator alerts

The existing minute monitor reads only the fixed backup systemd unit and root-pinned
copy receipts. It does not depend on the app, query the business DB, read media,
call S3 or run the backup service. It reuses the existing SNS outbox, persistence,
deduplication, retry bounds and destinations. No AWS resources or identities change.

- `business_backup_failed`: confirmed failed oneshot; unknown/running observations
  cannot resolve it.
- `business_backup_missing`: no validated receipt for the last due daily copy.
  The reviewed deadline is **18:00 UTC**, from the existing 16:30 UTC timer, up to
  five minutes of jitter, 65-minute service budget and 20 minutes of grace. Before
  18:00, yesterday's copy is sufficient; after 18:00 today's copy is required.
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
exception and intent. The current timeout and replay guards are unchanged.

The post-copy hosted-user check uses the existing bounded payload reader, because
the frozen snapshot intentionally belongs to uid10001 and a real manifest may
exceed the 64 KiB root-operation-metadata limit. This prevents a successful copy
from failing solely because the wrong reader was used. Hosted-user unavailability
still fails visibly after the original business-copy receipt is saved.

## Remaining gated recovery

1. Review and adopt the exact monitor/helper bytes under the existing CAS/backup
   installation protocol, first staging. Do not remove original intent files.
2. Qualify a separate bounded recovery of the original production snapshot using
   its pinned image/manifest, no active process and exact empty remote-prefix proof.
   A timeout correction must fit the whole service budget; do not blindly replay
   `docker exec` or allocate a new operation UUID.
3. Determine how the absent staging local media can be recovered. If explicit
   incomplete capture is needed, review a contract change that lists unexpected
   loss and preserves it in copy/restore reports. No forced "expected deletion".
4. Under a fresh zero-work gate, obtain a new pinned S3 copy and an isolated restore
   verification. Report exact dates, coverage and original command receipts.
5. Sentry wiring is a separate #655 follow-up through the existing deployment config
   path and acceptance gates. Charging/Checkout remain off.

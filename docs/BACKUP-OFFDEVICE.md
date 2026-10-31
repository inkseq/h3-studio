# Encrypted off-device copies of portable business backups

This is a bounded operator workflow for work package F. It does not switch the
application's Local storage, create a second asset/job ledger, provision AWS,
enable restored jobs, or authorize deletion. Source and fake-S3 tests are not
production backup/restore acceptance.

## Use one consistent snapshot

Use the existing `studio_platform.backup_cli postgres-local` in the exact
approved application image, with the existing protected database URL file and
Local object directory. The operator must verify that image, container,
read-only input mounts and explicit new private output directory first. Do not
run the backup against an inferred path, expose the DSN, or combine an unrelated
database dump and media listing. See [the existing snapshot/restore guide](BACKUP-RECOVERY.zh-CN.md).

For the deployed PostgreSQL/R2 application, `backup_cli configured --destination`
uses its existing explicit database URL and R2 identity. It reads immutable
published keys and verifies provider, owner-bound key, size and SHA before
publishing a manifest. It neither constructs the app nor migrates the source.
There is no fallback to a default AWS identity or Local file when R2 fails.
Local and R2 references may coexist only when both original stores are present.
With explicit `--allow-missing-media`, a missing Local object is recorded only
when its immutable object directory is absent (`observation: local_absent`).
An existing directory with missing metadata or body remains corruption and
refuses capture, as do access errors and unsafe paths. R2 absence still requires
the existing `head` observation. Neither observation proves an intentional
deletion: classification still requires evidence from the same snapshot;
otherwise coverage is incomplete and `loss_alert_required` remains true.
Strict capture and restore remain the defaults. Explicit incomplete restore
retains the missing-object inventory and keeps execution disabled. Readers from
before this change refuse archives containing `local_absent`; use the qualified
updated reader. This does not authorize retrying a failed capture, changing its
partial directory, or clearing a daily-preserve history guard.
Isolated restore preserves the original manifest/database, copies verified bytes
to Local, and changes only the restored asset/artifact provider locators before
applying the existing execution hold. No R2 write/versioning API is called.

`backup_remote` accepts only a completed backup that passes `verify_local`.
It copies only `database.sqlite3`, the manifest-listed `media/*.bin`, and
`manifest.json`; extra files are refused. Preserve the original backup and its
source while qualifying the off-device copy.

The snapshot covers published database-to-media references. Authentication,
Agent credentials, live sessions, staging, orphan objects, and uncommitted
writer output are excluded. Unresolved operations remain obligations; their
unique staging bytes need separate preservation before migration/deletion.
Project editing activity is business history and is included with its exact
tenant/owner/project/version identity, safe actor display fields and structured
operations/targets. Older backups without that table restore with empty history.
Unknown tables or changed columns still require review; this does not expand
the authentication exclusion into a general ignore-unknown rule.

## Explicit AWS destination and authority

Use a **dedicated private general-purpose S3 backup bucket**, not the application
release bucket. Root must inspect or provision it separately. The helper does
not create buckets, keys, policies, schedules or retention rules.

Required source contract:

- One explicit account, AWS Region, bucket and same-account customer-managed KMS
  key ARN. Each copy uses `business-backups/<new UUIDv4>`.
- Versioning enabled, all four Block Public Access settings enabled,
  bucket-owner-enforced ownership/ACLs disabled, and bucket default SSE-KMS using
  the exact key. These are read back before copying and restoring.
- Every object request supplies `ExpectedBucketOwner`. PUT supplies the exact
  SSE-KMS key, SHA-256 checksum and `If-None-Match: *`. All traffic uses the
  official regional HTTPS endpoint with certificate verification enabled.
- One explicit same-account backup-role ARN, assumed from the existing host AWS
  role using a separate in-memory SDK session; no static keys,
  new project `.env`, credentials in arguments, presigned links or copied vault.
  The caller account and returned/verified assumed-role identity are checked
  before S3 access. The application/release host role receives only narrow
  `sts:AssumeRole` authority; S3/KMS permissions belong to the backup role.

These checks do not prove that IAM is least privileged. The operator must grant
only the dedicated backup bucket/prefix and key to the selected backup role.
The application/release publisher should not inherit backup read permissions.
Required bucket checks use `s3:GetBucketVersioning`, `s3:GetBucketPublicAccessBlock`,
`s3:GetBucketOwnershipControls`, `s3:GetEncryptionConfiguration`, and
`s3:ListBucketVersions` (prefix-scoped). Object operations require `s3:PutObject`,
`s3:GetObject` and `s3:GetObjectVersion`; do not grant object/version deletion or
bucket/key administration to this role. KMS requires `kms:GenerateDataKey` and
`kms:Decrypt`, including checksum readback. Scope the key policy using the
account, regional `kms:ViaService` and the actual S3 encryption context. When S3
Bucket Keys are enabled that context may be the bucket ARN rather than an object
ARN; do not install a guessed condition that prevents recovery. [SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html),
[KMS least privilege](https://docs.aws.amazon.com/kms/latest/developerguide/least-privilege.html).

Require TLS in the bucket policy, and independently review the bucket/key's
read/admin principals. Enable appropriate encrypted access logging, CloudTrail
data events and CloudWatch monitoring during infrastructure acceptance. No such
resources are enabled by the helper. [S3 security](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html),
[monitoring](https://docs.aws.amazon.com/AmazonS3/latest/userguide/monitoring-overview.html).

S3 storage, PUT/HEAD/GET operations, KMS operations, monitoring and any applicable
data transfer are billable. Retained versions/partial copies consume storage;
this implementation never expires or deletes them. Determine a retention policy
and cost estimate from actual backup size/frequency separately. [S3 pricing](https://aws.amazon.com/s3/pricing/),
[KMS pricing](https://aws.amazon.com/kms/pricing/).

## Commands and evidence

`target.json` contains only `account_id`, `region`, `bucket`, `prefix`, and
`kms_key_arn`, and `role_arn`. It contains identifiers, not credentials. Use explicit absolute
private paths. The CLI defaults to an offline plan and does not initialize the
AWS SDK without `--execute`.
Role credentials are requested for one hour and kept only in memory; they are
not injected into the app/controller environment. An operation that outlasts
them remains partial and requires reconciliation, rather than refreshing
authority or automatically replaying writes.

```text
python -m studio_platform.backup_remote_cli plan --target <target.json> --backup <verified-backup>
python -m studio_platform.backup_remote_cli copy --execute --target <target.json> --backup <verified-backup> --receipts <new-private-action-dir>
python -m studio_platform.backup_remote_cli reconcile --execute --target <same-target.json> --receipts <original-action-dir> --reconciliation <new-private-evidence-dir>
python -m studio_platform.backup_remote_cli restore --execute --target <target.json> --receipt <retained-completion.json> --download <new-private-readback-dir> --destination <new-isolated-restore-dir>
```

The copy first checks that the entire snapshot prefix has no object versions or
delete markers, then conditionally creates a unique operation claim. Every file
is conditional-create-only. Successful PUTs require exact-version encryption,
metadata and SHA-256 receipt checks; an unknown PUT gets one exact content
readback and never a second PUT. A collision is refused even if bytes match.
`manifest.json` is the last business payload; `complete.json` is the final
transport completion manifest containing every file's exact version. No
completion marker is emitted for a partial copy.

Keep the local private `completion.json` **and target configuration in independent
operator custody away from the source host**, together with access to the KMS
key through a separately recoverable AWS identity. They pin the completion
version, checksum, size, and snapshot-manifest checksum. An S3 copy whose only
recovery credentials/key authority or identifying receipt are on the failed host
has not met the recovery requirement. KMS key deletion/disabled access can make
otherwise intact encrypted data unrecoverable.

Restore reads the exact completion version/hash and exact file versions, checks
all bytes, then invokes the existing `verify_local` and `restore_local`. It only
writes new private directories. The result is an isolated SQLite/Local recovery
copy with execution disabled, unfinished jobs held, and authentication requiring
reprovisioning. **It is not production PostgreSQL failover or a service start.**

Copy/download intents and partial directories remain after failures. A hard
interruption may leave a partial remote prefix or a complete remote manifest
without a local success receipt. Do not replay the same copy or treat an absent
object/list as proof that a write failed. Reconcile the recorded exact prefix,
operation ID, version/checksum and key binding through an independently reviewed
operator action; never erase unique copies to make a retry pass.

### Reconcile an uncertain copy without writing to S3

The `reconcile` action reads the original `intent.json`; it does not create a new
prefix or rerun the copy. The exact target, operation ID, snapshot hash and full
file inventory must match. It initializes the existing separately assumed backup
role only after local evidence validation and a new private read-intent receipt.
It uses only bucket checks and object GETs, never PUT, DELETE or absence inferred
from a listing. Missing/denied/ambiguous reads do not grant retry authority.

The original remote claim must match the original operation. If the original
local `completion.json` exists, its exact version/hash is mandatory; an optional
`--receipt <independently-retained-completion.json>` may supply that pin when the
local final receipt was lost. Conflicting retained receipts are rejected. A
missing/corrupt pinned version never falls back to the latest version. Without a
retained completion pin, one current completion GET discovers its immutable
version and validates its bytes, encryption, operation and exact original plan.
All referenced payloads are then read by their completion-pinned versions and
full SHA-256. Every returned streaming body is closed.

- `complete` (exit 0): all pinned bytes were verified. The new evidence directory
  contains a restored **transfer receipt** named `completion.json`, suitable for
  the existing explicit isolated-restore command. This is not a restore-success
  receipt; `restore_verified` remains false.
- `partial` (exit 2): the matching claim and observed files were verified, but no
  transport completion was observed. Even all payloads without `complete.json`
  remain partial. The helper does not synthesize remote completion or resume
  writes. A current `NoSuchKey` is a dated observation, not proof an in-flight
  write failed.
- `unknown` (exit 2): identity, checksum, key, version or read outcome could not be
  established. Static diagnostics and any already verified version evidence are
  retained. Permission denial is not treated as absence. A malformed local
  intent or refused output location fails before object reads (exit 1).

The original action directory and uncertainty receipts remain untouched. A new
private directory receives `read-intent.json` and `reconciliation.json`; only a
fully verified result receives its local transfer receipt. Reusing that output
directory is refused. Reconciliation can be explicitly repeated as a new local
read observation of the same original prefix; this never authorizes upload
replay, a new prefix, deletion, restore, automatic cadence or execution of held
jobs. Preserve any observed completion/version pins during follow-up review;
changed remote evidence is not permission to silently substitute versions.

Source tests use real synthetic portable snapshots and a fake client that
forbids all writes during reconciliation. They cover lost final receipts,
partial copies, version/content/key/operation mismatch, ambiguous reads, exact
retained-version behavior and a disabled isolated restore from the recovered
receipt. These tests do not exercise AWS, real role credentials or a live copy.

Current bounds reuse the portable backup envelope: at most 512 MiB per copied
file, 40 GiB plus bounded database/manifests in total, and bounded file count and
JSON. No archive extraction, multipart upload, chunk store, deduplication ledger,
automatic deletion, recurring job or client-side key-file scheme is introduced.
SSE-KMS means encryption at AWS storage; it is not client-side ciphertext.
The offline plan also reserves worst-case JSON space for every bounded S3
version ID; large file counts can be refused before any upload so the final
version manifest always fits the 16 MiB readback limit.

## Acceptance still required

The source tests use real synthetic PNG/database backups and fake S3. They
cover content/version/key ownership checks, interrupted writes, private receipt
preservation, non-overwrite, tampering and disabled owner-isolated restoration.
They do not certify AWS encryption/IAM behavior, key recovery, real off-device
readback, production PostgreSQL restoration, or the availability of runtime-role
credentials in a selected operator process. Root must verify those actual
deployment facts before claiming an off-device backup, while preserving the
source and all unresolved obligations.

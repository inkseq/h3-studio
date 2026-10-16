# Independent business backup storage

`business-backup.json` is a source-only CloudFormation template for work package
F. It complements [the portable backup transport](../../BACKUP-OFFDEVICE.md).
It does not deploy itself, move live Local storage, back up authentication, or
change a job, GPU, budget or release bucket. Source checks are not AWS acceptance.

## Explicit destination and authority

Supply every parameter; none has a default. The independently observed target
is account `829135631045`, Region `ap-southeast-1`. Production uses existing role
`arn:aws:iam::829135631045:role/sixnine-platform-ec2`; staging uses
`arn:aws:iam::829135631045:role/inkseq/staging/sixnine-staging-ec2`.
Supply the exact role name and path as separate parameters; template rules
reject crossed pairs. Recheck these before an
authorized deployment. `ExpectedAccountId` and `ExpectedRegion` are asserted
against the stack context. `BackupBucketName` must be a new globally unique
`sixnine-business-backup-...` or `inkseq-business-backup-...` name; this stack does
not adopt an existing bucket. Staging creates its own bucket/key/backup role;
do not update the existing production stack or widen its trust/bucket policy.

The six resources are one backup role, one customer-managed KMS key, one bucket,
its deny policy, the backup role's access policy, and a uniquely named policy
attached to the existing host role. The existing host role's trust and other
policies remain unchanged. The only new host permission is `sts:AssumeRole` on
the generated backup role. The backup role trusts only that exact host role,
with session name `sixnine-backup-copy` and a maximum one-hour session.

The host-root operator supplies the explicit output `BackupRoleArn` as
`target.role_arn`; the transport assumes it in memory. No permanent key is
created. Application/controller containers must still be unable to access IMDS
or host credentials. **That process/network isolation is a separate deployment
check, not something this storage template establishes.** A host administrator
with access to the instance role can assume the backup role.

| Operation | Backup role permission and boundary |
| --- | --- |
| Verify bucket safety | Four specific bucket configuration reads, exact bucket |
| Reject a reused snapshot | `ListBucketVersions`, only `business-backups/*/` prefixes; transport uses a new UUIDv4 and `MaxKeys=1` |
| Create a payload/receipt | `PutObject`, only `business-backups/*`, exact KMS ARN, `aws:kms`, and `If-None-Match: *` |
| Verify/restore content | `GetObject` and `GetObjectVersion`, same prefix; transport pins checksums and version IDs |
| Encrypt/read checksums/decrypt | `GenerateDataKey` and `Decrypt`, exact key, same account and regional S3, object-prefix encryption context |

`HeadObject` uses object-read permissions; `GetCallerIdentity` requires no added
allow. There is no multipart, `ListBucket`, delete, ACL mutation, bucket
administration or KMS administration grant on the backup role.

The bucket has all four Block Public Access settings, bucket-owner-enforced
ownership, versioning, default exact-key SSE-KMS, and Bucket Keys disabled.
Bucket Keys would change the KMS context from the object ARN to the bucket ARN,
weakening this prefix boundary. [AWS SSE-KMS permissions and context](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html).

The bucket policy rejects plaintext transport, TLS below 1.2, missing/wrong
encryption headers, missing/wrong key ARN, unconditional writes, deletion of
objects or versions, and direct object access outside the backup role. It
requires the documented `s3:if-none-match` condition key to equal the literal
`*`; this is not a wildcard comparison. No multipart exemption is included
because the transport only uses single PUTs. Service log delivery and
`CopyObject` are not supported destinations/paths here.
[Conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html),
[TLS policy keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html).

Wildcards in deny statements deny access and grant none. The object wildcard in
the role is confined to one bucket's `business-backups/` prefix. `Resource: "*"`
in this KMS **key policy** refers to this key; the role's identity policy names
the exact key ARN. The account administration statement permits explicit key
management, including recovery of its policy, without directly granting data
cryptography, key deletion or grants. An authorized key/bucket policy
administrator can change these controls; this is not WORM or protection against
account-administrator compromise. [KMS key policy administration](https://docs.aws.amazon.com/kms/latest/developerguide/key-policy-default.html).

## Retention and recovery

All six resources have `DeletionPolicy: Retain` and `UpdateReplacePolicy:
Retain`. Retaining the policies and role preserves the data-access path alongside
the key and bytes when a stack is deleted. Stack deletion therefore leaves
resources, permissions and ongoing charges. There are no expiration rules,
deletion automation, replicas or Object Lock. This is off-device, same-account,
same-Region storage, not off-account or regional disaster recovery.

Do not replace the role, bucket name or key as an ordinary cleanup/update. A
retained old key policy names the old role, and changing a bucket/key parameter
does not migrate content or prove access to old versions. Plan recovery access
and re-read the complete change set before any such change. Keep the output
identifiers, target configuration, completion VersionId/checksum and independent
AWS account recovery authority away from the source host. The backup role can
also be reached from a replacement host through the retained source role;
using another recovery principal requires a reviewed trust-policy change.

## Deployment acceptance remains separate

Local checks parse JSON and exercise the narrowly used policy conditions;
they do not implement an AWS IAM simulator. `cfn-lint` and `cfn-guard` were not
available in this authoring environment and were not installed. No service
validation, IAM acceptance or backup has been claimed from these checks.

Before execute, the integration owner must:

1. Recheck account/Region, exact existing role, current app/container credential
   isolation, a new bucket name, and recoverable account administration. Run
   official CloudFormation `ValidateTemplate`, then inspect a change set with
   `CAPABILITY_IAM`; require exactly these six resources and no release-bucket,
   EC2 role replacement, application or GitHub deployment-role change. Validate
   the KMS key policy's creator/administration access before execution.
2. After an explicitly authorized deployment, read back stack outputs, role
   trust/permissions, bucket policy, encryption, versioning and ownership. Match
   the transport's exact target and ExpectedBucketOwner. Do not place credentials
   in the target JSON, source, logs or stack parameters.
3. Prove the actual assumed identity and a small synthetic conditional encrypted
   write/read of exact versions. Verify wrong/missing key/encryption/conditional
   headers and direct host-role data access are denied. A repeated conditional
   PUT must fail without changing the original version. Do not delete test data
   to make a retry pass. Preserve partial receipts and source files.
4. Complete a real portable snapshot, off-device readback, checksum verification
   and isolated execution-disabled restore before claiming recovery coverage.
   This does not establish production PostgreSQL failover. Add separately
   reviewed encrypted CloudTrail data events, access monitoring and alerts to
   the operational acceptance; none is configured by these six resources.

If S3 server access logging is added, use a **separate** compatible SSE-S3 log
destination, not this bucket with its operator-only, conditional-write policy.
Do not widen this policy to make unrelated service delivery work.
[S3 security guidance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html).

## Cost inputs

Estimate from measured backup size, file count, frequency and retention: S3
current/noncurrent version storage, retained partial copies, PUT/list/read and
verification requests; customer-managed KMS key storage/rotation and API
requests; applicable restore/data-transfer charges; and any separately enabled
logging/monitoring. There is no expiration to bound growing storage
automatically. Use current Singapore pricing and actual usage; no numeric price
or monthly ceiling is asserted here. [S3 pricing](https://aws.amazon.com/s3/pricing/),
[KMS pricing](https://aws.amazon.com/kms/pricing/).

### Historical R2 output provenance

Historical output metadata may omit `provider`. The backup resolves GPU and CPU
R2 outputs only through the original artifact-write receipt: tenant, owner, job,
attempt-derived receipt ID, verified role, key, byte count, SHA-256 and MIME must
match. CPU receipts without a route must match an explicitly configured storage
identity hash; unknown bindings fail closed. GPU receipts additionally require
`gpu-r2-v1`. No failed read selects another provider. Original Local manifest
layout remains supported.

Presigned GPU uploads can lack optional HEAD SHA metadata. Missing metadata is
accepted only with matching provider/key/size/MIME and a complete body read that
matches the original SHA-256 and length. A present mismatching HEAD SHA fails.
The isolated recovery copy maps verified remote media to Local while retaining
original remote receipts and budget history; it stays on execution hold and is
not an automatically deployable replacement database.

### Explicit incomplete-media archives

Default v1 export, verification and restoration remain strict. Explicit
`backup_cli configured --allow-missing-media` emits v2 with available `objects`,
`missing_objects` and exact `coverage`. Each missing entry retains the original
owner/key/size/SHA/provider binding and reference provenance, plus
`reason: source_object_not_found`, `observation: head` and observation time.
Only definitive not-found from the exact configured R2 HEAD qualifies. No
fallback provider, ignored GET failure, permission error or truncated body does.

Verification proves available and missing sets are disjoint and their union
matches every original DB reference, including identities and available body
hashes. Incomplete `verify`, `restore-local`, and remote plan/copy/restore require
`--allow-incomplete`. Transport uses `sixnine-private-backup-copy-v2`, propagating
coverage through its intent, pinned completion, receipt, reconciliation and
recovery summary. Reconciliation `classification: complete` describes all bytes
of the declared archive; `coverage.status` still reports missing source media.

Private manifests and recovery holds contain object identities; public reports
expose only counts, hashes and closed error codes. An incomplete archive does
not repair sources, release unknown reservations, establish why objects went
missing or authorize production recovery. Keep failed UUIDs and partial evidence
until a separately reviewed positive terminal closure.

The S3 object-metadata envelope keeps its existing v1 integrity marker for both
archive formats. Explicit v2 identity and coverage are carried by the hashed
manifest, intent and completion; encryption/checksum guards are unchanged.

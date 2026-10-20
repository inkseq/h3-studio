# Private local-media to R2 copy

`studio_platform.storage_migration_cli` is a bounded operator tool. It copies
published `LocalObjectStore` objects under their **unchanged logical keys** into
the explicitly selected private R2 bucket. It never connects to a database,
rewrites ownership or receipt records, deletes local files, creates buckets,
switches production configuration, or starts/stops compute.

## Operating sequence

1. The release owner verifies the private bucket, exact central profile,
   protected runtime credentials and private signed browser access. Inventory
   database references and unfinished upload/collection receipts read-only.
2. Run `plan` against the actual existing local object root and a **new** private
   receipt directory outside that root. This step is offline and loads no key.
3. Run `apply` using the same target arguments and receipt directory. It refuses
   changed source inventory/files, symlinks, hardlinks, foreign target identity,
   conflicting destination content or objects larger than the single-PUT limit.
4. Establish a short fence for all local media writers, then make a fresh plan
   for any final delta and copy. Existing identical R2 objects are checked and
   adopted without another PUT. Do not silently abandon unfinished receipts
   bound to the original local store.
5. Switch the existing API and CPU execution store together, verify upload,
   references, video and independent audio plus authenticated browser/Agent
   download, then run `verify` against the fenced final plan. The release owner
   records cutover and writer-fence evidence separately; this tool's result
   always reports `writer_fence_verified: false`.
6. Keep pre-cutover local originals, staging, rollback configuration and private receipts
   until the user explicitly confirms deletion. Copy verification is not
   deletion authorization or a claim that local staging stops growing.

New uploads and collections created on R2 opt into the existing receipt's
`remote_verified_v1` scratch policy. Only their own recorded source/normalized
or raw/verified processing files can be removed, after publication is ready and
released or the result transaction is settled, and after full remote hash
verification. Old local or migrated copies never gain this policy. Unknown or
incomplete writes retain scratch and conservative quota; interrupted cleanup
retries the same receipt under its original operation/slot lock. There is no
timer, separate reconciler or general historical-media cleaner.

## Display scratch for an old settled artifact

The existing `studio_platform.rendition_backfill apply` command accepts the
explicit artifact-only flag `--use-data-display-scratch`. Keep `--work-dir` equal
to the original output receipt's `staging_root`, and supply that receipt's current
`--receipt-version`. The flag uses the current configured data directory only for
poster processing, under
`artifact-display/<tenant>/<owner>/<original-receipt-id>/display-v1`. It does not
remap or create the old output root, change storage identity, or rewrite original
roles, accepted jobs or accounting. Default collection and CLI paths are unchanged.

Before processing or storage IO, the original receipt CAS freezes the selected
data directory in its auxiliary `display` state. Retry and cleanup must use the
same explicit flag and data directory; even completed or missing display states
cannot be moved. Unknown writes keep the original body, keys and reservation.
The stopped-writer assertion does not override path, version or ownership checks.
Only the exact auxiliary scratch manifest is cleaned; old source/tag directories
and local originals remain untouched.

This option does not relax a legacy Local storage binding. An operator may use
the original CLI with a process-scoped `SIXNINE_STORAGE_PROVIDER=local` only after
proving the original Local identity and retained objects. That creates Local
display objects, **not R2 publication**. For an R2 site, subsequently create a new
private immutable copy plan and apply/verify it through the existing migration
tool, with the same keys, full readback hashes and all local copies retained.
Keep the earlier migration plans intact; do not reuse their frozen inventories
after adding new display objects. Do not declare R2 thumbnails ready until that
copy is verified: between Local publication and completed copy, the R2 route may
correctly report that the new object is absent.

## Bounded all-owner display backfill

`python -m studio_platform.rendition_backfill plan --all-owners` lists the existing
tenant's ready assets and succeeded video artifacts, including owners without an
active login. It does not load storage credentials or read objects. Owner pages
use `--owner-limit` (default20, maximum100) and `--owner-offset`; each owner's
asset/artifact pages retain `--limit`/`--offset` (maximum100 items per kind).
Follow `next_owner_offset` and each owner's independent `next_offset` /
`artifacts_next_offset`, or use the original explicit `--owner` mode for that page.
Counts and completion describe the requested pages, not an unbounded whole-store scan.

After reviewing that metadata, `apply --all-owners --max-items 100` attempts at
most100 original receipts from those pages (maximum1000). Every attempt rechecks
the snapshotted owner and receipt version through the same single-item path.
Ready records are left alone; busy, missing-receipt or different-binding records
remain unchanged or refused, with a finite per-item reason. Earlier successful
items remain accepted if a later item fails. Exit2 / `state=partial` means there
are skipped/refused items or more requested pages; it is not successful global
coverage. Re-running never regenerates a completed rendition or resets quota.

Bulk mode cannot globally assert stopped writers, override artifact roots, pass
one receipt version for several records, or rebind storage. For each artifact it
uses that original receipt's frozen staging root; the explicit
`--use-data-display-scratch` option still freezes the current data directory in
that receipt's auxiliary display intent. Original video/audio, accepted roles,
storage bindings and normal scratch/derived-byte accounting are preserved.

An accepted artifact display attempt publishes `rendition_status: pending` in
the same receipt/metadata CAS before downloading or decoding. Original downloads
remain available. Verified completion becomes ready; a bounded first-frame
failure becomes missing. An uncertain PUT stays pending and retains the original
keys/reservation for exact stopped-writer recovery, rather than being retried by
the all-owner command.

Example (replace the uppercase bucket placeholder with the approved name):

```sh
python -m studio_platform.storage_migration_cli plan \
  --source-root /srv/sixnine/platform-data/objects \
  --receipt-dir /var/lib/sixnine/r2-migration/INITIAL_COPY \
  --endpoint https://cc1332ffcc46df68e32f4172247bb733.r2.cloudflarestorage.com \
  --bucket SIXNINE_PRIVATE_BUCKET --profile cloudflare-r2--rig-root

python -m studio_platform.storage_migration_cli apply \
  --receipt-dir /var/lib/sixnine/r2-migration/INITIAL_COPY \
  --endpoint https://cc1332ffcc46df68e32f4172247bb733.r2.cloudflarestorage.com \
  --bucket SIXNINE_PRIVATE_BUCKET --profile cloudflare-r2--rig-root \
  --credentials-file /run/secrets/r2_storage
```

`verify` uses the same arguments as `apply`, but performs only remote reads and
local validation (the private operator lock is shared). Target endpoint,
region, bucket, service, profile and size limit must match the immutable plan.
The protected file uses the reviewed `storage_config` runtime reader; keys
never enter command arguments, process environment, public output or Git.

## Readable historical display copies

An original ready image/video may remain readable after its Quick Chat history
is trashed. The existing explicit backfill now uses a dedicated display receipt
CAS for that case. It preserves the original/model identity, receipt binding,
normal byte/active quotas and operation ownership. Ordinary uploads, collection
and generation writes still require an active parent. Actual asset/project
trash, purging/purged history, tombstones and unconfirmed parent bindings remain
refused. An unknown display PUT retains its original key and reservation.

For an asset whose original Local objects were already immutably copied to the
current R2 store, the exact-item command may add this operator-only proof:

```sh
python -m studio_platform.rendition_backfill apply \
  --owner ORIGINAL_OWNER --asset-id ORIGINAL_ASSET_ID \
  --receipt-version EXACT_CURRENT_VERSION \
  --original-copy-receipts PRIVATE_EXISTING_COPY_DIRECTORY \
  --original-local-root /data/objects \
  --original-copy-provenance PRIVATE_ACCEPTED_MOUNT_PROOF.json
```

The copy plan and verified receipt must be protected private files, match the
current R2 identity, and contain the exact original/model manifests. Every
selected R2 source is freshly GET-read and fully SHA256-checked before a derived
PUT. Derived objects are written and verified in current R2 before the original
asset receipt publishes their metadata; no Local-ready-before-copy window is
created. The original `storage_binding` and original/model roles stay unchanged.
The new derived target and proof hashes are frozen in the rendition receipt.

If the plan's literal Local root already reproduces the original binding, omit
the final two flags. Otherwise both are required: the protected version1
`logical_local_mount_provenance` receipt binds the explicit logical root to the
plan source root/identity, original binding, raw/canonical plan digests, copy
receipt digest and accepted covering bind mount. The command checks the mapped
path and the **container logical root's** actual device/inode against the plan;
it does not stat a host-only path, instantiate a Local store or infer an alias
from matching hashes. Private metadata copies must be available in the CLI's
container namespace with their original hashes and restrictive permissions.

These proof options require one explicit asset or artifact and its current receipt
version; plan and all-owner selections reject them. They do not grant authority
to process other historical objects or rewrite an existing unknown target.

For a settled successful generated-video artifact whose original video/audio
were already copied, use the same proof flags with `--artifact-id`, its exact
original `--work-dir` and receipt version. Add `--use-data-display-scratch` when
display scratch must use the current data directory rather than the immutable
original writer root. The original root identity still must match exactly.

The artifact proof validates **all** frozen video/audio roles against the
original copy plan and fresh complete R2 GET/hash before any derived PUT. Only
the existing display CAS may accept that evidence: collection and normal
ArtifactWriter reads/writes still refuse another binding. Original binding,
roles, fingerprint, phase, job/attempt and billing remain unchanged. The current
R2 derived target and closed proof hashes are frozen in `display`; a changed
plan/copy/provenance, target, source or unknown key cannot be substituted. A
completed replay requires its current exact version and the same proof, performs
no new PUT, and retains the normal accounting. Pending writers still require
the existing explicit stopped-writer assertion and exact current version before
recovery; a missing/unknown object is not automatic retry permission.

ArtifactWriter's original Local and R2 identity hashes use **compact** sorted
JSON, while asset receipts retain their existing noncompact sorted JSON. A
logical-root proof for an artifact must bind that artifact's original compact
source hash; do not reuse an asset proof with a different source hash or rebind
either receipt. Protected metadata remains private to the CLI UID, and the
logical root/inode check is the same existing mount-provenance contract.

## Integrity and recovery

Production deployment uses the existing managed migration secret's optional
`r2_storage` field, hydrated as root:10001 mode 0440 at
`/run/sixnine-secrets/r2_storage`. Configure the protected migration `paths.json`
with all six nonsecret settings together: `SIXNINE_STORAGE_PROVIDER=r2`,
`SIXNINE_STORAGE_ENDPOINT`, `SIXNINE_STORAGE_REGION=auto`,
`SIXNINE_STORAGE_BUCKET`, `SIXNINE_STORAGE_PROFILE=cloudflare-r2--rig-root`, and
`SIXNINE_STORAGE_CREDENTIAL_FILE=/run/sixnine-secrets/r2_storage`. API and
controller share the same identity; dispatch receives no storage credential.
The unchanged local configuration does not read the unused Compose secret alias.
Keep the previous protected configuration as a rollback receipt. Do not claim
an old local-only application image can read newer R2-only objects, or that the
existing local-media backup already covers those objects.

Each local object's directory must match SHA256(logical key); metadata, private
regular blob, byte length, MIME and full SHA256 are validated. Source inode,
size and timestamps are bound into the private plan and rechecked on apply.
R2 uses conditional `If-None-Match: *` writes. A successful HEAD and metadata
hash are insufficient: every object is fully GET-read and SHA256-checked.

`plan.json` and `copy-receipt.json` are private; reports expose only aggregate
counts/bytes, a plan hash and static diagnostics. The receipt is durably written
before a PUT. After an uncertain acknowledgement, repeating `apply` only checks
the same key. A verified matching object is adopted; an absent unknown object
requires review and is never automatically resubmitted. No remote object is
overwritten or deleted. Linux operator locking prevents concurrent edits to the
same receipt; separate plans remain safe through conditional target creation.

The initial tool deliberately supports at most 100 MiB per published object,
100,000 objects, 40 GiB total and a 16 MiB private plan. Larger input/outputs may
already use the application's durable multipart adapter; migration of those
needs its own bounded transport evidence, rather than writing a new production
multipart journal during a database-free copy.

Focused offline verification: `python -m unittest test_platform_storage_migration -q`.
Production evidence must name the exact release, bucket, verified object/byte
counts, unchanged local copies and end-to-end results separately.

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

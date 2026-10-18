# Project-scoped library: interim storage decision (#238)

Storage stays in the original project namespace. Profiles remain project entities; uploaded bytes remain original owner/project asset receipts. An aggregated library is only an authorized read projection, not a new account library or accounting authority. No existing row, object, accepted recipe or identifier is migrated.

Tags and filing change the original project authoring document with its existing expected-version CAS, owner lock and guided edit receipt. Work/unused are computed from effective visible authoring usage; historical accepted input retention is reported separately and is never deletion permission. Scoped assistants see only accessible projects and an explicit authorized-project usage scope.

Cross-story import creates an owner/actor/source/destination/body-bound intent in existing platform documents, then uses original upload journals and quotas with deterministic per-file client keys. Source and destination permissions are checked before the intent or copying and on resume. Exact source bytes are SHA/size checked. Profiles and referenced file entities receive new destination IDs; only reference fields are remapped. The original source, orphaned partial copies and accepted receipts are retained. Destination project CAS commits the final authoring change through the existing guided receipt. Unknown storage outcomes retain the original intent/key; a definitively stale target produces a closed blocked operation only when uploads are positively settled. No automatic restart, cleanup, generation or new ledger.

Deletion remains #236 explicit trash/restore/purge. Full account-wide storage is a separate future design decision.

Pending library imports block source/destination lifecycle changes under the original owner reference gate, even before receiving bytes. Terminal receipt reads/replays retain current owner/scopes without requiring active project content. Only positively completed same-owner import audit capsules are excluded from an explicit universal-purge reference census; pending and blocked capsules remain conservative references. No audit rows are removed. Original guided validation rollback after verified copies produces `blocked`, `reason=library_authoring_validation_failed`, `no_authoring_committed=true`; retain original copies/key and explicitly choose a new intent if needed. / 未完成导入阻止相关生命周期变更；已确认的校验回滚保留原回执和副本，不自动重试或清理。

## Image display copies (#287, source preparation)

Image upload and cross-story import create `thumb` (long edge at most320px,
at most64KiB) and `preview` (at most1280px/512KiB) WebP objects on the **same**
asset, upload receipt, storage binding and quota. EXIF orientation is applied,
private image metadata is stripped, alpha is preserved and small originals are
not upscaled. Original/model bytes and model snapshots are unchanged. Physical
role enumeration includes display copies in quota, lifecycle/reference closure,
explicit purge and backup/restore; an incomplete display write retains the whole
asset during explicit purge. No GET generates or backfills files.

Public assets and corresponding authorized Library file entries advertise
`thumbnail_url`, `preview_url`, `rendition_ready`, `rendition_status` and safe
`renditions.{thumb,preview}` descriptors (URL, width, height, byte count, SHA256,
content type, immutable identity). A missing copy has no URL and the explicit
`/v1/assets/{id}/content?rendition=thumb|preview` request returns404
`rendition_not_ready`; it **never** returns original bytes as a fallback.
Selectors are strict and retain the original project/owner, deletion and
`expected_account` fences. Tiny thumbnail reads have a separate global8/owner8
admission, within existing global/auth/owner limits; previews and originals
retain the heavy-media gates. API responses and redirects stay `no-store`.
Only the immutable R2 WebP object receives private24h cache headers through
the existing request-local12h-bucket signer; issued URLs are bearer grants until
expiry, not an immediate-revocation guarantee.

Offline operator commands use the installed environment's original database and
storage configuration (no API/controller startup or copied credentials):

```console
python -m studio_platform.rendition_backfill plan --owner OWNER --limit 100 --offset 0
python -m studio_platform.rendition_backfill apply --owner OWNER --asset-id ASSET --receipt-version VERSION
```

Plan is metadata-only and never loads storage credentials. Apply addresses one
owned original receipt/version, reserves before borrowing bytes, verifies source
size/hash, freezes each output before PUT and resumes only the same keys after an
unknown write. An actually stopped interrupted writer additionally requires
`--assert-writers-stopped` and the current receipt version; elapsed time is not
that proof. R2 backfill scratch uses the existing manifest retention after all
objects verify; local/original copies remain retained. Ready legacy assets with
no original upload receipt are explicitly reported `missing_original_receipt`
and left unchanged, not silently adopted or re-accounted. This command has not
been run on staging/production as part of source preparation.

### Video posters and output receipts

Uploaded/imported videos use the same two physical display roles on their
original asset receipt. Only the first selected video frame is decoded with
the existing media process/admission, demuxer/protocol/stream/dimension,
30-second and memory bounds. The original and normalized model copy retain
their own identities; poster selection never changes generation inputs.

Generated videos keep the frozen GPU delivery manifest **video/audio only**.
The original `ArtifactWriter` receipt has auxiliary `display` state, source
SHA, immutable object specs, reservation and exact scratch manifest; publication
and storage-accounting markers commit in the same receipt CAS transaction.
An existing verified full-SHA stream retains at most8MiB of MP4 prefix in
memory; the CPU writes only that prefix and decodes one frame, not the whole
video. Native output already uses `+faststart`; CPU normalization does too.
Old outputs are streamed for full size/SHA validation during explicit backfill,
but still retain only that bounded prefix on disk. No new GPU image or delivery
role, hidden image asset or independent task/receipt system is introduced.

Posters run after result acceptance and original R2 scratch cleanup. Missing or
late-moov/late-first-frame videos beyond the prefix/parser bounds remain
successful with explicit missing poster; no original fallback, whole-video
decode, replacement generation or changed accepted job. Known failed extraction
cleans only its frozen derived scratch and releases that scratch reservation.
Unknown PUTs keep the same original keys/body/quota until explicit same-version
stopped-writer recovery. A crash after derived scratch unlink reconciles its
counter once. API GET/HEAD never creates a poster or runs recovery.

`/v1/artifacts/{id}/content?rendition=thumb|preview` retains original artifact,
project, owner, deletion and expected-account gates. Assets, Library, public job
artifacts and session take summaries advertise safe poster URLs and dimensions/
size/hash only. Raw auxiliary storage keys stay private. Thumbnail admission and
R2 cache policy are the same as image copies; API redirects remain `no-store`.
Purge/reference closure, original storage quota and backup/restore enumerate
auxiliary objects; unresolved output display writes conservatively retain the
original artifact and companions.

```console
python -m studio_platform.rendition_backfill plan --owner OWNER --limit 100 --offset 0
python -m studio_platform.rendition_backfill apply --owner OWNER --artifact-id ARTIFACT --receipt-version VERSION --work-dir ORIGINAL_WRITER_ROOT
```

The plan returns whole-owner `coverage.assets` and `coverage.artifacts` counters:
`total`, `ready`, `missing_original_receipt`, `writer_active`,
`backfill_required`, `unsupported`; paged safe item IDs/receipt versions and
separate continuation offsets remain available. It performs no storage calls or
credential loads. Artifact apply validates exact original owner/receipt/version
and staging root before loading the existing protected store configuration.
Legacy outputs without an original receipt are counted and left unchanged.
Actual staging coverage/backfill and browser performance/cache acceptance remain
required before switching existing production thumbnails; new-fixture unit
results are not coverage evidence for old media.

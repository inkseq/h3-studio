# Project-scoped library: interim storage decision (#238)

Storage stays in the original project namespace. Profiles remain project entities; uploaded bytes remain original owner/project asset receipts. An aggregated library is only an authorized read projection, not a new account library or accounting authority. No existing row, object, accepted recipe or identifier is migrated.

Tags and filing change the original project authoring document with its existing expected-version CAS, owner lock and guided edit receipt. Work/unused are computed from effective visible authoring usage; historical accepted input retention is reported separately and is never deletion permission. Scoped assistants see only accessible projects and an explicit authorized-project usage scope.

Cross-story import creates an owner/actor/source/destination/body-bound intent in existing platform documents, then uses original upload journals and quotas with deterministic per-file client keys. Source and destination permissions are checked before the intent or copying and on resume. Exact source bytes are SHA/size checked. Profiles and referenced file entities receive new destination IDs; only reference fields are remapped. The original source, orphaned partial copies and accepted receipts are retained. Destination project CAS commits the final authoring change through the existing guided receipt. Unknown storage outcomes retain the original intent/key; a definitively stale target produces a closed blocked operation only when uploads are positively settled. No automatic restart, cleanup, generation or new ledger.

Deletion remains #236 explicit trash/restore/purge. Full account-wide storage is a separate future design decision.

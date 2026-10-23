# Closed production adoption (#481)

This is an operator entry, not an HTTP migration API or a release approval.
The original `tools/migrate_quick_chat_stories.py` remains default read-only;
its apply and rollback actions still refuse production.

## Host qualification

Claude review6371428118 approves the small entry. First promote reader-complete R
after Claude's seven exact-tuple journeys, then entry G with R genuinely retained
as rollback. Both retain archive/alias/Works readers, legacy write fencing and the
option-1 permanent-purge guard. This source does not promote releases or authorize
another cohort. No production rollback is opened.

Keep the private review manifest immutable, pinned by the reviewed host operation:
17 ready rows (4+13 by owner),23 exclusions, original snapshot and receipt hashes
from number review6370536037. Never regenerate its hashes after source drift or
put the manifest in Git. Runtime proof is separate; do not fill its review-only
placeholders to turn a planning file into an alleged operation receipt.

The host qualifies actual successful promotions, retained bytes and current app
markers. JSON shapes and hashes below detect drift; they cannot authenticate an
arbitrary operator-provided claim. The host retains a durable original batch
intent, per-session intent/command IDs, and uses the approved deterministic order.

## One-session CLI

`tools/apply_approved_quick_chat_stories.py` takes one `--owner` and `--session`,
`--manifest`/`--manifest-sha256`, `--reader-evidence`/`--reader-evidence-sha256`,
existing `--actor-user-id`, stable `--operation-id`, and a new private `--result`.
These are identifiers and paths, never credentials. There is no all-session flag.

Reader proof is a closed object with production environment/origin, tenant, actor,
per-session and batch operation IDs, manifest hash, UTC Unix `checked_at`, current
and rollback readers, and `source_sha256`. It pins the installed core, wrapper and
CLI bytes. Each reader includes exact platform/frontend SHAs, image ID, original
promotion receipt hash, self-check ID, archive contract2, and positive archive,
version-alias, Works, legacy-write-fence and permanent-purge-guard checks. Current G
and rollback R identify distinct commits. Exact names are defined by
`validate_production_readers`; secret/raw user content is forbidden. Proof is valid
for15 minutes; refresh through actual read-back, not timestamp editing.

Target/origin, membership, proof hash, actual source bytes and Postgres driver are
checked before DB access. The CLI then exclusively creates and fsyncs a private
`started_reconcile_before_retry` result. Existing files are never overwritten to
start another invocation. It initializes no schema or provider.

## Conversion and reconciliation

Both wrappers share the original atomic body and owner-reference gate, source and
project locks, exact closure/version checks and job/attempt/upload/cleanup guards.
Production additionally compares approved owner/session/project, session/child
hashes, target, counts and archive size. Only the original project and existing
migration receipt are written, in one transaction. Contract1/mapping2 is retained;
additive `production_approval` metadata leaves R readers compatible.

Any existing outcome, including malformed or cross-project receipt, refuses a
production write. After commit the CLI fsyncs closed metadata before stdout;
original content remains in the protected DB receipt. Never print the full receipt.
A command/connection/commit/output failure is not proof of rollback: stop, inspect
the original command and receipt read-only, and preserve any later user edits.
Never invoke apply again to discover an outcome. Only positively known pre-write
refusals/rollbacks can be listed as skips while other approved entries proceed.

Finally verify source/jobs/media/accounting preservation, all23 exclusions, current
release/config gates and old-session redirects. Report actual successes, not the
approved count. Claude removes old UI after this check. Soft trash/restore remains
available; permanent purge stays refused pending separate option-2 redaction work.

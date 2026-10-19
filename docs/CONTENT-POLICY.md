# Legal and moderation API

Legal text remains a draft, not an owner-approved agreement. This backend
does not approve an entity, age limit, law, training promise or retention SLA.
Default policy is `version:2026-10-draft`, `status:draft`, moderation OFF.
No startup writes, schema additions or classifier/provider setup run.

Public GET/HEAD SPA paths: `/pricing`, `/terms`, `/privacy`, `/content`, each
with/without a trailing slash. Public `GET /v1/legal/policy` reports only the
current revision/version/status and screening booleans, never keyword rules or
internal classifier identities. Price and credit-pack APIs stay public.

## Human agreement and admin policy

Human `POST /v1/account/terms {version}` accepts only the current reviewed
version. It uses the existing canonical user terms fields, server timestamp and
audit. Repeating acceptance retains the first timestamp. Machine credentials
cannot accept an agreement or edit policy. `GET /v1/account` adds `terms` with
version/status, accepted_version/accepted_at, and acceptance_required.

Human-admin GET/POST `/v1/admin/content-policy` uses the existing account gate,
document store and audit. POST requires `Idempotency-Key` and exactly:
`expected_revision`, `version`, `status` (`draft|reviewed`),
`moderation:{enabled,keywords,output_classifier}`, `reason`.
Existing role/status are reread. CAS, replay and changed-key conflicts preserve
the original action. No policy has been marked reviewed by this source change.

Checkout requires current reviewed terms. For paid generation, the reviewed
policy gates new admissions and first enqueue, before supplier/customer holds;
the plan freezes terms and input-screening evidence. A stale preview requires
preflight again. Original accepted job/purchase replays, workers, cancellations,
downloads and settlement are not reauthorized against a new agreement.
Draft policy keeps existing development generation behavior; it never enables
payments. Before activating reviewed policy, both approved current/rollback
images must enforce it; reverting to older permissive code is not a safe rollback.

## Moderation and money

Optional bounded literal keywords screen prompt text only, before a new plan
or reservation. The decision stores policy/version/hash/static reason, without
copying prompts or media into another journal. Admission verifies it again.
Keywords do not classify images, video or audio.

The trusted server-side output adapter registry is currently empty. Admin JSON
cannot select an arbitrary URL, import or secret. Adapters must bound their I/O
and return allowed/refused; failures retain collection of the original attempt.
When configured, output screening runs outside SQL after verified collection,
then a stored owner/job/attempt/media-hash decision is verified during the
existing terminal transaction. No actual media classifier is claimed today.

A refusal settles original provider and storage accounting, creates no
downloadable artifact rows, releases customer credits and retains private
objects plus an auditable decision. It never completes/captures then refunds.
Unknown supplier cost remains pending; no invoice is invented. A lost response
reuses the original terminal/receipt and cannot publish renditions or resubmit.

Privacy limits: GPU content-addressed inputs currently remain until VM stop;
after-each-job GPU purge is not established. Host journald retention is7days
and128MiB, which does not prove Grafana retention or account-deletion timing.
Legal text/contact, selected classifier/credentials, real screening coverage,
browser/legal acceptance and live payments remain separate owner/release gates.

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

## Improvement-report disclosure handoff — DRAFT (#374 / #580 step 6)

This is an implementation fact sheet for Claude's canonical legal-page update
and owner review, not legal approval or published text. It describes candidate
[PR #636](https://github.com/inkseq/h3-studio/pull/636), exact source
`dcdf51adb9c0f47a181a5714175870d9053175ee`; it is **not evidence of deployment**.
Recheck that candidate's final reviewed contract before publication. No policy
revision, reviewed status, acceptance record or runtime setting changes here.

### Data paths in that candidate

| Data | Local application storage | Sentry feedback |
|---|---|---|
| Product-problem summary | Up to 280 characters, explicitly submitted | Same summary |
| Suggested improvement / agent intent | Optional, up to 500 / 200 characters | Not forwarded |
| Category, severity, source label | Closed values | Same values |
| Authenticated actor | Hashed connection identity and human/agent kind | Human/agent kind only |
| Optional project/shot/take references | Validated IDs under the submitting account's grants | Hashed references scoped to tenant/account |
| Recovery metadata | Original key fingerprint, stable report/event IDs, timestamp, forwarding status | Event/report IDs and timestamp |
| Prompts, media, attachments, email, cookies, request/trace context | Not attached automatically; extra fields refused | Not attached automatically |

The API refuses obvious URLs, email addresses and credential syntax in supplied
text. This is not a general detector for personal information or copied story
content: a caller can still put inappropriate text in a summary. Disclosure must
say what is sent and instruct callers not to include private material; do not
promise that arbitrary free text is guaranteed anonymous. Hashed references are
pseudonymous correlation identifiers, not proof of irreversible anonymization.

Reports stay account-scoped in the existing application database. The receipt
API is not an all-users dashboard. Sentry remains the existing triage destination;
this implementation adds no new analytics provider or public GitHub publication.
Take review notes remain separate product data, not improvement-report payloads.
PostHog UI analytics and any future LLM tracing are separate data paths.

### Controls, retention and delivery limits

- The owner's share-report preference defaults to enabled. Human-only settings
  read/update use revision checks; a PAT cannot change it. The API exists in the
  candidate, but the Settings toggle must be implemented and verified by Claude
  before a public statement tells people it is available in the UI.
- Disabling sharing refuses new reports and suppresses a pending report if the
  setting wins before its one enqueue attempt. It does not recall a report
  already enqueued or erase earlier local/Sentry records.
- Context inclusion is disabled for everyone in this slice. Do not advertise
  an available optional context-sharing control yet. Automatic Story Chat and
  system reports, per-reply surveys and global agent-intent logging are also
  follow-ups, not implemented capabilities of this candidate.
- Proven project/artifact purge redacts matching local reports; completed account
  deletion redacts owned local documents through the existing guarded workflow.
  Neither statement means immediate deletion at the moment a request is made,
  nor proves that account deletion is enabled in production.
- There is no new feedback TTL or automated Sentry-feedback deletion in this
  slice. Local redaction does not erase an already forwarded Sentry summary.
  Sentry retention, access and deletion procedure, and backup expiry, need their
  actual configured evidence before any numeric promise is published. The old
  generic draft's “up to 30 days” log claim is not evidence for feedback retention.
- `enqueued` is only local SDK queue acceptance, not remote delivery proof.
  Pending/unknown outcomes retain the original ID and are not retried with a new
  key. A future assistant must not say “sent to the team” for disabled, suppressed,
  unknown or pending reports, or claim that someone reviewed a queued report.

### Proposed copy for review, after the corresponding feature is released

> **Improvement reports.** When you or a connected assistant submit a product
> problem report, we store its short summary and any suggested improvement with
> your account. We send the summary, problem category, severity and pseudonymous
> reference identifiers to Sentry to help us investigate. We do not automatically
> attach your prompts, story text, media, sign-in information or request contents.
> Do not put private content, contact details or credentials in a report. The
> sharing preference is on by default; turning it off stops new reports but does
> not delete reports already received.

This draft copy intentionally promises no retention period or current UI location.
Claude should add the verified control location when the toggle is released, and
the owner should approve a concrete retention/deletion statement after evidence
is available. Separately revise the Sentry processor row: sanitized **error
reports** still exclude request contents, while **improvement reports** deliberately
include the submitted summary. The current blanket “without your typed text”
wording cannot accurately describe both paths. Keep the distinction in both
languages; do not silently change the exception sanitizer to match prose.

The existing final legal/operator/refund and real-card gates remain outstanding.
This handoff does not mark DRAFT as reviewed, accept terms for anyone, enable
charging/Checkout or authorize a payment.

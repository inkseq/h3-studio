# Account lifecycle

## Browser sessions (#415 slice A)

`POST /api/auth/sessions/revoke-all` accepts exactly `{}` and requires a
nonempty `Idempotency-Key`. Use the authenticated same-origin human browser;
machine principals and caller-selected owners are refused. It clears the
current cookie and returns `{operation_id,state,logout_url?}`. `200/completed`
means local and linked-provider revocation are proven; `202/pending` means the
original durable operation remains unresolved. The optional navigation URL is
the exact #395 allowlisted AuthKit logout URL. The original operation UUID is
not a public status credential. A new authenticated browser can replay the
same key for the original result; it does not revoke newer sessions.

Local session removal, authority-cutoff advance, original operation journal
and DBOS enqueue share the original transaction. No PAT is revoked and no job
is canceled by this action. The provider operation freezes the canonical
subject/client and original cutoff, records requested SIDs before provider IO,
and proves absence using the original user's complete active-session listing.
A lost revoke response is reconciled before any subsequent original-SID call.
Provider failures retain the same operation and safe diagnostic. The existing
DBOS application/queue owns wake/recovery; this adds no table family.

Password authentication captures the global authority epoch before checking
credentials; AuthKit state captures it before provider sign-in. Session
issuance checks the account's revoke cutoff under the account gate. An older
in-flight flow is refused even if it finishes after revocation. Fresh sign-ins
and unrelated accounts remain usable.

If that older verified exchange nevertheless creates a provider SID after the
cutoff, its exact verified subject/client/SID is attached to the same original
revocation journal. It resumes that operation, including after the first
transport completed; no unrelated post-cutoff sessions are swept up. Each
verified late SID enqueues a deterministic transport epoch for the same
business operation, avoiding a lost wake when the original workflow commits
terminal concurrently. A scan may mark
only the original SID set it covered: a concurrent appended SID cannot be
mistaken for an already absent session.

Hosted browser use requires its verified #395 SID binding and a successful
provider check no older than 60 seconds. Checks use manual bounded pagination,
validate subject/SID/status/expiry and configured client, and run without an
open database transaction. The private success cache uses a separate document
kind; the #395 sidecar/frozen grant shape is unchanged. A partial listing never
proves absence. An unavailable provider after cache expiry returns
`503/session_provider_unavailable` without deleting the cookie; confirmed SID
absence invalidates that local session. Historical hosted sessions without a
verified SID must sign in again. Self-logout and revoke-all can still use their
local cookie authority during a provider outage, solely to remove sessions.

The browser and controller register the same checks. Existing hosted WorkOS
activation stays unchanged. Offline tests are not a real hosted-browser
acceptance; a separate authorized staging loop must exercise revocation from
the WorkOS security widget and provider outage.

## Deletion boundary (#415 slice B)

Deletion implementation and staging acceptance follow slice A. Production
deletion remains disabled. Existing accounts, profiles, assets, accepted work
and financial rows are not removed by slice A. Owner/lawyer retention wording,
field-level de-identification and backup-restore tombstone coverage are
separate prerequisites before any production deletion enable.

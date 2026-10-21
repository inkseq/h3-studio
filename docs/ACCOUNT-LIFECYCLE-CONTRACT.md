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

Production deletion is unconditionally disabled. Staging also defaults OFF;
the operator must explicitly set `INKSEQ_ACCOUNT_DELETION_TEST_OWNERS` to the
canonical `u_<32hex>` owners of at most twenty proven disposable fixtures.
The environment must be staging at exactly `https://staging.inkseq.com`, with
no origin aliases. `qa-admin`, `qa-user`, admins, operators and legacy owner
accounts remain nondeletable, regardless of configuration. No credential or
disposable user is created by these routes. `/api/auth/me.features.account_deletion`
is true only for an eligible authenticated human; the capability check is
read-only and does not create previews.

- `POST /v1/account/deletion/preview`, exact `{}`: returns
  `{preview_id,expires_at,can_delete,blockers,removed,retained,retention_policy,production_enabled}`.
  Blockers are finite `{code,count}` objects; summaries expose counts, never
  object keys, email, provider IDs or other owners' project names.
- `POST /v1/account/deletion`, exact `{preview_id,confirmed:true}` and required
  `Idempotency-Key`: returns `202 {operation_id,state:"pending"}` after durable
  acceptance, and clears the requesting cookie. Both responses are no-store.
  The ten-minute preview binds canonical user, browser session, policy and a
  source fingerprint. A stale source/expired/foreign preview refuses with
  `409/account_deletion_preview_changed`; blockers give
  `409/account_deletion_blocked`; protected accounts give403 and disabled
  environments give `403/account_deletion_not_enabled` before database work.
  An operation UUID is not a public progress or recovery credential. After
  suspension, ordinary login and old cookies no longer authenticate; durable
  worker/audit records preserve the original result, without another deletion.

Confirmation takes existing account/reference gates, rechecks jobs, unknown
upstream work, leases, live/unknown machines, reservations, credit holds,
unresolved Checkout/events/disputes, paid-credit balances, imports/film runs
and storage receipts. It never cancels jobs, stops machines, grants/releases
money or fabricates settlement to clear a blocker. It atomically suspends the
same canonical user, advances the login cutoff, revokes local sessions, PATs,
service clients and Agent Connect grants, writes exact media tombstones and
enqueues one original operation in the existing DBOS application/queue.
Acceptance and completion each retain one audit row with the original UUID.

The existing owner-reference gate checks the private owner/operation fence
inside its existing lock. A request authenticated before confirmation cannot
write after the fence. Only server-side cleanup context bound to the exact
tenant, owner and original operation can bypass that write fence; no request
parameter/header grants this authority. Other owners and normal reads remain
unaffected. Admin role/status changes cannot reactivate a fenced account.

The existing OS operation lock serializes concurrent transport epochs for
that original operation; the existing per-object cleanup rechecks storage
binding, bytes/hash and exact key, journals an unknown delete response, and
confirms genuine absence. Shared or unknown media is retained. The provider
phase freezes the original WorkOS subject/client, journals before DELETE,
then confirms original-ID absence. It performs network IO outside database
transactions. Outages/unknown results remain pending on the same journal;
they never switch identities or declare completion. Disabling staging's
fixture allowlist stops future turns rather than broadening authority.

After a DELETE was requested, a still-present original user remains
`account_deletion_provider_delete_unconfirmed`; the worker never automatically
POSTs another DELETE, including after process reconstruction. Only confirmed
original-ID absence advances that phase.

Deletion has the additive `inkseq.account.deletion.v1` workflow and
`inkseq.account.deletion-turn.v1` step in the **same existing DBOS app/queue**.
The session workflow/step/ID remain unchanged. A rollback to an A-only binary
does not understand deletion: pinned DBOS may claim it as PENDING, then raises
unknown-function before recording a terminal outcome. It cannot use the
session handler to declare deletion complete. Pending deletion therefore
requires restoring a matching worker for that same original workflow ID;
rolling back a disposable deletion mid-flight is not a claim of completed
cleanup or automatic old-binary recovery.

Finalization rechecks late financial blockers, uses the existing purge quota
release, redacts owner authoring/Quick Create content with existing execution
redaction rules, and replaces profile fields with a suspended opaque tombstone.
The canonical user UUID, original payment/credit/budget entries and idempotency
keys remain. Signed late payment/refund handling still resolves that UUID;
a new same-email WorkOS identity receives a fresh UUID/owner.
The same confirmation transaction also retains a private provider-subject
hash fence, bound to tenant and the fixed WorkOS issuer, with original-client
hash provenance. An exchange verified before confirmation but arriving at
canonical upsert after it receives `403/account_deleted` before creating any
UUID, welcome grant or budget binding. Changing the OAuth client cannot
resurrect that same provider subject; a genuinely new subject may sign up.
Completed
account journals can prove expected missing backup objects only through their
matching original markers, tombstones, storage identities and byte hashes;
pending, ambiguous, shared or mismatched evidence remains UNKNOWN.

This is disposable staging workflow evidence, **not full production
anonymization or legal acceptance**. Production enable requires all of:
1. Owner/lawyer approval of entity-specific retention wording and paid-credit
   treatment; the seven-year financial retention text is a DRAFT proposal.
2. Field-by-field review of retained immutable payment/event/credit inputs,
   upload/write receipts, Agent grant capsules and technical audit IDs.
3. Backup/R2 version expiry and restore-tombstone enforcement that cannot
   recreate deleted profiles or re-enable revoked credentials.
4. Independent safety review and actual disposable staging phase/recovery
   acceptance, including the provider boundary, before any production flag.
   Retained/rollback auth readers must honor `deleted_subject`, alongside a
   matching B worker and backup restore markers; A-only binaries do not.

No production data deletion, hosted provider action, LIVE purchase/refund or
GPU action is authorized by the local implementation or its offline fixtures.

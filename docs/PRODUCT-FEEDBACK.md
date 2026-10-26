# Product feedback API (#635)

Thin backend slice of #580 step6 / CR67. Reuses the existing document store,
authentication and configured Sentry destination. No schema migration, analytics
vendor, worker, retry queue, new credential or infrastructure. This is a contract,
not deployment evidence. Claude owns settings UI, acceptance and promotion.

## Submit and recover

`POST /v1/feedback` needs a browser session or PAT `projects:read`, and a saved
`Idempotency-Key`. A support view cannot submit or change settings.

```json
{
  "category": "missing_capability",
  "severity": "low",
  "summary": "The tool cannot import a timeline",
  "suggestion": "Add timeline import",
  "source": "agent",
  "intent": "Import an existing edit",
  "include_context": false
}
```

Categories: tool_error, missing_capability, confusing_output, documentation,
other, model_limit. Severity: low, medium, high. Source: story_chat, agent,
telegram, system (a caller label, not verified automated origin). Actor kind is
derived from authentication separately. Summary1..280, suggestion0..500,
intent0..200. Intent is only local report metadata, not a global API-write hook.

Optional project_id/shot_id/artifact_id must resolve under the original account
and current project read grant. Shot requires project; artifact requires both and
the existing take-review accepted-artifact proof, including verified migration
aliases. Inaccessible, trashed or purging content refuses. Report general product
friction without IDs when no live reference exists; do not invent IDs.

The response is a small receipt:
`{report_id,event_id,created_at,category,severity,source,forwarding}`.
Forwarding is pending, enqueued, unknown, disabled or suppressed. Enqueued means
the local SDK transport accepted the envelope, **not** Sentry delivery/ingestion
acknowledgement. Disabled means no matching configured Sentry transport. Pending
can mean interruption before or after enqueue; unknown means an exception around
enqueue. Neither state is retried automatically. Suppressed means opt-out won
before forwarding.

`GET /v1/feedback/{report_id}` reads the original own receipt. Repeating the same
POST key/body returns its saved status and never sends again; a different body
with the same key is409. Never rotate keys to retry an unknown outcome. The
original pending report and UUID event ID are committed before forwarding.
Opt-out/deletion and the one enqueue are serialized by the existing owner gate.
If a process fails before recording the result, recovery remains pending. There
is intentionally no automatic resubmission path or claim of exactly-once remote
delivery.

`GET /v1/feedback?category=&source=&limit=50` returns bounded
`{reports:[receipt],has_more}` (limit1..100), newest updated first. Browser sees
only its own account; PAT sees only its own connection's reports and currently
granted projects. No cross-owner admin bypass or all-users dashboard. `has_more`
describes the bounded scan; it is not an exact filtered count or pagination cursor.
Sentry is the triage system; this view is for receipt recovery.

## Controls and privacy

Human-only `GET /v1/feedback/settings` returns
`{revision,enabled,include_context_allowed:false}`, default enabled:true.
`PATCH` accepts `{expected_revision,enabled}` plus a saved key. CAS409 is atomic;
same key/body replays its original settings receipt. Agents cannot opt in for an
owner. Opt-out refuses new reports; old report recovery never causes a send.
Limits are30 reports/account/hour and10/connection/hour, serialized with creation;
an idempotent replay uses no quota.429 affects only feedback, never generation.

No prompts, media, credentials, cookies, request URLs, screenshots or attachments
are collected automatically. Context inclusion stays disabled in this slice;
`include_context:true` is403 even for a human. The API rejects URLs, email
addresses, control characters and recognizable credential syntax in supplied
text. This is a conservative pattern check, **not** a general PII/prompt detector.
Callers must summarize product behavior without copying customer content or
secrets. Unknown fields are rejected, not stored as context.

Local `product_feedback` documents in owner `__feedback` hold bounded report
text, validated reference IDs, hashed actor, original fingerprint and response.
Sentry receives only the explicit product summary, closed category/severity/
source/actor tags, opaque report ID and hashed reference IDs. It receives no
suggestion/intent text, raw owner/project/shot/artifact names, user/contact object,
request context, logs or trace scope. A narrowly constructed modern `feedback`
envelope reuses the app-owned transport, because the pinned Python SDK has no
capture_feedback API. Exception sanitization is unchanged; no global SDK client
is initialized or consulted. Only the existing exact DSN and staging/production
environment are accepted. Protocol: [Sentry feedback wire format](https://develop.sentry.dev/sdk/telemetry/feedbacks/).

Existing account deletion redacts local feedback documents. Proven artifact or
project purge redacts matching local reports atomically and they do not retain
that exact owned media; foreign/other references remain guarded. Replays of a
redacted report cannot recreate it. Local purge does not claim to erase an already
forwarded Sentry product summary: Sentry retention/deletion remains in that
existing service's policy, and no media/customer context is sent automatically.
Settings/receipt storage has no new automatic TTL or public GitHub publication.

## Follow-ups, not enabled here

Automatic Story Chat `report_product_issue`, repeated-redo system signals,
global PAT intent hooks, optional typed context consent, PostHog LLM tracing and
per-reply thumbs are separate slices. This API does not add unsolicited reporting
or weaken request/body privacy. Production data, charging and Checkout are
unchanged. Tests use disposable databases and an in-memory Sentry sink only.

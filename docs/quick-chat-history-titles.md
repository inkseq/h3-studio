# Quick Chat history naming

This optional display feature uses the exact Google AI Studio model
`gemma-4-31b-it`. It does not select a video model or enable the conversational
assistant. Runtime configuration and its protected secret loader determine
whether it is enabled; the default without a generator remains disabled.
`GET /v1/quick-chat/schema` reports `history_titles.enabled`.

## Eligibility and input

Only sessions created while naming is configured are eligible. Omit `title`
when creating a session to receive the default `新的创作` label and automatic
naming. Any explicitly supplied title is manual, including `新的创作` or a
whitespace-padded version of that label. Existing sessions are not
backfilled when the feature is enabled later.

The first meaningful accepted turn or direct card/revision prompt becomes the
input. The session stores its existing source object ID; it does not copy media
or create a second prompt/history authority. The provider receives at most the
first 2,000 characters of that one description, never uploaded files, prior
turns, account identifiers or hidden project documents. The name is sanitized
as one short plain text line, at most 40 characters.

The optional provider is injected through `QuickChatService(...,
title_generator=...)` and `register_routes(..., title_generator=...)`. Its
`generate(first_text)` method returns a string, or a mapping containing `text`.
It must enforce a bounded request timeout and must not retry automatically.

## Persistence, replay and races

Authoring commands remain the existing owner-isolated business writes. The
accepted write stores a one-shot claim on the session row, and the HTTP route
schedules background naming after returning the authoring response. A model
request never runs inside a database transaction or blocks card/video
admission. Claiming `pending -> running` under the session lock limits multiple
API processes and idempotent replays to one supplier call per session.

An explicit creation or `PATCH` containing `title`, including choosing `新的创作` again,
makes the name manual and fences any pending or late supplier response. Naming
completion re-reads and locks the current session, preserving concurrent
settings, references, ownership and the current authoring `version`. It emits
`session.title_generated` or `session.title_failed` without incrementing that
version. Hidden project/shot titles and immutable generation snapshots do not
change when a history label changes.

Public session/list responses expose only:

```json
{
  "title_generation": {
    "status": "completed",
    "model_id": "gemma-4-31b-it",
    "error_code": null
  }
}
```

Statuses are `awaiting_input`, `pending`, `running`, `completed`, `failed` and
`manual`. A missing/disabled legacy feature returns `title_generation: null`.
The private claim, source object and original title are never exposed in that
projection. Clients can refresh the session/history while naming is pending;
read/list requests never initiate a model call.

## Failure and process interruption

An unavailable model, timeout, invalid response or configuration failure keeps
the default label and the accepted card/job intact. Safe public codes include
`title_unavailable`, `title_rate_limited`, `title_invalid_response`,
`title_generation_failed` and `title_call_unknown`. Raw exceptions, model
responses, prompts and credentials are not logged or surfaced as errors.
There is no automatic paid retry on replay or the next turn.

The existing SQL-only Quick Chat recovery loop also reconciles stale naming
claims after at least 180 seconds. A pending claim becomes failed with
`title_interrupted_before_call`; a running claim becomes failed with
`title_call_unknown`, because the original call may have executed. Neither
case sends another provider request, and late responses stay fenced.

## Credential configuration and deployment

The local opt-in is `SIXNINE_TITLE_USE_CENTRAL=1`; it uses the existing
`api_registry.load_api('gemini', profile='gemini--user-supplied')` adapter with
the original `https://generativelanguage.googleapis.com` endpoint. It is not
permitted for a public deployment. Default local previews remain disabled.

Production uses `SIXNINE_TITLE_CONFIG_FILE=/run/secrets/google_titles`.
The protected host loader reads `/sixnine/platform/google-titles` in AWS Secrets
Manager for production, or `/sixnine/staging/google-titles` for the closed staging
target, and hydrates a root-owned, group-10001, mode-0440 file in `/run` tmpfs;
only the app mounts it. No API key enters an environment variable, frontend
bundle, connection instruction, repository or public guide. The host installs
the dependency-free `studio_platform/google_title_config.py` beside the existing
`runtime_secrets_aws.py`; hydration needs no model SDK or HTTP dependency.
Staging never falls back to the production source. Invalid deployment targets
are rejected before reads or writes. Each target needs an explicitly installed
protected helper and its own authorized managed-secret source; deploying the app
alone does not update an already installed host helper or enable story chat.

The optional source has a disabled sentinel `{"enabled":false}`. Missing,
denied or malformed upstream configuration disables naming for the next app
start, without changing database/account secrets or blocking their hydration.
Google file replacement is atomic: a running app retains its already-bound
inode until restart. Rotation therefore takes effect at an app restart; it is
distinct from the existing database credential policy that forbids implicit
rotation. Protected host preflight validates file ownership, mode and tmpfs.

The reviewed Compose configuration gives the app outbound connectivity for the
fixed Google HTTPS endpoint; ingress stays behind Caddy, and provider/GPU
credentials, controller paths and SSH keys remain outside the app. Legacy
two-secret/no-egress Compose bundles remain accepted for rollback. This source
change participates in the conservative worker compatibility fingerprint.
An exact independently reviewed app-only compatibility receipt can allow this
optional title secret and app egress while preserving the existing controller,
accepted queue and rentals. It does not authorize other service/configuration
changes, a GPU restart, resubmission or an operating-window extension.

The request is limited to 64 output tokens, 20-second HTTP timeout (5 seconds to
connect), no retries, and `thinkingConfig.thinkingLevel=minimal`, as supported
by the [official Gemma API documentation](https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api).
Automatic naming is one request per eligible session, independent of the
selected conversational model. It does not enable the conversation assistant.

## Verification boundary

`test_platform_quick_chat_titles.py` checks owner/reader isolation, first-input
selection, background route integration, replay, multiple API claims, manual
name races, concurrent authoring versions, failure and restart recovery with an
isolated database and fake provider. These checks do not prove live Google
authorization or production release; those require exact runtime receipts.

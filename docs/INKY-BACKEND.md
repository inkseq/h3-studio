# Inky backend — implementation draft (#684)

This branch is not enabled in the application. `inky_routes.register_routes`
and `InkyWorker` exist for offline contract tests but are not mounted or started
by `create_app`. No rollout or model-quality acceptance is claimed.

## Contract

- `GET /v1/inky/suggestions?project_id=`: current account/project only,
  `projects:read`, `{items: [...]}`, at most three. Each item has `id`, `kind`,
  `shot_id`, `field`, `base_version` (shot version), `tip_key`, `lang`, and an
  optional `example`. `tip_key` selects a fixed frontend zh/en template.
- `POST /v1/inky/suggestions/{id}/ignore?project_id=`: `projects:read+write`;
  `{id,ignored:true}`. Idempotent, stores a preference for the target/kind, not a
  shot edit. Later revisions remain ignored. Stale uncached IDs refuse409.
- `GET /v1/inky/settings?project_id=`: `{revision,muted}`, initially0/false.
- `PATCH` the same settings URL: `{expected_revision,muted}`; CAS409 on stale
  revision. Project mute suppresses both views and new evaluation claims.
- Reads are side-effect-free and `Cache-Control:no-store`. No provider calls,
  new cache entries, workflow recovery or writes on a view.

Text kinds are `vague_action`, `needs_keyframe`, `emotion_not_shown`,
`multiple_camera_moves`, `action_too_long`. Keyframe navigation uses `frames`;
the other text kinds use `action`. `upload_card`/`refs` is reserved but its asset
integration is not implemented. Do not show invented upload suggestions.

Source identity binds the shot, its version, minimal resolved scene/cast/place/
props, incoming links and rubric revision. It changes when shared scene/card
context changes even without a shot version bump. Explicit empty shot cast
means no people. Old results are hidden on read and rejected on publication.
Claims contain IDs/status only; the current per-shot cache may contain the
generated example. Project-purge redaction is implemented as a function and
tested, but still needs wiring into the existing lifecycle transaction.

## Models and credentials

Jev: `jev-1.14.0`, TypeSafe `/systemone`, explicit central profile
`typesafe--avatar-user-20261008`, exact origin `https://api.typesafe.ai/v1`.
Text-only requests omit `assets`. Use probabilities over0..3, never the scalar
score. Malformed distributions mean unavailable advice, not a clean verdict.

Terra: `openai/gpt-6.1-terra`, low reasoning, JSON object, explicit code-derived
`lang` and shot `seconds`, only the shot's resolved cast. Profile is
`openrouter--inkseq-chat`; only this selected profile may default its missing
origin to `https://openrouter.ai/api/v1`. Never use the obsolete secret-drop,
dead rig-root profile or an automatic profile fallback. Central credentials
are loaded lazily through the existing loader. No loader/import network calls.
AWS provisioning is not implemented; a local registry loader is not an AWS
credential transport. Do not copy its vault or put secrets in the repository.

## Budget and recovery

`InkyBudget` reuses existing documents and a global PostgreSQL transaction lock
(SQLite BEGIN IMMEDIATE in tests). Hard ceilings are30 Terra calls/account/UTC
day and USD20/global/UTC month. This also bounds each story to at most30/day.
Successful, failed and unknown calls count against daily admission. Unknown
costs retain their full reservation. Replay of the same source/kind never
authorizes another call. A known cost settles once; conflicting cost refuses.
Unexpected over-reservation cost is recorded honestly and freezes further
monthly calls. Over-cap suggestions retain templates and log
`inky_example_budget`, without creator text.

The adapter currently proposes a conservative USD0.25 reservation per request:
16KiB text message bytes,4096 framing-token allowance,1024 total output tokens,
rates at most USD5/M input and USD30/M output. It pins the OpenAI provider,
forbids fallback, requires supported parameters, and sets per-request/image
price ceilings to0. No tools, plugins, images or automatic retries. Official
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
documents price filters, while [max tokens](https://openrouter.ai/docs/api_reference/parameters)
covers reasoning plus visible output on most providers. **The exact Terra
provider's token accounting and envelope still need qualification before live
activation**; the observed ~$0.0006 average is not a worst-case bound. This draft
does not claim a server-side ledger can control a provider that ignores its
request limits. On response errors/timeouts, retain the original reservation.

## Remaining integration

1. Qualified server credential transport and activation, with the reviewed
   request-cost envelope; no new key or credential-file import.
2. Post-commit save and explicit preflight background hooks, no work on GET or
   failed mutation. Existing accepted job inputs must remain unchanged.
3. Mount the routes, connect project purge/mute and account lifecycle fences,
   publish discovery/Agent docs and integration regressions.
4. Upload/card candidate provenance, bounded assets, evaluated decoy fixtures.
5. One Claude Tier2 review of the final PR, exact-head CI, then staging
   qualification. No review request or activation for this draft milestone.

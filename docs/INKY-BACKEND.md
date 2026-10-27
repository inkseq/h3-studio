# Inky backend — implementation draft (#684)

This branch mounts scoped readers and explicit preference writes in `create_app`.
The evaluator is off by default. An explicit protected `INKSEQ_INKY_CONFIG_FILE`
can construct it without a network call; no deployment configuration supplies
that file yet. No rollout or model-quality acceptance is claimed.

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
generated example. Project-purge redaction runs inside the existing proven purge
transaction; account deletion already redacts all owner-scoped documents.

When a worker is explicitly configured, a pure ASGI boundary collects committed
Guided saves/actions and successful explicit generation preflights. It runs
optional advice after the response and any original background task, using the
existing Starlette thread pool. Reads, rejected writes and idempotent action
replays cannot trigger it. Durable source claims prevent duplicate evaluations.
No daemon, automatic recovery or retry is introduced. A crash can omit advice;
it cannot authorize replay of an uncertain provider call. Optional worker errors
do not change a committed save and log only a fixed event name.

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
The runtime file contract validates the exact profiles/origins, refuses duplicate
JSON keys, symlinks, multiple hardlinks and unprotected Linux ownership/modes.
Invalid/missing configuration disables the evaluator with a static diagnostic;
it never falls back to the local registry. Budget ceilings are not configurable
through this file. Source construction tests use fake credentials only.

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
2. Qualify the post-response hooks under the actual server lifecycle and publish
   the final Agent skill contract. Existing accepted job inputs stay unchanged.
3. Preserve account/project erasure and discovery integration regressions.
4. Upload/card candidate provenance, bounded assets, evaluated decoy fixtures.
5. One Claude Tier2 review of the final PR, exact-head CI, then staging
   qualification. No review request or activation for this draft milestone.

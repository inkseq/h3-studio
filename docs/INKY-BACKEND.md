# Inky backend — implementation draft (#684)

This branch mounts scoped readers and explicit preference writes in `create_app`.
The evaluator is off by default. An explicit protected `INKSEQ_INKY_CONFIG_FILE`
can construct it without a network call; no deployment configuration supplies
that file yet. Compose and the protected host allowlist now carry an app-only
opt-in; absent opt-in preserves the existing recipe-file alias without reading
it. No rollout or model-quality acceptance is claimed.

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
The 2026-10-27 read-only managed-secret metadata census found no existing
Inky/TypeSafe/OpenRouter server secret in either environment. Existing database,
auth, payment and title secrets must not be repurposed. The app-only mount is
prepared, but an approved durable hydration source remains a deployment
dependency; this PR does not create secret containers, credentials or paid
infrastructure. A helper install follows the existing zero-work host gate.
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

Public metadata read-back on 2026-10-27 (no authentication or inference):
the [exact model endpoints](https://openrouter.ai/api/v1/models/openai/gpt-6.1-terra/endpoints)
reported OpenAI regular/flex/fast routes with `max_tokens`, `reasoning` and
`response_format` support. Regular rates were USD1/M input and USD5/M output;
the highest listed long-context variant was USD4/M and USD15/M. Both remain
below the request price filters. This confirms route/parameter availability,
not a live billing test. Under the proposed byte/framing bound, the filtered
token-price envelope is `(16384+4096)*5/1e6 + 1024*30/1e6 = USD0.13312`, below
the USD0.25 reservation. Exact token accounting and the bounded live response
still require staging qualification before general activation. Do not infer a
hard cap from the historical average example cost.

## Remaining integration

1. Qualified durable server credential hydration and activation, with the
   reviewed request-cost envelope; no new key or obsolete-file import.
2. Upload/card candidate provenance, bounded assets, evaluated decoy fixtures.
   #684 comment6436424621 asks how an unattached upload maps to the required
   `shot_id`; no synthetic shot assignment is permitted.
3. One Claude Tier2 review of the final PR, exact-head CI, then staging
   qualification. No review request or activation for this draft milestone.
   The post-response lifecycle and Agent contract are tested/documented locally;
   account/project erasure and discovery regressions stay in PostgreSQL CI.

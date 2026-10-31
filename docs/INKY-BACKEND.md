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
the other text kinds use `action`. `upload_card` uses `cast` and `card_name`.
Only saved shot-attached uploads (candidate/frame/reference/child) are eligible;
unattached library media and generated results are excluded. One image per shot
is compared with all project character/location/prop cards (at most16, otherwise
skip); argmax expected score must be >=2 and untied. It is a suggestion only:
no card assignment, shot change or Terra example. Missing preview skips advice.
Owned ready receipts are rechecked, and bounded immutable preview bytes are
hash-verified before CPU conversion to a metadata-free JPEG (long edge640,
pad short edges to280). The original remains unchanged. Shared card edits stale
the source, and deleted/trashed assets hide cached upload advice.
The protocol follows [TypeSafe images](https://docs.typesafe.ai/images) and its
[official SDK](https://github.com/typesafe-ai/typesafe-sdk-js/blob/main/src/assets.ts);
only image calls send the experimental image header. Text omits assets.

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
The explicit staging importer and host hydration adapter now transport the two
existing keys through Standard-tier SSM SecureString with AWS-managed `aws/ssm`.
No operator has executed them; source preparation is not credential adoption.
Do not copy the vault or put secrets in the repository.
The 2026-10-27 read-only managed-secret metadata census found no existing
Inky/TypeSafe/OpenRouter server secret in either environment. Existing database,
auth, payment and title secrets must not be repurposed. The app-only mount is
prepared. Staging adoption requires the single final Tier2 review and owner
credential authority; production adoption is explicitly refused by the helper.
A helper install follows the existing zero-work host gate.
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

1. Qualify durable server credential hydration and activation, with the
   reviewed request-cost envelope; no new key or obsolete-file import.
2. Evaluate #687 labelled text/image/decoy fixtures on the bounded provider
   adapters after source review. Offline regressions do not prove model quality.
3. One Claude Tier2 review of the final PR, exact-head CI, then staging
   qualification. No review request or activation for this draft milestone.
   The post-response lifecycle and Agent contract are tested/documented locally;
   account/project erasure and discovery regressions stay in PostgreSQL CI.

## Staging adoption (prepared, not executed)

The trusted host allowlist and companion checker changes are deferred.
Inky mount validation stays in `tools/inky_deploy_validation.py` and offline
rollout tests until the qualified host installation. The disabled application and
Compose preparation can deploy with the existing installed maintenance helper;
`INKSEQ_INKY_CONFIG_FILE` is deliberately refused in protected host settings,
including an empty value. No credential path may be added yet. Activation needs
a separately reviewed restoration of the fixed `/run/sixnine-secrets/inky` path
validation and its qualified helper installation in the zero-work window below.
This does not relax installed-helper hash checks or cancel queued acceptance jobs.

1. Verify the final reviewed SHA and green CI, original SSM parameter outcomes,
   owner credential authority, and a fresh zero-nonterminal-job staging window.
   Preserve the original protected host tuple and helper hashes under the
   existing operation qualification/release lock before installing any bytes.
2. The credential operator needs `ssm:GetParameters` and `ssm:PutParameter` on
   exactly `arn:aws:ssm:ap-southeast-1:829135631045:parameter/sixnine/staging/inky/typesafe`
   and the same ARN ending in `openrouter`. The staging instance role needs
   `ssm:GetParameters` only on those two ARNs; it must not receive write access.
   The helper never creates IAM grants, provider keys, KMS keys or production parameters.
3. From the reviewed checkout on the existing central-registry host, run
   `python tools/import_inky_credentials.py --apply-staging` once. No credential
   argument or input file exists. Preserve its names/types/versions receipt.
   A lost response is unknown: inspect original parameters, never overwrite.
4. Through the qualified root helper installation lane, install the reviewed
   maintenance helper with the explicit Inky allowlist and fixed-path validation,
   the companion migration checker with Inky mount validation integrated,
   `runtime_secrets_aws.py`, sibling `inky_credentials.py`, and dependency-free
   `studio_platform/inky_config.py` alongside the existing host package. Preserve
   their previous bytes and metadata for rollback; no hot patch of app code.
   Add `INKSEQ_INKY_CONFIG_FILE=/run/sixnine-secrets/inky` only to the protected
   staging `migration/paths.json` environment under its existing validation/CAS.
   The shared validator lives in the already root-installed `deployment_target.py`;
   adopt it together with the maintenance/checker pair, never from incoming
   artifact code. It rejects nonempty production configuration, alternate paths,
   another service's mount, and another secret aliased onto the Inky target.
   The exact retained staging current/previous helper pairs from SSM
   `3dd45659-accb-4be9-afab-82600e38991a` are preserved for rollback. Before
   merging this helper change, finish its single Tier2 review and fresh CAS
   qualification/install; otherwise normal main deployment will correctly refuse
   the changed pair. A changed current/previous tuple requires fresh reconciliation.
   Do not replay the historical #705/#708 refusal/recovery or the completed SSM
   imports. CPU adoption still waits for zero nonterminal jobs and leases.
5. Existing secret hydration reads the protected deployment target, verifies the
   AWS account, fetches those exact parameters, validates both complete configs,
   and writes through the existing inode-preserving secret writer. The file is
   root:10001 `0440` (not `0600`: the app runs non-root), parent root:root `0700`.
   Compose mounts this file read-only into the app alone as `/run/secrets/inky`.
   Differing existing bytes refuse; there is no automatic rotation or fallback.
6. Adopt the accepted staging tuple, read back names/versions/type, file
   ownership/mode and app-only mount without printing the contents. Qualify the
   request-price envelope and #687 text/image/decoy cases with bounded calls;
   inspect actual usage and persistent holds. Over caps returns template only.
7. Rollback removes the optional path from the protected rollout environment
   and restores the original app tuple through existing gates. Keep original
   SSM parameters and financial/evaluation receipts; do not rotate/delete/replay.

### Existing staging role: parameter-read permission

The actual EC2-role read in SSM `cd729422-3535-42a0-ba71-4258b707f14b`
returned `AccessDeniedException`; importing the two parameters as an operator
did not grant the host access. The existing role is
`arn:aws:iam::829135631045:role/inkseq/staging/sixnine-staging-ec2`.
`deploy/platform/inky-staging-read-policy.json` is the complete proposed inline
policy `InkseqStagingInky684Read`. It permits only `ssm:GetParameters` for the
two exact existing staging ARNs in ap-southeast-1. No path/list/history/write,
production resource, wildcard, new credential, trust-policy or other-role change.
Existing parameters use `alias/aws/ssm`; this slice adds no KMS grant.
See the AWS [parameter-policy examples](https://docs.aws.amazon.com/systems-manager/latest/userguide/security_iam_id-based-policy-examples.html)
and [default-key note](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-policy-template-list.html).

After one Tier2 PR review and exact-head green CI, adopt this policy only on
that existing role. First re-read the role ARN, tags, trust policy, boundary,
inline policies and attachments; refuse unexpected ownership or scope changes.
Record the canonical policy digest and original absence of that policy name.
Use one `PutRolePolicy` with the exact reviewed document, then read back the
original role/name with `GetRolePolicy`. Unknown results require read-only
reconciliation; never overwrite a differing policy or issue a second put.
An identical existing policy is an observation, not a new apply.

Validate the actual host read of both parameters with decryption, emitting only
names/types/versions. Denial or malformed parameters blocks hydration; do not
broaden the grant. This read does not turn on the evaluator or call providers.
Hydration/application adoption still use the zero-work gates above. Retain the
prior helper, SSM and operation receipts. Rollback disables the Inky path using
the existing host gate and removes only this exact inline policy after its
original digest is verified; it must not delete either parameter or alter any
other role policy. Production permission remains outside this approval.

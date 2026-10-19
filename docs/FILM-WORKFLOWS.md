# Frozen film runs (workflow revision inkseq-film-v1)

This is an orchestration receipt in the existing project document store. The
original plans, batches, jobs, attempts, reservations and artifacts remain the
only authorities for executable work and costs. Importing film modules starts
no executor, FFmpeg, database migration or provider request.

## Public API

All routes use the existing authenticated owner/project grants and content
lifecycle gates. Browser and Agent use identical routes.
Authenticated capabilities expose `story_film={enabled,workflow_version}`;
disabled is not permission for an alternative submission route. The public
[Agent API reference](../skills/inkseq/reference/api.md#one-click-film) documents
exact request bodies, original write keys, artifact URLs and helper commands;
[film recovery](../skills/inkseq/reference/recovery.md#one-click-film-recovery)
covers uncertain confirmation and explicit same-run resume. The Skill does not
grant spending authority or create a separate whole-film budget.

* `POST /v1/projects/{project_id}/films/preflights`, `Idempotency-Key`: body
  `{expected_version, resolution, aspect?, burn_subtitles?}`. Output contains
  the run `id`, exact `project_version`/`snapshot_hash`, original generation
  plan IDs/seeds, chapter IDs, reuse counts and estimated additional cost.
* `POST /v1/projects/{project_id}/films`, `Idempotency-Key`: body
  `{preflight_id, confirm:true, spending_settings_version?}`. This does not
  replace the existing spending-confirmation API/grants. Header acceptance and
  deterministic DBOS enqueue share one original SQL transaction.
* `GET /v1/projects/{project_id}/films/{run_id}` returns phase, safe original
  plan/job/artifact links, counts, warnings and finite error code, without object keys,
  frozen prompts, credential material or grant identity.
* `POST .../{run_id}/cancel` requests cancellation of existing original jobs and
  stops future admission. A run is cancelled only when its original jobs are
  terminal; unknown native outcomes/reservations are retained.
* `POST .../{run_id}/resume` signals the same original workflow/run. It never
  assigns a new job/attempt/tag or re-generates a failed original task. Same-actor
  explicit browser resume may renew its session identity; it cannot transfer an
  Agent's work to an unrestricted browser actor.

Current output bounds deliberately reuse the CPU contract: 480P/720P,24fps,
maximum600s total,50chapters,50shots per chapter, at most100 missing shots in
one existing generation batch,32 audio tracks in final export, and the original
per-source/total-input/work-cache quotas. Unsupported recipes fail rather than
trimming inputs. Subtitle burn requires existing explicitly confirmed chapter
caption tracks; no AI image/voice/caption generation or tier mapping is implied.

## Frozen source and grant authority

The header keeps an immutable copy of the requested project revision and exact
selected take/range. Missing/outdated shots reuse storyboard preflight and
accepted immutable model/material/seed plans. New results are applied to a
private copy for render compilation; the editable Story is never overwritten.
Selected generated takes are compared against their immutable effective
authoring recipe, not the editable shot version or selected-asset field. Normal
adoption does not purchase a replacement. An authorized historical selection
without recipe evidence is reused with `film_selected_recipe_unknown`; changing
known generation parameters or materials makes a take outdated. Public counts
include `generated_completed`, `chapters_completed` and `export_completed`, and
successful chapter artifact IDs are retained while later chapters/export wait.
An unadmitted original planned/blocked batch is rechecked using its same original
key/job IDs; failed originals are not regenerated. Unchanged waiting turns do not
rewrite the header or toggle a later export phase back to chapter preparation.

The existing admission gates call
`validate_film_source(connection, repo, scope, compiled)` for the server-created
`film_source_ref={run_id,snapshot_hash}`. It requires the exact tenant, owner,
project, original actor, accepted/noncancelled frozen header and registered
compiled request hash. Other project, trash, asset, expiry, confirmation and
budget checks remain active. User-supplied references cannot manufacture plans.

Controller registration supplies `principal_for(tenant,owner,project_id,run_id)`.
`FilmRuns.execution_context` returns only its private original grant descriptor
for revalidation through existing Auth tables. It does not construct a Principal.
Browser sessions must still be valid with current account/password/mode; keys
and service clients must retain current scope, owner, expiry, enabled/revocation
and pause checks. No owner-ID-to-browser fallback is allowed. The session hash
is a non-bearer identity, never a token and never part of workflow inputs/public
responses. Revocation blocks future admissions; independently accepted jobs
retain their original native reconciliation/collection and accounting.
The controller records a finite `authorization_required` metadata state when
original grant revalidation fails; it keeps the same durable workflow waiting.
That path never constructs a Principal or admits/cancels jobs. An already
requested cancellation may finish by reading only its own original terminal job
statuses. Content lifecycle gates and header CAS remain active. Explicit
same-actor resume updates the existing grant reference and original run only.
Known finite business refusals (including render inputs, spending confirmation
and budget limits) show `blocked` with their safe original error code. The
workflow durably waits; it does not retry admission until explicit same-run
resume. Unexpected exceptions remain errors, rather than being relabelled as
an authorization or budget refusal. The creator can open an Agent-created run
with the ordinary Story URL's `project=<project_id>&film=<run_id>` query, using
the same owner-fenced GET; the URL grants no access.

## CPU checkpoint recovery

`CPURenderBackend.resume(job,tag,task_id,heartbeat)` returns the same original
task ID and is CPU-only. The original WorkerRunner must own the fresh domain
lease/heartbeat and retain task/tag/payload/source identity. Old receipts without
checkpoint version1 remain unknown and require explicit reconciliation.

An inherited POSIX flock FD fences FFmpeg children even after controller death.
Resume cannot run while that original child holds the fence. Completed outputs
have durable argument-identity/size/full-SHA checkpoints and are reused only
after verification; changed inputs/checkpoints refuse recovery. Successful
chapter job/artifact IDs are retained. Export render version4 copies their
validated H264/24fps/dimensions/frame-count bitstreams at concat; chapters are
not re-encoded. Separate original audio is mixed using the existing sample-grid
contract. The original ArtifactWriter handles uncertain R2 PUTs.
On the DBOS path a shared CPU-operation OFD lock is also acquired before any
original job claim and passed to FFmpeg. A surviving child therefore fences all
CPU configurations after a controller process crash, not just its own attempt.

`register_film_workflows(DBOS,service,principal_for)` registers one stable named
workflow and no-automatic-retry domain step. Only tenant/owner/project/run IDs
are inputs; waiting uses `recv_async(topic='film_changed',timeout_seconds=10)`.
DBOS replay is not paid GPU exactly-once proof. Matching-version deploy/resume,
real FL/REF film/R2/browser acceptance and the scoped workflow-state backup
contract remain staging acceptance gates; no production activation is inferred.

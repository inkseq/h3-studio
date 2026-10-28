# Original staging refusal reconciliation (#707)

This is a one-operation staging recovery, not a deployment retry or a generic
release-lock reset. It requires the single Claude Tier2 PR review and exact-head
green CI before execution. Production is rejected by the sealed instance/target
binding. No CPU service, database row, queue, GPU or billing operation is changed.

## Evidence and prerequisites

- Original command `fcbe99e1-fbd4-477a-8b0a-7d86f6b9df29` on staging
  `i-07805df7bd6984bd9` ended Failed/1 at `2026-10-27T13:02:11.486Z` with only
  `migration_root_helpers_need_reviewed_update`. Re-read that original SSM
  invocation; never invoke it again. The proof file must contain exactly the
  seven fields in the reviewed tool's `PROOF` constant, taken from that read.
- This error occurs in `maintenance.apply_locked` before loading application
  images, writing pending state or stopping any service. The refused tuple is
  `fd0f7aba83b72a38deb1ac2344445cc23bdb1bda`; its intent and candidate hashes are
  pinned in the tool. No other failure is eligible.
- Independent read-only SSM `41e549a2-ba0f-442e-b6c6-ba9b45fba504` confirms the
  original in-progress intent and current app/frontend
  `f8d6fb8c4176c6283b2d400b050c68dea8e543e9`, with no platform pending state.
  Fresh planning additionally requires matching migration state and healthy
  current containers; any different current tuple requires renewed analysis.
- #700 run40519288300 / SSM `eb3da49e-6f1e-470f-aba4-4b33dc5fd292` subsequently
  refused `another_environment_release_requires_reconciliation`. This is not
  successful staging adoption. #705 independently restores compatible source
  helper bytes for the next new candidate; this recovery does not install them.

## Prepare, review the plan, then apply once

Use the existing protected staging operation channel to place the exact reviewed
tool and the original SSM proof under a root-owned, non-writable-by-others
directory in `/srv/sixnine`. No secret is part of either file. Verify the tool
SHA against the reviewed commit before execution. Do not run an incoming release
bundle's copy. All paths and their parents must be protected; `/tmp` is refused.

```sh
python3 /srv/sixnine/reviewed-707/reconcile_staging_intent.py \
  --proof /srv/sixnine/reviewed-707/original-ssm-failure.json
```

The default is read-only. Retain its complete plan and `plan_sha256`. Review the
sealed staging identity, exact original failure, current/active/frontend tuple,
helper pair, protected marker hashes, existing retention pins and container
identities. No accepted job needs to be cancelled to make this metadata-only
reconciliation possible. Keep the three #659 reproduction jobs unchanged.

```sh
python3 /srv/sixnine/reviewed-707/reconcile_staging_intent.py \
  --proof /srv/sixnine/reviewed-707/original-ssm-failure.json \
  --apply-plan-sha256 THE_REVIEWED_FRESH_PLAN_SHA256
```

Under the existing nonblocking release lock, apply rechecks the entire plan.
It creates a new operation directory exclusively, saves the plan/proof/original
pins, atomically adds the failed commit to explicit retention pins, rechecks
markers and containers, and moves the original intent unchanged into
`/srv/sixnine/refused-intent-707-fd0f7ab/original-intent.json`. It records
`completed.json` as `original_refusal_archived_not_deployed`, never as a successful
release. The failed candidate remains retained and the old receipt is immutable.

## Read-back and interruption

Independently verify the completion receipt, archived intent's original hash,
the failed commit's explicit pin, absence of the active intent, and unchanged
app/frontend/migration/paths markers and containers. Post the original apply
command ID and these hashes on #707/#684. The next normal qualified candidate may
then acquire its own intent; do not replay #685 or #700's refused deployment.

An existing operation directory always refuses another apply, even if incomplete.
If the response is unknown, inspect that original operation and SSM invocation.
A partial failure after pinning keeps the original intent; a failure after the
move keeps the archived intent plus the explicit pin. Neither permits automatic
retry, cleanup or invented completion. Preserve any temporary pin file too.

Rollback is a separately reconciled metadata operation under the release lock:
only before any newer candidate acquires an intent, with the original runtime
tuple unchanged and all recovery receipt hashes verified. Restore the archived
original intent and original pins; never overwrite another candidate's intent.
No runtime rollback, deletion or retention sweep is part of this tool.

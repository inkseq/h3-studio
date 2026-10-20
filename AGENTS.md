# Inkseq / H3 session entry

Working agreement: [#328](https://github.com/inkseq/h3-studio/issues/328) governs lanes, risk tiers and delivery; it supersedes conflicting older pacing rules.
Read this entry, the assigned issue's latest owner comments/claims and affected contracts only. Use current origin/main; never reset preserved checkouts.

## Ownership and coordination
- Backend/API/schema/infra/GPU/money/Agent docs: Codex, `inkseq/h3-studio`.
- UI: Claude, canonical `inkseq/inkseq-design/studio-app` AND its `h3-studio/yingxu` sync. Codex does not edit UI; API-client types only when requested by the frontend lane.
- Shared work starts from an issue API contract (routes, JSON, errors). Post contract changes there. Board: github.com/orgs/inkseq/projects/1; routing: `workflow/project.json`.
- Claim bounded scope, integration owner, branch/worktree and base before editing. Preserve parallel changes. Per lane: one issue in progress plus one in review. GitHub only; no second task database.
- Latest owner decisions win; record them in `workflow/owner-changes.md` and give the owner a one-line relay for Codex. Re-read that file on origin/main before any production write or spend.
- Labels replace, never stack: `needs-claude-review` (Tier2 options) or `needs-claude-acceptance` (staged slice); Claude's 2h pass comments, sets `accepted`/`changes-requested` and removes the request label. Read new Claude/owner comments before each task. End every turn with `Waiting on: <who/what, #issue>`.

## Risk and delivery
| Tier | Changes | Gate and write-up |
|---|---|---|
| 0 | UI/copy/layout/docs | CI, automatic staging, self-check; PR description only, no receipts. |
| 1 | APIs/additive schema/non-money features | Affected tests, staging self-check; one short issue comment with changes and staging SHA. |
| 2 | Money/GPU/queue/DBOS/auth/isolation/deletion | Safety tests, non-author safety review, relevant staging acceptance checklist and Claude sign-off. Evidence links. |

Classify behavior, not filenames. Run affected checks once per finished slice, not per edit. Merge small PRs continuously; unfinished features stay flagged. CI auto-deploys staging. Production promotes the same accepted artifact bytes, without rebuild, on Tuesday/Friday. Tier2 needs Claude sign-off. Issues close on promotion, not merge. Continue the next independent staged milestone without approval waits.

Current order (CR42/43): promote staging batch -> #353 prod backfill -> GPU demand start <=2 (CR43; staging cold-start soak first) -> go-live checklist issue -> frontend repo merge into `yingxu` -> #347 editor. No new epics before M1.

## Safety
- Reuse existing owners/assets/jobs/attempts/ledger/fences. Preserve immutable accepted inputs, configuration identity, original jobs/media and cumulative accounting. Unknown outcomes require reconciliation, never duplicate generation/rental or invented settlement.
- Test GPUs: USD20 per change/acceptance task, counting its confirmed spend plus running committed price. Other tasks' unresolved projections do not consume it. Preserve editable admin limits, original accounting and task tags. Every rental has max_price, idle stop (normally10min), maximum runtime (normally2h); stop near the limit and after testing. Staging only; never operate owner production machines/jobs.
- Pre-authorized on staging (owner 2026-10-20), no asking: QA accounts and their test connections/keys/grants (revoke after), Stripe TEST mode, test GPUs within USD20/task, staging config/backfills. Ask, batched once at task start, only for: production, live money, real users' data, new credentials/provider accounts, budget overrun, permanent deletion. Live payments/legal stay with owner. After2h without direction, post five lines of evidence/options and choose another bounded approach.
- Use central AI Registry loaders and exact profiles; read its instructions before credentials/cleanup. Never copy secrets into project env files, logs or GitHub.
- Browser: Chrome via cua_repl; staging acceptance uses qa-admin/qa-user. No repeated login, DevTools MCP or autoConnect.
- Tier2: first compare fix-only/small/larger options in the issue; Claude reviews; disagreements go to the owner. New queue/scheduler/storage/provider/table family needs owner approval; prefer delete/reuse.
- Chinese owner handoffs: Tier0 1-2 lines; Tier1/2 ~10 lines: user gain, changed flow steps and failure effect, verified code/test/staging/prod, 1-3 "Owner 技术理解" points. Exact SHAs, links. GitHub: English. Release claims with Session: Unclaimed. [WORKFLOW.md](WORKFLOW.md).

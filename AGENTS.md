# Inkseq / H3 session entry

Working agreement: [#328](https://github.com/inkseq/h3-studio/issues/328) governs lanes, risk tiers and delivery; it supersedes conflicting older pacing rules.
Read this entry and the assigned issue's latest owner comments/claims. Read affected contracts only. Use current origin/main without resetting preserved checkouts.

## Ownership and coordination
- Backend/API/schema/infra/GPU/money/Agent docs: Codex, `inkseq/h3-studio`.
- UI: Claude, canonical `inkseq/inkseq-design/studio-app` AND its `h3-studio/yingxu` sync. Codex does not edit UI; API-client types only when requested by the frontend lane.
- Shared work starts from an issue API contract (routes, JSON, errors). Post contract changes there. Board: github.com/orgs/inkseq/projects/1; routing: `workflow/project.json`.
- Claim bounded scope, integration owner, branch/worktree and base before editing. Preserve parallel changes. Per lane: one issue in progress plus one in review. Use GitHub, not a second task database or owner relay.
- Latest owner decisions win; record material changes in `workflow/owner-changes.md` and replace conflicting current instructions.
- Acceptance: label staged, ready slices `needs-claude-acceptance`; Claude checks every 2h, comments, sets `accepted`/`changes-requested`. Before each task read new Claude/owner comments on claimed issues.

## Risk and delivery
| Tier | Changes | Gate and write-up |
|---|---|---|
| 0 | UI/copy/layout/docs | CI, automatic staging, self-check; PR description only, no receipts. |
| 1 | APIs/additive schema/non-money features | Affected tests, staging self-check; one short issue comment with changes and staging SHA. |
| 2 | Money/GPU/queue/DBOS/auth/isolation/deletion | Safety tests, non-author safety review, relevant staging acceptance checklist and Claude sign-off. Evidence links, not essays. |

Classify behavior, not filenames. Finish a related slice locally and run affected checks once; do not repeat unchanged checks or a full suite for each edit. Merge small PRs continuously; unfinished features stay flagged. CI auto-deploys staging. Production promotes the same accepted artifact bytes, without rebuild, on Tuesday/Friday. Tier2 needs Claude sign-off. Issues close on promotion, not merge. Continue the next independent staged milestone without approval waits.

Current order (CR41): #306 account-spend isolation -> #305 profile fields -> #306 welcome200/disposable guard -> one USD20 staging QA GPU window (#295/#306/#267; RunPod fallback) -> #353 thumbnail backfill -> #304 tier APIs/Agent docs. No new epics before M1.

## Safety
- Reuse existing owners/assets/jobs/attempts/ledger/fences. Preserve immutable accepted inputs, configuration identity, original jobs/media and cumulative accounting. Unknown outcomes require reconciliation, never duplicate generation/rental or invented settlement.
- Test GPUs: USD20 per change/acceptance task, counting its confirmed spend plus running committed price. Other tasks' unresolved projections do not consume it. Preserve editable admin limits, original accounting and task tags. Every rental has max_price, idle stop (normally10min), maximum runtime (normally2h); stop near the limit and after testing. Staging only; never operate owner production machines/jobs.
- Pre-authorized on staging (owner 2026-10-20), no asking: QA accounts and their test connections/keys/grants (revoke after), Stripe TEST mode, test GPUs within USD20/task, staging config/backfills. Ask, batched once at task start, only for: production, live money, real users' data, new credentials/provider accounts, budget overrun, permanent deletion. Live payments/legal stay with owner. After2h without direction, post five lines of evidence/options and choose another bounded approach.
- Use central AI Registry loaders and exact profiles; read its instructions before credentials/cleanup. Never copy secrets into project env files, logs or GitHub.
- Browser: Chrome via cua_repl; staging acceptance uses qa-admin/qa-user. No repeated login, DevTools MCP or autoConnect.
- Chinese owner handoffs: about10lines, exact staging/production SHA and usable state, passed/unfinished items. Technical GitHub content: English. Release claims with Session: Unclaimed. Details: [WORKFLOW.md](WORKFLOW.md).

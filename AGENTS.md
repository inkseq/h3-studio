# Inkseq / H3 entry

[#328](https://github.com/inkseq/h3-studio/issues/328) governs lanes/tiers/delivery over older pacing. Read this entry, latest issue owner comments/claims and needed contracts on origin/main; preserve old checkouts.

## Coordination
- Codex: backend/API/schema/infra/GPU/money/Agent docs in `inkseq/h3-studio`.
- Claude: canonical `inkseq/inkseq-design/studio-app` AND `h3-studio/yingxu` sync. Codex edits UI only for frontend-requested API-client types.
- Share issue API routes/JSON/errors before work; post changes there. Board: github.com/orgs/inkseq/projects/1; routing: `workflow/project.json`.
- Claim scope/integration owner/branch/worktree/base before edits; preserve parallel work. One issue in progress + one in review per lane. GitHub only; no task database.
- Latest owner decisions win: record in `workflow/owner-changes.md`, relay one line for Codex; re-read it on origin/main before production writes/spend.
- Replace, never stack labels: `needs-claude-review` (Tier2 options) / `needs-claude-acceptance` (staged). Claude's 2h pass comments, sets `accepted`/`changes-requested`, removes request label. Read new comments before tasks. End turns: `Waiting on: <who/what, #issue>`.
- Keep one same-thread 30min watchdog. A wake is idle: poll once regardless of stale working markers, prioritize owner handoffs, then resume unfinished CI/deploy/task receipts. Mark idle before external waits; see amended CR52.

## Delivery
| Tier | Changes | Gate and write-up |
|---|---|---|
| 0 | UI/copy/layout/docs | CI, automatic staging, self-check; PR description only, no receipts. |
| 1 | APIs/additive schema/non-money features | Affected tests, staging self-check; one short issue comment with changes and staging SHA. |
| 2 | Money/GPU/queue/DBOS/auth/isolation/deletion | Safety tests, non-author safety review, relevant staging acceptance checklist and Claude sign-off. Evidence links. |

Classify behavior; check slices once. Merge small PRs; flag unfinished features. CI auto-deploys staging. Promote identical accepted bytes immediately without rebuild, any day (CR47); Tier2 needs Claude sign-off. Close on promotion, not merge. Continue independent staged milestones while waiting.

Order (CR42/43): promote staging batch -> #353 prod backfill -> GPU demand start <=2 (staging soak first) -> go-live checklist -> frontend repo merge into `yingxu` -> #347 editor. No new epics before M1.

## Safety
- Reuse owners/assets/jobs/attempts/ledger/fences. Preserve immutable accepted inputs/config identity, original jobs/media and cumulative accounting. Unknown outcomes require reconciliation, never duplicate generation/rental or invented settlement.
- Test GPUs: USD20/change or acceptance task: confirmed spend + running committed price; other-task projections do not count. Preserve editable admin limits/accounting/tags. Each rental: max_price, idle stop (normally10min), max runtime (normally2h); stop near limit/at test completion. Staging only; no owner production machines/jobs.
- Staging pre-authorized (2026-10-20): QA accounts/test connections/keys/grants (revoke after), Stripe TEST, USD20/task GPUs, config/backfills. Ask once, batched at task start, only for production/live money/real users' data/new credentials or provider accounts/overrun/permanent deletion. Owner owns live payments/legal. After2h without direction, post five evidence/options lines and change bounded approach.
- Use central AI Registry loaders/exact profiles; read instructions before credentials/cleanup. No secrets in project env/logs/GitHub.
- Chrome via cua_repl; staging qa-admin/qa-user. No repeated login, DevTools MCP or autoConnect.
- Tier2: compare fix-only/small/larger options in issue; Claude reviews, disagreements to owner. New queue/scheduler/storage/provider/table family needs owner approval; prefer delete/reuse.
- Chinese handoffs: Tier0 1-2 lines; Tier1/2 ~10: user gain, flow/failure effect, code/test/staging/prod proof, 1-3 "Owner 技术理解" points, SHAs/links. GitHub English. Release claims: Session: Unclaimed. [WORKFLOW.md](WORKFLOW.md).

# Inkseq delivery workflow

Current owner process: 2026-10-15, #200. Start with [AGENTS.md](AGENTS.md); read this only when delivery coordination needs detail. Latest owner decisions are in [workflow/owner-changes.md](workflow/owner-changes.md).

## One task, one authority

Use issues in `inkseq/h3-studio` and https://github.com/orgs/inkseq/projects/1. Read the assigned issue's latest owner comments, dependencies, claims and related open PRs. `workflow/project.json` contains routing IDs, not current status. Architecture/decisions/contracts are read only for the affected area; do not bulk-load historical reports.

Before editing, claim the bounded scope: integration owner, branch/worktree, base commit, affected files/contracts and next deliverable. Preserve existing checkouts and coordinate overlapping contracts. Do not infer abandonment from silence. Agents open and merge routine PRs; the user does not do manual PR work.

## Select the smallest adequate process

| Risk | Scope | Checks and review |
|---|---|---|
| Low | UI, copy/styles/docs, read-only diagnostics/preview tools, non-billing UI flags | Affected checks/build, merge, applicable frontend/app release, one live browser check. No independent review, live GPU or private evidence file. |
| Medium | API/contracts, queue/dispatch, storage writes, additive migrations | Affected checks, one independent review, release. Live GPU only if the generation path changes. |
| High | Money, credentials, user-data deletion, GPU rental/stop, provider accounts | Existing strict path: affected safety/integration tests, independent non-author review, exact-version protected release and relevant live operational acceptance. |

When uncertain, choose the lower tier and state it in one line. A high-risk slice does not impose extra GPU tests or evidence files on unrelated low-risk changes in its batch. Reviews go in the PR: reviewer, exact revision/scope, findings/fixes and result. Never call the author's own review independent.

Make related changes locally, then run necessary regression once. Do not repeat unchanged successful checks. Required CI remains the single `test` aggregate; no check bypass or self-hosted runner is introduced. Preserve complete test coverage across the existing shards.

Source merge is not release. Use the existing exact-version release route appropriate to the changed service. High-risk release approval/rollback checks remain. After #195, main automatically deploys to staging; production promotes the accepted image digest and frontend bytes without rebuild. Until then, the owner has authorized small P0 #193/#126/#194 production fixes.

## Time and operating authority

If an investigation or fix still has no clear direction after two hours, record a five-line checkpoint: observed problem, evidence, tried approach, options and the selected next bounded approach. Resolve the blocker autonomously under the latest owner authority; ask only for budget overrun, permanent user-data deletion or credential/provider-account changes. Do not let documenting uncertainty replace fixing a clear issue. Finish, close or settle when evidence supports it; retain only genuinely uncertain money/data obligations.

Test GPU spend is pre-authorized up to **USD 20 per Australia/Sydney calendar day**, on test machines only, with `max_price` and idle stop; stop at test completion. Count the day's cumulative test spend and active reservations conservatively before starting another rental. Once #195 exists, tests use staging. Production machines/jobs started by supervan are never touched. Existing account budgets remain enforced; this cap does not increase them or authorize settlement of unknown invoices.

Latest human decisions take precedence. Record each mid-course decision once as `CR-YYYYMMDD-n` in `workflow/owner-changes.md`, with date, decision and affected issues. Replace conflicting current requirements in the same change; retain old evidence in Git or `docs/archive/`. Do not create a parallel plan/task database.

## Acceptance and handoff

Use a short **Chinese handoff of at most about 10 lines**, including GitHub owner-facing handoffs. GitHub titles, technical descriptions and code remain English. State:

1. What changed and risk tier.
2. Whether it is live and what owner/supervan can do now, including where to open it.
3. What passed; remaining material limits/issues with links.
4. Request-to-usable time: issue `createdAt` to first verified live handoff, or pending.

Put detailed acceptance mapping, hashes, command IDs, receipts and review notes in the PR or one linked file, not long issue comments. Low-risk work needs no private evidence file. State a limitation only when it affects use or a decision. Mark each original acceptance checkbox only when satisfied; a composite box needs all its parts. A test count alone is not acceptance.

Record new material bugs, money/data risks or out-of-scope work in an existing issue or a bounded follow-up before handoff. Keep fixed small in-scope findings in PR evidence. Preserve original jobs, attempts, budget records and assets; unknown results require reconciliation rather than duplicate submission.

Project fields: `Status` (Backlog/Ready/In progress/In review/Done), `Blocked`, `Release`, `Session`. On handoff/relinquishment set Session to Unclaimed. An unfinished leaf task becomes Ready if actionable, otherwise Backlog/Blocked with its dependency. Parent rollups may be partly delivered without an active claim. Done requires the issue's acceptance, including actual deployment when specified.

Keep source on a remote branch at useful checkpoints. Do not reset preserved feature checkouts or delete branches as a completion ritual. Never put secrets, signed URLs, private prompts/media, environments or runtime data into GitHub.

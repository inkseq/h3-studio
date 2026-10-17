# Inkseq / H3 session entry

Read this file, the assigned issue's latest owner comments and current claims before editing. This is the only mandatory repository start-up document; expand linked contracts only for the affected task. Check current remote main without resetting another checkout.

## Authority and coordination

- Backend/issues: `inkseq/h3-studio`; canonical frontend: `../video-studio-design/studio-app` in `inkseq/sixnine-design`; board: https://github.com/orgs/inkseq/projects/1. Project IDs/fields are in `workflow/project.json`, read when updating the board.
- Latest human instructions win. Record mid-course decisions as numbered entries in [workflow/owner-changes.md](workflow/owner-changes.md); the latest applicable CR supersedes older plan/contract wording. Update conflicting current text in place, rather than adding overrides. The CR log records decisions, not a second task board.
- Claim a bounded scope with integration owner, branch/worktree, base revision and affected contracts. Inspect concurrent claims/PRs. Preserve other sessions' changes; never reset a shared checkout or develop on shared main. Agents manage routine PRs, checks and merges.
- Complete the related batch locally, then run the affected checks once. Do not trigger full CI/CD for each small change. Source merged, released and usable are separate outcomes.

## Process by risk

| Tier | Examples | Required process |
|---|---|---|
| Low | UI/copy/styles/docs, read-only diagnostics, preview tools, non-billing UI flags | Affected checks/build, merge, applicable release and one live browser check. No independent review, live GPU or private evidence file. |
| Medium | API/contracts, queue/dispatch, storage writes, additive migrations | Affected checks and one independent review, then release. Live GPU only when the generation path changes. |
| High | Money/reservations/billing, credentials, user-data deletion, GPU rental/stop, provider accounts | Targeted safety tests, independent non-author review and the existing protected exact-version release and operational acceptance. |

Classify the affected behavior, not the filename. When uncertain choose the lower tier and state that choice in one line. Details and handoff procedure: [WORKFLOW.md](WORKFLOW.md).

- After **2 hours** without direction, record a five-line evidence/options checkpoint and choose another bounded approach. Resolve blockers autonomously; ask only before budget overrun, permanent user-data deletion, credential changes or supplier-account changes (CR-20261015-10). Hold only genuinely uncertain money/data outcomes.
- Owner reports are **Chinese, at most about 10 lines**: change, live state, what owner/supervan can now do, remaining issue links, and request-to-usable duration. Put hashes/receipts/review details in the PR or a linked file, not long issue comments. GitHub titles, technical descriptions and code use English; owner-facing handoffs are the explicit Chinese exception.
- Track request-to-usable time from issue creation to the first verified live handoff. Mark it pending while not live. Do not replace acceptance with a test count.
- Record material out-of-scope discoveries in an existing issue or one bounded follow-up before handoff. Check only acceptance criteria actually satisfied. Release a claim with `Session: Unclaimed`; unfinished leaf issues return to Ready, or Backlog/Blocked when a dependency is missing.

## Product and operating boundaries

- Reuse one backend authority for owners, assets, jobs, attempts, budgets and GPU intents. Browser and Agent share the business API. `yingxu/` is generated: edit canonical frontend and synchronize through its release path.
- Preserve accepted immutable inputs, model/config identity, jobs/attempts, leases, cancellation, output receipts and cumulative accounting. Unknown outcomes require reconciliation, never silent duplicate generation/rental. Never drop controls, change precision or trim references to make a request fit.
- Test GPU safety ceiling is **USD20 total per Australia/Sydney calendar day**. It is an automatic safety limit, not approval: within it, start/test/replace test machines directly. Every test machine sets max_price, idle stop and maximum runtime (normally10 minutes idle /2 hours maximum); stop near the daily ceiling and notify the owner, and stop when tests finish. Reuse the existing Vast account/key with separate dstack projects/tags; no staging prepayment, small Team allowance or transfer key (CR-20261016-18). All development/GPU tests use staging; never stop owner production machines.
- P0 #193/#126/#194 may be fixed directly in production in small reviewed batches. After #195, development/GPU tests use staging; production promotes the same accepted image/frontend bytes. No rebuild during promotion.
- Current owner order (CR-20261017-28): v2 is accepted live. Merge design#64, complete #261 phone polish, then #252 Vast inventory diagnostics/fix, then a new issue for stale machine reservations and generated-video spend after read-only verification. Afterwards add live acceptance and close #233–#239/#242, handle PR#253, and review #109–#171 obsolete backlog with a checklist in #200. Preserve completed #254 recovery without replay, all original records and owner machines/jobs. Financial corrections use existing audited operations after positive evidence, with independent review; unknown supplier invoices stay explicit. Preserve #226/#240 outcomes; #232 remains with its existing observer. Before staging publication verify the release lane is idle. At most three simultaneous tasks; no independent Low-risk review.
- CR-20261017-27's human-admin budget editor is live; stored retired bootstrap limits remain nonbinding. Settings, original audited CAS, reservations/spend and idle/max-runtime safety remain authoritative.
- Reuse the central AI Registry loader and exact selected profile/endpoint; read its instructions before credential work. Do not copy secrets or create project `.env` files. Never publish keys, cookies, signed URLs, private prompts or user media. Read central housekeeping guidance before destructive cleanup.
- Reuse an authenticated Chrome session; ordinary acceptance needs only one account. Prefer superdan in Monash. Use another identity only for an explicit isolation/own-account requirement. Do not repeatedly log in or switch accounts.

## Code and contract map (read only what the task touches)

| Area | Sources / contracts |
|---|---|
| API, ownership, tasks, budgets | `platform_app.py`, `studio_platform/{api,repository,queue,auth}.py`; [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md) |
| GPU/queue orchestration | `studio_platform/dstack_*.py`, `postgres_dispatch.py`, `host_policy.py`; [DSTACK-MIGRATION.md](DSTACK-MIGRATION.md), [OPERATOR-CAPACITY-CONTRACT.md](OPERATOR-CAPACITY-CONTRACT.md) |
| Native runtime | `studio_platform/inference/`, `runtime_hosts/`, `native_runtime/`; pinned manifests and existing execution policies |
| Quick Chat / storyboard / assets | `studio_platform/quick_chat*.py`, `story*.py`, `assets.py`; linked issue UX/contract and canonical frontend |
| Release | [DEVELOPMENT-RELEASE.zh-CN.md](DEVELOPMENT-RELEASE.zh-CN.md), `deploy/platform/`, `deploy/dstack/RELEASE.md` |
| Architecture / dated observations | [PROJECT-PLAN.md](PROJECT-PLAN.md), [DECISIONS.md](DECISIONS.md), [CURRENT-BASELINE.md](CURRENT-BASELINE.md); these are not live status endpoints |

Local previews: `tools/run_quick_chat_preview.py` (8870), `tools/run_operator_preview.py` (8897), isolated loopback data and fake/disabled generation. Do not import old rental/bootstrap scripts for tests; they may have top-level side effects. Previous process/history is in `docs/archive/workflow-20261008/`, for reference only.

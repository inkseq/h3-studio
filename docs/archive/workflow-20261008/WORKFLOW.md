# Sixnine cross-session development workflow

Updated: 2026-10-08 (Australia/Sydney). This is the GitHub workflow entry point. `WORKFLOW.zh-CN.md` is a Chinese reading edition; this English entry is authoritative. Keep the rules aligned rather than maintaining separate processes. This workflow does not change product requirements, API contracts, production budgets, or operating authorization.

## Planning and execution have distinct sources of truth

- [PROJECT-PLAN.md](PROJECT-PLAN.md) provides the English architecture and A–H work-package entry point. Local detailed research may be indexed by `PLANNING-INDEX.zh-CN.md`; local-only documents are not assumed to exist on GitHub or in a fresh clone.
- [DECISIONS.md](DECISIONS.md) records durable choices, reasons and revisit conditions; the plan defines direction and contracts define required behavior. A decision being accepted does not mean its implementation or deployment has passed acceptance. Use the dated baseline for observed state and issues/PRs for execution progress.
- Current repository specifications, contracts, and approved UX define the intended behavior and invariants. Issues link to them instead of duplicating the architecture. Label proposals, implemented behavior, local verification, and production evidence separately.
- Keep durable contracts, decisions and operating instructions in versioned documents. Keep batch progress, claims, acceptance receipts and outstanding work in the issue/PR, with links to the applicable document revision. Update `CURRENT-BASELINE.md` when an accepted change alters its dated overview; do not create another status document for every batch.
- GitHub Issues and the Project track ownership, progress, dependencies, blockers, and acceptance evidence. Resolve the actual repository, Project, fields, and work-package issue links through [workflow/project.json](workflow/project.json). Do not create a parallel board or duplicate task from memory.
- PRs and commits identify code versions. Deployment receipts identify released versions. Closing an issue, merging code, or passing local tests does not by itself mean the change is live.
- Use English for GitHub titles, bodies, comments, milestones, labels, and statuses. Existing local Chinese research may remain; transfer relevant requirements into the English issue-linked specification. Do not link unpublished local files as if they were accessible on GitHub.
- The user's latest explicit instructions take precedence over an older plan. Record resulting scope and dependency changes in the existing issue and relevant specification. Do not ask again for an action already clearly authorized.

## Start a new session

Read **applicable AGENTS → PROJECT-PLAN → DECISIONS → this workflow → the assigned issue, its parent work package, and relevant specification**. Expand only the contracts, code, and evidence needed for that task. Local sessions may also consult `PLANNING-INDEX.zh-CN.md` for detailed historical research. Do not load every historical report as current guidance.

The public repositories are `inkseq/h3-studio` and `inkseq/sixnine-design`. The active Delivery Project is the private `inkseq` organization project. Use its verified identifiers in `workflow/project.json` to locate the actual remote work items; the former personal project is retained as closed migration evidence after cutover. Historical receipt URLs, immutable source IDs and signed release manifests retain their recorded provenance. Read the latest claim, comments, PRs, dependencies, and status before proceeding. A screenshot or a past chat saying “done” does not replace these records. Reconcile discrepancies rather than assuming the current production state.

Check the latest remote main revision and compare relevant guidance with the active branch before relying on it. Read newer documents through the remote or another worktree if needed; do not switch, reset or overwrite a preserved checkout to make its documents look current. Record the revisions compared and any unresolved difference in the claim/handoff. If the remote is unavailable, state that freshness is unverified and follow the bounded-work rule below.

This entry point depends on project context. **A completely new session outside this project does not automatically know this repository or board.** The user must provide a repository, issue, or Project link, or open the project. A shared board also does not authorize a session to message other sessions automatically.

For a session without project context, paste:

```text
Continue work in https://github.com/inkseq/h3-studio.
Read AGENTS.md, PROJECT-PLAN.md, DECISIONS.md, WORKFLOW.md, and workflow/project.json.
Check the Project, current claims, and dependencies; pick the highest-priority Ready task
unless I assign a specific issue. Claim a bounded scope before editing and leave a handoff.
Preserve other sessions' changes and existing operational limits. Use English on GitHub.
```

## Work packages and near-term order

| Work package | Outcome |
|---|---|
| A — Baseline and contracts | Verify existing entry points, versions, capabilities, states, and contracts |
| B — Minimum generation loop | Complete submission, on-demand capacity, generation, persistence, and download for the same job |
| C — Failure recovery | Handle duplicate submissions, unknown outcomes, cancellation, restart, and artifact collection |
| D — WanGP runtime integration | Integrate upstream WanGP through an adapter and qualify it against the fixed baseline |
| E — Dual-GPU redundancy | Isolate members and budgets, serve from the first ready member, and release idle capacity |
| F — Object storage and recovery | Establish independent backups, object-storage migration, and recovery |
| G — Scenario integration | Connect approved Quick Chat, Yingxu, and other scenarios to the shared backend |
| H — Collaboration and scale | Add collaboration permissions and scale only when supported by demonstrated needs |

The near-term sequence is **A1 → A2 → B plus D integration → C recovery acceptance → public B/D proof**, then E/G. C semantics are designed alongside B/D. Use [CURRENT-BASELINE.md](CURRENT-BASELINE.md), [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md), the plan and board for evidence and dependencies. Authorized local UX work can proceed independently, but a completed page cannot substitute for generation-loop acceptance. Split parent work packages into bounded, verifiable vertical slices rather than assigning “the entire frontend” or “the entire backend.”

## Claim work before editing

1. Find the existing issue for the outcome. Confirm its parent work package, current versus expected behavior, relevant specification, and acceptance criteria. Related low-risk changes can share one batch issue; every button does not require a separate process.
2. Inspect dependencies and concurrent work, then claim the task. Record the session, branch/worktree, base commit, change boundary, dependencies, and next checkpoint. If the checkout is shared, state that explicitly.
3. Inspect current workspace changes and shared API, schema, and execution contracts. Different files can still change the same contract; coordinate with its owner before implementation.
4. Set the corresponding Project status to `In progress`. Re-read the latest claim before starting: GitHub comments are not an atomic distributed lock. If claims or changes overlap, preserve existing work and coordinate. Do not overwrite, take over silently, or infer abandonment from inactivity.
5. If implementation changes the scope or contract, update the same issue/specification and its dependency impact. Do not generate another competing “final design.”

Use a short claim or resume comment:

```text
Claim / resume
- Session: <session title and ID>
- Checkout: <branch and worktree, or shared checkout>
- Base revision: <commit>
- Scope: <files/modules and shared contracts>
- Depends on: <issue links, or none known>
- Next checkpoint: <concrete deliverable>
- Verification: <offline/local/production; allowed boundary>
```

Every work item has one explicit integration owner. Parallel sessions use bounded child tasks with coordinated scopes and separate branches/worktrees; do not develop on shared main. Separate worktrees do not eliminate contract conflicts. Commit only owned changes, except an explicitly authorized source-preservation batch that records whose unfinished work it protects.

Readiness applies to the next bounded scope. A dependency issue can remain open if the exact capability needed for that scope has already been delivered and verified: link its revision/evidence, record the remaining dependency gate, and explain why it does not block this scope. Do not silently remove the dependency or describe the whole parent as accepted. Missing required capabilities mean `Backlog` with `Blocked: Yes`, a reason and the next action. `Ready` does not grant production or paid-operation authority.

## Keep durable decisions consistent

Record a material, lasting product, architecture or operating-policy choice in `DECISIONS.md`, with a stable ID, status, source, reason and revisit condition. Routine button text, small UI adjustments and batch progress stay in their specification or issue; they do not each need a decision entry. Use the record format there and distinguish the original decision date from the date it was recorded; use unknown when the original date is not evidenced.

The same PR updates the decision record and affected plan/contract in place. Remove or revise conflicting current guidance instead of appending another paragraph that silently overrides it. When a choice changes, retain the old ID and mark its supersession with a link to the replacement; Git preserves previous wording. Historical receipts remain dated evidence. If an affected document belongs to another branch/repository, coordinate its matching change and link both revisions before calling the documentation aligned. An unresolved mismatch stays explicit, not a competing source of authority.

## Agent-managed integration and source protection

The user does not perform routine manual PR work. Agents create the PR, inspect its complete diff, run the selected checks and merge the authorized batch when ready. Use independent agent review for material authentication, persistence, generation or billing changes. Required checks are not bypassed; a human approval count is not a substitute for that review. The stable backend CI check is `test`.

Backend CI partitions the complete Python and PostgreSQL inventories into six deterministic shards per suite. Each PostgreSQL shard uses its own isolated service; the single required `test` gate waits for every selected shard and rejects failed, cancelled or accidentally skipped checks. Preserve full inventory coverage when adding tests. Use the uploaded per-shard timings to rebalance measured slow groups; an under-three-minute target is not evidence of achieved duration. Public repositories use hosted runners, without production credentials in pull-request tests. Complete a related batch before its integrated CI pass; publication of the exact main revision and protected deployment remain separate release phases.

Leave an independent review receipt in the PR or linked issue: reviewer/session, reviewed revision and boundaries, exclusions or authoring overlap, findings, fix commits and re-review result. An agent must not describe review of its own changes as independent. A technical review is not automatically full acceptance of every issue criterion; state whether acceptance was also assessed and which criteria remain unverified. An agent review receipt is sufficient; a mandatory human PR review is not required.

Keep incomplete or unapproved work in a clearly labelled draft/source branch; do not merge it just to back it up. Push reviewed source at meaningful checkpoints and before handoff, excluding secrets, media, runtime data, environments and dependencies. Unpublished does not mean uncommitted. Git protects source; database/media recovery remains a separate F obligation.

A handoff or merged PR does not require switching an existing shared feature checkout to `main`. Preserve its branch, unpublished work and preview dependencies; use an isolated worktree for the next task. Cleanup or retirement is a separate authorized action, not a completion ritual.

Canonical frontend and approved mock are in the public `inkseq/sixnine-design` repository at the existing `../video-studio-design` path. Its `test` workflow checks source without deploying it. The generated backend `yingxu/` snapshot remains on the existing release path. An approved source merge does not remove the user's existing frontend publication gate.

Main protection should require PRs and the stable `test` result, disallow force-push/deletion, and require zero human approvals. If GitHub's account plan prevents enforcement, record that limitation and follow the same agent-managed workflow; do not make the repository public or buy an upgrade automatically.

Engineering decides whether a request needs configuration or code. Account lists, bounded operating windows, idle limits and recipe envelopes belong in validated configuration where behavior already exists. New recovery semantics need implementation and tests. Neither path extends authorization or discards ledgers.

## Implement and verify a batch

- Edit canonical sources. The frontend source is `../video-studio-design/studio-app`; `yingxu/` is a generated release snapshot. Follow the current AGENTS rules for snapshot synchronization and local UX acceptance.
- Follow [DEVELOPMENT-RELEASE.zh-CN.md](DEVELOPMENT-RELEASE.zh-CN.md): complete a related batch, then run affected regression checks. During development, run necessary checks without repeating unchanged full CI/CD suites for every small edit.
- Preserve account isolation, job identity, budget ledgers, immutable inputs, and existing assets. An unverified `unknown` outcome does not justify duplicate submission or rental.
- Creating an issue, moving a card, or accepting a PR does not authorize paid API calls, GPU startup, increased budgets, production deployment, data migration, or deletion. Check existing user authorization and release rules before such actions; report missing authorization clearly.
- Never put API keys, connection codes, tokens, cookies, passwords, signed URLs, private prompts, or user media in issues, PRs, comments, or attachments. Use redacted error codes, versions, aggregate evidence, or approved synthetic media. Keep sensitive receipts in protected locations and reference them without secrets.
- If GitHub is unavailable, independent authorized work without ownership conflicts may continue. State that remote status is not synchronized. Do not claim that an issue was assigned or a board was updated unless that operation succeeded.

## Triage discoveries before they are lost

Record a new blocker immediately in the current issue and stop only work that depends on its resolution. Triage other material discoveries before merge or handoff. Reuse a relevant existing issue; create a bounded task only when the finding needs its own scope, acceptance or ownership. This includes unresolved bugs/risks, money/data/security obligations, out-of-scope capability gaps and work another session must pick up. A small in-scope bug fixed and verified in the same batch can stay in the PR evidence rather than getting another issue.

For each unresolved finding, record the observed behavior and evidence, impact/priority, owning issue and next scope, acceptance condition, dependencies and release effect. Distinguish a confirmed defect, a risk needing verification and a capability not yet implemented. Keep sensitive evidence protected. A follow-up link cannot turn a failed criterion into a pass or remove it from the original scope without recording an explicitly authorized scope change.

## Pause or hand off

Update the original issue with completed work, the exact commit or uncommitted files, verification results, blockers, next steps, and any running operations. State what remains unverified, particularly paid calls, GPU execution, deployment, and migration.

Before ownership changes, identify the change boundary and unfinished operations. The receiving session checks the latest code and evidence before proceeding. A handoff does not cancel accepted jobs, restart instances, reset ledgers, or extend authorization windows. A brief absence does not release ownership; a transfer or relinquishment must be explicit.

When explicitly releasing a claim, set the Project `Session` field to `Unclaimed` and keep the branch/worktree as preservation evidence. If the issue is not accepted, set it to `Ready` only when the documented next scope is ready under the dependency rule above; otherwise use `Backlog` and `Blocked: Yes` for missing required dependencies. Do not leave a released leaf task `In progress` as if a session were still implementing it. A retained claim awaiting review can be `In review`; inactivity alone never releases it.

## Finish and accept

Record the following in the original issue/PR:

1. Assess every acceptance criterion as `pass`, `partial`, `unverified` or `fail`, with the exact revision, evidence link/check and environment. `partial` identifies the satisfied part and the remaining condition; `unverified` is not a pass. Split composite criteria into independently assessable parts while preserving every original requirement; do not check the parent until all parts pass.
2. The commit/PR, specification and contract updates, and compatibility impact. Code or mock tests are not proof of production success.
3. Remaining work with linked follow-up issues and dependencies.
4. Release state and applicable rollback evidence. Use `Not released` if deployment has not occurred.

Use a compact acceptance table, not only a total test count:

| Criterion | Result | Evidence, environment and revision | Remaining condition / issue |
|---|---|---|---|
| AC identifier or exact requirement | pass / partial / unverified / fail | Specific check/receipt, offline or live environment, commit | Link or none |

Only `pass` maps to a checked acceptance box. The integration owner records the acceptance decision against this evidence and the review receipt; implementation progress alone does not close an issue. Reports lead with what is accepted, what is pending and the follow-up links. Test totals support those conclusions rather than replacing them.

Project fields are:

- `Status`: `Backlog`, `Ready`, `In progress`, `In review`, `Done`.
- `Blocked`: `No`, `Yes`; include the reason, dependency, and next action when blocked.
- `Release`: `Not required`, `Not released`, `Released`.
- `Session`: the active claim or `Unclaimed`; historical ownership remains in comments.

Read actual field IDs from `workflow/project.json`. `Done` means the acceptance criteria for that issue are satisfied. If those criteria include deployment, the issue cannot be completed without deployment and the required verification. A local-only deliverable can be completed while release remains a separate work item.

Parent work-package status summarizes delivery, separately from active session ownership. Update it when children are accepted or material follow-ups change the remaining scope. A partly delivered parent can be `In progress` without a current coding claim; identify it as a rollup and use `Session`/child claims for ownership. Do not close a parent merely because one child passed, or automatically mark every parent blocked by one child's dependency.

Evidence must distinguish `Local verified`, `Released`, and `Production verified`. Use the existing release process when deployment is required. Deployment receipts establish release facts; test counts, merged PRs, and board status do not establish online availability.

At batch acceptance, the integration owner checks A–H dependencies, duplicate work, and contract differences, then updates the existing plan with material discoveries. Sessions do not need to rewrite the entire architecture, and document count is not a progress metric.

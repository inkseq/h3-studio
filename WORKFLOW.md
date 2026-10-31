# Inkseq delivery workflow

[#328](https://github.com/inkseq/h3-studio/issues/328) and [AGENTS.md](AGENTS.md) define the current lanes, tiers, cadence and safety. Latest owner decisions: [workflow/owner-changes.md](workflow/owner-changes.md). Older process text is archived under `docs/archive/workflow-20261019/` and is not a current gate.

## One bounded issue
Read latest owner comments and concurrent claims/PRs; claim integration owner, worktree/branch, base and contracts before editing. Never reset shared/preserved checkouts. Put a shared API contract in the issue before backend/frontend implementation. Claude owns studio-app and inkseq-web; Codex owns backend and operations. Keep one issue in progress plus one in review per lane.

## Checks and staging
Use the smallest checks appropriate to the changed behavior, once per related slice. Keep required CI's single `test` aggregate; no bypass or self-hosted runners. Tier0 has CI + staging self-check, PR description only. Tier1 adds affected tests and one short issue comment. Tier2 (closed list in CR-20261027-70: money, schema/user-data deletion, auth/credentials, production host machinery) retains safety/isolation/unknown-outcome tests, one non-author safety review and the relevant staging acceptance checklist; everything else merges on the author's green CI. Record review scope/revision and findings in the PR; the author's own review is not independent.

Automatic main staging is separate from production. Promote identical accepted image/frontend bytes, without rebuilding, as soon as accepted (any day; CR-20261021-47). Claude signs off Tier2. Record source merged, staged, owner accepted and production separately. GPU checks batch into the approved acceptance window, not a new rental for each edit. Never present an untested checklist item as passed.

## Handoff and authority
GitHub technical descriptions are English. Owner handoffs are Chinese, about10lines: exact versions, what is usable, changes, checks and unfinished issue links. No long Tier0/1 receipts. Request-to-usable time is pending until verified live. Release claims with `Session: Unclaimed`; leave unfinished work Ready or blocked by its actual dependency. Issues close on production promotion.

Preserve original financial/job/media history and protect unknown outcomes. An API read, merged PR, board item or old receipt grants no extra mutation authority. Do not repeat completed financial operations. All development/GPU tests use staging. Keep max_price, idle stop, maximum runtime, task-scoped USD20 safety and stop-at-completion; protect owner production machines.

After two hours without direction, record a five-line problem/evidence/options checkpoint and choose another bounded approach. Record material out-of-scope findings in an existing issue or one follow-up. Ask only for budget overrun, permanent user-data deletion, credential/provider-account changes or a product/legal decision outside the specification.

Release contracts: [DEVELOPMENT-RELEASE.zh-CN.md](DEVELOPMENT-RELEASE.zh-CN.md), `deploy/platform/`, `deploy/dstack/RELEASE.md`. API/queue sources: `studio_platform/`; generation contract: `docs/GENERATION-CONTRACT.md`. Board IDs are routing data in `workflow/project.json`, never live acceptance evidence.

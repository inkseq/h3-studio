# Owner change requests

These entries record owner decisions, not task status. The latest applicable CR wins over older wording. Detailed technical contracts remain linked from their issue.

| ID | Date (Australia/Sydney) | Decision | Issues / source |
|---|---|---|---|
| CR-20261015-1 | 2026-10-15 | No reserved always-on supervan group. Machine use now follows CR-20261015-11: production, including her machines, may be used for testing before staging; afterwards all development tests use staging. | [#193](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6183439332), superseded in part by CR-20261015-11 |
| CR-20261015-2 | 2026-10-15 | Open every genuinely model/API-supported control and input combination now; no pre-release qualification whitelist/test prerequisite. Keep hard validity, ownership, resources and budgets; record actionable failures. | [#126](https://github.com/inkseq/h3-studio/issues/126#issuecomment-6183470849) |
| CR-20261015-3 | 2026-10-15 | Release the original budget reservation when the failed start is positively proven to have allocated no machine; retain uncertainty only where real allocation/billing is unknown. Includes the 02:19 failed start under #193. | [#193](https://github.com/inkseq/h3-studio/issues/193), owner instruction 2026-10-15 |
| CR-20261015-4 | 2026-10-15 | With no machines, jobs or unresolved start/stop outcomes, controller/dispatch have no work loop: event wakeup plus one safety check every15–30minutes. | [#193](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6183407125) |
| CR-20261015-5 | 2026-10-15 | The CR18 staging acceptance, production promotion and resize batch is complete. Current continuation follows CR-20261016-19: #193 idle Vast/CPU measurement, then #217 PostgreSQL wakeups, then #218 alerts; staging acceptance precedes same-artifact production promotion. Other issue priorities remain deferred. | [#193](https://github.com/inkseq/h3-studio/issues/193), superseded by CR-20261016-19 |
| CR-20261015-6 | 2026-10-15 | Risk-tiered process, Chinese owner handoff ≤about 10 lines, a two-hour checkpoint to reassess an investigation without direction, one request-to-usable duration metric and a short mandatory entry. Owner questions follow CR-20261015-10. | [#200](https://github.com/inkseq/h3-studio/issues/200), updated by CR-20261015-10 |
| CR-20261015-7 | 2026-10-15 | Current test-GPU ceiling is USD20 per change/acceptance task under CR-20261018-32; the previous global daily rule and the dated USD30 exception are superseded. This is an automatic safety limit, not an approval gate: within it, start, test and replace test machines directly. Every test machine has max_price, idle stop and maximum runtime (normally 10 minutes idle / 2 hours maximum). Automatically stop test machines and notify the owner near the task ceiling; stop when tests finish. No separate staging prepayment or small Team allowance. See CR-20261016-18. | [#200 owner comment](https://github.com/inkseq/h3-studio/issues/200#issuecomment-6183602897), updated by CR-20261015-9/11 |
| CR-20261015-8 | 2026-10-15 | Increase the existing shared GPU budget limit by USD5 once, preserving spend and reservations. Continue REF acceptance and #126; the current test ceiling follows CR-20261018-32. This does not authorize repeating the shared-limit increase. | Owner instruction 2026-10-15 after desktop restart; [#193](https://github.com/inkseq/h3-studio/issues/193) |
| CR-20261015-9 | 2026-10-15 | Test-GPU ceiling is temporarily USD30 for Australia/Sydney calendar day 2026-10-15 only; USD20 resumes at 2026-10-16 00:00 Australia/Sydney (2026-10-15 13:00 UTC). Conservatively include existing spend, unresolved reservations and transfer costs; retain max_price and idle stop. | Owner instruction 2026-10-15 in this session; [#126](https://github.com/inkseq/h3-studio/issues/126) |
| CR-20261015-10 | 2026-10-15 | Resolve in-scope blockers autonomously and minimize owner interruptions. Ask only before exceeding the authorized budget, permanently deleting user data, changing credentials, or changing supplier accounts. Existing ownership, accounting and protected exact-version release checks remain in force. | Owner instruction 2026-10-15 in this session |
| CR-20261015-11 | 2026-10-15 | Until staging is live, supervan will not use production; production, including her machines, may be used for testing. Provision one separate small AWS host for #195 staging, explicitly authorized by the owner. Once staging is live, all development and GPU tests run only there. | Owner instruction 2026-10-15; [#195](https://github.com/inkseq/h3-studio/issues/195) |
| CR-20261015-12 | 2026-10-15 | Finish #126 configuration, real REF generation, live execution policy and static frontend release. The owner has resumed work under CR-20261015-15: finish #209 first, then product domain migration, then #194 and #195 concurrently, with #196 still afterwards. Report #126 live usability separately from completion of all three issues. | Owner instruction 2026-10-15; [#126](https://github.com/inkseq/h3-studio/issues/126), [#194](https://github.com/inkseq/h3-studio/issues/194), [#195](https://github.com/inkseq/h3-studio/issues/195); updated by CR-20261015-14/15 |
| CR-20261015-13 | 2026-10-15 | The earlier staging-only Vast Team/funding design is superseded by CR-20261016-18: reuse the existing Vast account/key, distinguish machines by dstack project/tags, and do not transfer USD5, prepay staging, impose a separate small allowance or create a transfer key. Keep separate staging R2/database/queue and retained credential/history records. The temporary bootstrap was already revoked and must not be reused. | Owner explicitly accepted the staging isolation/account/credential and USD5 transfer question in this session; [#195](https://github.com/inkseq/h3-studio/issues/195); timing updated by CR-20261015-15 and bootstrap retention by CR-20261016-16 |
| CR-20261015-14 | 2026-10-15 | Publish the low-risk #126 frontend read recovery without another independent review; first handoff line states whether supervan can use all supported H3 parameters live. That bounded chat ended without domain changes. The owner has now resumed under CR-20261015-15: #209 first, domain migration second, then #194/#195 concurrently. The owner's exact domain requirements are preserved below unchanged and remain authoritative for the migration. | Owner instruction 2026-10-15 in this session; [#126](https://github.com/inkseq/h3-studio/issues/126), [#194](https://github.com/inkseq/h3-studio/issues/194), [#195](https://github.com/inkseq/h3-studio/issues/195); continuation updated by CR-20261015-15 |
| CR-20261015-15 | 2026-10-15 | Resume end-to-end work under CR-20261016-18. The domain migration and #209 acceptance have completed. Keep the separately authorized staging CPU host and staging.inkseq.com; reuse the existing Vast account/key with namespaced machines and the USD20-per-task test safety line (CR-20261018-32). No staging prepayment or transfer-key approval is required. Resolve in-scope blockers autonomously. | Owner continuation instruction 2026-10-15; [#209](https://github.com/inkseq/h3-studio/issues/209), [#194](https://github.com/inkseq/h3-studio/issues/194), [#195](https://github.com/inkseq/h3-studio/issues/195) |
| CR-20261016-16 | 2026-10-16 | The prepared staging R2 credential and Vast bootstrap were approved; the bootstrap was subsequently revoked. Prefer inkseq names for new resources and retain existing names/history. The earlier independent provider Team/USD5 funding requirement is superseded by CR-20261016-18; do not recreate bootstrap/funding credentials. | Owner action-time confirmations and follow-up instruction in this session; [#195](https://github.com/inkseq/h3-studio/issues/195) |
| CR-20261016-17 | 2026-10-16 | The earlier order/funding prerequisite is superseded by CR-20261016-18: real staging GPU/browser/Agent acceptance using the existing Vast account/key, stop tests, promote the same accepted version including PR215 and both idle-CPU fixes, then downsize staging to t3a.small. First owner-report line states the production version. Close #193/#194/#195/#182 only where acceptance is satisfied. | Owner instruction2026-10-16 in this session; [#193](https://github.com/inkseq/h3-studio/issues/193), [#194](https://github.com/inkseq/h3-studio/issues/194), [#195](https://github.com/inkseq/h3-studio/issues/195), [#182](https://github.com/inkseq/h3-studio/issues/182) |
| CR-20261016-18 | 2026-10-16 | Owner supersedes separate staging USD5/Team funding: use the existing Vast account/key and distinguish staging/production machines by dstack project or tags. Do not prepay staging, add a separate small allowance or create a transfer key. The former global daily test line is superseded by USD20 per change/acceptance task (CR-20261018-32); it is not an approval workflow. Every test machine sets max_price, idle stop (normally10 minutes), and maximum runtime (normally2 hours); automatically stop test machines near the task ceiling and notify the owner. Within this line, start/test/replace machines directly. Complete actual staging GPU/browser/Agent acceptance, stop the tests, promote the same accepted version including PR215 and both idle-CPU fixes, then downsize the existing staging host to t3a.small. Finish with a Chinese handoff whose first line is the production version. | Owner instruction in this session; [#193](https://github.com/inkseq/h3-studio/issues/193), [#194](https://github.com/inkseq/h3-studio/issues/194), [#195](https://github.com/inkseq/h3-studio/issues/195), [#182](https://github.com/inkseq/h3-studio/issues/182) |
| CR-20261016-19 | 2026-10-16 | Continue from the CR18 handoff: #193 actual idle Vast HTTP frequency and safe idle suppression first, #217 replace Hatchet with PostgreSQL LISTEN/NOTIFY on the existing fenced queue second, #218 push alerts third. Diagnose idle CPU on the existing staging t3a.small. Finish and accept on staging before same-artifact production promotion. Resolve blockers autonomously; at most three simultaneous tasks and no Low-risk review subtask. Retain per-task USD20 test-GPU and stop safeguards under CR-20261018-32. | Owner continuation instruction; [#193](https://github.com/inkseq/h3-studio/issues/193), [#217](https://github.com/inkseq/h3-studio/issues/217), [#218](https://github.com/inkseq/h3-studio/issues/218) |
| CR-20261016-20 | 2026-10-16 | Finish the current #217 step before interleaving additional operations work. Keep both repositories public; verify GitHub secret scanning/push protection and host public exposure limited to HTTP/HTTPS plus necessary management ports, with databases private. Enable daily EBS snapshots retained 7 days, verify scheduled database backups and rehearse restoration, address R2 object versioning in #147, bind staging to an Elastic IP and provide exact Namecheap records if owner action is needed. Close completed/superseded issues with one-line reasons (including #107/#132/#7/#20/#21 where justified); reconcile Vast and RunPod billing monthly under #28. At most three concurrent subtasks; no Low-risk review subtask; retain per-task USD20 GPU safeguards under CR-20261018-32. | Owner supplemental instruction in this session; [#147](https://github.com/inkseq/h3-studio/issues/147), [#28](https://github.com/inkseq/h3-studio/issues/28) |

## CR-20261017-21/22 — current v2 and backup scope

- **CR-20261017-21:** Begin #239 code without waiting for the existing #241 pipeline. Return for staging backup acceptance and genuine #226 closeout once ready; #232 is observed by its existing daily task. Follow the v2 handoff in `inkseq/sixnine-design`: #239 → #233 → #234 → #242 → #235 → #236; #237 phone work accompanies each batch and #238 comes last. Verify no other deployment is running before publishing frontend to staging. Keep at most three simultaneous tasks and no Low-risk review subtask.
- **CR-20261017-22:** The two #240 references are superdan test media normally removed through completed empty-trash cleanup. Classify this kind of provably completed permanent deletion as expected missing coverage and exclude it from unexplained-loss alerts, then close #240 after implementation/acceptance. Preserve original audit references, truthful missing-byte coverage and all existing superdan/supervan account data; no bulk cleanup is authorized.

## CR-20261015-14 — owner domain migration requirements (verbatim)

域名迁移（owner 决定，记 CR）：项目整体改用 inkseq.com，sixnine.art 以后另作他用，不再作为本产品域名。
1. 主域名用 https://inkseq.com，www.inkseq.com 跳转到主域名。DNS 在 Namecheap，@ 和 www 都已指向 18.136.57.227 并已生效。
2. 改 Caddyfile、SIXNINE_PUBLIC_ORIGIN、Agent 说明和 skill 下载链接、发布和验收脚本、R2 的 CORS、测试里所有 sixnine.art，配置项能改名的一起改（比如 PUBLIC_ORIGIN）。
3. 过渡期：sixnine.art 和 www.sixnine.art 永久跳转到 inkseq.com 对应的路径；API 用 308 跳转，保证 POST 不丢请求。等我通知再停掉这个跳转。
4. 登录 cookie 跟域名绑定，换域名后用户需要重新登录一次，在汇报里写清楚。已有的 API key 和 Agent 连接要确认在新域名下继续能用。
5. 界面和文档里对外的品牌名从 Sixnine 改成 映序 / Inkseq。
6. staging 用 staging.inkseq.com（并入 #195）。
低风险部分（文案、文档）直接做；域名切换按中风险处理：先确认新域名 HTTPS 和登录正常，再把旧域名改成跳转。

Append a numbered entry for a new owner decision and update affected current wording in place. Do not copy operational receipts or routine implementation choices here.


## CR-20261017-23 — #239: no automatic creation purge

Owner v2 HANDOFF rule 4 supersedes the former five-day automatic trash expiry (DEC-014). Stop creating time-triggered purge intents, including old consented deadlines; public expiry is absent. Preserve original explicit durable purge/empty-trash reconciliation, irreversible guards, ledgers, tombstones and completed deletions. No historical rows are migrated and no live cleanup is invoked by this source change.

## CR-20261017-24 — #251: visual parity before combined promotion

Finish #251 under CR-20261017-25 before promoting the v2 functional batch. Accept the functional and visual batch together on staging, then promote identical accepted artifacts immediately, without waiting for the owner to preview staging. Preserve the existing functional APIs, original receipts/accounting, user data, single release lane and three-task limit.

## CR-20261017-25 — #251: prototype components and persistent internal navigation

The owner rejected CSS-only approximation after reviewing local8914 and staging. Use the approved prototype's actual page components/DOM as the view layer for all seven pages, replacing only its simulated store with the existing real controllers/APIs. Remove replaced legacy page styles and remove the old seven-step story wizard from v2; new stories open the workspace directly. Preserve real path routes, shareable story/conversation/shot URLs, refresh and browser Back/Forward. Internal navigation must not reload the document; keep loaded data and drafts between pages, without loading the same data again on a return visit. Compare prototype and final staging at1440×900 and375×812 for every page, listing intentional real-data/safety differences. The currently deployed partial visual build is not accepted for promotion. Finish the component and navigation batch together, verify the staging lane is idle, accept on staging, then promote the same artifacts immediately without an owner preview gate.

## CR-20261017-26 — urgent #254 reservations and Settings authority

The #254 emergency recovery and subsequent #251 same-artifact promotion completed; original operation receipts are frozen and must not be replayed. Retain positive provider evidence, audited release/settlement and unknown actual invoices. Retired bootstrap fixed caps stay nonbinding; Settings and idle/max-runtime protections remain authoritative. Current work follows CR29.

## CR-20261017-27 — #254: visible administrator budget controls

Owner follow-up https://github.com/inkseq/h3-studio/issues/254#issuecomment-6228797265 required the authenticated human administrator's GPU My budget tab (and Settings Admin entry) to expose the original audited budget editor for all accounts, including shared GPU/job accounts, with readable labels. This is live. Preserve CAS, actor audit, reservations and confirmed spend; identify retired bootstrap limits honestly as nonbinding historical values. The emergency freeze ended with the accepted #251 release; current work follows CR29.

## CR-20261017-28 — accepted v2 follow-ups in owner order

The owner confirms v2 is live and production is normal. Continue in order: (1) merge sixnine-design#64 and complete #261 phone polish; (2) diagnose/fix #252 Vast query failures; (3) create a new issue for remaining machine holds (reported6 stopping,2 unknown,1 draining; USD27.76 shared/4.93 supervan) and generated-video spend showing zero, first read-only verification then an evidence-based audited fix; (4) record live acceptance and close #233–#239/#242 and handle PR253; (5) review old #109–#171 backlog, close obsolete work with reasons and record the list in #200. Read-only preparation may overlap; implementation/release and financial corrections preserve this order. Do not repeat completed #254 recovery, fabricate provider charges, stop owner machines, delete user records or change credentials/accounts. Existing risk tiers, independent High-risk review and same-artifact staging→production apply; no owner preview gate for #261.

## CR-20261017-29 — production story-opening findings and shell corrections

The owner accepted production #269 with a clean browser and found10/12 cloud stories open, while a browser containing an unsynced cloud draft falls into the old account dialog. Fix this with a project-specific draft choice that preserves the draft, safe handling of the two legacy Untitled records, one localized navigation-scoped error, and URL-driven restoration of the exact cloud story after authentication/refresh. Include #268 nonshrinking shell headers and the untranslated Quick Create no-machine label in the same batch. Preserve original cloud/local projects, downloadable drafts, media and all accounting; do not auto-discard drafts or silently rewrite unsupported documents. Verify on staging at desktop/phone sizes and immediately promote the same accepted frontend bytes to production. No generation changes or paid GPU acceptance are required.

Owner priority clarification at11:03UTC ([comment6233135259](https://github.com/inkseq/h3-studio/issues/269#issuecomment-6233135259)): merely opening some cloud stories marks their draft dirty with no user edit. Fix that source behavior first, preserve real edits and ask directly in the story, retain `project=` on every open/restore path, and verify opening the same unchanged story twice on staging before immediate promotion. The bounded production read also proved the two Untitled records are existing `content_deleted` tombstones, not old story schemas: exclude them from the live catalog and reject direct content reads without altering their audit rows or restoring deleted content. This narrow read-only backend correction joins the same staged artifact batch.

## CR-20261018-30 — card-based Quick Create history and combined regression

The owner accepts the latest#271 diagnosis6233793301: event-heavy pages containing only preflight/submission records have no card identity and add nothing visible. Add the existing session's owner-scoped card-level history endpoint and use it for initial/older card loads; each successful click must reveal earlier cards and the end must be explicit. Preserve event polling, existing turns/revisions/submissions, draft/composer state, authorization and budgets. Keep the live#269/#268 fixes in the same version and include their relevant regression and remaining shell-density checks. Accept on staging with a synthetic session of at least60 cards and thread geometry at375/1440/1920, then immediately promote the same artifacts. Production acceptance of supervan's original long session is read-only. No GPU generation, rental, new credentials, accounting replay or destructive user-data change is needed for this batch.

## CR-20261018-31 — owner follow-ups after card history

The owner confirms the ordered batch: finish #271 with read-only production acceptance of supervan's66-card session; then #277 functional blockers, #273, #275 after merging design#73, the remaining #277 page standards, #276 first visible compatibility/naming batch, #272 server links before bounded video caching, and the real Agent scene bulk/compare/adopt scenario in #267. Accept each batch on staging and promote identical artifacts directly, without an owner preview gate. Every affected page gets side-by-side demo/live screenshots at1440×900 and375×812. Preserve old aliases, existing tenant identifiers, original media/jobs/drafts and owner machines. Test-GPU budget now follows CR-20261018-32: USD20 per change/acceptance task; staging only, max_price/idle stop/maximum runtime and stop after testing remain required. No added credential, permanent deletion or supplier-account authority is inferred.


## CR-20261018-32 — owner acceptance, urgent retention and per-task test budgets

The owner reviewer accepted production e739d9a and will perform production acceptance after each future batch. Continue posting the exact production revision and concrete change list; preserve separate technical staging/live checks and owner review status. Order: #286 urgent retention and cleanup → #285 sessions/UI → task-scoped guard and #267 actual Agent scene bulk/compare/adopt → #287 media/history performance. Accept staging then promote identical artifacts without a new preview gate.

Test GPUs are pre-authorized up to USD20 **per change / acceptance task** (issue or PR acceptance ID), replacing the shared Sydney-day cap. Count confirmed spend and committed price for machines running for that task. Older unresolved invoice projections from other tasks must not block a new task or consume its allowance. Tag each rental with its task identity and preserve the editable Settings → Admin limit, original audit/financial attribution, max_price, idle stop, maximum runtime, staging-only boundary, stop-at-completion and protection of owner production machines/jobs. This does not forgive old invoices, release historical reservations, raise account budgets, create credentials or change supplier accounts. Source: [#285 owner comment](https://github.com/inkseq/h3-studio/issues/285#issuecomment-6244479881) and owner continuation in this session.

#286 authorizes cleanup of reproducible obsolete deployment bundles/tags via the reviewed controller: retain current, previous rollback, newest three extra and all in-flight/pinned references; after successful promotion and daily. CR-20261019-36 additionally authorizes verified redundant local business-backup payload retention. Never touch databases, unverified backups, remote backup versions, user media, active volumes or retained versions. Existing SNS receives80%/90% disk alerts. Record before/after usage and preserve release/rollback evidence.

## CR-20261019-33 — accepted d1e9c90 and consolidated batch order

The owner/Claude accepted production d1e9c90 and supplied #296 follow-ups. Follow [#257 consolidated order6250292882](https://github.com/inkseq/h3-studio/issues/257#issuecomment-6250292882): #296 items1–2 financial display/invoice reconciliation first; #290 step1 production/staging telemetry labels next; #257 mechanical test/doc layout and formatting before runtime inventory/dead-path removal; Alembic baseline before DBOS adds a schema; #295 comparative stub-only resource spike, then the chosen queue transport and one-click film; remaining #290 observability and #291; then #257 remaining splits/libraries/source consolidation/error/timestamp/naming work.

Each step is its own small reversible batch. Staging acceptance precedes identical-artifact production promotion, and **Claude accepts each batch on production before the next starts**. Report exact live revision and concrete changes; do not advance solely because CI or merge passed. Risk tiers remain authoritative: light flow for mechanical/UI work, full financial/GPU/queue checks for affected behavior. Preserve original financial histories and unknown invoices; #254/#264 completed operations are not replayed. CR32's USD20 per change/acceptance-task safety and staging-only test boundary remain unchanged. Source: owner request in this session, #296, and the linked consolidated comment.

## CR-20261019-34 — #296 audited actual settlement and internal-account waiver

The owner extends the current #296 financial batch before production promotion: add human-admin actions to settle against actual supplier charge evidence and to waive a user's charge, requiring a reason and retained audit history. Never delete ledger rows or overwrite true supplier cost with zero. User-facing account/Agent charges must reflect the explicit waiver while provider costs remain available for cost-per-take statistics and cost safety. Settle only positively identified original obligations with exact evidence and a total-conserving documented rental allocation; ambiguous lineage or unavailable supplier actuals stay explicit rather than invented.

Apply the reviewed operation to the positively identified supervan 2026-10-17 batch after reading its Vast actual charges, then waive with reason **内测账户，上线前不收费**. This is specific financial mutation authority for those original records, not authority to replay completed #254/#264 recovery, forgive unrelated accounts, delete data, change credentials/provider accounts, or operate owner machines. Use preview/CAS, idempotency, targeted transaction/isolation checks, independent non-author review and the existing exact-artifact release lane. Other #296 follow-ups and the later consolidated batches retain CR33's ordering and owner production-acceptance gate. Source: direct owner instruction in this session and [#296 comment6250618624](https://github.com/inkseq/h3-studio/issues/296#issuecomment-6250618624).

## CR-20261019-35 — accepted settlement; telemetry labels and two display fixes next

The owner/Claude accepted production backend d2516ce and frontend4aaf57a, including the original97 audited settlement/waiver ([#296 comment6251774655](https://github.com/inkseq/h3-studio/issues/296#issuecomment-6251774655)). Financial operations are complete and further invoice work is deferred until pre-launch. Do not replay them. Correct the two approved low-priority display issues alongside #290 step1: do not present combined non-additive rental/job waiver views as a single cash amount; default-hide inactive Agent connections and distinguish equal names using existing creation/client data. No new billing operation or credential is authorized by those presentation fixes.

Continue CR33's separate-batch sequence at #290 step1 (production/staging resource identity labels and exact release version, retaining existing exporter destination/credentials), then #257 mechanical refactoring, runtime inventory/dead-path removal, Alembic, and #295. The owner production-acceptance gate remains between batches. Broader #290 dashboards, read-token wiring, startup diagnostics and stage-reader work stay in the later observability batch. Source: direct owner continuation2026-10-19 and the linked acceptance.

## CR-20261019-36 — accepted5586788; verified local-backup retention before refactoring

The owner accepted production5586788 and inserts [#286 comment6252530983](https://github.com/inkseq/h3-studio/issues/286#issuecomment-6252530983) before the existing #257→#295 sequence. Keep only the newest verified local business-backup payload after verifying its remote S3 copy. Delete older local payloads and redundant transport snapshot copies only when each exact run's original target, completion version, inventory and hashes verify. Unknown/unverified/manual/active copies stay protected; retain all operation intents, completion/transfer receipts, coverage limitations and all remote versions. This exception does not authorize database/media deletion, new backup prefixes to hide unknown outcomes, credential/account changes, financial replays or owner-machine operations.

Reuse the existing daily backup helper and exclusive release lock; include post-copy and completed same-day retention plus a bounded catch-up plan/apply entry. Existing80/90 filesystem disk alerts must continue including local backup storage. Read-only observations of release/migration/publication artifacts do not authorize another cleanup path. Accept this prerequisite on staging and then install the same reviewed bytes on production, report concrete state, and retain the existing Claude acceptance boundary before #257 mechanical layout/formatting, runtime inventory/deletion, Alembic and the #295 resource gate/migration. Source: direct owner instruction2026-10-19 in this session and the linked latest comment.

## CR-20261019-37 — completed overnight delivery; continued by CR38

The owner supersedes the between-batch acceptance wait for this run: finish #286 backup retention, then the ordered #257 organization/runtime inventory and Alembic baseline, with risk-graded releases, and first verify #295 DBOS on staging without production activation. Keep slices bounded and reversible, apply the existing tests/review gates by actual behavior, and preserve accepted artifact identity when promoting eligible slices. Record questions/decisions in the relevant issue and continue independent work rather than waiting for routine choices; the owner will review the combined results in the morning. Budget overrun, permanent user-data deletion beyond the verified-copy retention scope, credential or provider-account changes still require the owner. Existing GPU safety, owner-machine protections, original ledgers/media, no financial replays and all remote backup versions remain unchanged. Source: direct owner continuation2026-10-19 in this session.

## CR-20261019-38 — accepted baseline; retention repair, DBOS10s and launch Phase A

The owner/Claude accepted production5d97d5d for the #257 organization/runtime/baseline and #286 backup-retention round ([#257 acceptance6257609141](https://github.com/inkseq/h3-studio/issues/257#issuecomment-6257609141)). First repair the failed scheduled release-retention task from [#286 comment6257608844](https://github.com/inkseq/h3-studio/issues/286#issuecomment-6257608844): non-secret actionable failure codes, fix the cause, verify one original manual service run and report failures through the existing alert monitor. Preserve every original retention, backup, database, user-media and rollback boundary.

The engine choice is now **DBOS**, superseding the pending cost-choice note in #295. Configure supported queue polling intervals to10seconds and complete #295 on staging: real generation-transport replacement with existing owner/ledger/fence/intent/receipt rules; one-click film; ordinary releases surviving in-flight matching-version workflows; real GPU acceptance and all remaining #267 Agent/lifecycle/trash/import scenarios. Keep stable explicit workflow-schema versions and one executor per environment, no Conductor, custom reconciler or dual product queue. Record SDK limitations honestly; do not silently patch vendor internals or claim every SQL operation occurs at the configured queue interval.

The production hold remains. CR39 replaces the remaining order, pacing and acceptance-window rules; earlier accepted results and unfinished #295/#267 checks remain recorded in their issues.

## CR-20261019-39 — parallel lanes, simplify sprint and M1

Owner adopts [#328](https://github.com/inkseq/h3-studio/issues/328) and its [simplify addendum](https://github.com/inkseq/h3-studio/issues/328#issuecomment-6264673058). Codex owns backend/API/infrastructure/GPU/money and Agent documentation; Claude owns canonical `studio-app` and its `yingxu` synchronization. Codex does not edit UI, except API-client types requested by the frontend lane. Publish shared API contracts in the assigned issue before either lane implements them.

Replace old process tiers with #328: Tier0 UI/docs needs CI, automatic staging and self-check, with PR description only and no receipts; Tier1 API/additive non-money changes needs tests, staging self-check and one short issue comment; Tier2 money, GPU/queue/DBOS, auth/isolation or deletion needs safety tests and the relevant staging acceptance checklist, then Claude acceptance. Keep at most one issue in progress plus one in review per lane. Do not wait between routine staged milestones. Production promotes accepted identical artifacts on Tuesday/Friday; no direct main deployment or production testing.

Original order (continued by CR41): fix #324 first; time-box the #328 dead-code/test/process simplification to one day, with AGENTS at most4KB and PR/push CI targets8/12minutes; then #304 → #305 (WorkOS, existing `inkseq/workos-*` credentials) → #306 → #307 (Stripe test mode only, existing `inkseq/stripe-test`) → #308. Inventory actual live dependencies before deleting legacy providers/scalers and their tests/docs. Keep money/GPU/queue invariant coverage. Legal/live-payment and credential/provider-account decisions remain with the owner.

After M1, use one separate USD20 staging GPU acceptance task for #295 actual in-flight restart/no duplicate submission or charge, real credit hold/capture/release and remaining #267 scenarios. Use RunPod if Vast has no capacity. Every test rental retains max_price, idle stop, maximum runtime and stop-at-completion; do not mutate owner production machines or replay old financial operations. Claude signs off before a Tuesday/Friday production promotion. Source: owner continuation2026-10-19 and #328.

## CR-20261020-40 — dedicated staging QA identities

Owner inserts the [#304 QA supplement](https://github.com/inkseq/h3-studio/issues/304#issuecomment-6267219196) after the current #305 PR, within M1: fixed password accounts `qa-admin` (admin) and `qa-user` (user), staging only. Generate random passwords locally and write plaintext only to the gitignored `h3-studio/.local/staging-qa-accounts.json`; no secrets in GitHub, logs, argv/environment or production. Provisioning is idempotent, refuses production, and never resets mismatched existing credentials/roles/status. Use these test identities for Claude's browser acceptance instead of real users. After #306 adoption, grant `qa-user` exactly 2,000 staging test credits once through the audited ledger; retain the normal USD20 per-task GPU safeguards. Reply on #304 when the private local file and staging accounts are ready. This explicitly authorizes creating these two test credentials, not rotating any existing account or touching production.

## CR-20261020-41 — account isolation, identity, welcome grant and QA GPU window

Owner's current order supersedes CR39's remaining sequence: first #306 account-spending isolation (new WorkOS accounts must not inherit tenant/legacy exposure); then #305 additive `display_name`/`email` in `/api/auth/me`; then #306 publish welcome200 through the audited immutable pricing/policy path with disposable-domain exclusion. Preserve the original QA2000 grant, verified-new-user insertion-only semantics, ledger rows and unknown genuine liabilities. No retroactive regrant or invented settlement.

Then run one separate USD20 staging test task using `qa-user`: #295 in-flight restart without duplicate submit/charge, #306 real hold/capture/failure/queued-cancel release, and remaining #267 scenarios. Use RunPod if Vast has no5090. Retain max_price, idle stop, maximum runtime, task accounting and stop-at-completion; never touch owner production machines. Next #353 idempotent thumbnail backfill (staging first, production only at promotion; originals unchanged), then #304 non-admin tier routing/model-name projections and Agent docs. Claude owns parallel UI changes and their synced PRs.

Proceed without owner relay or per-step approval. Read new Claude/owner comments before each task; label each actually staged, ready Tier1/2 slice `needs-claude-acceptance`; Claude comments and sets `accepted`/`changes-requested` on the two-hour pass. Production still requires the existing sign-off and identical-artifact promotion boundary. Credential/provider-account changes, budget overrun and permanent user-data deletion remain explicit owner decisions. Source: direct owner continuation2026-10-20 and latest #306/#305/#304/#353 comments.

## CR-20261020-42 — audit follow-up: reporting, design review, launch order

Owner adopts the Claude read-only audit (2026-10-20). Owner handoffs for Tier1/2 add a short flow-change and verification-level summary plus 1-3 owner technical-understanding points from the real code; Tier0 stays one or two lines. Tier2 designs compare fix-only, small change and larger rework before implementation; Claude reviews independently and disagreements go to the owner. New infrastructure components (queue, scheduler, storage, GPU provider, table family) need owner approval; prefer deletion and reuse. Order: promote the accepted staging batch (owner approved), #353 production backfill (approved), launch GPU capacity via one operator-held machine while a staging cold-start soak proves unknown allocations clear, one go-live checklist issue, then the frontend repo merge into `yingxu` (approved) and the #347 editor with a Voices entry point. The real execution-failure refund test is skipped (owner). Source: direct owner chat 2026-10-20.

## CR-20261020-43 — production GPU: no authority expiry, demand start up to two machines

**Operating limits amended by CR-20261022-53:** explicit adoption adds an editable
tenant supplier-cost admission ceiling and operating limits. No production
configuration is changed by that code's deployment; no-expiry and accepted-work
continuation remain.

Owner supersedes the time-boxed production authority renewal (#193 option 2) and the operator-held launch machine in CR42. Production FL/REF generation authority has **no expiry**; do not add time-boxed renewal or a total/daily spending cap for production user generation. Generation is cheap; supply it generously. When creatable work is queued and fewer than **two** production machines are running or starting, the system starts one automatically; never more than two at once. The only spend guard is against waste: keep the per-machine hourly price ceiling (avoid mispriced offers), stop a machine after its idle window with no queued or running work, release reservations of provably unallocated starts, and never leave a machine running without jobs. The admin console shows running/starting machines and how many generation jobs are queued. supervan is no longer required to start machines manually; manual operator start/stop remains available. Staging/test GPU work keeps the USD20 per-task safeguards (CR-20261018-32). Source: direct owner chat 2026-10-20.

## CR-20261020-44 — handoff: decisions reach Codex, explicit waits, clean labels

Owner adopts three Claude process fixes after CR43 nearly missed an in-flight renewal. (1) Any session that records an owner decision also gives the owner a one-line relay for Codex; Codex re-reads `workflow/owner-changes.md` on origin/main before any production write or spend. (2) Codex and Claude end every turn with `Waiting on: <who/what, #issue>` so the owner sees when to say "continue". (3) Labels replace, never stack: `needs-claude-review` for Tier2 option reviews, `needs-claude-acceptance` for staged slices; Claude sets `accepted`/`changes-requested` and removes the request label. Source: direct owner chat 2026-10-20.

## CR-20261021-45 — slow-download protection and the original C2 replacement

Owner adds the latest #193 download time-box and minimum advertised-bandwidth conditions before the demand-start soak can close. Keep the Tier2 options/review gate for the bounded controller/offer-selection/admin-visibility change; baking weights, a persistent cache or daytime warm capacity remains a later owner cost decision. Source: direct owner continuation and Claude comments6296849470/6296879928 on2026-10-20 UTC.

Owner explicitly orders stopping staging test Vast instance56923906 (physical machine34612, Vietnam, slow cross-border download) through the **existing guarded operator stop**, never the Vast console. Record its actual timings as a slow-download outlier. Then rerun the original cycle2 on a US/EU offer with good advertised download bandwidth, inside the same `launch193-cr43-20261021` USD20 task; retain max_price, idle stop and maximum runtime. Preserve original job/attempt/input/credit-hold identities and existing billing. An allocated slow host is not provably zero-spend; keep its unsettled liability until actual billing evidence. No production GPU action or activation before accepted staging soak. Source: direct owner decision2026-10-20 UTC.

## CR-20261021-46 — wake after a Claude review wait

**Superseded by CR-20261022-52:** its persistent watchdog and local busy lease replace this stop/pause/resume scheduling rule. The original decision below is historical; its authority limits remain.

When this session actually stops awaiting Claude review or acceptance, schedule one same-thread30-minute review-check heartbeat. Read the original issue's latest comments/labels and continue the already-authorized work when review arrives. On receiving new work or resuming implementation, pause that check first; enable it again at the next real review wait. Reuse the existing check instead of stacking automations. No review outcome expands spending or production authority. Source: direct owner instruction2026-10-21 Australia/Sydney.

## CR-20261021-47 — promote as soon as accepted

Owner (direct chat 2026-10-21, "为啥周五发布？尽快啊"): drop the Tuesday/Friday production window. Promote the identical accepted artifact bytes as soon as Claude has signed off, any day, through the existing promotion path, rollback target and checks. Applies now to #193: promote accepted `498e073` and staged frontend `d6806b8`, then perform the audited CR43 production activation. Everything else is unchanged: no rebuild, Tier2 sign-off, rollback evidence, and owner approval for live money, credentials and new infrastructure. Source: direct owner chat 2026-10-21.

## CR-20261021-48 — the wake heartbeat covers every wait and re-reads the rules

**Superseded by CR-20261022-52 for scheduling and wake entry:** keep the watchdog enabled, check local activity first, and exit on busy/no new events before loading rules/issues. Re-read current rules/issues before acting; do not follow the historical pause/read-rules-first instructions below.

Amends CR46 after the #193 promotion was held for "the Friday window" although the owner had removed it (CR47). The heartbeat had been paused because the session was waiting on a date, not on Claude, and its prompt hard-coded the old Tuesday/Friday rule.
- Keep the single 30-minute same-thread heartbeat on whenever the `Waiting on:` line names anyone or anything other than this session: Claude, the owner, CI, a deploy, a date or an external provider. Pause it only while actively working, and re-enable it at the next wait.
- Every wake first reads origin/main `AGENTS.md` and `workflow/owner-changes.md`, then the claimed issues' latest comments and labels. If a new owner decision or review removes the blocker, resume the authorized work immediately.
- The wake prompt must not restate rules, dates or windows. It points to the files above. Remove any hard-coded rule from existing wake prompts.
Other CR46 limits are unchanged: one heartbeat, no stacking, no new spending or production authority. Source: direct owner chat 2026-10-21.

## CR-20261021-49 — Telegram linking reuses Agent Connect

Owner assigns the #382 backend addition after the existing frontend and bot work: grant preview and exchange with bot-first nonce binding and an owner-visible claimed Telegram user. Reuse existing Agent Connect grants, PATs, connection controls, scopes and budgets; no new login authority. Claude approved the small extension in [review6307080701](https://github.com/inkseq/h3-studio/issues/382#issuecomment-6307080701): preview takes the code in a POST body, never a query string; expose no owner username/email; preserve exact legacy fingerprints and one-use, same-material recovery. Claimed Telegram identity is unverified metadata, inspectable and revocable by the original owner. Independent security review and Claude staging acceptance precede production promotion. Source: direct owner request2026-10-21 and the linked review.

## CR-20261021-50 — prepare existing owner Stripe LIVE setup; purchases stay OFF

Owner authorizes importing the existing restricted LIVE key from the specific protected local drop into production's existing secret-store pattern, deleting only the unchanged source after verified durable readback, and wiring the actual LIVE prices and webhook. No broader key, key rotation, LIVE product modification or purchase is authorized. Claude approved option2 in [review6306371858](https://github.com/inkseq/h3-studio/issues/374#issuecomment-6306371858): reuse existing payment/event tables and audited catalog, ship compatible current/rollback readers before a guarded successor to frozen005, verify exact mode/account/origin isolation on staging, then promote identical artifacts. New Checkout and credit charging remain OFF until the separate audited enable and owner's real-card loop. Keep async methods disabled pending hosted acceptance. Report an exact missing restricted-key permission rather than working around it; store any authorized returned webhook signing secret only through the protected flow. Source: direct owner request2026-10-21 and #374 owner setup6306275254.

## CR-20261021-51 — overnight ordered backend/operations batch

Owner directs the overnight sequence: #374 approved host LIVE wiring (Claude6315459515), staging before production; #395 WorkOS logout_url and sign-up/sign-in hint; #409 credit-mode generation-budget onboarding; #347 filmCuts export inclusion and order/trim/exclusion fixture; #415 sign-out-all and self-service deletion design; #408 speed/detach/fades/crossfade design; #353 missing-only production historical thumbnails; then CA/certifi/Node patch work.

For each ready completed slice, post its exact staged/operational receipt and replace labels with needs-claude-acceptance, then continue the next item. Tier2 compares options and awaits Claude design review before implementation; blocked items record the actual dependency and are skipped while other authorized work advances. Charging and LIVE Checkout stay OFF; the WorkOS activation hold is superseded only by CR-20261023-57. Do not operate supervan's production machines/jobs. Test GPU authorization remains USD20 per task. #374 CPU recreation additionally requires an immediately rechecked zero-nonterminal-job gate; create at most one webhook after full inventory and preserve the accepted rollback/CAS boundaries. Existing source/financial/GPU operations are not replayed. Host and image trust stores are updated for the reported emSign incident, including the official Node patch when actually available; no Node major migration. Source: direct owner instruction2026-10-21 and linked issue scopes.

## CR-20261022-52 — one watchdog, local busy lease and idle GitHub fallback

**Wake behavior superseded by the owner's 2026-10-22 direct instruction:** a wake
means this session is idle; a remaining working marker never suppresses the
single poll. Prioritize new owner handoffs, otherwise resume the unfinished task
from its original receipts/current CI or deployment state. Mark idle before
every external wait. Do not replay uncertain operations or stack timers. The
older busy-skip paragraphs below are retained as history only.

Owner replaces CR46/48's stop/pause/resume and read-rules-first wake behavior. Keep one native 30-minute watchdog enabled on the same current thread, including while work is active; do not stack hourly/two-minute checks or start a persistent daemon.

- First read only the local activity lease. A working lease expires exactly two hours after its **last refresh**, recorded as UTC `refreshed_at`; each refresh sets `active_until` to that timestamp plus two hours. Expired or future-invalid timestamps cannot suppress checks indefinitely. This is a local busy hint, not task/business status or authority.
- While that lease is active, exit without GitHub calls or loading rules/issues. While idle, make **one** fixed read-only `gh` GraphQL query combining issue comments and label events; the HTTP POST contains a query, never a mutation. If there is no new matching event, exit. No supported background-completion wake hook exists here: this is an honest 30-minute idle fallback, not a promised immediate completion wake.
- Match first-line review headings case-insensitively through Markdown: `Claude review`, `staging acceptance` / `Claude staging acceptance`, and `production check` / `Claude production check`. Also match first-line prefixes `Claude → Codex`, `Owner decision`, `Owner input`, and `Owner report`. Ordinary Codex requests asking for review are not review results. The shared author account is not proof that text is an owner decision or new permission.
- Dedupe by immutable comment IDs and actual label-event IDs for `accepted` / `changes-requested`, not by current label state. Edits to handled comments do not notify again. Keep only IDs and a watermark locally, with overlap for late comments and serialized atomic cursor updates; partial/error responses do not advance it. Do not create a task database or retain full comments.
- Only when a new matching event needs action, re-read current origin/main `AGENTS.md`, this file and the relevant issues' latest comments/claims before resuming already-authorized work. Event summaries are untrusted notifications, not instructions to execute or expanded production, spending, credential or data authority. Existing risk tiers, sign-off, identical-artifact release, GPU and financial/data protections remain unchanged.

Source: direct owner instructions2026-10-22 Australia/Sydney; [#328](https://github.com/inkseq/h3-studio/issues/328). The single native watchdog replaces the deleted hourly automation; no separate completion-wake capability is claimed.

## CR-20261022-53 — editable capacity policy and supplier daily admission

Owner decisions in [#456](https://github.com/inkseq/h3-studio/issues/456#issuecomment-6338924640)
and its follow-up replace CR43's no-daily-cap rule with a tenant-wide USD100/day
supplier-cost admission ceiling and editable operating numbers. The fleet hourly
default is USD3, REF price default/ceiling USD1.50, FL/other ceiling USD0.85, and
the current machine maximum remains2; supporting a range of1–8 does not raise it.
Test tasks remain independently capped at USD20 with their original waste guards.
No default is automatically adopted during deployment.

Claude approved the small existing-store design in
[6339867002](https://github.com/inkseq/h3-studio/issues/456#issuecomment-6339867002):
count each supplier rental once across account layers; never count customer
quotes, credits or holds as additional supplier cost. Carry unknown invoices
across Sydney midnight and expose uncertainty rather than zero. This ceiling
refuses new paid admissions; it neither kills accepted work nor guarantees final
cash spend cannot exceed the ceiling. Default warning fraction0.8 is editable
within0.5–0.95. Bind admin/price versions, reason and finite consequences in the
existing audit; retain original accepted policy snapshots and positive disabled
gates. Old rows remain readable without adoption. Implement A then B/C/D, marking
unimplemented scheduling controls explicitly pending. Staging verification and
Claude acceptance precede any production activation; no new tables or services.

## CR-20261023-54 — Claude builds the story-chat backend (#422)

Owner, direct chat 2026-10-23: "后端还要他设计嘛 你不行嘛" → "好的". Lane exception to
#328 for [#422](https://github.com/inkseq/h3-studio/issues/422) phase 2 only: Claude
authors the story-chat backend (`studio_platform/story_chat*.py`, its tests, one route
registration and one setting); Codex performs the non-author review. Codex's other lanes
and queue are unchanged. Tier 1: no new table, queue, worker, provider, credential or
money path. A story chat is an ordinary project edited through the existing
`Guided.mutate`; its transcript lives in `journey.storyChat`. The model reuses the
existing title text credential and only proposes generation, which the user confirms
through the existing generation routes. On by default in code (#469);
`INKSEQ_STORY_CHAT=0` turns it off. Production receives it only through a normal
promotion of a Claude-accepted exact tuple; there is no separate flag write.

## CR-20261023-55 — story-card image credits reuse the customer ledger

Owner requests implementation of [#463](https://github.com/inkseq/h3-studio/issues/463),
option2 approved in [6345957205](https://github.com/inkseq/h3-studio/issues/463#issuecomment-6345957205).
Use the existing immutable price revisions, owner spending lock, credit accounts and
hold/capture/release entries; no synthetic video jobs, table family, queue or provider.
Recommend5 credits per image, explicitly published through the audited admin price
path. Legacy revisions stay unpriced for images; deployment/read does not adopt5.
Freeze owner/project/card/request/input hash/original actor and price. Capture requires
a durable owner-bound attachment; release cannot override a committed picture. One
terminal outcome only, including retries, changed prices and charging subsequently OFF.
Claude retains the image route/UI and its duplicate-provider-call fencing; Codex owns
the money primitive. No automatic stale-hold release, production activation or real
provider call follows from this implementation. Separate integrated staging acceptance
and Claude sign-off remain required before charged images become usable.

## CR-20261023-56 — staged title-key hydration and gated story-chat launch

Owner decisions relayed on [#422](https://github.com/inkseq/h3-studio/issues/422)
in [6353525309](https://github.com/inkseq/h3-studio/issues/422#issuecomment-6353525309)
and [6353528761](https://github.com/inkseq/h3-studio/issues/422#issuecomment-6353528761)
authorize copying the existing central `gemini--user-supplied` configuration to
staging's existing protected title-secret path, not a new provider credential or
account. Claude approved the small target-scoped hydrator in
[6353640343](https://github.com/inkseq/h3-studio/issues/422#issuecomment-6353640343).
Staging reads only `/sixnine/staging/google-titles`, with no production fallback;
production's source stays unchanged. Preserve atomic runtime replacement and
disabled optional-config behavior. Host-helper installation and app configuration
are separately reviewed, receipt-bound steps; app recreation requires zero
nonterminal work. A config read is not a real-model acceptance.

Production story chat may be enabled only after Claude's explicit real-model
staging acceptance, using the identical accepted platform/frontend tuple and a
retained rollback target. The accepted tuple must include the admin picture
switch from PR466, verified OFF; do not enable production before that switch is
present. Enablement is the code default from #469 (no deploy-contract change or flag
write), reusing the existing title mount/config path through normal guarded promotion
of the identical accepted tuple. Preserve billing, Checkout and auth activation
state. Image-price publication and charging remain separate CR55 decisions;
neither this change nor deployment enables them. Prepare production now, execute
only after the staged sign-off, and refuse state/identity drift before writes.

## CR-20261023-57 — enable existing production WorkOS Google sign-in

Owner directly authorizes guarded production import and hosted sign-in activation
for [#305](https://github.com/inkseq/h3-studio/issues/305), following prerequisites
[6359710953](https://github.com/inkseq/h3-studio/issues/305#issuecomment-6359710953)
and [6359789675](https://github.com/inkseq/h3-studio/issues/305#issuecomment-6359789675).
Use the existing Windows Credential Manager target `inkseq/workos-prod` and
`tools/import_workos_credentials.py`; no new key, rotation or secret-drop file.
The exact production client is `client_01M592BTTGGDTK1A9WCRJGFGRQ`, callback
`https://inkseq.com/api/auth/workos/callback`, login
`https://inkseq.com/api/auth/workos/start`, and sign-out `https://inkseq.com/`.

This supersedes only the earlier WorkOS-OFF requirement. Preserve password login,
billing, Checkout, the picture switch, accepted artifacts and rollback. Do not
edit account emails or identities directly: superdan and supervan each use their
password session and Settings → Account → Link Google sign-in; the verified
provider callback sets the Gmail. Before activation, include Claude's accepted
password-entry frontend fix from design#180 ([6359898950](https://github.com/inkseq/h3-studio/issues/305#issuecomment-6359898950)).
Use independently reviewed, receipt-bound configuration CAS and immediately
recheck zero nonterminal work before CPU recreation. Post the exact production
receipt on #305; Claude performs the anonymous production read-back, while users
complete their own authenticated linking. No paid generation or billing
activation is authorized by this decision.

## CR-20261024-58 — free production pictures, existing Gmail admin and overnight preparation

Owner explicitly authorizes the existing audited story-chat settings API to set
production `pictures_enabled=true`, reason `owner: open pictures`. This supersedes
only the earlier picture-OFF launch condition: credit charging and Stripe Checkout
remain OFF, pictures are temporarily free to users, and the owner accepts provider
cost. Include `image_credits=5` with rates2/4/12 in revenue preparation; publication,
charging and LIVE Checkout remain separate guarded actions. Do not infer payment
enablement from the picture switch. The completed switch receipt is
[#422/6370033341](https://github.com/inkseq/h3-studio/issues/422#issuecomment-6370033341).

Owner also supersedes the earlier instruction to wait for Google linking before
granting production admin: grant only the existing Google-created Dan Mo account
`danmo2060@gmail.com` the admin role through the audited human-admin API, reason
`owner Gmail admin`. No new account, merge, email/provider/credits/works change;
superdan remains admin. Completed role-only receipt:
[#305/6370152170](https://github.com/inkseq/h3-studio/issues/305#issuecomment-6370152170).

The overnight priority is revenue preparation without turning charging or LIVE
payments on, then production Film gate preparation, backend Sentry and staging QA
journeys in CI, one isolated staging restore drill, and #481. For #481, owner
preauthorizes production conversion after accepted staging checks and Claude's
delegated review of a reasonable per-account production census (about26 superdan
sessions including trash and14 supervan). Skip/list uncertainty; never force or
resurrect trash. Option2 purge redaction is not a prerequisite; retain option1's
permanent-purge refusal. Exact source hashes, current-state checks, original
operation receipts and the normal accepted-artifact production gate still apply.
Revenue wins conflicts. Source: direct owner decisions2026-10-24 and
[#481/6369552441](https://github.com/inkseq/h3-studio/issues/481#issuecomment-6369552441).

## 2026-10-24 — Quick Create chats can be renamed and removed; Works newest first

Each Quick Create chat is one Works story. The owner asked that chats can be renamed
and removed from Quick Create without deleting the story, and that the Works list
defaults to newest first. Removing only hides the chat (`journey.storyChat.hidden`);
continuing the story in the chat from Works brings it back with its messages. Source:
direct owner message 2026-10-24.

## CR-20261024-59 — generous capacity, warning-only supplier uncertainty, provider qualification

Owner's direct instruction and [#193/6380342993](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6380342993)
supersede CR53's earlier operating defaults and the conservative A1 review conditions.
Explicit audited adoption targets fleet USD15/hour, a hard maximum of10 running or
starting machines, REF USD3/hour per machine, and the existing USD100/day supplier
ceiling. Keep per-machine max_price and idle stop. Unresolved supplier references
remain visible warnings and never independently prevent new admission; only
confirmed supplier exposure can reach the daily ceiling. Do not invent invoices,
zero unknown costs, delete evidence or replay settlements. Snapshot settings and
profile versions before adoption. One quick staging adopt/read-back precedes
same-day production activation; the read-only classification is not a gate.

Widen ordinary queue, per-owner request and transient-admission retry limits by a
large margin, retaining protection against extreme anomalies. This does not waive
authentication, ownership, CAS, idempotency, immutable accepted work or the separate
USD20 test-task budget. The prohibition on operating supervan's production machines
or jobs is superseded only for recoverable incident handling by CR-20261024-60 below.
Production charging and LIVE Checkout remain OFF.

Per [#517/6380346694](https://github.com/inkseq/h3-studio/issues/517#issuecomment-6380346694),
B1's stock-pinned dstack0.22.3 to0.22.5 upgrade starts alongside A2. Stage it and run
one Vast cold cycle as the regression check, then qualify RunPod, Lium and CloudRift
on staging under USD20 per item. Each test rental retains max_price, idle stop,
maximum runtime and stop-at-completion. Reuse existing protected provider profiles;
missing/new credentials, provider accounts or budget overruns still require owner
action. Production enablement of each new provider is now governed by CR61 below.
Keep Tier2 independent safety review and exact-artifact receipts; no new service,
queue or financial repair is implied. Source: direct owner instruction2026-10-24.

## CR-20261024-60 — recoverable abnormal-machine stops and upgrade priority

The owner first authorized stopping the stuck node `1c2db2ac` / Vast instance
`57459470`, confirming provider removal without waiting indefinitely for the
unreachable worker to drain, and failing the original job `9ef33766` through normal
reconciliation without automatic retry; supervan will resubmit. Keep queued job
`569a6d94`. Source: [#193/6381680638](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6381680638).

The owner's subsequent direct instruction authorizes stopping abnormal machines,
including production machines, without a separate per-stop question, provided
accepted tasks and queued work remain recoverable. This narrowly supersedes CR59's
blanket production-machine prohibition, including for supervan's machines; it does
not authorize discarding accepted work. Give upgrades priority when production is
recoverable, without adding another approval or process gate.

The latest owner follow-up reports that the machine was manually stopped and asks
to return to development. Do not issue a duplicate incident command or make further
incident diagnosis an unnecessary prerequisite for development and upgrades;
preserve the durable queue/task recovery state and original receipts.

Preserve original jobs, attempts, operation IDs, recoverable queue/task state and
R2 assets. Check uncertain lifecycle or generation results against their original
receipts before further action; never blindly replay generation, rentals, stops,
settlements or releases. Retain confirmed spend and unknown supplier liabilities
separately. Charging and LIVE Checkout stay OFF; cost caps, the USD20 test-task
budget, max_price, idle/max-runtime safeguards and existing release/rollback
mechanisms remain unchanged. Source: direct owner instructions2026-10-24 and the
linked #193 decision; incident results remain in the original issue receipts.

## CR-20261025-62 — same-job timeout recovery and production incident priority

The owner directly prioritizes recovery of supervan's stuck production work over
B2. Recover the original jobs `9ef33766`, `987615fa` and `438a9f31` by fencing
their lost attempts and re-queuing the same jobs immediately, retaining original
queue age, accepted inputs and holds. This supersedes CR60's earlier fail/resubmit
wording. Removal proof is required only to reconcile supplier billing; it must not
delay the fenced replacement attempt. Never invent a supplier invoice or replay
an operation with an unknown result.

Adopt the Small timeout rule: no engine progress for10minutes, or runtime greater
than max(2×expected,30minutes), marks the attempt lost, fences late results,
re-queues the same job and stops its owned node. Keep the hold for one delivered
capture. Permit at most2 automatic reclaims, then fail with an explicit exhausted
reason. Replace a node that is not ready within20minutes. These timeouts are
settings; report every reclaim through the existing Grafana event and a sanitized
Sentry message, and retain the host-failure scorecard and blocklist.

Honor a later explicit user cancellation instead of resurrecting that job.
Use the same resource projection and limit predicate for demand admission and
worker registration; reconcile stale capacity against fresh complete provider
evidence in the existing controller cadence. Alert on a matching paid idle node
with no dispatch for10minutes, queue age over30minutes, or2 zero-service
start/stop cycles within1hour. Diagnose capacity/configuration faults without
renting a replacement; stop and replace only a proven node fault with fenced work.
The repeated zero-service loop pauses the existing demand switch and alerts.
No new timer, service or task store follows from this decision.

The urgent follow-up authorizes releasing the original removed workers' stale
capacity and admitting the already-paid ready node `f7c407c7` / Vast57521189.
Promote/adopt accepted A1 limits if the legacy cap still prevents registration:
fleet USD15/hour, maximum10machines, REF USD3/hour, USD100/day unchanged.
Demand start must check the effective registration limit before spending on a
node. Preserve recoverable work, rollback, max_price and idle stop; charging and
LIVE Checkout remain OFF. No new credential, provider account or budget is
authorized. Sources: direct owner2026-10-25 and
[#193/6384745467](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6384745467),
[#193/6385240655](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6385240655),
[#193/6385265680](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6385265680).

## CR-20261024-61 — conditional production enablement of qualified providers

Owner preauthorizes production enablement of RunPod, Lium and CloudRift per
[#517/6383431565](https://github.com/inkseq/h3-studio/issues/517#issuecomment-6383431565).
This supersedes CR59's separate production decision requirement for these three
providers; no further owner decision is needed for each provider once its
conditions below are met.

Each provider must first pass one staging cold start, generation, R2 delivery,
idle stop and removal proof. Record measured time-to-ready and USD cost in
[#130](https://github.com/inkseq/h3-studio/issues/130), then obtain Claude's
`needs-claude-acceptance` to `accepted` sign-off for that provider. Enable accepted
providers in production with the next normal promotion, retaining exact accepted
artifacts, original receipts and rollback. This authorization is not evidence
that any provider has already passed or been enabled.

Preserve max_price, per-machine and fleet caps, the USD100/day ceiling, idle stop,
the host blocklist and B4 price-ordered selection with fallback on `no capacity`.
Unknown create or generation outcomes still require original-receipt reconciliation,
never a duplicate operation or an inferred no-allocation result. Staging remains
limited to USD20 per change/acceptance task with the existing maximum-runtime and
stop-at-completion safeguards. Reuse existing protected profiles; this decision
does not authorize new credentials/provider accounts or a budget increase.

A provider that fails qualification stays OFF and is reported with its blocker;
it does not block independently qualified and accepted providers. Charging and
LIVE Checkout remain governed by their separate owner decisions.

## CR-20261025-63 — Claude takes over Codex's lane; production diagnosis and alerts

Owner, 2026-10-25 (chat): Codex is out of tokens, so Claude takes over its open
work: release helpers, staging/production promotion paperwork, and backend fixes.
Claude may run read-only diagnosis on the production host (SSM/SSH reads, no
writes) when the owner asks for a root cause. The REF "runtime unconfirmed" root
cause and the split of the fix are in
[#193/6386153093](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6386153093).

The production operator alert topic `sixnine-production-operator-alerts` had no
subscriber, so alerts such as `job_stuck` reached no one. On the owner's
explicit "订阅" (subscribe), Claude subscribed the owner's existing staging-alert
email; the owner confirmed it the same day. No other alert, threshold or
destination changed.

Owner product directions, same day: the story list collapses to an icon rail when
dragged narrow; item menus are plain rows; an open tab offers "Refresh" when a
newer build is served. Production promotion still requires Claude's staging
acceptance of the exact candidate; charging and LIVE Checkout stay OFF.

## CR-20261025-64 — long shots with live engine progress are not reclaimed at 2× expected

Production evidence, 2026-10-25: job bbee9b98 is a 15 s 1344×768 REF take (362 frames).
It ran past 60 min at full GPU with fresh engine heartbeats, and attempt 12 was
stopped by the runtime bound while it was still generating. Claude, as the
current owner of the flow, refines CR-62's timeout rule:
- silence (no positive engine heartbeat for 10 min) is still reclaimed at once;
- without a fresh heartbeat, the original `max(2 × expected, 30 min)` bound applies;
- with a fresh heartbeat, a run may continue up to a hard 3 h ceiling.

The reclaim limits (max 2 automatic reclaims, fenced old attempts, charge once)
are unchanged.

## CR-20261025-65 — TaoMate 5 steps is the only user-facing H3 sampler

Owner, 2026-10-25 (taomate H3 session): "taomate 5步以后作为我们的default选择 模型直接都用这个", then
"以后只用taomate". Evidence on Lium RTX 5090 (WanGP 0e58385, pruned rank8 INT8): FL 480P across four
prompts and a 15 s 768P long shot (single window + TaoMate 5 steps: 8.6 min vs 42.5 min for 20 steps).
- New immutable profile `h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7` = controls-v6 plus one pinned
  LoRA (`loras/minimax_h3_taomate_3step_lora_avg_rank_19_bf16.safetensors`, 181697688 bytes, sha256
  de9663d9…ab6a7) at multiplier 1.0, guidance 1, steps fixed at 5. No other turbo LoRA is added.
- The plain 20-step profiles stay in the catalog for internal debug/fallback only; users are not offered them.
- Step skipping stays `none` by default; 5 s windows stay optional for long shots.
- Before production: staging GPU checks for REF2VA with TaoMate, 768P at 5 steps, and a 15 s long shot.

## CR-20261026-66 — burned-in subtitles are advisory, never an automatic re-roll

Owner, 2026-10-26, #575 comment6398437828 and #580 C2: detect subtitles and tell
the customer; let them keep, regenerate or trim the take. This supersedes #575's
original automatic-re-roll proposal. CPU OCR runs on the GPU node after generation
and before upload, sampling the bottom third and comparing with the original
dialogue. Persist bounded take-level quality flags/confidence/timestamps; no raw
OCR text in public evidence. An unavailable/failed check is explicit, not a clean
bill of health. Detection never re-rents, re-generates, changes a seed, or changes
charging. Optional Gemini verification remains OFF. Claude owns warning UI,
staging acceptance, production promotion and the TaoMate production switch.

## CR-20261026-67 — human-agent review loop and privacy-safe improvement reports

Owner direction relayed in the single plan:
[#580/6408772792](https://github.com/inkseq/h3-studio/issues/580#issuecomment-6408772792),
[#580/6408786752](https://github.com/inkseq/h3-studio/issues/580#issuecomment-6408786752),
and the superseding feedback detail
[#580/6408801323](https://github.com/inkseq/h3-studio/issues/580#issuecomment-6408801323).

Codex's next bounded backend slices are #290 review-loop metrics, versioned
take marks, suggested pre-generation risk checks and post-generation VLM marks,
then redo from accepted marks. Record explicit review outcomes; selected/starred
takes and partial cost records must not masquerade as usability or full cost.
Reuse existing stores and account/project scopes. Suggestions never automatically
apply edits, re-roll or spend. Claude owns the review UI, acceptance and promotion.

Improvement reporting is a later thin API slice using existing authorization and
Sentry feedback, with account/connection rate limits and idempotency. The latest
category set is tool_error, missing_capability, confusing_output, documentation,
other and model_limit. Prompts/media/credentials are excluded by default; optional
context requires the owner's setting. Reports are not automatically posted to
public GitHub. Take marks remain scoped product data. PostHog covers UI usage;
no additional analytics vendor or infrastructure is authorized.

This plan does not enable charging or Checkout, authorize new credentials, enlarge
GPU budgets, or bypass production gates. The #290 source audit and proposed metric
contract are in `docs/REVIEW-LOOP-TELEMETRY.md`; they are not implementation or
deployment receipts.

Owner confirmation in this session and Claude's
[#625/6409187825](https://github.com/inkseq/h3-studio/pull/625#issuecomment-6409187825)
approve the metric definitions and existing-store Small approach. Add a separately
named behavioural first-take-shipped rate; never merge it with explicit usability.
Whole-take verdicts are explicit keep/redo/null with revision, CAS and actor kind;
picks, stars and range-change marks do not set them. v1 reads one owner at a time;
all-owners aggregate is a later, content-free view. Frontend remains Claude's lane.

## CR-20261026-69 — creator model controls belong to the TaoMate profile

Owner, 2026-10-26, backend follow-up to inkseq-design#266, tracked in #649:
creator requests retain quality tier, mode, duration, seed and copies; stored
shot/story/account internal controls must not override TaoMate v7 defaults or
cause stale steps=50 to fail new compilation. Filter new creator inputs before
compiling; TaoMate remains five steps. Explicitly permitted human-admin internal
API overrides continue to use strict profile validation. Preserve accepted
requests and historical receipts. Report staging/production incompatibility
counts read-only and propose cleanup separately; no production data write is
authorized here. Tier2 independent review and usual release gates apply.
Charging and Checkout remain OFF. CR68 is reserved by the separate maintainability
ratchet PR #644; this change does not replace that pending decision.

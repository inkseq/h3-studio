# Scheduling scenarios: where a job runs and when a machine starts

The scenario list maps one to one to `SCENARIOS` in `studio_platform/scheduling/decide.py`. If a rule changes, change its table row and behavioral test. The six hosted decisions use that table's pure predicates at the existing call sites. The other five rows name the existing claim, backoff, fallback and idle owners; they do not introduce a second scheduler. Design background is in [PROVIDER-ARCHITECTURE.md](PROVIDER-ARCHITECTURE.md) and [STORY-AUTO-QUEUE.md](STORY-AUTO-QUEUE.md).

Several rows can apply at once. Existing transaction and call order decides what happens: removing a hosted hold only makes work eligible for the original machine admission checks. It does not authorize or guarantee a rental. Capacity, funding, original unknown operations, claims and idle-stop ownership remain authoritative. The baseline clarifications below are recorded in [#899](https://github.com/inkseq/h3-studio/issues/899#issuecomment-6578212797); this refactor preserves behavior.

## What decides

Six signals:

1. Is a person waiting (`interactive`), or is it auto-queue (`economy`)?
2. Is a machine of ours free, or about to be?
3. How many hosted renders ran in the last 15 min?
4. How much economy work is queued?
5. Does the vendor balance allow hosted?
6. Is the lane backing off, and is the vendor window used up?

## Scenarios

| # | Rule key | Scenario and current decision | Decision owner | Existing live evidence |
|---|---|---|---|---|
| 1 | `occasional_click` | No machine, occasional click: hosted chain at the tier wait limit; machine hold lasts until wait + 30 s | `decide.hosted_offload` / `hosted_has_first_claim`, applied by `rebind` | Passed on staging |
| 2 | `sustained_clicks` | 5+ interactive hosted attempts/reservations in the window release the machine hold; hosted may still take work while a machine warms | `rebind.used_in_window` / `hold_for_hosted`, then original demand admission | Not yet seen live |
| 3 | `ready_machine` | A free compatible machine claims before the hosted wait limit; dedicated readiness also prevents an extra Modal start | `WorkerControl.claim` / `modal_admission.dedicated_ready` | Built |
| 4 | `small_economy_backlog` | Fewer than 40 economy shots waiting: one hosted input at a time, using Vivix where the catalog allows it | `rebind.offload` / `economy_in_flight` | One job passed on staging |
| 5 | `large_economy_backlog` | 40+ economy shots waiting: no new hosted offload; release the machine hold. Threshold is recomputed each tick | `rebind.economy_backlog` / `hold_for_hosted`, then original demand admission | Not yet seen live |
| 6 | `click_during_economy` | The click takes the next free slot; past its wait limit queued work can go hosted. A running render is never interrupted | `TaskQueue.schedule_order` / claim | Not yet seen live |
| 7 | `no_machine_can_start` | Hosted work still follows its existing eligibility. Machine lanes back off; unknown allocations reconcile the original operation | `dstack_demand_start.start_once`, original provider journals | Not yet seen live |
| 8 | `low_vendor_balance` | Under USD 1 prevents hosted work and releases its machine hold; exactly USD 1 or unknown balance allows hosted | `rebind.balance_allows_hosted`, then original demand admission | Not yet seen live |
| 9 | `hosted_fallback` | Confirmed refusal/failure or known-cost deadline moves to the next model, then original machine route. Unknown submission is not repeated | `WorkerRunner._reroute` / `_past_deadline`, `rebind.next_plan` | Built |
| 10 | `window_full` | A full environment or owner share prevents further hosted offload. Machine hold still follows rows 1, 2 and 5 | `decide.hosted_offload`, original `hold_for_hosted` order | Not yet seen live |
| 11 | `backlog_finished` | Idle stop only with fresh safe-to-stop proof; active work, unknown obligations, holds, funding and original ownership still apply | `DstackController._reconcile_observed_node` / `dstack_capacity.idle_action` | Built |

### One primary behavioral check per row

These are isolated fake-provider/temporary-database checks, not new live acceptance claims. `test_scheduling_decide` also verifies that the eleven document keys stay aligned with the runtime table. The older Modal-first/lookahead decision cases remain in that module.

| Row | Primary behavioral test | Additional failure/boundary coverage |
|---|---|---|
| 1 | `test_hosted_rebind.OffloadDecisionTests.test_scenario_01_exact_wait_and_grace_boundaries` | Real rebind retains money/plan fields; stale or leased input does not move |
| 2 | `test_hosted_rebind.RebindLedgerTests.test_scenario_02_reservations_four_five_six_release_the_hold` | Replaced attempts count; economy work does not warm machines for clicks |
| 3 | `test_platform_control.ControlTests.test_scenario_03_ready_machine_claims_without_a_route_change` | Exact pool/config/lease matching; `test_modal_admission` dedicated-ready prevents duplicate create |
| 4 | `test_hosted_rebind.RebindLedgerTests.test_scenario_04_economy_goes_to_vivix_at_once_and_one_at_a_time` | Economy switch/768P, catalog aspect choice, in-flight work |
| 5 | `test_hosted_rebind.RebindLedgerTests.test_scenario_05_real_backlog_boundary_releases_only_the_machine_hold` | 39/40/41 real queued rows; original capacity/funding admission still applies |
| 6 | `test_hosted_rebind.RebindLedgerTests.test_scenario_06_click_claim_does_not_preempt_running_economy` | Active attempt and accepted upstream task unchanged; queued click wins next claim |
| 7 | `test_dstack_demand_start.DemandStartTests.test_429_planning_backoff_is_durable_and_respects_retry_after` | Unknown before/after start commit preserves frozen preview/original allocation; no rerental |
| 8 | `test_hosted_rebind.RebindLedgerTests.test_scenario_08_low_balance_sends_jobs_to_our_machines` | Reserve 0.999/1/1.001/unknown; four-hour cache and failed reads |
| 9 | `test_hosted_rebind.RebindLedgerTests.test_scenario_09_exact_deadline_moves_on_and_keeps_its_charge` | Real worker refusal/refunded failure, chain exhaustion, recorded uncertain-submit cassettes |
| 10 | `test_hosted_rebind.OffloadDecisionTests.test_scenario_10_full_window_preserves_oldest_first_owner_share` | Environment cap and owner 9/10/11; zero window; existing grace/economy distinction |
| 11 | `test_dstack_controller.DstackControllerTests.test_hold_preserves_original_deadline_and_expired_idle_stops` | Compatible queue keeps host; unknown stop acknowledgment retains cost and is not replayed |

### Preserved baseline details

- The 15-minute count includes all submitted attempts, including failed/replaced ones, plus current queued/claimed hosted reservations. The timestamp boundary is inclusive for submitted attempts; reservations have no age cutoff. Economy is excluded from the interactive count.
- Economy backlog is tenant-wide queued non-hosted economy work, not only jobs compatible with one current profile. There is no sticky large-backlog mode: if the count falls below 40, hosted can become eligible again.
- A full window does not create a universal grace timer. Five interactive attempts/reservations already release the hold immediately. Economy below 40 can remain held indefinitely without checking window occupancy, a live hosted slot or an in-flight economy render. A configured zero window disables the hold immediately.
- A ready machine is expected to claim before the wait limit; hosted selection does not add its own machine-readiness query. Database locks, queries, claim/fence checks and admission order remain in their existing owners.
- At a hosted deadline, fallback requires a known original provider charge. The old vendor render may continue and be charged; there is no cancellation API and its late output is not collected. One current logical attempt does not promise that an old physical render has stopped.

## The numbers, and where they come from

All figures are for a 5090 at about USD 0.65/h, 480P, 5 s, about 20 s per clip (about USD 0.004 per clip), and a boot of about 23 min (about USD 0.25). Faster boots lower both break-even points.

| Rule | Value | Why |
|---|---|---|
| Wait limit | fast 30 s, standard 60 s, refine 120 s | Owner's cold-start goal (#918) |
| Machines hold back | Wait limit + 30 s | Stops a machine booting for a job about to go hosted; found on staging |
| Clicks start a machine at | 5 hosted renders in 15 min | Against turbo (USD 0.038) a machine pays back after about 7 clips |
| Economy starts a machine at | 40 shots waiting | Against Vivix (USD 0.01) each clip saves about USD 0.006, so about 40 clips |
| Hosted deadline | 300 s; Vivix 30 min | A move pays twice, and the vendor's batch queue measured 250-290 s |
| Hosted window | Each environment's share in `everyframe-routing.json`; shares must total 40 or less | One key, one vendor limit (owner 2026-11-01) |
| Balance | Read every 4 h; stop under USD 1; warn under USD 10 | Owner 2026-11-01. A refusal costs nothing, so a late read is safe |

## Rules that always hold

- One current logical attempt per job. A hosted submit is never repeated on an uncertain answer; known-cost deadline fallback retains the previous render's charge as described above.
- The user pays the tier's credits once, whatever the route.
- A rebind happens only while a job is queued and unsubmitted.
- Models that refuse faces or IP are not in the catalog.
- H3-based hosted models and our v9 look nearly the same given the same references (owner 2026-11-01), so one story may mix routes.

## Vivix sizes

Vivix ignores the requested resolution, so its output is scaled to fill the take's size and centre-cropped. The economy tier is always 768P at 2 credits per second (owner 2026-11-02), and Vivix serves only that tier: it renders one at a time across all vendor customers, so a person waiting never uses it.

| Aspect | Vivix renders | Effect |
|---|---|---|
| 16:9 | 1264x720 | About 6.7% upscale to 1344x768; about 0.3% cropped |
| 9:16 | 720x1264 | Same as 16:9, turned |
| 1:1 | 960x960 | No crop |
| 4:5 | 832x1104 (3:4) | About 6% cropped top and bottom; allowed |
| 21:9 | 16:9 canvas | Would lose about a quarter of the height, so these shots go to turbo |

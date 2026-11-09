"""Scenario catalog for the controller simulator (#1034).

Each scenario scripts machines and the outside world; the simulator checks
the same invariants for all of them. A scenario that fails because of a real
controller rule bug carries ``expected_failure`` with the reason and issue,
so the suite stays green while the bug stays visible. Fix the rule, and the
scenario reports ``unexpected_pass``: then delete its ``expected_failure``.
"""

from capacity_sim import Event, JobBatch, MachineScript, Scenario, incident_scenarios

# Boot of a healthy machine: 60 s allocation + 30 s runtime + 300 s download
# + 60 s model load, about 7.5 minutes before the first claim.
FAST = MachineScript()
GUARD = {"task_limit_microusd": 20_000_000, "max_runtime_seconds": 7200}

SCENARIOS = [
    Scenario(
        "steady_40_fl",
        "40 queued FL jobs, two machines drain them and then stop.",
        hours=6,
        jobs=[JobBatch(0, 40)],
    ),
    Scenario(
        "burst",
        "10 jobs, then 30 more 40 minutes later, on up to four machines.",
        hours=4,
        max_machines=4,
        jobs=[JobBatch(0, 10), JobBatch(40 * 60, 30)],
    ),
    Scenario(
        "no_offers_then_offers",
        "No provider offers for the first hour, then normal stock.",
        hours=4,
        offers=False,
        jobs=[JobBatch(0, 6)],
        events=[Event(3600, "offers_on")],
        # Nothing may start while no machine is offered.
        expect_capacity=False,
    ),
    Scenario(
        "failed_start",
        "First machine fails its model load, second never gets capacity.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 4)],
        machines=[MachineScript(load_fails=True), MachineScript(no_capacity=True)],
    ),
    Scenario(
        "lost_create_reply",
        "The create call times out but the provider did allocate the machine.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 4)],
        machines=[MachineScript(create="reply_lost")],
    ),
    Scenario(
        "machine_lost_mid_job",
        "The provider deletes the first machine while it runs a job.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 3, actual_runtime_s=900)],
        machines=[MachineScript(removed_at=900)],
    ),
    Scenario(
        "collect_fails_then_machine_removed",
        "Collection fails after the run finished, then the provider removes the host.",
        # Staging job 27b6213f: collecting/collection_failed for hours, its
        # worker unknown and its machine destroyed.
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 2, actual_runtime_s=300)],
        machines=[MachineScript(collect_fails=True, removed_at=17 * 60)],
    ),
    Scenario(
        "collect_keeps_failing",
        "Collection keeps failing while the machine stays up and billed.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 2, actual_runtime_s=300)],
        machines=[MachineScript(collect_fails=True)],
    ),
    Scenario(
        "run_turns_terminal",
        "The dstack run fails while idle; dstack then removes its machine.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 6)],
        machines=[MachineScript(terminal_at=20 * 60)],
    ),
    Scenario(
        "restart_while_downloading",
        "An app deploy restarts the controller while both machines download.",
        hours=4,
        jobs=[JobBatch(0, 8)],
        events=[Event(4 * 60, "restart_app")],
    ),
    Scenario(
        "restart_while_idle_ready",
        "A deploy while ready machines sit idle; more work arrives after.",
        hours=4,
        jobs=[JobBatch(0, 2), JobBatch(14 * 60, 6)],
        events=[Event(13 * 60, "restart_app")],
    ),
    Scenario(
        "worker_never_reregisters",
        "After a deploy the dial-out workers never come back (#1140).",
        hours=5,
        jobs=[JobBatch(0, 12, actual_runtime_s=300)],
        machines=[
            MachineScript(tunnel_lost_on_restart="never"),
            MachineScript(tunnel_lost_on_restart="never"),
        ],
        events=[Event(15 * 60, "restart_app")],
    ),
    Scenario(
        "worker_returns_after_restart",
        "After a deploy the dial-out workers come back five minutes later.",
        hours=4,
        jobs=[JobBatch(0, 12, actual_runtime_s=300)],
        machines=[
            MachineScript(tunnel_lost_on_restart="returns"),
            MachineScript(tunnel_lost_on_restart="returns"),
        ],
        events=[Event(15 * 60, "restart_app")],
    ),
    Scenario(
        "worker_heartbeat_lost",
        "One worker goes silent mid-queue without any restart.",
        hours=5,
        jobs=[JobBatch(0, 12, actual_runtime_s=300)],
        machines=[MachineScript(worker_lost_at=20 * 60)],
    ),
    Scenario(
        "download_stalls",
        "The first machine stops downloading at 40 percent.",
        hours=4,
        max_machines=1,
        jobs=[JobBatch(0, 3)],
        machines=[MachineScript(download_stall_at=0.4)],
    ),
    Scenario(
        "inventory_slow_every_tick",
        "Every other provider inventory read is stale (slow).",
        hours=4,
        inventory="slow",
        jobs=[JobBatch(0, 10)],
    ),
    Scenario(
        "inventory_read_fails",
        "Every provider inventory read fails.",
        hours=4,
        inventory="failed",
        jobs=[JobBatch(0, 10)],
    ),
    Scenario(
        "history_130_removed",
        "130 historical machines with complete removal proof in the table.",
        hours=4,
        history_removed=130,
        inventory="slow",
        jobs=[JobBatch(0, 10)],
    ),
    Scenario(
        "unsettled_rentals_fill_daily_cap",
        "Production tenant: 130 removed rentals whose invoices never settle.",
        hours=2,
        history_removed=130,
        history_invoiced=False,
        jobs=[JobBatch(0, 3)],
    ),
    Scenario(
        "test_cap_nearly_spent",
        "Staging test task cap 95 percent spent before the queue arrives.",
        hours=1,
        test_guard=GUARD,
        spent_fraction=0.95,
        jobs=[JobBatch(0, 6)],
        expect_all_done=False,
        expect_capacity=False,
    ),
    Scenario(
        "test_cap_spent_while_running",
        "Staging test cap allows about one machine-hour; the queue is longer.",
        hours=2.5,
        test_guard=GUARD,
        spent_fraction=0.75,
        jobs=[JobBatch(0, 20, actual_runtime_s=300)],
        expect_all_done=False,
        expect_capacity=False,
    ),
    Scenario(
        "runtime_window_short",
        "Staging runtime window of one hour against 30-minute expected jobs.",
        hours=5,
        test_guard={"task_limit_microusd": 50_000_000, "max_runtime_seconds": 3600},
        jobs=[JobBatch(0, 10)],
    ),
    Scenario(
        "two_owner_profiles",
        "qa-admin and owner jobs, each owner with its own demand profile.",
        hours=4,
        owner_profiles=("superdan", "supervan"),
        jobs=[JobBatch(0, 4), JobBatch(0, 4, owner="supervan")],
    ),
    Scenario(
        "qa_admin_on_owner_profile",
        "qa-admin FL jobs with only an owner FL demand profile (#1140 Q2).",
        hours=3,
        owner_profiles=("superdan",),
        jobs=[JobBatch(0, 4, owner="supervan")],
    ),
    Scenario(
        "demand_toggled_off",
        "Demand start is switched off for an hour mid-queue, then back on.",
        hours=5,
        jobs=[JobBatch(0, 4), JobBatch(30 * 60, 10)],
        events=[Event(25 * 60, "demand_off"), Event(85 * 60, "demand_on")],
    ),
    Scenario(
        "recorded_slow_boot",
        "Machines boot like the slow Vast fixture (22 minutes, 58 MB/s).",
        # Below the owner's 50 MB/s floor (dstack_preparation.py:149) every
        # start is stopped and four stops pause demand by design.
        hours=4,
        default_machine=MachineScript.from_fixture("vast_slow_boot"),
        jobs=[JobBatch(0, 10)],
    ),
    Scenario(
        "two_no_capacity_starts",
        "The provider has stock in planning but two creates find no capacity.",
        hours=3,
        max_machines=1,
        jobs=[JobBatch(0, 3)],
        machines=[MachineScript(no_capacity=True), MachineScript(no_capacity=True)],
    ),
    Scenario(
        "three_failed_boots",
        "Three machines in a row fail their model load, the fourth is fine.",
        hours=5,
        max_machines=1,
        jobs=[JobBatch(0, 2)],
        machines=[MachineScript(load_fails=True)] * 3,
    ),
    Scenario(
        "demand_config_edited",
        "The staging test task id is rotated in operator.json mid-queue",
        # (#1068 item 5). Both machines are already up at the edit and serve
        # the shared queue, so this alone never showed a stale demand journal;
        # see demand_task_rotated_after_cap for the stranding case.
        hours=4,
        test_guard={"task_limit_microusd": 50_000_000, "max_runtime_seconds": 7200},
        jobs=[JobBatch(0, 12, actual_runtime_s=600)],
        events=[Event(15 * 60, "edit_demand_config", {"test_task_id": "sim-task-2"})],
    ),
    Scenario(
        "demand_task_rotated_after_cap",
        "Test task cap used up; the task id is rotated and a profile id added.",
        # Staging 11-10 (#1140 Q3): every queued job had a demand journal from
        # the refused starts, and its identity hashed test_task_id and
        # profile_ids, so after the rotation each one read policy_changed.
        # Two jobs: the refusals alternate between them, so both hold a
        # journal; a third, unjournaled job would start a machine for all.
        hours=4,
        test_guard={"task_limit_microusd": 50_000_000, "max_runtime_seconds": 7200},
        spent_fraction=0.99,
        jobs=[JobBatch(0, 2, actual_runtime_s=600)],
        events=[
            Event(
                20 * 60,
                "edit_demand_config",
                {
                    "test_task_id": "sim-task-2",
                    "profile_ids": ["fl-superdan", "fl-superdan-b"],
                },
            )
        ],
    ),
    Scenario(
        "lookahead_fl_30_min_jobs",
        "Start rules (lookahead v1) with 24 queued 30-minute FL jobs.",
        hours=6,
        max_machines=4,
        lookahead=True,
        # Rules sized to these 10-minute jobs, keeping the defaults' ratio
        # (K x job = boot = 600 s). The 30 s default would leave one machine
        # on 24 such jobs for 4 h; this scenario used to pass only because a
        # busy worker read as a cold slot and rented per claim (#688).
        start_rules={"job_seconds": 600, "minimum_jobs": 1},
        jobs=[JobBatch(0, 24, actual_runtime_s=600)],
    ),
    # #688 staging acceptance, start rules at their defaults (job 30 s, boot
    # 600 s, 20 jobs left at ready). max_machines=4 so the fleet cap never
    # hides an extra start; every idle stop must come 600 s after the last job.
    Scenario(
        "lookahead_floor_one_job",
        "Lookahead: 1 queued job and no machine starts exactly one (floor).",
        hours=2,
        max_machines=4,
        lookahead=True,
        jobs=[JobBatch(0, 1, actual_runtime_s=30)],
        expect_starts=1,
        expect_idle_stop_seconds=600,
    ),
    Scenario(
        "lookahead_ready_30_queued",
        "Lookahead: 1 ready machine and 30 queued (10 left at ready) adds none.",
        hours=3,
        max_machines=4,
        lookahead=True,
        jobs=[
            JobBatch(0, 1, actual_runtime_s=30),
            JobBatch(15 * 60, 30, actual_runtime_s=30),
        ],
        expect_starts=1,
        expect_idle_stop_seconds=600,
    ),
    Scenario(
        "lookahead_ready_45_queued",
        "Lookahead: 1 ready machine and 45 queued (25 left at ready) adds one.",
        hours=3,
        max_machines=4,
        lookahead=True,
        jobs=[
            JobBatch(0, 1, actual_runtime_s=30),
            JobBatch(15 * 60, 45, actual_runtime_s=30),
        ],
        expect_starts=2,
        expect_idle_stop_seconds=600,
    ),
    Scenario(
        "lookahead_idle_stop_at_boot",
        "Lookahead: an idle machine stops 600 s (boot) after its last job.",
        # The policy idle default is 30 minutes; a lookahead start keeps the
        # start rules' boot time instead (rent-or-buy keep-alive).
        hours=2,
        max_machines=4,
        lookahead=True,
        idle_shutdown_seconds=1800,
        jobs=[JobBatch(0, 2, actual_runtime_s=30)],
        expect_starts=1,
        expect_idle_stop_seconds=600,
    ),
    Scenario(
        "slow_ticks_fresh_30",
        "Ticks 9 to 100 s apart, 30 s input transfer, 30 s readiness proof.",
        hours=4,
        tick_jitter=(9, 100),
        prepare_seconds=30,
        observation_fresh_seconds=30,
        jobs=[JobBatch(0, 12, actual_runtime_s=300)],
    ),
    Scenario(
        "provider_reads_hold_capacity_lock",
        "Provider reads take 8 s; demand start reads every machine each tick (#1148).",
        # The fleet is full, so every tick demand start reads each machine's
        # run before refusing with capacity_limit. Workers claim and submit
        # concurrently; their consumer write needs the same capacity lock.
        hours=3,
        max_machines=3,
        provider_latency=8,
        prepare_seconds=30,
        observation_fresh_seconds=120,
        jobs=[JobBatch(0, 30, actual_runtime_s=300)],
    ),
    Scenario(
        "test_window_typical_fl",
        "Staging 2 h test window, 18-minute boots, 4-minute FL jobs (#1148).",
        # Each FL job reserves 1800 s (an allowance, not a speed estimate).
        hours=6,
        test_guard={"task_limit_microusd": 50_000_000, "max_runtime_seconds": 7200},
        default_machine=MachineScript(download_seconds=900),
        jobs=[JobBatch(0, 30, actual_runtime_s=240)],
    ),
    Scenario(
        "budget_row_malformed",
        "One settled test-task reservation row gets an unknown pool mid-queue.",
        hours=3,
        test_guard={"task_limit_microusd": 50_000_000, "max_runtime_seconds": 7200},
        spent_fraction=0.02,
        jobs=[JobBatch(0, 20, actual_runtime_s=300)],
        events=[
            Event(20 * 60, "corrupt_reservation", {"pool": "inkseq-staging-v9-fl"})
        ],
    ),
    Scenario(
        "slow_ticks_fresh_120",
        "Controller ticks 9 to 100 s apart and a 30 s input transfer (#1140 Q1).",
        hours=4,
        tick_jitter=(9, 100),
        prepare_seconds=30,
        observation_fresh_seconds=120,
        jobs=[JobBatch(0, 12, actual_runtime_s=300)],
    ),
]
# Exported incidents (tests/fixtures/capacity_sim/incident_*.json) replay as
# scenarios too: python tools/export_capacity_incident.py <intent_id>.
SCENARIOS += incident_scenarios()

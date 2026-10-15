"""Partition the existing unittest suites without narrowing their coverage.

Whole modules stay together, including load_tests and module/class fixtures.
Largest-first allocation uses discovered test counts and explicit media cost
heuristics, not claimed runtime measurements. CI timing artifacts allow those
heuristics and the shard count to be tuned from real hosted-runner evidence.
No credentials, provider client or production database are used by this tool.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARD_COUNT = 6

# The exact modules from the previous four PostgreSQL command blocks. This is
# intentionally a distinct contract from the complete SQLite/offline discovery.
# New PostgreSQL coverage is added explicitly; existing modules never disappear
# as a side effect of CI path selection or balancing.
POSTGRES_MODULES = tuple("""
test_platform_repository test_platform_queue test_platform_autoscale test_platform_control
test_platform_worker test_platform_drain_safe_runner test_platform_production_scaler
test_platform_on_demand_scaler test_platform_backlog_handoff test_platform_preparation_recovery
test_platform_preparation_idle test_platform_production_boot_memory test_platform_fleet
test_platform_scaler test_platform_api test_platform_execution_policy test_platform_batches
test_platform_storage_multipart test_platform_artifact_writer test_platform_diagnostics
test_platform_capacity test_platform_capacity_api test_platform_guided test_platform_generation_draft
test_platform_generation_services test_platform_project_activity test_platform_capacity_cli
test_platform_reliability test_platform_request_admission test_platform_admission_review
test_platform_generated_audio test_platform_caption_plans test_platform_render_plans
test_platform_render_backend test_platform_lium_provider test_platform_boyesir_backend
test_platform_backup_postgres test_platform_upload_route test_platform_asset_recovery
test_platform_queued_task_policy test_platform_queued_boot test_platform_queued_task_runner
test_platform_queued_task_hold test_platform_long_duration_policy test_platform_duration_quote
test_platform_runtime_adoption test_platform_live_runtime_handoff test_operator_capacity
test_operator_runtime test_capacity_market test_capacity_market_refresh test_capacity_candidates
test_targon_provider test_platform_engine_routing
test_platform_wangp_api test_platform_wangp_recovery_integration test_platform_wangp_failure_diagnostics
test_platform_wangp_seed_domain
test_platform_engine_cold_capacity
test_platform_pool_members test_platform_pool_member_controller test_platform_pool_service
test_platform_pool_member_replacement test_platform_member_readiness test_platform_wangp_service_hold
test_platform_wangp_first_last_policy test_platform_wangp_ref test_platform_wangp_ref_api
test_platform_preparation_hold_recovery test_platform_quick_chat_integration
test_platform_quick_chat_admission test_platform_quick_chat_wangp test_platform_quick_chat_titles test_platform_agent_connect
test_platform_agent_connect_app test_operator_manual_review test_operator_owned_drain
test_operator_extensions test_managed_worker_admission
test_dstack_operator test_dstack_controller test_dstack_runtime test_dstack_factory test_dstack_budget_window
test_dstack_prices test_operator_budgets
test_job_cancellation_ledger test_job_events test_operator_jobs
test_host_policy test_host_scorecard test_dstack_host_selection test_operator_host_routes
test_quick_chat_history test_creation_history_storage test_creation_history_routes
test_hatchet_dispatch test_hatchet_dispatch_telemetry test_hatchet_affinity
test_demand_wakeup test_dstack_allocation_proof
""".split())

# CPU ffmpeg/font fixtures have additional process/codec costs. These multipliers
# are balancing heuristics only, not seconds or accepted performance benchmarks.
MODULE_COST_MULTIPLIERS = {
    "test_platform_render_backend": 4,
    "test_platform_render_subtitles": 4,
    "test_platform_native_delivery": 3,
    "test_platform_captions": 3,
    "test_platform_media_streams": 3,
    "test_platform_wangp_recovery_integration": 3,
    "test_platform_generated_audio": 3,
    "test_platform_wangp_ref_api": 2,
}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load_suite(kind: str, root: Path = ROOT, *, loader=None):
    loader = loader or unittest.TestLoader()
    root = root.resolve()
    # Running a tools/ script does not otherwise put the project root on sys.path.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if kind == "python":
        suite = loader.discover(str(root), pattern="test_*.py", top_level_dir=str(root))
    elif kind == "postgres":
        if len(POSTGRES_MODULES) != len(set(POSTGRES_MODULES)):
            raise ValueError("duplicate_postgres_module")
        suite = loader.loadTestsFromNames(POSTGRES_MODULES)
    else:
        raise ValueError("unknown_ci_suite")
    # Do not let a failed import or broken load_tests hook become an apparently
    # empty/skipped shard. Fail every shard before executing any partial suite.
    if loader.errors:
        raise ValueError("ci_test_loading_failed")
    return suite


def partition(suite, shard_count: int):
    if type(shard_count) is not int or not 1 <= shard_count <= 32:
        raise ValueError("invalid_shard_count")
    groups = defaultdict(list)
    identifiers = Counter()
    for case in flatten(suite):
        identifier = case.id()
        # Existing discovery includes some imported TestCases twice. Preserve
        # their original multiplicity rather than silently changing the suite.
        identifiers[identifier] += 1
        module = case.__class__.__module__
        if not isinstance(module, str) or not module:
            raise ValueError("invalid_test_module")
        groups[module].append(case)
    if len(groups) < shard_count:
        raise ValueError("too_few_test_modules_for_shards")
    weights = {name: len(cases) * MODULE_COST_MULTIPLIERS.get(name, 1)
               for name, cases in groups.items()}
    assigned = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    for name in sorted(groups, key=lambda item: (-weights[item], item)):
        index = min(range(shard_count), key=lambda item: (loads[item], len(assigned[item]), item))
        assigned[index].append(name)
        loads[index] += weights[name]
    shards = []
    for index, names in enumerate(assigned):
        names.sort()
        cases = [case for name in names for case in groups[name]]
        shards.append({"index": index, "modules": names, "test_count": len(cases),
                       "estimated_work_units": loads[index], "suite": unittest.TestSuite(cases)})
    # This check is independent of the balancing algorithm: every discovered
    # occurrence must occur exactly once across the complete matrix, including
    # any duplicate IDs already present in the original discovered suite.
    selected = [case.id() for shard in shards for case in flatten(shard["suite"])]
    if Counter(selected) != identifiers:
        raise ValueError("ci_shard_coverage_mismatch")
    return shards


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.module_seconds = defaultdict(float)

    def startTest(self, test):
        self.started_at = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.module_seconds[test.__class__.__module__] += time.monotonic() - self.started_at
        super().stopTest(test)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("python", "postgres"), required=True)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    parser.add_argument("--plan", action="store_true", help="Report coverage counts without running tests")
    parser.add_argument("--timing-output", type=Path)
    args = parser.parse_args(argv)
    if not args.plan and (args.shard_index is None or not 0 <= args.shard_index < args.shard_count):
        parser.error("--shard-index must identify one shard in the complete matrix")
    started_at = time.monotonic()
    try:
        shards = partition(load_suite(args.suite), args.shard_count)
    except ValueError as exc:
        # Errors are fixed descriptions; never serialize environments or imports.
        print(str(exc), file=sys.stderr)
        return 1
    summary = {"suite": args.suite, "shard_count": args.shard_count,
               "total_tests": sum(shard["test_count"] for shard in shards),
               "shards": [{key: value for key, value in shard.items() if key != "suite"} for shard in shards]}
    print(json.dumps(summary, sort_keys=True), flush=True)
    if args.plan:
        return 0
    shard = shards[args.shard_index]
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(shard["suite"])
    if args.timing_output:
        timing = {"suite": args.suite, "shard_index": args.shard_index,
                  "shard_count": args.shard_count, "total_discovered_tests": summary["total_tests"],
                  "selected_tests": shard["test_count"], "tests_run": result.testsRun,
                  "successful": result.wasSuccessful(), "elapsed_seconds": time.monotonic() - started_at,
                  "module_test_seconds": dict(sorted(result.module_seconds.items()))}
        args.timing_output.parent.mkdir(parents=True, exist_ok=True)
        args.timing_output.write_text(json.dumps(timing, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

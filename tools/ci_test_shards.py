"""Partition the existing unittest suites without narrowing their coverage.

Whole modules stay together, including load_tests and module/class fixtures.
Largest-first allocation uses recorded hosted-runner module timings, with a
test-count fallback for new modules. Imported copies of the same test identity
run once. Explicit slow suites can be path-selected without narrowing the
money, GPU, queue or authentication invariants.
No credentials, provider client or production database are used by this tool.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
from statistics import median
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARD_COUNT = 6
TIMINGS = ROOT / "tools" / "ci_test_timings.json"

# Only these CPU/media or backup integration modules are eligible for path
# selection. Critical and unknown source changes select all of them; the rest
# of both suites always runs. Keep dependencies explicit, not inferred imports.
_MEDIA_PATHS = frozenset(
    f"{directory}/{name}.py"
    for directory, prefix in (("studio_platform", ""), ("tests", "test_platform_"))
    for name in (prefix + item for item in (
        "caption_server", "generation_draft", "guided", "guided_schema", "media",
        "media_process", "project_validation", "render_backend", "render_cli", "render_plans",
    ))
)
SLOW_MODULE_PATHS = {
    name: _MEDIA_PATHS | {f"tests/{name}.py"}
    for name in ("test_platform_render_backend", "test_platform_render_subtitles",
                 "test_platform_captions", "test_platform_media_streams")
}
SLOW_MODULE_PATHS["test_platform_backup_postgres"] = frozenset({
    "studio_platform/backup_postgres.py", "tests/test_platform_backup_postgres.py",
})

# Maintained PostgreSQL integration boundary, distinct from offline discovery.
# Retired runtime tests leave with their code; current invariants are retained
# explicitly, independently of path selection and shard balancing.
POSTGRES_MODULES = tuple("""
test_inky_budget test_inky_store test_inky_worker test_inky_integration test_inky_uploads
test_platform_repository test_schema_version test_schema_baseline test_workflow_state test_dbos_postgres
test_platform_queue test_queue_fairness test_result_collection test_platform_autoscale test_platform_control test_platform_worker test_platform_drain_safe_runner
test_platform_fleet test_platform_api test_platform_execution_policy test_platform_batches test_platform_storage_multipart
test_platform_artifact_writer test_platform_diagnostics test_platform_capacity test_platform_capacity_api test_platform_guided
test_platform_generation_draft test_platform_generation_services test_platform_project_activity test_platform_capacity_cli test_platform_reliability
test_platform_request_admission test_platform_admission_review test_platform_generated_audio test_platform_caption_plans test_platform_render_plans
test_platform_render_backend test_platform_boyesir_backend test_platform_backup_postgres test_platform_upload_route test_platform_asset_recovery
test_platform_queued_task_policy test_platform_duration_quote test_operator_capacity test_capacity_market test_capacity_market_refresh
test_capacity_candidates test_platform_engine_routing test_platform_wangp_api test_platform_wangp_failure_diagnostics test_platform_wangp_seed_domain
test_platform_engine_cold_capacity test_platform_pool_members test_platform_wangp_first_last_policy test_platform_wangp_ref test_platform_wangp_ref_api
test_platform_quick_chat_integration test_platform_quick_chat_admission test_platform_quick_chat_wangp test_platform_quick_chat_titles test_platform_agent_connect
test_platform_agent_connect_app test_operator_manual_review test_operator_extensions test_managed_worker_admission test_dstack_operator
test_dstack_controller test_dstack_workspace test_dstack_runtime test_dstack_factory test_dstack_budget_window test_dstack_prices
test_capacity_admission test_capacity_test_continuation test_supplier_spending test_capacity_settings test_capacity_wait_settings test_settings_inventory test_spending_routes test_capacity_reclaim test_capacity_watchdog test_capacity_health
test_dstack_demand_api test_dstack_demand_start test_dstack_demand_projection test_dstack_cold_admission test_capacity_start_rules test_capacity_lookahead_runtime
test_operator_budgets test_job_cancellation_ledger test_job_events test_operator_jobs test_host_policy
test_host_scorecard test_dstack_host_selection test_operator_host_routes test_quick_chat_history test_quick_chat_card_history
test_quick_chat_migration test_quick_chat_production_migration test_storyboard_versions
test_creation_history_storage test_creation_history_routes test_postgres_dispatch test_dispatch_telemetry test_dstack_passive_review
test_demand_wakeup test_dstack_allocation_proof test_alert_snapshot test_alert_http
test_identity_schema test_identity_queue test_identity_adoption test_identity_admin
test_workos_auth test_workos_grants test_workos_navigation395 test_workos_callback_token395
test_credit_schema test_credits test_credits_routes test_credit_spending
test_image_credit_pricing test_service_credit_core
test_credit_admission test_credit_admission_reader test_lookup_indexes test_credit_only_admission test_credit_identity_billing test_agent_credit_caps
test_credit_limit_migration
test_staging_qa
test_payment_schema
test_payments test_payments_routes
test_content_policy test_output_moderation test_content_admission
test_content_film
test_quality_tiers test_quality_views test_quality_authoring test_quality_http
""".split())

# CPU ffmpeg/font fixtures have additional process/codec costs. These multipliers
# are balancing heuristics only, not seconds or accepted performance benchmarks.
MODULE_COST_MULTIPLIERS = {
    "test_platform_render_backend": 4,
    "test_platform_render_subtitles": 4,
    "test_platform_native_delivery": 3,
    "test_platform_captions": 3,
    "test_platform_media_streams": 3,
    "test_platform_generated_audio": 3,
    "test_platform_wangp_ref_api": 2,
}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load_suite(kind: str, root: Path = ROOT, *, loader=None, slow_modules=None):
    if slow_modules is not None and (
        not isinstance(slow_modules, list)
        or any(not isinstance(name, str) or name not in SLOW_MODULE_PATHS for name in slow_modules)
    ):
        raise ValueError("invalid_slow_test_selection")
    loader = loader or unittest.TestLoader()
    root = root.resolve()
    tests = root / "tests"
    # Keep the existing bare test module names and their fixture imports. Tests
    # are not application packages, and tools/ execution also needs the app root.
    for directory in (root, tests):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    if kind == "python":
        suite = loader.discover(str(tests), pattern="test_*.py", top_level_dir=str(tests))
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
    if slow_modules is not None:
        suite = unittest.TestSuite(
            case for case in flatten(suite)
            if case.__class__.__module__ not in SLOW_MODULE_PATHS
            or case.__class__.__module__ in slow_modules
        )
    return suite


def read_timings(kind, path=TIMINGS):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        timings = value[kind]
        if (type(value["version"]) is not int or value["version"] != 1 or not isinstance(timings, dict)
                or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,199}", name)
                       or type(seconds) not in (int, float)
                       or not math.isfinite(seconds) or not 0 <= seconds <= 3600
                       for name, seconds in timings.items())):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_ci_test_timings") from None
    return timings


def partition(suite, shard_count: int, *, timings=None):
    if type(shard_count) is not int or not 1 <= shard_count <= 32:
        raise ValueError("invalid_shard_count")
    groups = defaultdict(list)
    identities = {}
    for case in flatten(suite):
        identifier = case.id()
        if identifier in identities:
            # Importing a TestCase into another test module must not rerun its
            # identical methods. A colliding different class is not an alias.
            if type(identities[identifier]) is not type(case):
                raise ValueError("ci_test_identity_collision")
            continue
        identities[identifier] = case
        module = case.__class__.__module__
        if not isinstance(module, str) or not module:
            raise ValueError("invalid_test_module")
        groups[module].append(case)
    if len(groups) < shard_count:
        raise ValueError("too_few_test_modules_for_shards")
    weights = {name: len(cases) * MODULE_COST_MULTIPLIERS.get(name, 1)
               for name, cases in groups.items()}
    if timings:
        rates = [timings[name] / len(cases) for name, cases in groups.items()
                 if name in timings and timings[name] > 0]
        fallback = median(rates) if rates else 1.
        weights = {name: max(.001, timings.get(name, cost * fallback))
                   for name, cost in weights.items()}
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
    # Whole modules preserve their fixtures and hooks; every unique discovered
    # identity must occur exactly once across the complete matrix.
    selected = [case.id() for shard in shards for case in flatten(shard["suite"])]
    if Counter(selected) != Counter(identities.keys()):
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
    parser.add_argument("--slow-modules", help="JSON null for full coverage, or reviewed slow module names")
    args = parser.parse_args(argv)
    if not args.plan and (args.shard_index is None or not 0 <= args.shard_index < args.shard_count):
        parser.error("--shard-index must identify one shard in the complete matrix")
    started_at = time.monotonic()
    try:
        slow_modules = json.loads(args.slow_modules) if args.slow_modules is not None else None
        timings = read_timings(args.suite)
        shards = partition(load_suite(args.suite, slow_modules=slow_modules), args.shard_count,
                           timings=timings)
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

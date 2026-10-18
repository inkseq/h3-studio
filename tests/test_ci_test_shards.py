"""CI coverage, fixture and aggregate invariants; no dependencies or networking."""
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import uuid

from tools import ci_test_shards as ci


def module_cases(name, count, *, failing=False):
    def method(self):
        if failing:
            self.fail("synthetic failure")
    cls = type("SyntheticTests", (unittest.TestCase,), {
        "__module__": name, **{f"test_{index:04d}": method for index in range(count)}})
    return [cls(f"test_{index:04d}") for index in range(count)]


class ShardTests(unittest.TestCase):
    def fixture(self):
        return unittest.TestSuite(unittest.TestSuite(module_cases(f"fixture_{index}", count))
                                  for index, count in enumerate((25, 17, 12, 8, 6, 4, 3, 2, 1)))

    def test_complete_suite_is_partitioned_exactly_once(self):
        suite = self.fixture()
        expected = Counter(case.id() for case in ci.flatten(suite))
        shards = ci.partition(suite, 6)
        actual = Counter(case.id() for shard in shards for case in ci.flatten(shard["suite"]))
        self.assertEqual(expected, actual)
        self.assertTrue(all(value == 1 for value in actual.values()))
        self.assertTrue(all(shard["test_count"] for shard in shards))
        self.assertEqual(sum(shard["test_count"] for shard in shards), 78)

    def test_nested_discovery_order_does_not_change_module_assignment(self):
        suite = self.fixture()
        reverse = unittest.TestSuite(reversed(list(ci.flatten(suite))))
        planned = [shard["modules"] for shard in ci.partition(suite, 6)]
        self.assertEqual(planned, [shard["modules"] for shard in ci.partition(reverse, 6)])

    def test_module_fixtures_and_all_class_tests_stay_in_one_shard(self):
        shards = ci.partition(self.fixture(), 6)
        memberships = {}
        for shard in shards:
            for case in ci.flatten(shard["suite"]):
                module = case.__class__.__module__
                memberships.setdefault(module, set()).add(shard["index"])
        self.assertTrue(all(len(indices) == 1 for indices in memberships.values()))

    def test_largest_first_balancing_spreads_expensive_media_modules(self):
        suite = unittest.TestSuite([*module_cases("test_platform_render_backend", 10),
                                   *module_cases("test_platform_render_subtitles", 10),
                                   *module_cases("light_a", 20), *module_cases("light_b", 20)])
        shards = ci.partition(suite, 2)
        self.assertEqual([shard["estimated_work_units"] for shard in shards], [60, 60])
        self.assertEqual([shard["test_count"] for shard in shards], [30, 30])

    def test_invalid_and_empty_shards_fail_closed(self):
        for count in (0, -1, 33, True, "6"):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "invalid_shard_count"):
                ci.partition(self.fixture(), count)
        with self.assertRaisesRegex(ValueError, "too_few_test_modules"):
            ci.partition(unittest.TestSuite(), 1)
        with self.assertRaisesRegex(ValueError, "too_few_test_modules"):
            ci.partition(unittest.TestSuite(module_cases("only_one", 10)), 2)

    def test_existing_duplicate_test_identity_is_not_silently_deduplicated(self):
        cases = [*module_cases("duplicated", 1), *module_cases("duplicated", 1)]
        shards = ci.partition(unittest.TestSuite(cases), 1)
        self.assertEqual(shards[0]["test_count"], 2)
        self.assertEqual(Counter(case.id() for case in ci.flatten(shards[0]["suite"])),
                         Counter(case.id() for case in cases))

    def test_full_discovery_honors_load_tests_hooks_and_new_modules(self):
        prefix = "test_ci_fixture_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / (prefix + "_hook.py")).write_text(textwrap.dedent('''
                import unittest
                class Base(unittest.TestCase):
                    def test_inherited(self): pass
                class Derived(Base):
                    def test_explicit(self): pass
                def load_tests(loader, tests, pattern):
                    return unittest.TestSuite([Derived("test_explicit")])
            '''), encoding="utf-8")
            (tests / (prefix + "_new.py")).write_text(textwrap.dedent('''
                import unittest
                class New(unittest.TestCase):
                    def test_added(self): pass
            '''), encoding="utf-8")
            try:
                suite = ci.load_suite("python", root)
                discovered = [case.id() for case in ci.flatten(suite)]
                shards = ci.partition(suite, 2)
                self.assertEqual(len(discovered), 2)
                self.assertFalse(any("inherited" in identifier for identifier in discovered))
                self.assertEqual(set(discovered), {case.id() for shard in shards for case in ci.flatten(shard["suite"])})
            finally:
                for directory in (root, root / "tests"):
                    if str(directory.resolve()) in sys.path:
                        sys.path.remove(str(directory.resolve()))
                for name in list(sys.modules):
                    if name.startswith(prefix):
                        del sys.modules[name]

    def test_broken_import_fails_before_running_partial_suite(self):
        name = "test_ci_broken_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / (name + ".py")).write_text("raise RuntimeError('synthetic import error')\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(ValueError, "ci_test_loading_failed"):
                    ci.load_suite("python", root)
            finally:
                for directory in (root, root / "tests"):
                    if str(directory.resolve()) in sys.path:
                        sys.path.remove(str(directory.resolve()))
                sys.modules.pop(name, None)

    def test_all_previous_postgres_command_modules_are_retained(self):
        # Frozen inventory from the pre-sharding CI's four command blocks.
        expected = set("""
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
            test_operator_runtime test_capacity_market test_targon_provider test_platform_engine_routing
            test_platform_wangp_api test_platform_wangp_recovery_integration test_platform_engine_cold_capacity
            test_platform_pool_members test_platform_pool_member_controller test_platform_pool_service
            test_platform_pool_member_replacement test_platform_member_readiness test_platform_wangp_service_hold
            test_platform_wangp_first_last_policy test_platform_wangp_ref test_platform_wangp_ref_api
            test_platform_preparation_hold_recovery test_platform_quick_chat_integration
            test_platform_quick_chat_admission test_platform_quick_chat_wangp test_platform_agent_connect
            test_platform_agent_connect_app
        """.split())
        self.assertEqual(len(expected), 70)
        self.assertTrue(expected.issubset(set(ci.POSTGRES_MODULES)))
        self.assertEqual(set(ci.POSTGRES_MODULES) - expected,
                         {"test_operator_manual_review", "test_capacity_market_refresh", "test_capacity_candidates",
                          "test_platform_wangp_failure_diagnostics", "test_platform_wangp_seed_domain", "test_operator_owned_drain", "test_platform_quick_chat_titles",
                          "test_operator_extensions", "test_managed_worker_admission",
                          "test_dstack_operator", "test_dstack_controller", "test_dstack_runtime",
                          "test_dstack_factory", "test_dstack_budget_window", "test_postgres_dispatch", "test_dispatch_telemetry",
                          "test_dstack_passive_review", "test_dstack_prices", "test_operator_budgets",
                         "test_job_cancellation_ledger", "test_job_events", "test_operator_jobs",
                         "test_host_policy", "test_host_scorecard", "test_dstack_host_selection", "test_operator_host_routes",
                         "test_quick_chat_history", "test_quick_chat_card_history", "test_creation_history_storage", "test_creation_history_routes",
                         "test_demand_wakeup", "test_dstack_allocation_proof", "test_alert_snapshot", "test_alert_http"})
        self.assertEqual(len(ci.POSTGRES_MODULES), len(set(ci.POSTGRES_MODULES)))

    def test_postgres_loader_uses_complete_manifest(self):
        loader = unittest.mock.Mock(errors=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                ci.load_suite("postgres", root, loader=loader)
                loader.loadTestsFromNames.assert_called_once_with(ci.POSTGRES_MODULES)
                loader.discover.assert_not_called()
            finally:
                for directory in (root, root / "tests"):
                    if str(directory.resolve()) in sys.path:
                        sys.path.remove(str(directory.resolve()))

    def test_plan_reports_counts_without_running_any_tests(self):
        with patch.object(ci, "load_suite", return_value=self.fixture()), patch.object(ci.unittest, "TextTestRunner") as runner:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ci.main(["--suite", "python", "--plan", "--shard-count", "6"]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["total_tests"], 78)
            self.assertEqual(len(plan["shards"]), 6)
            runner.assert_not_called()

    def test_missing_or_out_of_range_selection_never_runs(self):
        for selection in ([], ["--shard-index", "-1"], ["--shard-index", "6"]):
            with self.subTest(selection=selection), patch.object(ci, "load_suite") as load, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    ci.main(["--suite", "python", *selection])
                load.assert_not_called()

    def test_failed_shard_returns_failure_and_writes_nonsecret_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            timing = Path(directory) / "timing.json"
            suite = unittest.TestSuite(module_cases("failure_fixture", 1, failing=True))
            with patch.object(ci, "load_suite", return_value=suite), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ci.main(["--suite", "python", "--shard-count", "1", "--shard-index", "0",
                                          "--timing-output", str(timing)]), 1)
            report = json.loads(timing.read_text(encoding="utf-8"))
            self.assertFalse(report["successful"])
            self.assertEqual(report["tests_run"], 1)
            self.assertEqual(set(report["module_test_seconds"]), {"failure_fixture"})
            self.assertNotIn("environment", report)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        aggregate = cls.workflow.split("\n  test:\n", 1)[1].split("\n  publish-aws:\n", 1)[0]
        cls.aggregate_code = textwrap.dedent(re.search(r"python - <<'PY'\n(.*?)          PY", aggregate, re.S).group(1))

    def test_each_suite_matrix_is_complete_parallel_and_preserves_caches(self):
        for kind, next_job in (("python", "postgres"), ("postgres", "frontend")):
            block = self.workflow.split(f"\n  {kind}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]
            self.assertIn("fail-fast: false", block)
            indices = re.search(r"shard: \[(.*?)\]", block).group(1)
            self.assertEqual([int(value.strip()) for value in indices.split(",")], list(range(ci.DEFAULT_SHARD_COUNT)))
            self.assertIn(f"--suite {kind} --shard-index ${{{{ matrix.shard }}}} --shard-count {ci.DEFAULT_SHARD_COUNT}", block)
            self.assertIn("cache: pip", block)
            self.assertIn("tools/ci_media_tools.py install", block)
            self.assertIn("persist-credentials: false", block)
            self.assertIn("if: always()", block)
            self.assertNotIn("secrets.", block)
            self.assertNotIn("id-token: write", block)
        self.assertNotIn("self-hosted", self.workflow)

    def aggregate(self, *, selected=True, override=None):
        flags = {name: "true" if selected else "false" for name in ci_gate_names()}
        jobs = {"changes": {"result": "success", "outputs": flags}}
        jobs.update({name: {"result": "success" if selected else "skipped"}
                     for name in ("python", "postgres", "frontend", "frontend-tools", "containers", "macos-helper")})
        if override:
            jobs[override[0]]["result"] = override[1]
        with patch.dict(os.environ, {"RESULTS": json.dumps(jobs)}), redirect_stdout(io.StringIO()):
            exec(self.aggregate_code, {})

    def test_stable_aggregate_accepts_successful_matrix_rollups(self):
        self.aggregate()
        self.aggregate(selected=False)
        self.assertIn("needs: [changes, python, postgres, frontend, frontend-tools, containers, macos-helper]", self.workflow)

    def test_required_aggregate_rejects_failed_cancelled_or_skipped_matrix(self):
        for job in ("python", "postgres", "macos-helper"):
            for result in ("failure", "cancelled", "skipped"):
                with self.subTest(job=job, result=result), self.assertRaises(SystemExit):
                    self.aggregate(override=(job, result))

    def test_unselected_accidental_execution_and_classifier_failure_fail_closed(self):
        with self.assertRaises(SystemExit):
            self.aggregate(selected=False, override=("python", "success"))
        with self.assertRaises(SystemExit):
            self.aggregate(override=("changes", "failure"))


def ci_gate_names():
    return ("python", "postgres", "frontend", "frontend_tools", "containers", "macos_helper")


if __name__ == "__main__":
    unittest.main()

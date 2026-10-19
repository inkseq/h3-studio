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

    cls = type(
        "SyntheticTests",
        (unittest.TestCase,),
        {"__module__": name, **{f"test_{index:04d}": method for index in range(count)}},
    )
    return [cls(f"test_{index:04d}") for index in range(count)]


class ShardTests(unittest.TestCase):
    def fixture(self):
        return unittest.TestSuite(
            unittest.TestSuite(module_cases(f"fixture_{index}", count))
            for index, count in enumerate((25, 17, 12, 8, 6, 4, 3, 2, 1))
        )

    def test_complete_suite_is_partitioned_exactly_once(self):
        suite = self.fixture()
        expected = Counter(case.id() for case in ci.flatten(suite))
        shards = ci.partition(suite, 6)
        actual = Counter(
            case.id() for shard in shards for case in ci.flatten(shard["suite"])
        )
        self.assertEqual(expected, actual)
        self.assertTrue(all(value == 1 for value in actual.values()))
        self.assertTrue(all(shard["test_count"] for shard in shards))
        self.assertEqual(sum(shard["test_count"] for shard in shards), 78)

    def test_nested_discovery_order_does_not_change_module_assignment(self):
        suite = self.fixture()
        reverse = unittest.TestSuite(reversed(list(ci.flatten(suite))))
        planned = [shard["modules"] for shard in ci.partition(suite, 6)]
        self.assertEqual(
            planned, [shard["modules"] for shard in ci.partition(reverse, 6)]
        )

    def test_module_fixtures_and_all_class_tests_stay_in_one_shard(self):
        shards = ci.partition(self.fixture(), 6)
        memberships = {}
        for shard in shards:
            for case in ci.flatten(shard["suite"]):
                module = case.__class__.__module__
                memberships.setdefault(module, set()).add(shard["index"])
        self.assertTrue(all(len(indices) == 1 for indices in memberships.values()))

    def test_largest_first_balancing_spreads_expensive_media_modules(self):
        suite = unittest.TestSuite(
            [
                *module_cases("test_platform_render_backend", 10),
                *module_cases("test_platform_render_subtitles", 10),
                *module_cases("light_a", 20),
                *module_cases("light_b", 20),
            ]
        )
        shards = ci.partition(suite, 2)
        self.assertEqual([shard["estimated_work_units"] for shard in shards], [60, 60])
        self.assertEqual([shard["test_count"] for shard in shards], [30, 30])

    def test_invalid_and_empty_shards_fail_closed(self):
        for count in (0, -1, 33, True, "6"):
            with (
                self.subTest(count=count),
                self.assertRaisesRegex(ValueError, "invalid_shard_count"),
            ):
                ci.partition(self.fixture(), count)
        with self.assertRaisesRegex(ValueError, "too_few_test_modules"):
            ci.partition(unittest.TestSuite(), 1)
        with self.assertRaisesRegex(ValueError, "too_few_test_modules"):
            ci.partition(unittest.TestSuite(module_cases("only_one", 10)), 2)

    def test_imported_aliases_run_once_but_inherited_class_invariants_remain(self):
        original = module_cases("duplicated", 1)[0]
        inherited = type("Derived", (type(original),), {"__module__": "duplicated"})(
            "test_0000"
        )
        cases = [original, type(original)("test_0000"), inherited, inherited]
        shards = ci.partition(unittest.TestSuite(cases), 1)
        self.assertEqual(shards[0]["test_count"], 2)
        self.assertEqual(
            [case.id() for case in ci.flatten(shards[0]["suite"])],
            [original.id(), inherited.id()],
        )
        with self.assertRaisesRegex(ValueError, "ci_test_identity_collision"):
            ci.partition(
                unittest.TestSuite([original, *module_cases("duplicated", 1)]), 1
            )

    def test_measured_module_seconds_balance_without_changing_coverage(self):
        suite = unittest.TestSuite(
            [
                *module_cases("heavy", 1),
                *module_cases("large_count", 60),
                *module_cases("small_a", 1),
                *module_cases("small_b", 1),
            ]
        )
        shards = ci.partition(
            suite,
            2,
            timings={"heavy": 100, "large_count": 1, "small_a": 1, "small_b": 1},
        )
        self.assertEqual(shards[0]["modules"], ["heavy"])
        self.assertEqual(shards[1]["estimated_work_units"], 3)
        self.assertEqual(sum(shard["test_count"] for shard in shards), 63)

    def test_slow_selection_never_filters_unmarked_critical_tests(self):
        suite = unittest.TestSuite(
            [
                *module_cases("test_platform_render_backend", 1),
                *module_cases("test_platform_backup_postgres", 1),
                *module_cases("test_dstack_operator", 1),
                *module_cases("test_platform_queue", 1),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                for chosen, count in (
                    (None, 4),
                    ([], 2),
                    (["test_platform_render_backend"], 3),
                ):
                    loader = unittest.mock.Mock(errors=[])
                    loader.discover.return_value = suite
                    loaded = ci.load_suite(
                        "python", root, loader=loader, slow_modules=chosen
                    )
                    cases = list(ci.flatten(loaded))
                    self.assertEqual(len(cases), count)
                    self.assertTrue(
                        {"test_dstack_operator", "test_platform_queue"}.issubset(
                            case.__class__.__module__ for case in cases
                        )
                    )
                for invalid in ("all", {}, ["test_platform_queue"]):
                    with self.assertRaisesRegex(
                        ValueError, "invalid_slow_test_selection"
                    ):
                        ci.load_suite(
                            "python", root, loader=loader, slow_modules=invalid
                        )
            finally:
                for directory in (root, root / "tests"):
                    if str(directory.resolve()) in sys.path:
                        sys.path.remove(str(directory.resolve()))

    def test_timing_data_is_bounded_and_cannot_change_test_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timings.json"
            for seconds in (2.5, -1, True, float("nan"), 3601):
                path.write_text(
                    json.dumps({"version": 1, "python": {"test_example": seconds}})
                )
                if seconds == 2.5:
                    self.assertEqual(
                        ci.read_timings("python", path), {"test_example": 2.5}
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "invalid_ci_test_timings"):
                        ci.read_timings("python", path)

    def test_full_discovery_honors_load_tests_hooks_and_new_modules(self):
        prefix = "test_ci_fixture_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / (prefix + "_hook.py")).write_text(
                textwrap.dedent('''
                import unittest
                class Base(unittest.TestCase):
                    def test_inherited(self): pass
                class Derived(Base):
                    def test_explicit(self): pass
                def load_tests(loader, tests, pattern):
                    return unittest.TestSuite([Derived("test_explicit")])
            '''),
                encoding="utf-8",
            )
            (tests / (prefix + "_new.py")).write_text(
                textwrap.dedent('''
                import unittest
                class New(unittest.TestCase):
                    def test_added(self): pass
            '''),
                encoding="utf-8",
            )
            try:
                suite = ci.load_suite("python", root)
                discovered = [case.id() for case in ci.flatten(suite)]
                shards = ci.partition(suite, 2)
                self.assertEqual(len(discovered), 2)
                self.assertFalse(
                    any("inherited" in identifier for identifier in discovered)
                )
                self.assertEqual(
                    set(discovered),
                    {
                        case.id()
                        for shard in shards
                        for case in ci.flatten(shard["suite"])
                    },
                )
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
            (tests / (name + ".py")).write_text(
                "raise RuntimeError('synthetic import error')\n", encoding="utf-8"
            )
            try:
                with self.assertRaisesRegex(ValueError, "ci_test_loading_failed"):
                    ci.load_suite("python", root)
            finally:
                for directory in (root, root / "tests"):
                    if str(directory.resolve()) in sys.path:
                        sys.path.remove(str(directory.resolve()))
                sys.modules.pop(name, None)

    def test_postgres_manifest_preserves_current_critical_suites(self):
        # Assert the maintained safety boundary, not a duplicate retired list.
        critical = {
            "test_platform_repository",
            "test_platform_queue",
            "test_platform_control",
            "test_platform_worker",
            "test_platform_api",
            "test_platform_agent_connect",
            "test_operator_manual_review",
            "test_operator_budgets",
            "test_dstack_operator",
            "test_dstack_controller",
            "test_dstack_runtime",
            "test_dstack_factory",
            "test_dstack_budget_window",
            "test_dstack_allocation_proof",
            "test_dbos_postgres",
            "test_postgres_dispatch",
            "test_platform_queued_task_policy",
            "test_platform_wangp_failure_diagnostics",
            "test_platform_wangp_first_last_policy",
            "test_platform_wangp_ref_api",
        }
        self.assertTrue(critical.issubset(ci.POSTGRES_MODULES))
        self.assertEqual(len(ci.POSTGRES_MODULES), len(set(ci.POSTGRES_MODULES)))
        for name in ci.POSTGRES_MODULES:
            self.assertTrue((ci.ROOT / "tests" / (name + ".py")).is_file(), name)

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
        with (
            patch.object(ci, "load_suite", return_value=self.fixture()),
            patch.object(ci.unittest, "TextTestRunner") as runner,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    ci.main(["--suite", "python", "--plan", "--shard-count", "6"]), 0
                )
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["total_tests"], 78)
            self.assertEqual(len(plan["shards"]), 6)
            runner.assert_not_called()

    def test_missing_or_out_of_range_selection_never_runs(self):
        for selection in ([], ["--shard-index", "-1"], ["--shard-index", "6"]):
            with (
                self.subTest(selection=selection),
                patch.object(ci, "load_suite") as load,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    ci.main(["--suite", "python", *selection])
                load.assert_not_called()

    def test_failed_shard_returns_failure_and_writes_nonsecret_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            timing = Path(directory) / "timing.json"
            suite = unittest.TestSuite(module_cases("failure_fixture", 1, failing=True))
            with (
                patch.object(ci, "load_suite", return_value=suite),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    ci.main(
                        [
                            "--suite",
                            "python",
                            "--shard-count",
                            "1",
                            "--shard-index",
                            "0",
                            "--timing-output",
                            str(timing),
                        ]
                    ),
                    1,
                )
            report = json.loads(timing.read_text(encoding="utf-8"))
            self.assertFalse(report["successful"])
            self.assertEqual(report["tests_run"], 1)
            self.assertEqual(set(report["module_test_seconds"]), {"failure_fixture"})
            self.assertNotIn("environment", report)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text(encoding="utf-8")
        aggregate = cls.workflow.split("\n  test:\n", 1)[1].split(
            "\n  publish-aws:\n", 1
        )[0]
        cls.aggregate_code = textwrap.dedent(
            re.search(r"python - <<'PY'\n(.*?)          PY", aggregate, re.S).group(1)
        )

    def test_each_suite_matrix_is_complete_parallel_and_preserves_caches(self):
        for kind, next_job in (("python", "postgres"), ("postgres", "frontend")):
            block = self.workflow.split(f"\n  {kind}:\n", 1)[1].split(
                f"\n  {next_job}:\n", 1
            )[0]
            self.assertIn("fail-fast: false", block)
            indices = re.search(r"shard: \[(.*?)\]", block).group(1)
            self.assertEqual(
                [int(value.strip()) for value in indices.split(",")],
                list(range(ci.DEFAULT_SHARD_COUNT)),
            )
            self.assertIn(
                f"--suite {kind} --shard-index ${{{{ matrix.shard }}}} --shard-count {ci.DEFAULT_SHARD_COUNT}",
                block,
            )
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
        jobs.update(
            {
                name: {"result": "success" if selected else "skipped"}
                for name in (
                    "python",
                    "postgres",
                    "frontend",
                    "frontend-tools",
                    "containers",
                    "macos-helper",
                )
            }
        )
        if override:
            jobs[override[0]]["result"] = override[1]
        with (
            patch.dict(os.environ, {"RESULTS": json.dumps(jobs)}),
            redirect_stdout(io.StringIO()),
        ):
            exec(self.aggregate_code, {})

    def test_stable_aggregate_accepts_successful_matrix_rollups(self):
        self.aggregate()
        self.aggregate(selected=False)
        self.assertIn(
            "needs: [changes, python, postgres, frontend, frontend-tools, containers, macos-helper]",
            self.workflow,
        )

    def test_required_aggregate_rejects_failed_cancelled_or_skipped_matrix(self):
        for job in ("python", "postgres", "macos-helper"):
            for result in ("failure", "cancelled", "skipped"):
                with (
                    self.subTest(job=job, result=result),
                    self.assertRaises(SystemExit),
                ):
                    self.aggregate(override=(job, result))

    def test_unselected_accidental_execution_and_classifier_failure_fail_closed(self):
        with self.assertRaises(SystemExit):
            self.aggregate(selected=False, override=("python", "success"))
        with self.assertRaises(SystemExit):
            self.aggregate(override=("changes", "failure"))


def ci_gate_names():
    return (
        "python",
        "postgres",
        "frontend",
        "frontend_tools",
        "containers",
        "macos_helper",
    )


if __name__ == "__main__":
    unittest.main()

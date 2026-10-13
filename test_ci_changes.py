"""CI scope must fail closed; fixtures never invoke GitHub, Docker or a cloud."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import ci_changes as ci


SHA = "a" * 40
BASE = "b" * 40
MAIN = "refs/heads/main"


class ChangeClassificationTests(unittest.TestCase):
    def assert_full(self, result):
        self.assertEqual(result["category"], "full")
        for gate in ("python", "postgres", "frontend", "containers", "legacy"):
            self.assertTrue(result[gate], gate)

    def test_only_explicit_document_modifications_skip_heavy_checks(self):
        result = ci.classify([("M", "README.md"), ("M", "deploy/platform/RELEASE.zh-CN.md")])
        self.assertEqual(result["category"], "docs")
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for path in ("new-guide.md", "skills/sixnine-yingxu/SKILL.md", "tools/README.md"):
            with self.subTest(path=path):
                self.assert_full(ci.classify([("M", path)]))

    def test_reviewed_governance_modifications_are_docs_only(self):
        paths = (
            "AGENTS.md", "WORKFLOW.md", "WORKFLOW.zh-CN.md", "PROJECT-PLAN.md",
            "CURRENT-BASELINE.md", "GENERATION-CONTRACT.md", "GENERATION-FOUNDATION-RESULT.md",
            "PLANNING-INDEX.zh-CN.md", "UNIFIED-BACKEND-API-PLAN.zh-CN.md",
            "REUSE-AND-MIGRATION-DECISION.zh-CN.md",
        )
        # Release preparation imports DOCUMENTS as a dirty-source exemption.
        # Governance-only CI must not silently broaden that separate boundary.
        self.assertFalse(set(paths) & ci.DOCUMENTS)
        changes = [("M", path) for path in paths]
        result = ci.classify(changes)
        self.assertEqual(result["category"], "docs")
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for status in ("A", "D", "T"):
            for path in paths:
                with self.subTest(status=status, path=path):
                    self.assert_full(ci.classify([(status, path)]))

    def test_governance_docs_cannot_hide_critical_changes(self):
        for path in (
            "studio_platform/auth.py", "studio_platform/settings.py", "studio_platform/queue.py",
            "studio_platform/on_demand_scaler.py", "tools/ci_changes.py",
            ".github/workflows/ci.yml", "workflow/project.json", ".gitignore", ".dockerignore",
            "skills/sixnine-yingxu/SKILL.md", "new-policy.md",
        ):
            with self.subTest(path=path):
                self.assert_full(ci.classify([("M", "AGENTS.md"), ("M", "WORKFLOW.md"), ("M", path)]))

    def test_docs_only_push_keeps_the_stable_test_aggregator(self):
        with patch.object(ci, "git_changes", return_value=[("M", "AGENTS.md"), ("M", "WORKFLOW.md")]):
            result = ci.plan("push", {"before": BASE}, SHA, MAIN, Path("."))
        self.assertEqual(result["category"], "docs")
        self.assertEqual(result["action"], "check")
        self.assertFalse(any(result[gate] for gate in ci.GATES))

        # Without a YAML dependency, guard the small literal workflow contract:
        # no workflow-level paths filter may leave the required check pending,
        # and unselected heavy jobs still flow into the unconditional aggregator.
        workflow = (Path(__file__).resolve().parent / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        triggers = workflow.split("\njobs:", 1)[0]
        self.assertNotRegex(triggers, r"(?m)^\s*paths(?:-ignore)?:")
        aggregate = workflow.split("\n  test:\n", 1)[1].split("\n  publish-aws:\n", 1)[0]
        self.assertIn("if: always()", aggregate)
        self.assertIn("needs: [changes, python, postgres, frontend, frontend-tools, containers]", aggregate)
        self.assertIn("expected = 'success' if selected == 'true' else 'skipped'", aggregate)

    def test_only_frontend_snapshot_can_use_frontend_gates(self):
        result = ci.classify([("M", "yingxu/src/Freestyle.jsx"), ("M", "yingxu/source-manifest.json")])
        self.assertTrue(result["frontend"])
        self.assertTrue(result["frontend_tools"])
        for gate in ("python", "postgres", "containers", "legacy"):
            self.assertFalse(result[gate], gate)
        for path in ("web/app.js", "other/src/App.jsx", "yingxu/start.sh", "yingxu/.env"):
            self.assert_full(ci.classify([("M", path)]))

    def test_backend_changes_keep_full_python_database_and_image_checks(self):
        result = ci.classify([("M", "studio_platform/guided.py")])
        self.assertTrue(result["python"])
        self.assertTrue(result["postgres"])
        self.assertTrue(result["containers"])
        self.assertFalse(result["legacy"])
        self.assertFalse(result["frontend"])
        combined = ci.classify([("M", "studio_platform/guided.py"), ("M", "yingxu/src/App.jsx")])
        self.assertTrue(combined["frontend"])
        self.assertTrue(combined["python"])

    def test_security_billing_provider_and_ci_changes_require_every_gate(self):
        for path in ("studio_platform/auth.py", "studio_platform/on_demand_scaler.py",
                     "studio_platform/repository.py", "studio_platform/queue.py",
                     "studio_platform/lium_provider.py", "studio_platform/unknown.py",
                     "tools/ci_changes.py", ".github/workflows/ci.yml", "Dockerfile.platform",
                     ".dockerignore", "requirements.lock.txt", "deploy/platform/release.py"):
            with self.subTest(path=path):
                self.assert_full(ci.classify([("M", path)]))

    def test_added_removed_renamed_and_type_changed_safe_paths_still_require_full(self):
        for status in ("A", "D", "T", "U", "R100", "C100"):
            for path in ("README.md", "yingxu/src/New.jsx"):
                self.assert_full(ci.classify([(status, path)]))
        self.assert_full(ci.classify([("D", "studio_platform/auth.py"), ("A", "yingxu/src/Auth.jsx")]))

    def test_unsafe_paths_never_reduce_verification(self):
        for path in ("yingxu/src/../../auth.py", "/yingxu/src/App.jsx", "yingxu\\src\\App.jsx",
                     "yingxu/src//App.jsx", "yingxu/src/App.jsx\n", "C:/yingxu/src/App.jsx"):
            self.assert_full(ci.classify([("M", path)]))

    def test_full_diff_is_not_truncated_at_github_path_filter_limit(self):
        raw = b"".join(f"M\0yingxu/src/View{n}.jsx\0".encode() for n in range(700))
        raw += b"M\0studio_platform/auth.py\0"
        changes = ci.parse_diff(raw)
        self.assertEqual(len(changes), 701)
        self.assert_full(ci.classify(changes))

    def test_nul_parser_rejects_partial_or_rename_records(self):
        self.assertEqual(ci.parse_diff(b"M\0README.md\0"), [("M", "README.md")])
        for raw in (b"M\0README.md", b"M\0", b"R100\0old\0new\0", b"M\0\0", b"M\0\xff\0"):
            with self.assertRaises((ValueError, UnicodeError)):
                ci.parse_diff(raw)

    def test_git_unavailable_is_full_instead_of_empty_success(self):
        with patch.object(ci, "git_changes", side_effect=subprocess.CalledProcessError(1, "git")):
            self.assert_full(ci.plan("push", {"before": BASE}, SHA, MAIN, Path(".")))
        with patch.object(ci, "git_changes", side_effect=ValueError("missing baseline")):
            self.assert_full(ci.plan("pull_request", {}, SHA, MAIN, Path(".")))

    def test_pr_uses_tested_merge_sha_and_full_local_baseline(self):
        with patch.object(ci, "git_changes", return_value=[("M", "README.md")]) as read:
            result = ci.plan("pull_request", {"pull_request": {"base": {"sha": BASE}}},
                             SHA, "refs/pull/7/merge", Path("fixture"))
        read.assert_called_once_with(Path("fixture"), BASE, SHA)
        self.assertEqual(result["category"], "docs")


class DispatchTests(unittest.TestCase):
    def test_native_retention_is_explicit_platform_prepare_only(self):
        values = {"retain_native_sources_run": "42", "retain_native_sources_commit": BASE,
            "retain_native_sources_index_sha256": "c" * 64}
        self.assertEqual(ci.dispatch_action(dict(values, deploy="true", release_kind="platform"), MAIN), "prepare-platform")
        for change in ({}, {"deploy": "true", "release_kind": "frontend"}, {"approved_commit": SHA},
                {"approved_frontend_commit": SHA}):
            with self.subTest(change=change), self.assertRaises(ValueError): ci.dispatch_action(dict(values, **change), MAIN)
        for key in values:
            incomplete = dict(values, deploy="true", release_kind="platform"); incomplete.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError): ci.dispatch_action(incomplete, MAIN)

    def test_platform_preparation_is_always_full_and_not_deployment(self):
        result = ci.plan("workflow_dispatch", {"inputs": {"deploy": "true", "release_kind": "platform"}},
                         SHA, MAIN, Path("."))
        self.assertEqual(result["action"], "prepare-platform")
        self.assertEqual(result["category"], "full")
        self.assertTrue(result["postgres"])

    def test_frontend_artifact_is_independent_of_python_pg_and_images(self):
        result = ci.plan("workflow_dispatch", {"inputs": {"deploy": True, "release_kind": "frontend"}},
                         SHA, MAIN, Path("."))
        self.assertEqual(result["action"], "prepare-frontend")
        self.assertTrue(result["frontend"])
        self.assertTrue(result["frontend_tools"])
        for gate in ("python", "postgres", "containers", "legacy"):
            self.assertFalse(result[gate], gate)

    def test_approved_deployment_preserves_exact_commit_without_rebuilding(self):
        for key, action in (("approved_commit", "deploy-platform"), ("approved_frontend_commit", "deploy-frontend")):
            result = ci.plan("workflow_dispatch", {"inputs": {key: BASE}}, SHA, MAIN, Path("."))
            self.assertEqual(result["action"], action)
            self.assertFalse(any(result[gate] for gate in ci.GATES))

    def test_conflicting_inputs_invalid_refs_and_incomplete_hashes_fail(self):
        cases = [
            {"deploy": True, "approved_commit": SHA},
            {"approved_commit": SHA, "approved_frontend_commit": BASE},
            {"release_kind": "skip-everything"}, {"deploy": 1},
            {"approved_commit": "main"}, {"approved_frontend_commit": "a" * 39},
            {"resume_command": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            {"approved_commit": SHA, "resume_command": "not-a-command-id"},
        ]
        for inputs in cases:
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                ci.dispatch_action(inputs, MAIN)
        with self.assertRaises(ValueError):
            ci.dispatch_action({"deploy": True}, "refs/heads/topic")

    def test_push_cannot_smuggle_workflow_dispatch_inputs(self):
        with patch.object(ci, "git_changes", return_value=[("M", "studio_platform/auth.py")]):
            result = ci.plan("push", {"before": BASE, "inputs": {"deploy": True, "release_kind": "frontend"}},
                             SHA, MAIN, Path("."))
        self.assertEqual(result["action"], "check")
        self.assertTrue(result["postgres"])


class LocalGitDiffTests(unittest.TestCase):
    def test_renaming_critical_file_cannot_hide_old_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()

            git("init")
            git("config", "user.name", "Synthetic CI Test")
            git("config", "user.email", "ci@example.invalid")
            git("config", "commit.gpgsign", "false")
            hooks = root / "inert-hooks"
            hooks.mkdir()
            git("config", "core.hooksPath", str(hooks))
            (root / "studio_platform").mkdir()
            (root / "studio_platform/auth.py").write_text("synthetic fixture\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "fixture base")
            base = git("rev-parse", "HEAD")
            (root / "yingxu/src").mkdir(parents=True)
            git("mv", "studio_platform/auth.py", "yingxu/src/App.jsx")
            git("commit", "-m", "fixture rename")
            head = git("rev-parse", "HEAD")
            changes = ci.git_changes(root, base, head)
            self.assertIn(("D", "studio_platform/auth.py"), changes)
            self.assertIn(("A", "yingxu/src/App.jsx"), changes)
            self.assertTrue(ci.classify(changes)["postgres"])
            with self.assertRaises(ValueError):
                ci.git_changes(root, base, BASE)
            with self.assertRaises(ValueError):
                ci.git_changes(root, "0" * 40, head)


if __name__ == "__main__":
    unittest.main()

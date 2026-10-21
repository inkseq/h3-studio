"""CI scope must fail closed; fixtures never invoke GitHub, Docker or a cloud."""

from pathlib import Path
import io
import json
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
        for gate in ("python", "postgres", "frontend", "containers"):
            self.assertTrue(result[gate], gate)
        self.assertTrue(result["macos_helper"])
        self.assertIsNone(result["slow_modules"])

    def test_only_explicit_document_modifications_skip_heavy_checks(self):
        result = ci.classify(
            [("M", "README.md"), ("M", "deploy/platform/RELEASE.zh-CN.md")]
        )
        self.assertEqual(result["category"], "docs")
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for path in ("new-guide.md", "skills/inkseq/SKILL.md", "tools/README.md"):
            with self.subTest(path=path):
                self.assert_full(ci.classify([("M", path)]))

    def test_reviewed_governance_modifications_are_docs_only(self):
        paths = (
            "AGENTS.md",
            "WORKFLOW.md",
            "docs/WORKFLOW.zh-CN.md",
            "docs/PROJECT-PLAN.md",
            "docs/CURRENT-BASELINE.md",
            "docs/GENERATION-CONTRACT.md",
            "docs/GENERATION-FOUNDATION-RESULT.md",
            "docs/PLANNING-INDEX.zh-CN.md",
            "docs/UNIFIED-BACKEND-API-PLAN.zh-CN.md",
            "docs/REUSE-AND-MIGRATION-DECISION.zh-CN.md",
        )
        # Release preparation imports DOCUMENTS as a dirty-source exemption.
        # Governance-only CI must not silently broaden that separate boundary.
        self.assertFalse(set(paths) & ci.DOCUMENTS)
        changes = [("M", path) for path in paths]
        result = ci.classify(changes)
        self.assertEqual(result["category"], "docs")
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for path in paths:
            self.assert_full(ci.classify([("T", path)]))

    def test_prose_additions_and_archives_do_not_run_runtime_suites(self):
        result = ci.classify(
            [
                ("D", "docs/old-guide.md"),
                ("A", "docs/archive/20261019/old-guide.md"),
                ("A", "docs/new-guide.md"),
            ]
        )
        self.assertEqual(result["category"], "docs")
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for path in (
            "docs/new-security.md",
            "docs/auth-policy.md",
            "docs/secrets.md",
            "skills/inkseq/SKILL.md",
            "tools/new-guide.md",
        ):
            self.assert_full(ci.classify([("A", path)]))
        self.assert_full(
            ci.classify(
                [
                    ("D", "studio_platform/auth.py"),
                    ("A", "docs/archive/removed-auth.md"),
                ]
            )
        )

    def test_governance_docs_cannot_hide_critical_changes(self):
        for path in (
            "studio_platform/auth.py",
            "studio_platform/settings.py",
            "studio_platform/queue.py",
            "studio_platform/on_demand_scaler.py",
            "tools/ci_changes.py",
            ".github/workflows/ci.yml",
            "workflow/project.json",
            ".gitignore",
            ".dockerignore",
            "skills/inkseq/SKILL.md",
            "new-policy.md",
        ):
            with self.subTest(path=path):
                self.assert_full(
                    ci.classify([("M", "AGENTS.md"), ("M", "WORKFLOW.md"), ("M", path)])
                )

    def test_docs_only_push_keeps_the_stable_test_aggregator(self):
        with (
            patch.object(
                ci,
                "git_changes",
                return_value=[("M", "AGENTS.md"), ("M", "WORKFLOW.md")],
            ),
            patch.object(
                ci,
                "staging_platform_baseline",
                return_value={"commit": BASE, "run_id": 1, "run_attempt": 1},
            ),
        ):
            result = ci.plan("push", {"before": BASE}, SHA, MAIN, Path("."))
        self.assertEqual(result["category"], "docs")
        self.assertEqual(result["action"], "check")
        self.assertFalse(any(result[gate] for gate in ci.GATES))

        # Without a YAML dependency, guard the small literal workflow contract:
        # no workflow-level paths filter may leave the required check pending,
        # and unselected heavy jobs still flow into the unconditional aggregator.
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text(encoding="utf-8")
        triggers = workflow.split("\njobs:", 1)[0]
        self.assertNotRegex(triggers, r"(?m)^\s*paths(?:-ignore)?:")
        aggregate = workflow.split("\n  test:\n", 1)[1].split("\n  publish-aws:\n", 1)[
            0
        ]
        self.assertIn("if: always()", aggregate)
        self.assertIn(
            "needs: [changes, python, postgres, frontend, frontend-tools, containers, macos-helper]",
            aggregate,
        )
        self.assertIn(
            "expected = 'success' if selected == 'true' else 'skipped'", aggregate
        )

    def test_only_frontend_snapshot_can_use_frontend_gates(self):
        result = ci.classify(
            [("M", "yingxu/src/Freestyle.jsx"), ("M", "yingxu/source-manifest.json")]
        )
        self.assertTrue(result["frontend"])
        self.assertTrue(result["frontend_tools"])
        for gate in ("python", "postgres", "containers"):
            self.assertFalse(result[gate], gate)
        for path in (
            "web/app.js",
            "other/src/App.jsx",
            "yingxu/start.sh",
            "yingxu/.env",
        ):
            self.assert_full(ci.classify([("M", path)]))

    def test_backend_changes_keep_full_python_database_and_image_checks(self):
        result = ci.classify([("M", "studio_platform/guided.py")])
        self.assertTrue(result["python"])
        self.assertTrue(result["postgres"])
        self.assertTrue(result["containers"])
        self.assertFalse(result["frontend"])
        self.assertFalse(result["macos_helper"])
        combined = ci.classify(
            [("M", "studio_platform/guided.py"), ("M", "yingxu/src/App.jsx")]
        )
        self.assertTrue(combined["frontend"])
        self.assertTrue(combined["python"])

    def test_slow_cpu_integration_selection_tracks_only_reviewed_dependencies(self):
        unrelated = ci.classify([("M", "studio_platform/agent_discovery.py")])
        self.assertEqual(unrelated["slow_modules"], [])
        media = ci.classify([("M", "studio_platform/render_backend.py")])
        self.assertIn("test_platform_render_backend", media["slow_modules"])
        self.assertIn("test_platform_render_subtitles", media["slow_modules"])
        self.assertNotIn("test_platform_backup_postgres", media["slow_modules"])
        for path in (
            "studio_platform/repository.py",
            "studio_platform/auth.py",
            "studio_platform/dstack_controller.py",
            "studio_platform/dbos_transport.py",
        ):
            self.assert_full(
                ci.classify([("M", "studio_platform/agent_discovery.py"), ("M", path)])
            )

    def test_native_keychain_gate_cannot_be_skipped_for_helper_or_unknown_changes(self):
        for path in (
            "skills/inkseq/scripts/connect.py",
            "test_agent_connect_macos_native.py",
            ".github/workflows/ci.yml",
            "new-security-code.py",
        ):
            self.assertTrue(ci.classify([("M", path)])["macos_helper"])
        for changes in (
            [("M", "README.md")],
            [("M", "yingxu/src/App.jsx")],
            [("M", "studio_platform/guided.py")],
        ):
            self.assertFalse(ci.classify(changes)["macos_helper"])
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text(encoding="utf-8")
        block = workflow.split("\n  macos-helper:\n", 1)[1].split("\n  python:\n", 1)[0]
        self.assertIn("if: needs.changes.outputs.macos_helper == 'true'", block)
        self.assertIn("os: [macos-15, macos-15-intel]", block)
        self.assertIn("test_agent_connect_macos_native", block)
        self.assertIn('sys.platform == "darwin" or sys.exit', block)
        self.assertIn("persist-credentials: false", block)
        self.assertNotIn("secrets.", block)
        self.assertNotIn("pip install", block)

    def test_security_billing_provider_and_ci_changes_require_every_gate(self):
        for path in (
            "studio_platform/auth.py",
            "studio_platform/on_demand_scaler.py",
            "studio_platform/repository.py",
            "studio_platform/queue.py",
            "studio_platform/lium_provider.py",
            "studio_platform/unknown.py",
            "tools/ci_changes.py",
            ".github/workflows/ci.yml",
            "Dockerfile.platform",
            ".dockerignore",
            "requirements.lock.txt",
            "deploy/platform/release.py",
        ):
            with self.subTest(path=path):
                self.assert_full(ci.classify([("M", path)]))

    def test_added_removed_renamed_and_type_changed_safe_paths_still_require_full(self):
        for status in ("A", "D", "T", "U", "R100", "C100"):
            for path in ("yingxu/src/New.jsx", "tools/new-guide.md"):
                self.assert_full(ci.classify([(status, path)]))
        self.assert_full(ci.classify([("T", "README.md")]))
        self.assert_full(
            ci.classify(
                [("D", "studio_platform/auth.py"), ("A", "yingxu/src/Auth.jsx")]
            )
        )

    def test_unsafe_paths_never_reduce_verification(self):
        for path in (
            "yingxu/src/../../auth.py",
            "/yingxu/src/App.jsx",
            "yingxu\\src\\App.jsx",
            "yingxu/src//App.jsx",
            "yingxu/src/App.jsx\n",
            "C:/yingxu/src/App.jsx",
        ):
            self.assert_full(ci.classify([("M", path)]))

    def test_full_diff_is_not_truncated_at_github_path_filter_limit(self):
        raw = b"".join(f"M\0yingxu/src/View{n}.jsx\0".encode() for n in range(700))
        raw += b"M\0studio_platform/auth.py\0"
        changes = ci.parse_diff(raw)
        self.assertEqual(len(changes), 701)
        self.assert_full(ci.classify(changes))

    def test_nul_parser_rejects_partial_or_rename_records(self):
        self.assertEqual(ci.parse_diff(b"M\0README.md\0"), [("M", "README.md")])
        for raw in (
            b"M\0README.md",
            b"M\0",
            b"R100\0old\0new\0",
            b"M\0\0",
            b"M\0\xff\0",
        ):
            with self.assertRaises((ValueError, UnicodeError)):
                ci.parse_diff(raw)

    def test_git_unavailable_is_full_instead_of_empty_success(self):
        with patch.object(
            ci, "git_changes", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assert_full(ci.plan("push", {"before": BASE}, SHA, MAIN, Path(".")))
        with patch.object(
            ci, "git_changes", side_effect=ValueError("missing baseline")
        ):
            self.assert_full(ci.plan("pull_request", {}, SHA, MAIN, Path(".")))

    def test_pr_uses_tested_merge_sha_and_full_local_baseline(self):
        with patch.object(ci, "git_changes", return_value=[("M", "README.md")]) as read:
            result = ci.plan(
                "pull_request",
                {"pull_request": {"base": {"sha": BASE}}},
                SHA,
                "refs/pull/7/merge",
                Path("fixture"),
            )
        read.assert_called_once_with(Path("fixture"), BASE, SHA)
        self.assertEqual(result["category"], "docs")


class StagingBaselineTests(unittest.TestCase):
    def run_record(self, run_id=7, **changes):
        return dict(
            id=run_id,
            run_attempt=2,
            repository={"full_name": ci.REPOSITORY},
            path=ci.WORKFLOW_PATH,
            event="push",
            head_branch="main",
            head_sha=BASE,
            status="completed",
            conclusion="success",
            **changes,
        )

    def jobs(self, run_id=7, *, containers="success", deploy="success"):
        return {
            "total_count": 2,
            "jobs": [
                dict(
                    run_id=run_id,
                    head_sha=BASE,
                    run_attempt=2,
                    name=name,
                    status="completed",
                    conclusion=conclusion,
                )
                for name, conclusion in (
                    ("containers", containers),
                    ("deploy-staging-aws", deploy),
                )
            ],
        }

    def test_only_same_attempt_successful_platform_deploy_is_a_baseline(self):
        requested = []

        def read(path):
            requested.append(path)
            if "/workflows/" in path:
                return {"workflow_runs": [self.run_record(i) for i in (9, 8, 7)]}
            if "/runs/9/" in path:  # Green docs run, no deployment.
                return self.jobs(9, containers="skipped", deploy="skipped")
            if "/runs/8/" in path:  # Green frontend deployment, no platform.
                return self.jobs(8, containers="skipped")
            return self.jobs()

        with patch.object(ci, "git_ancestor") as ancestor:
            result = ci.staging_platform_baseline(Path("fixture"), SHA, read_json=read)
        self.assertEqual(result, {"commit": BASE, "run_id": 7, "run_attempt": 2})
        ancestor.assert_called_once_with(Path("fixture"), BASE, SHA)
        self.assertTrue(all("/attempts/2/jobs?" in p for p in requested[1:]))
        self.assertTrue(
            all(p.startswith("/repos/inkseq/h3-studio/actions/") for p in requested)
        )

    def test_cross_repository_workflow_and_job_identity_cannot_prove_a_deploy(self):
        cases = [
            ({"repository": {"full_name": "other/repo"}}, None),
            ({"path": ".github/workflows/other.yml"}, None),
            ({"head_branch": "feature"}, None),
            ({"event": "pull_request"}, None),
            ({"repository": None}, None),
            ({}, {"run_id": 8}),
            ({}, {"head_sha": SHA}),
            ({}, {"run_attempt": 1}),
            ({}, {"name": {}}),
        ]
        for run_change, job_change in cases:
            record = self.run_record()
            record.update(run_change)
            jobs = self.jobs()
            if job_change:
                jobs["jobs"][0].update(job_change)
            with (
                self.subTest(run=run_change, job=job_change),
                patch.object(ci, "git_ancestor") as ancestor,
                self.assertRaises(ValueError),
            ):
                ci.staging_platform_baseline(
                    Path("."),
                    SHA,
                    read_json=lambda path: (
                        {"workflow_runs": [record]} if "/workflows/" in path else jobs
                    ),
                )
            ancestor.assert_not_called()

    def test_failed_deploy_incomplete_jobs_and_bounded_history_are_not_success(self):
        for jobs in (
            self.jobs(deploy="failure"),
            self.jobs(deploy="cancelled"),
            dict(self.jobs(), total_count=101),
        ):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                ci.staging_platform_baseline(
                    Path("."),
                    SHA,
                    read_json=lambda path: (
                        {"workflow_runs": [self.run_record()]}
                        if "/workflows/" in path
                        else jobs
                    ),
                )
        pages = []

        def read(path):
            if "/workflows/" in path:
                pages.append(path)
                return {
                    "workflow_runs": [
                        self.run_record(i + 1) for i in range(ci.RUNS_PER_PAGE)
                    ]
                }
            ident = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            return self.jobs(ident, containers="skipped")

        with self.assertRaises(ValueError):
            ci.staging_platform_baseline(Path("."), SHA, read_json=read)
        self.assertEqual(len(pages), ci.MAX_RUN_PAGES)

    def test_runtime_from_cancelled_predecessor_forces_full_main_platform(self):
        with (
            patch.object(
                ci,
                "staging_platform_baseline",
                return_value={"commit": BASE, "run_id": 7, "run_attempt": 2},
            ),
            patch.object(
                ci,
                "git_changes",
                side_effect=[
                    [("M", "yingxu/src/App.jsx")],
                    [("M", "studio_platform/frontend.py"), ("M", "yingxu/src/App.jsx")],
                ],
            ) as read,
        ):
            result = ci.plan("push", {"before": "c" * 40}, SHA, MAIN, Path("."))
        self.assertEqual(result["action"], "stage-platform")
        self.assertEqual(result["reason"], "unshipped-platform-changes")
        self.assertTrue(
            all(result[g] for g in ("python", "postgres", "containers", "frontend"))
        )
        self.assertEqual(read.call_args_list[-1].args, (Path("."), BASE, SHA))
        self.assertEqual(result["staging_platform_baseline"]["run_attempt"], 2)

    def test_known_runtime_keeps_frontend_docs_fast_unknown_history_never_does(self):
        for changes, action in (
            ([("M", "README.md")], "check"),
            ([("M", "yingxu/src/App.jsx")], "stage-frontend"),
        ):
            with (
                self.subTest(changes=changes),
                patch.object(
                    ci,
                    "staging_platform_baseline",
                    return_value={"commit": BASE, "run_id": 7, "run_attempt": 2},
                ),
                patch.object(ci, "git_changes", return_value=changes),
            ):
                known = ci.plan("push", {"before": BASE}, SHA, MAIN, Path("."))
            self.assertEqual(known["action"], action)
            self.assertFalse(known["containers"])
            for error in (
                ValueError("missing"),
                OSError("unavailable"),
                subprocess.CalledProcessError(1, "git"),
            ):
                with (
                    patch.object(ci, "staging_platform_baseline", side_effect=error),
                    patch.object(ci, "git_changes", return_value=changes),
                ):
                    unknown = ci.plan("push", {"before": BASE}, SHA, MAIN, Path("."))
                self.assertEqual(unknown["action"], "stage-platform")
                self.assertTrue(unknown["macos_helper"])

    def test_pr_never_reads_deployment_history(self):
        with (
            patch.object(
                ci,
                "staging_platform_baseline",
                side_effect=AssertionError("no API on PR"),
            ) as read,
            patch.object(ci, "git_changes", return_value=[("M", "README.md")]),
        ):
            result = ci.plan(
                "pull_request",
                {"pull_request": {"base": {"sha": BASE}}},
                SHA,
                "refs/pull/1/merge",
                Path("."),
            )
        self.assertFalse(result["containers"])
        read.assert_not_called()

    def test_github_reader_does_not_follow_redirect_or_expose_token_errors(self):
        class Response(io.BytesIO):
            status = 200
            url = "https://api.github.com/repos/inkseq/h3-studio/actions/test"

        with (
            patch.dict(
                ci.os.environ,
                {
                    "GITHUB_REPOSITORY": ci.REPOSITORY,
                    "GITHUB_TOKEN": "synthetic-runner-token",
                },
            ),
            patch.object(ci.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.return_value = Response(
                json.dumps({"jobs": []}).encode()
            )
            self.assertEqual(
                ci.github_json("/repos/inkseq/h3-studio/actions/test"), {"jobs": []}
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, Response.url)
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 5)
            response = Response(b"{}")
            response.url = "https://other.invalid/"
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "^staging_baseline_unavailable$"):
                ci.github_json("/repos/inkseq/h3-studio/actions/test")
        with self.assertRaisesRegex(ValueError, "redirect_refused"):
            ci.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.invalid/"
            )

    def test_changes_job_has_actions_read_only_and_scoped_token(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text()
        changes = workflow.split("\n  changes:\n", 1)[1].split("\n  macos-helper:", 1)[
            0
        ]
        self.assertIn("permissions: {contents: read, actions: read}", changes)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", changes)
        self.assertNotIn("id-token: write", changes)


class DispatchTests(unittest.TestCase):
    def test_native_retention_is_explicit_platform_prepare_only(self):
        values = {
            "retain_native_sources_run": "42",
            "retain_native_sources_commit": BASE,
            "retain_native_sources_index_sha256": "c" * 64,
        }
        self.assertEqual(
            ci.dispatch_action(
                dict(values, deploy="true", release_kind="platform"), MAIN
            ),
            "prepare-platform",
        )
        for change in (
            {},
            {"deploy": "true", "release_kind": "frontend"},
            {"approved_commit": SHA},
            {"approved_frontend_commit": SHA},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ci.dispatch_action(dict(values, **change), MAIN)
        for key in values:
            incomplete = dict(values, deploy="true", release_kind="platform")
            incomplete.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                ci.dispatch_action(incomplete, MAIN)

    def test_platform_preparation_is_always_full_and_not_deployment(self):
        result = ci.plan(
            "workflow_dispatch",
            {"inputs": {"deploy": "true", "release_kind": "platform"}},
            SHA,
            MAIN,
            Path("."),
        )
        self.assertEqual(result["action"], "prepare-platform")
        self.assertEqual(result["category"], "full")
        self.assertTrue(result["postgres"])

    def test_frontend_artifact_is_independent_of_python_pg_and_images(self):
        result = ci.plan(
            "workflow_dispatch",
            {"inputs": {"deploy": True, "release_kind": "frontend"}},
            SHA,
            MAIN,
            Path("."),
        )
        self.assertEqual(result["action"], "prepare-frontend")
        self.assertTrue(result["frontend"])
        self.assertTrue(result["frontend_tools"])
        for gate in ("python", "postgres", "containers"):
            self.assertFalse(result[gate], gate)

    def test_accepted_promotion_preserves_exact_commit_without_rebuilding(self):
        result = ci.plan(
            'workflow_dispatch',
            {'inputs': {'promote_commit': BASE}},
            SHA,
            MAIN,
            Path('.'),
        )
        self.assertEqual(result['action'], 'promote')
        self.assertFalse(any(result[gate] for gate in ci.GATES))
        for key in ('approved_commit', 'approved_frontend_commit'):
            with self.assertRaisesRegex(ValueError, 'staging_accepted'):
                ci.dispatch_action({key: BASE}, MAIN)

    def test_conflicting_inputs_invalid_refs_and_incomplete_hashes_fail(self):
        cases = [
            {"deploy": True, "approved_commit": SHA},
            {"approved_commit": SHA, "approved_frontend_commit": BASE},
            {"release_kind": "skip-everything"},
            {"deploy": 1},
            {"approved_commit": "main"},
            {"approved_frontend_commit": "a" * 39},
            {"resume_command": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            {"approved_commit": SHA, "resume_command": "not-a-command-id"},
        ]
        for inputs in cases:
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                ci.dispatch_action(inputs, MAIN)
        with self.assertRaises(ValueError):
            ci.dispatch_action({"deploy": True}, "refs/heads/topic")

    def test_push_cannot_smuggle_workflow_dispatch_inputs(self):
        with patch.object(
            ci, "git_changes", return_value=[("M", "studio_platform/auth.py")]
        ):
            result = ci.plan(
                "push",
                {
                    "before": BASE,
                    "inputs": {"deploy": True, "release_kind": "frontend"},
                },
                SHA,
                MAIN,
                Path("."),
            )
        self.assertEqual(result["action"], "stage-platform")
        self.assertTrue(result["postgres"])


class LocalGitDiffTests(unittest.TestCase):
    def test_renaming_critical_file_cannot_hide_old_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return (
                    subprocess.check_output(
                        ["git", *args], cwd=root, stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )

            git("init")
            git("config", "user.name", "Synthetic CI Test")
            git("config", "user.email", "ci@example.invalid")
            git("config", "commit.gpgsign", "false")
            hooks = root / "inert-hooks"
            hooks.mkdir()
            git("config", "core.hooksPath", str(hooks))
            (root / "studio_platform").mkdir()
            (root / "studio_platform/auth.py").write_text(
                "synthetic fixture\n", encoding="utf-8"
            )
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

"""Pure tests for disposable-stack safety; no Docker command or secret use."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.check_platform_stack import DockerDrill, StackCheckError, LABEL, IMAGES, application_commit, child_main, main


class StackDrillSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sixnine-stack-safety-")
        self.addCleanup(temporary.cleanup)
        self.drill = DockerDrill(Path(temporary.name), IMAGES[0])

    def test_image_selection_is_explicit_and_distinguishes_precommit(self):
        self.assertIsNone(application_commit(IMAGES[0]))
        self.assertEqual(application_commit("sixnine-platform:"+"a"*40), "a"*40)
        for value in (None, "", "sixnine-platform:latest", "other-image:"+"a"*40, "sixnine-platform:123"):
            with self.subTest(value=value), self.assertRaises(StackCheckError):
                application_commit(value)
        with patch.object(DockerDrill, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main([])
        run.assert_not_called()

    def test_commit_tag_requires_matching_image_label_before_resource_creation(self):
        self.drill.application_image = "sixnine-platform:"+"a"*40
        self.drill.commit = "a"*40
        result = subprocess.CompletedProcess([], 0, json.dumps([{"Id": "sha256:"+"b"*64,
            "Config": {"Labels": {"org.opencontainers.image.revision": "c"*40}}}]), "")
        with patch.object(self.drill, "command", return_value=result) as command:
            with self.assertRaisesRegex(StackCheckError, "revision_does_not_match_commit"):
                self.drill.run()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(self.drill.resources, [])

    def test_cleanup_refuses_wrong_label_before_any_removal(self):
        name = self.drill.prefix+"-foreign"
        self.drill.resources = [("container", name)]
        with patch.object(self.drill, "command", return_value=subprocess.CompletedProcess([], 0,
            json.dumps([{"Config": {"Labels": {LABEL: "another-owner"}}}]), "")) as command:
            with self.assertRaisesRegex(StackCheckError, "different_label"):
                self.drill.cleanup()
        self.assertEqual(command.call_args_list[0].args, ("container", "inspect", name))
        self.assertEqual(command.call_count, 1)

    def test_cleanup_only_removes_recorded_owned_resources_and_verifies_empty_label(self):
        resources = [(kind, self.drill.prefix+"-"+kind) for kind in ("network", "volume", "container")]
        self.drill.resources = resources
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if len(args) > 1 and args[1] == "inspect":
                info = {"Labels": {LABEL: self.drill.run_id}}
                if args[0] == "container":
                    info = {"Config": info}
                return subprocess.CompletedProcess(args, 0, json.dumps([info]), "")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(self.drill, "command", side_effect=command):
            self.drill.cleanup()
        removals = [args for args in calls if "rm" in args]
        self.assertEqual(removals, [("rm", "-f", resources[2][1]), ("volume", "rm", resources[1][1]), ("network", "rm", resources[0][1])])
        self.assertEqual(sum("--filter" in args for args in calls), 3)
        self.assertTrue(all("prune" not in args for args in calls))

    def test_created_container_is_tracked_before_start_failure_and_never_pulls(self):
        def command(*args, **kwargs):
            if args[0] == "start":
                raise StackCheckError("synthetic_start_failure")
            return subprocess.CompletedProcess(args, 0, "synthetic-id", "")
        with patch.object(self.drill, "command", side_effect=command) as mocked:
            with self.assertRaisesRegex(StackCheckError, "start_failure"):
                self.drill.create("container", "failed", "existing-image", "python")
        self.assertEqual(self.drill.resources, [("container", self.drill.prefix+"-failed")])
        self.assertIn("--pull=never", mocked.call_args_list[0].args)
        self.assertIn("--log-driver", mocked.call_args_list[0].args)

    def test_child_failure_never_prints_fixture_or_exception_details(self):
        canary = "SYNTHETIC-SECRET-NEVER-PRINT"
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"superdan": canary})+"\n")), \
             patch("tools.check_platform_stack.provision_fixture", side_effect=ValueError(canary)), \
             contextlib.redirect_stdout(output):
            self.assertEqual(child_main("provision"), 1)
        self.assertNotIn(canary, output.getvalue())
        self.assertEqual(json.loads(output.getvalue()), {"stage": "failed", "reason": "isolated_child_failed"})


if __name__ == "__main__":
    unittest.main()

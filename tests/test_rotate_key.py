"""Key rotation command (#976): fakes only; no registry, AWS or provider calls."""

from datetime import datetime, timezone
import io
import json
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
import tempfile
from types import SimpleNamespace
import unittest

from tools import rotate_key as rk

SECRET = "rk-NEW-" + "s3cr3t" * 6
NOW = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)


class FakeSSM:
    def __init__(self, current=None, statuses=("Success", "Success")):
        self.current, self.statuses = current, list(statuses)
        self.puts, self.sent = [], []

    def get_parameter(self, Name, WithDecryption):
        if self.current is None:
            raise type("ParameterNotFound", (Exception,), {})()
        return {"Parameter": {"Version": 7, "Value": self.current}}

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)

    def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-" + str(len(self.sent))}}

    def get_command_invocation(self, CommandId, InstanceId):
        return {"Status": self.statuses.pop(0)}


class RotateKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inbox").mkdir()
        for name in ("install.sh", "verify.sh"):
            (self.root / name).write_text("echo private host step\n")
        self.spec = {
            "service": "everyframe",
            "profile_prefix": "everyframe--inkseq",
            "env_field": "EVERYFRAME_API_KEY",
            "console_url": "https://provider.example/keys",
            "parameter": {
                "region": "ap-southeast-1",
                "name": "/inkseq/staging/example",
                "json_field": "api_key",
            },
            "hosts": {
                "staging": {
                    "instance_id": "i-0123456789abcdef0",
                    "install": str(self.root / "install.sh"),
                    "verify": str(self.root / "verify.sh"),
                }
            },
        }
        self.imports = []

    def runner(self, command, **kwargs):
        self.imports.append(command)
        return SimpleNamespace(returncode=0)

    def loader(self, service, profile):
        self.assertEqual(profile, "everyframe--inkseq-20261110")
        return SimpleNamespace(env={"EVERYFRAME_API_KEY": SECRET}, api_key=None)

    def run_execute(self, ssm, answers=("done",), spec=None):
        drop = self.root / "drop"

        def ask(_prompt):
            path = drop / "everyframe.env"
            path.write_text("EVERYFRAME_API_KEY=" + SECRET + "\n")
            return answers[0] if len(answers) == 1 else answers.pop(0)

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            result = rk.execute(
                "everyframe",
                spec or self.spec,
                ask=ask,
                say=print,
                run=self.runner,
                loader=self.loader,
                ssm_factory=lambda region: ssm,
                now=NOW,
                drop=drop,
                registry=self.root,
                sleep=lambda _s: None,
            )
        return result, out.getvalue(), drop

    def test_map_validation_is_closed(self):
        rk.target_spec({"version": 1, "targets": {"x": self.spec}}, "x")
        for change in (
            {"service": "Bad Name"},
            {"env_field": "lower"},
            {"console_url": "http://insecure"},
            {"parameter": {"name": "relative", "region": "r"}},
            {"hosts": {"dev": self.spec["hosts"]["staging"]}},
        ):
            with self.subTest(change=change), self.assertRaises(rk.RotationError):
                rk.target_spec(
                    {"version": 1, "targets": {"x": {**self.spec, **change}}}, "x"
                )

    def test_plan_lists_steps_and_changes_nothing(self):
        lines = rk.plan("everyframe", self.spec, NOW)
        self.assertIn("everyframe--inkseq-20261110", lines[0])
        self.assertEqual(len(lines), 6)

    def test_execute_rotates_without_printing_the_key(self):
        ssm = FakeSSM(current=json.dumps({"api_key": "old", "other": 1}))
        result, output, drop = self.run_execute(ssm)
        self.assertNotIn(SECRET, output)
        self.assertEqual(result["previous_parameter_version"], 7)
        put = ssm.puts[0]
        self.assertEqual(put["Type"], "SecureString")
        self.assertTrue(put["Overwrite"])
        self.assertEqual(json.loads(put["Value"]), {"api_key": SECRET, "other": 1})
        # Import takes only a file path, never the value.
        self.assertNotIn(SECRET, " ".join(self.imports[0]))
        self.assertIn("--profile", self.imports[0])
        self.assertEqual(len(ssm.sent), 2)  # install, then verify
        self.assertFalse((drop / "everyframe.env").exists())
        notes = list((self.root / "inbox").iterdir())
        self.assertEqual(len(notes), 1)
        self.assertNotIn(SECRET, notes[0].read_text(encoding="utf-8"))
        self.assertIn("revoke the OLD key", output)

    def test_plain_parameter_and_missing_previous_version(self):
        spec = {
            **self.spec,
            "parameter": {**self.spec["parameter"], "json_field": None},
        }
        ssm = FakeSSM(current=None)
        result, _, _ = self.run_execute(ssm, spec=spec)
        self.assertEqual(ssm.puts[0]["Value"], SECRET)
        self.assertIsNone(result["previous_parameter_version"])

    def test_failed_install_stops_before_verify_and_keeps_drop_file(self):
        ssm = FakeSSM(current="{}", statuses=("Failed",))
        with self.assertRaisesRegex(
            rk.RotationError, "rotation_install_staging_failed"
        ):
            self.run_execute(ssm)
        self.assertEqual(len(ssm.sent), 1)
        self.assertTrue((self.root / "drop" / "everyframe.env").exists())

    def test_production_needs_typed_confirmation(self):
        spec = {
            **self.spec,
            "hosts": {"production": self.spec["hosts"]["staging"]},
        }
        ssm = FakeSSM(current="{}")
        with self.assertRaisesRegex(rk.RotationError, "production_not_confirmed"):
            self.run_execute(ssm, answers=["done", "yes"], spec=spec)
        self.assertEqual(ssm.puts, [])

    def test_owner_can_stop_before_anything_changes(self):
        drop = self.root / "drop"
        with self.assertRaisesRegex(rk.RotationError, "stopped_by_owner"):
            rk.execute(
                "everyframe",
                self.spec,
                ask=lambda _p: "stop",
                say=lambda *_: None,
                run=self.runner,
                loader=self.loader,
                ssm_factory=lambda region: FakeSSM(),
                now=NOW,
                drop=drop,
                registry=self.root,
            )
        self.assertEqual(self.imports, [])
        self.assertEqual((drop / "everyframe.env").read_text(), "EVERYFRAME_API_KEY=\n")

    def test_example_map_is_valid(self):
        value = rk.load_map(rk.ROOT / "tools" / "key-rotation.example.json")
        rk.target_spec(value, "example-provider")


if __name__ == "__main__":
    unittest.main()

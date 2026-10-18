"""First boot uses the approved image, preserves configured accounts, stays private."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_platform_release import release, DIRECTORY, COMMIT

with patch.dict(sys.modules, {"release": release}):
    spec = importlib.util.spec_from_file_location("sixnine_bootstrap_test", DIRECTORY / "bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls, self.users = [], []
        self.env = {"SIXNINE_POSTGRES_IMAGE": "postgres@sha256:"+"1"*64, "SIXNINE_CADDY_IMAGE": "caddy@sha256:"+"2"*64}
        for name, replacement in {
            "regular": lambda *a, **kw: None,
            "approved_manifest": lambda *a: self.calls.append("approval"),
            "prepare_bundle": lambda *a: self.root / "releases" / COMMIT,
            "deployment_environment": lambda *a: self.env,
            "approved_configuration": lambda *a: self.calls.append("config"),
            "load_approved_image": lambda *a: self.calls.append("load"),
            "compose": lambda *a: self.calls.append(a[2:]),
        }.items():
            manager = patch.object(release, name, replacement)
            manager.start()
            self.addCleanup(manager.stop)
        manager = patch.object(bootstrap, "set_password", lambda d, e, u: self.users.append(u))
        manager.start()
        self.addCleanup(manager.stop)

    def status(self, users, ready=False):
        return {"accounts": [{"username": u, "configured": True, "disabled": False} for u in users], "auth_ready": ready}

    def test_partial_boot_retry_does_not_reset_existing_user_or_start_public_services(self):
        with patch.object(bootstrap, "management_status", side_effect=[self.status(["superdan"]), self.status(["superdan", "supervan"], True)]):
            bootstrap.bootstrap_locked(self.root, COMMIT)
        self.assertEqual(self.users, ["supervan"])
        state = json.loads((self.root / "release-state.json").read_text())
        self.assertIsNone(state["current"])
        self.assertEqual(state["pending"], COMMIT)
        self.assertEqual(state["status"], "accounts_ready_waiting_release")
        self.assertEqual([v for v in self.calls if isinstance(v, tuple) and v[0] == "up"], [("up", "-d", "db")])

    def test_existing_ready_release_cannot_be_reinitialized(self):
        (self.root / "release-state.json").write_text(json.dumps({"current": COMMIT}))
        with self.assertRaisesRegex(release.ReleaseError, "bootstrap_only_for_first_unpublished_release"):
            bootstrap.bootstrap_locked(self.root, COMMIT)
        self.assertEqual(self.calls, [])

    def test_interrupted_account_setup_is_journaled_and_unpublished(self):
        with patch.object(bootstrap, "management_status", return_value=self.status([])), patch.object(bootstrap, "set_password", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                bootstrap.bootstrap_locked(self.root, COMMIT)
        state = json.loads((self.root / "release-state.json").read_text())
        self.assertEqual(state["status"], "prepared_needs_accounts")
        self.assertIsNone(state["current"])


if __name__ == "__main__":
    unittest.main()

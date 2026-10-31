"""RunPod enhance credential transport and install with fake SSM: no AWS, no host changes."""

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.platform import runpod_enhance_credentials as credentials
from studio_platform.enhance_backend import load_config


class SSM:
    def __init__(self):
        self.rows, self.puts = {}, []

    def get_parameters(self, **args):
        assert args["WithDecryption"] is True
        return {"Parameters": [self.rows[n] for n in args["Names"] if n in self.rows]}

    def put_parameter(self, **args):
        assert args["Type"] == "SecureString"
        assert args["Overwrite"] is (args["Name"] in self.rows)
        self.puts.append(args["Name"])
        self.rows[args["Name"]] = {
            "Name": args["Name"],
            "Type": "SecureString",
            "Version": len(self.puts),
            "Value": args["Value"],
        }


def loader(service, *, profile):
    assert (service, profile) == ("runpod", credentials.PROFILE)
    return SimpleNamespace(env={"RUNPOD_API_KEY": "rpa_FAKE000000000000"})


def put(api, endpoint="abc123", rate=970):
    return credentials.import_existing(
        api, loader, "staging", endpoint_id=endpoint, rate_microusd_per_s=rate
    )


class TransportTests(unittest.TestCase):
    def test_import_once_then_same_value_is_a_noop_and_a_new_endpoint_updates(self):
        api = SSM()
        self.assertEqual(put(api)["Name"], "/inkseq/staging/runpod-enhance")
        put(api)
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(put(api, endpoint="def456")["Version"], 2)

    def test_invalid_values_and_targets_are_refused_before_writing(self):
        api = SSM()
        for endpoint, rate in (("../x", 970), ("abc123", 0)):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                put(api, endpoint, rate)
        self.assertEqual(api.puts, [])
        with self.assertRaises(ValueError):
            credentials.name("dev")


class SwitchTests(unittest.TestCase):
    def test_installed_credential_turns_enhance_on_unless_explicitly_off(self):
        from studio_platform.settings import enhance_switch

        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(enhance_switch(None, root))
            self.assertTrue(enhance_switch("1", root))
            secrets = Path(root) / "runtime-secrets"
            secrets.mkdir()
            (secrets / "runpod-enhance.json").write_text("{}")
            self.assertTrue(enhance_switch(None, root))
            self.assertFalse(enhance_switch("0", root))


@unittest.skipIf(os.name == "nt", "POSIX ownership required")
class InstallTests(unittest.TestCase):
    def test_install_writes_root_app_readonly_once_in_the_app_contract(self):
        api = SSM()
        put(api)
        with tempfile.TemporaryDirectory() as root, patch.object(os, "chown"):
            first = credentials.install(api, target="staging", data_root=root)
            again = credentials.install(api, target="staging", data_root=root)
            path = Path(root) / "runtime-secrets" / "runpod-enhance.json"
            self.assertEqual((first["changed"], again["changed"]), (True, False))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            with patch(
                "studio_platform.enhance_backend.read_protected_json",
                return_value=json.loads(path.read_text()),
            ):
                self.assertEqual(load_config(path).endpoint_id, "abc123")


if __name__ == "__main__":
    unittest.main()

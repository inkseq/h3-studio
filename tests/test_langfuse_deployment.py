"""Langfuse credential transport and install with fake SSM: no AWS, no host changes."""

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.platform import langfuse_credentials as credentials
from studio_platform import langfuse_config


class SSM:
    def __init__(self):
        self.rows, self.puts = {}, []

    def get_parameters(self, **args):
        assert args["WithDecryption"] is True
        return {"Parameters": [self.rows[n] for n in args["Names"] if n in self.rows]}

    def put_parameter(self, **args):
        assert args["Overwrite"] is False and args["Type"] == "SecureString"
        self.puts.append(args["Name"])
        self.rows[args["Name"]] = {
            "Name": args["Name"],
            "Type": "SecureString",
            "Version": 1,
            "Value": args["Value"],
        }


def loader(service, *, profile):
    assert (service, profile) == ("langfuse", langfuse_config.PROFILE)
    return SimpleNamespace(
        base_url="https://us.cloud.langfuse.com/",
        env={
            "LANGFUSE_PUBLIC_KEY": "pk-lf-FAKE0000000000000000",
            "LANGFUSE_SECRET_KEY": "sk-lf-FAKE0000000000000000",
        },
    )


class TransportTests(unittest.TestCase):
    def test_import_once_then_same_value_is_a_noop(self):
        api = SSM()
        receipt = credentials.import_existing(api, loader, "staging")
        self.assertEqual(receipt["Name"], "/sixnine/staging/langfuse")
        credentials.import_existing(api, loader, "staging")
        self.assertEqual(api.puts, ["/sixnine/staging/langfuse"])
        row = credentials.read(api, "staging")
        langfuse_config.validate(json.loads(row["Value"]))

    def test_differing_value_and_unknown_target_are_refused(self):
        api = SSM()
        credentials.import_existing(api, loader, "production")
        row = api.rows["/sixnine/production/langfuse"]
        value = json.loads(row["Value"])
        value["public_key"] = "pk-lf-OTHER000000000000000"
        row["Value"] = json.dumps(value)
        with self.assertRaises(ValueError):
            credentials.import_existing(api, loader, "production")
        with self.assertRaises(ValueError):
            credentials.name("dev")
        with self.assertRaises(ValueError):
            credentials.install(SSM(), target="staging", data_root="/nonexistent")


@unittest.skipIf(os.name == "nt", "POSIX ownership required")
class InstallTests(unittest.TestCase):
    def test_install_writes_root_app_readonly_once(self):
        api = SSM()
        credentials.import_existing(api, loader, "staging")
        with tempfile.TemporaryDirectory() as root, patch.object(os, "chown"):
            first = credentials.install(api, target="staging", data_root=root)
            again = credentials.install(api, target="staging", data_root=root)
            path = Path(root) / "runtime-secrets" / "langfuse.json"
            self.assertEqual((first["changed"], again["changed"]), (True, False))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            langfuse_config.validate(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()

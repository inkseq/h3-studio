"""Fake SSM and registry only: no credential reads, SDK calls or host changes."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy.platform import inky_credentials as credentials
from deploy.platform import runtime_secrets_aws as runtime
from studio_platform.inky_config import PROFILES, validate


class SSM:
    def __init__(self):
        self.rows = {}
        self.puts = []
        self.lose_reply = False

    def get_parameters(self, **args):
        assert args == {
            "Names": sorted(credentials.NAMES.values()),
            "WithDecryption": True,
        }
        return {
            "Parameters": list(self.rows.values()),
            "InvalidParameters": sorted(
                set(credentials.NAMES.values()) - set(self.rows)
            ),
        }

    def put_parameter(self, **args):
        assert args["Type"] == "SecureString" and args["Tier"] == "Standard"
        assert args["KeyId"] == "alias/aws/ssm" and args["Overwrite"] is False
        assert args["Name"] not in self.rows
        self.puts.append(args["Name"])
        self.rows[args["Name"]] = {
            "Name": args["Name"],
            "Value": args["Value"],
            "Type": "SecureString",
            "Version": 1,
        }
        if self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("DO NOT PRINT PROVIDER DETAILS")
        return {"Version": 1}


class InkyCredentialTests(unittest.TestCase):
    def setUp(self):
        self.api = SSM()
        self.configs = {
            service: SimpleNamespace(
                base_url=origin, api_key="fake-test-key-" + service
            )
            for service, (_, origin) in PROFILES.items()
        }

    def load(self, service, *, profile):
        self.assertEqual(profile, PROFILES[service][0])
        return self.configs[service]

    def test_import_once_reconcile_and_metadata_only(self):
        self.configs["openrouter"].base_url = None
        result = credentials.import_existing(self.api, self.load)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(set(row) == {"Name", "Type", "Version"} for row in result))
        self.assertNotIn("fake-test", json.dumps(result))
        self.assertEqual(credentials.import_existing(self.api, self.load), result)
        self.assertEqual(len(self.api.puts), 2)

    def test_lost_reply_reconciles_original_without_replaying_put(self):
        self.api.lose_reply = True
        with self.assertRaises(TimeoutError):
            credentials.import_existing(self.api, self.load)
        self.assertEqual(len(self.api.puts), 1)
        credentials.import_existing(self.api, self.load)
        self.assertEqual(len(self.api.puts), 2)
        self.assertEqual(len(set(self.api.puts)), 2)

    def test_validate_all_sources_before_any_write(self):
        self.configs["openrouter"].base_url = "https://wrong.example"
        with self.assertRaisesRegex(ValueError, "inky_profile_invalid"):
            credentials.import_existing(self.api, self.load)
        self.assertFalse(self.api.puts)

    def test_existing_difference_refuses_all_writes(self):
        credentials.import_existing(self.api, self.load)
        del self.api.rows[credentials.NAMES["typesafe"]]
        self.configs["openrouter"].api_key = "different-secret-key"
        with self.assertRaisesRegex(ValueError, "inky_existing_parameter_differs"):
            credentials.import_existing(self.api, self.load)
        self.assertEqual(len(self.api.puts), 2)

    def test_production_missing_or_wrong_type_never_writes_file(self):
        write = Mock()
        with self.assertRaisesRegex(ValueError, "inky_staging_only"):
            credentials.hydrate(Mock(), write, target="production")
        with self.assertRaisesRegex(ValueError, "inky_parameters_missing"):
            credentials.hydrate(self.api, write, target="staging")
        credentials.import_existing(self.api, self.load)
        self.api.rows[credentials.NAMES["typesafe"]]["Type"] = "String"
        with self.assertRaisesRegex(ValueError, "inky_parameter_invalid"):
            credentials.hydrate(self.api, write, target="staging")
        write.assert_not_called()

    def test_hydrate_uses_existing_protected_writer_closed_profile(self):
        credentials.import_existing(self.api, self.load)
        write = Mock()
        receipt = credentials.hydrate(self.api, write, target="staging")
        name, raw = write.call_args.args
        self.assertEqual(name, "inky")
        self.assertEqual(set(validate(json.loads(raw))), set(PROFILES))
        self.assertNotIn("fake-test", str(receipt))

    def test_client_pins_account_region_endpoints_and_disables_write_retries(self):
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {
            "Account": "wrong"
        }
        with patch.object(credentials.boto3, "Session", return_value=session):
            with self.assertRaisesRegex(ValueError, "inky_aws_account_invalid"):
                credentials.client()
        self.assertEqual(session.client.call_count, 1)
        session.reset_mock()
        session.client.return_value.get_caller_identity.return_value = {
            "Account": credentials.ACCOUNT
        }
        with patch.object(
            credentials.boto3, "Session", return_value=session
        ) as factory:
            credentials.client()
        factory.assert_called_once_with(region_name="ap-southeast-1")
        self.assertEqual(
            [c.args[0] for c in session.client.call_args_list], ["sts", "ssm"]
        )
        for call in session.client.call_args_list:
            self.assertEqual(
                call.kwargs["endpoint_url"],
                "https://" + call.args[0] + ".ap-southeast-1.amazonaws.com",
            )
            self.assertEqual(call.kwargs["config"].retries["total_max_attempts"], 1)

    @unittest.skipIf(os.name == "nt", "Root-owned metadata exercised on Linux CI")
    def test_hydration_opt_in_rejects_foreign_path_and_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "migration").mkdir()
            self.assertFalse(
                runtime.workos_requested(target="staging", root=root, service="inky")
            )
            path = root / "migration/paths.json"
            original_lstat, original_fstat = Path.lstat, os.fstat

            def owned_lstat(path):
                values = list(original_lstat(path))
                values[4] = 0
                return os.stat_result(values)

            def owned_fstat(fd):
                values = list(original_fstat(fd))
                values[4] = 0
                return os.stat_result(values)

            with (
                patch.object(Path, "lstat", owned_lstat),
                patch.object(runtime.os, "fstat", owned_fstat),
            ):
                for target, value, allowed in [
                    ("staging", "", False),
                    ("staging", "/run/sixnine-secrets/inky", True),
                    ("staging", "/run/sixnine-secrets/workos", None),
                    ("production", "/run/sixnine-secrets/inky", None),
                ]:
                    path.write_text(
                        json.dumps(
                            {
                                "environment": {
                                    "INKSEQ_DEPLOYMENT_TARGET": target,
                                    "INKSEQ_INKY_CONFIG_FILE": value,
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    path.chmod(0o600)
                    if allowed is None:
                        with self.assertRaises(RuntimeError):
                            runtime.workos_requested(
                                target=target, root=root, service="inky"
                            )
                    else:
                        self.assertEqual(
                            runtime.workos_requested(
                                target=target, root=root, service="inky"
                            ),
                            allowed,
                        )


if __name__ == "__main__":
    unittest.main()

"""Langfuse credential transport and mounts with fake SSM: no AWS, no host changes."""

import copy
import json
from types import SimpleNamespace
import unittest

from deploy.platform import deployment_target, langfuse_credentials as credentials
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
        written = []
        credentials.hydrate(
            api, lambda name, value: written.append((name, value)), target="staging"
        )
        self.assertEqual(written[0][0], "langfuse")
        langfuse_config.validate(json.loads(written[0][1]))

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
            credentials.hydrate(SSM(), lambda *a: None, target="staging")


class MountTests(unittest.TestCase):
    def document(self, enabled=True):
        selected = "/run/secrets/langfuse" if enabled else ""
        return {
            "services": {
                "app": {
                    "environment": {"INKSEQ_LANGFUSE_CONFIG_FILE": selected},
                    "secrets": ["langfuse"],
                },
                "controller": {"environment": {}, "secrets": []},
            },
            "secrets": {
                "langfuse": {
                    "file": "/run/sixnine-secrets/langfuse" if enabled else "/recipes"
                },
                "execution_profiles": {"file": "/recipes"},
            },
        }

    def test_enabled_disabled_and_old_rollouts(self):
        for target in ("staging", "production"):
            deployment_target.langfuse_mounts(self.document(), target=target)
            deployment_target.langfuse_mounts(self.document(False), target=target)
        old = self.document(False)
        old["services"]["app"] = {"environment": {}, "secrets": []}
        del old["secrets"]["langfuse"]
        deployment_target.langfuse_mounts(old, target="production")

    def test_credential_stays_on_app_at_one_path(self):
        original = self.document()
        for mutate in (
            lambda d: d["services"]["controller"]["secrets"].append("langfuse"),
            lambda d: d["services"]["controller"]["environment"].update(
                INKSEQ_LANGFUSE_CONFIG_FILE=""
            ),
            lambda d: d["services"]["app"]["environment"].update(
                INKSEQ_LANGFUSE_CONFIG_FILE="/run/secrets/workos"
            ),
            lambda d: d["secrets"]["langfuse"].update(
                file="/run/sixnine-secrets/workos"
            ),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.assertRaises(ValueError):
                deployment_target.langfuse_mounts(changed, target="staging")

    def test_host_environment_path_and_target(self):
        deployment_target.langfuse_environment({})
        deployment_target.langfuse_environment(
            {
                "INKSEQ_LANGFUSE_CONFIG_FILE": "/run/sixnine-secrets/langfuse",
                "INKSEQ_DEPLOYMENT_TARGET": "production",
            }
        )
        for values in (
            {
                "INKSEQ_LANGFUSE_CONFIG_FILE": "/tmp/x",
                "INKSEQ_DEPLOYMENT_TARGET": "staging",
            },
            {"INKSEQ_LANGFUSE_CONFIG_FILE": "/run/sixnine-secrets/langfuse"},
        ):
            with self.assertRaises(ValueError):
                deployment_target.langfuse_environment(values)


if __name__ == "__main__":
    unittest.main()

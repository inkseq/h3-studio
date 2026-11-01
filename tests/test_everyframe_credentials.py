"""EveryFrame import reads the exact central profile (#918); fake SSM, no network."""

import json
from types import SimpleNamespace
import unittest

from deploy.platform import everyframe_credentials as credentials


class FakeSSM:
    def __init__(self):
        self.value, self.version = None, 0

    def get_parameters(self, Names, WithDecryption):
        if self.value is None:
            return {"Parameters": []}
        return {
            "Parameters": [
                {
                    "Name": Names[0],
                    "Type": "SecureString",
                    "Version": self.version,
                    "Value": self.value,
                }
            ]
        }

    def put_parameter(self, **kwargs):
        self.value, self.version = kwargs["Value"], self.version + 1


def loader(base_url=credentials.ENDPOINT, seen=None):
    def load(service, *, profile):
        if seen is not None:
            seen.append((service, profile))
        return SimpleNamespace(base_url=base_url, env={credentials.VARIABLE: "key-1"})

    return load


class ImportTests(unittest.TestCase):
    def test_exact_profile_is_written_once_and_is_idempotent(self):
        api, seen = FakeSSM(), []
        receipt = credentials.import_existing(api, loader(seen=seen), "staging")
        self.assertEqual(seen, [("everyframe", credentials.PROFILE)])
        self.assertEqual(json.loads(api.value), {"api_key": "key-1"})
        self.assertEqual(receipt["Name"], "/inkseq/staging/everyframe")
        self.assertNotIn("Value", receipt)
        credentials.import_existing(api, loader(), "staging")
        self.assertEqual(api.version, 1)

    def test_another_endpoint_is_refused_before_any_write(self):
        api = FakeSSM()
        with self.assertRaisesRegex(ValueError, "everyframe_endpoint_mismatch"):
            credentials.import_existing(
                api, loader("https://other.example/v1"), "staging"
            )
        self.assertIsNone(api.value)


if __name__ == "__main__":
    unittest.main()

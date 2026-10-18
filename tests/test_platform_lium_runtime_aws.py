"""Offline managed-secret boundary tests; no AWS/provider traffic or real keys."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
import unittest

import httpx

from studio_platform.lium_provider import BASE_URL, KEY_VARIABLE, PROFILE, SERVICE, LiumProvider
from studio_platform.lium_runtime_aws import AwsLiumLoader, RuntimeCredentialError, SECRET_NAME


ARN = "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/lium-ABC123"
VERSION = "12345678-1234-1234-1234-123456789012"
SYNTHETIC = "offline-test-api-key-not-a-real-credential"


def payload():
    return {"schema_version": 1, "service": SERVICE, "profile": PROFILE, "base_url": BASE_URL,
        "primary_key_variable": KEY_VARIABLE, "api_key": SYNTHETIC}


class FakeSecrets:
    def __init__(self, data=None):
        self.calls, self.closed = [], False
        self.response = {"ARN": ARN, "Name": SECRET_NAME, "VersionId": VERSION,
            "SecretString": json.dumps(payload() if data is None else data)}

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.response)

    def close(self):
        self.closed = True


class LiumRuntimeTests(unittest.TestCase):
    def test_explicit_profile_pinned_version_once_in_memory_and_provider_compatibility(self):
        secrets = FakeSecrets()
        before = dict(os.environ)
        loader = AwsLiumLoader(ARN, VERSION, client_factory=lambda: secrets)
        self.assertEqual(secrets.calls, [])
        with ThreadPoolExecutor(max_workers=6) as pool:
            loaded = list(pool.map(lambda _: loader(SERVICE, profile=PROFILE), range(6)))
        self.assertEqual(len({id(config) for config in loaded}), 1)
        self.assertEqual(secrets.calls, [{"SecretId": ARN, "VersionId": VERSION}])
        self.assertEqual(loaded[0].api_key, SYNTHETIC)
        self.assertNotIn(SYNTHETIC, repr(loaded[0]))
        self.assertEqual(dict(os.environ), before)
        requests = []
        def respond(request):
            requests.append((str(request.url), request.headers.get("X-API-Key")))
            return httpx.Response(200, json=[])
        provider = LiumProvider(enabled=True, loader=loader, transport=httpx.MockTransport(respond))
        try:
            self.assertEqual(provider._rows("pods"), [])
            self.assertEqual(requests, [(BASE_URL + "/pods", SYNTHETIC)])
        finally:
            provider.close()
            loader.close()
        self.assertTrue(secrets.closed)

    def test_wrong_requested_identity_denied_before_client_construction(self):
        def deny():
            self.fail("Must not access credential backend for a mismatched identity")
        loader = AwsLiumLoader(ARN, VERSION, client_factory=deny)
        for service, profile in (("other", PROFILE), (SERVICE, "other"), (SERVICE, None)):
            with self.assertRaises(RuntimeCredentialError):
                loader(service, profile=profile)
        for arn, version in ((ARN.replace("ap-southeast-1", "us-east-1"), VERSION),
                             (SECRET_NAME, VERSION), (ARN, "AWSCURRENT")):
            with self.assertRaises(RuntimeCredentialError):
                AwsLiumLoader(arn, version, client_factory=deny)

    def test_profile_endpoint_and_secret_response_conflicts_have_no_fallback(self):
        for field, value in (("service", "other"), ("profile", "other"), ("base_url", "https://other.invalid"),
                             ("primary_key_variable", "OTHER_KEY"), ("schema_version", True),
                             ("api_key", SYNTHETIC + "\n"), ("extra", "unapproved")):
            data = payload()
            data[field] = value
            secrets = FakeSecrets(data)
            with self.subTest(field=field), self.assertRaises(RuntimeCredentialError) as failure:
                AwsLiumLoader(ARN, VERSION, client_factory=lambda: secrets)(SERVICE, profile=PROFILE)
            self.assertNotIn(SYNTHETIC, str(failure.exception))
        for field, value in (("ARN", ARN + "x"), ("Name", "other"), ("VersionId", "0"*32), ("SecretBinary", b"unexpected")):
            secrets = FakeSecrets()
            secrets.response[field] = value
            with self.subTest(response_field=field), self.assertRaises(RuntimeCredentialError):
                AwsLiumLoader(ARN, VERSION, client_factory=lambda: secrets)(SERVICE, profile=PROFILE)

    def test_parse_and_sdk_errors_suppress_raw_values_and_never_cache_failure(self):
        secrets = FakeSecrets()
        secrets.response["SecretString"] = '{"api_key":"' + SYNTHETIC + '","api_key":"duplicate"}'
        loader = AwsLiumLoader(ARN, VERSION, client_factory=lambda: secrets)
        with self.assertRaises(RuntimeCredentialError) as failure:
            loader(SERVICE, profile=PROFILE)
        self.assertNotIn(SYNTHETIC, str(failure.exception))
        secrets.response["SecretString"] = json.dumps(payload())
        self.assertEqual(loader(SERVICE, profile=PROFILE).api_key, SYNTHETIC)
        self.assertEqual(len(secrets.calls), 2)
        class FailingSecrets(FakeSecrets):
            def get_secret_value(self, **kwargs):
                raise RuntimeError("Upstream response included " + SYNTHETIC)
        with self.assertRaises(RuntimeCredentialError) as failure:
            AwsLiumLoader(ARN, VERSION, client_factory=FailingSecrets)(SERVICE, profile=PROFILE)
        self.assertNotIn(SYNTHETIC, str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()

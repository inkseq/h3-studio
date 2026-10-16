"""Release identity guards with fake STS only; never acquire credentials."""
import io
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import check_aws_release_account as guard


class ReleaseAccountTests(unittest.TestCase):
    def test_only_exact_actual_account_is_accepted(self):
        client = Mock()
        client.get_caller_identity.return_value = {"Account": guard.EXPECTED_ACCOUNT}
        self.assertIsNone(guard.verify_account(client=client))
        client.get_caller_identity.assert_called_once_with()
        for response in ({"Account": "999999999999"}, {}, None,
                         {"Account": int(guard.EXPECTED_ACCOUNT)}, {"Account": guard.EXPECTED_ACCOUNT+" "}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                guard.verify_account(client=SimpleNamespace(get_caller_identity=lambda: response))

    def test_real_client_factory_is_pinned_without_credentials_or_retries(self):
        with patch("boto3.client") as factory:
            factory.return_value.get_caller_identity.return_value = {"Account": guard.EXPECTED_ACCOUNT}
            guard.verify_account()
        args, kwargs = factory.call_args
        self.assertEqual(args, ("sts",))
        self.assertEqual(set(kwargs), {"region_name", "endpoint_url", "config"})
        self.assertEqual(kwargs["region_name"], "ap-southeast-1")
        self.assertEqual(kwargs["endpoint_url"], "https://sts.ap-southeast-1.amazonaws.com")
        config = kwargs["config"]
        self.assertEqual(config.signature_version, "v4")
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.assertEqual((config.connect_timeout, config.read_timeout), (10, 15))

    def test_unavailable_identity_stops_cli_without_raw_error_or_success(self):
        with patch.object(guard, "verify_account", side_effect=RuntimeError("private-token-must-not-log")), \
                patch("sys.stderr", io.StringIO()) as errors, patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(guard.main(), 1)
            self.assertNotIn("private-token", errors.getvalue())
            self.assertIn("release operation blocked", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
        with patch.object(guard, "verify_account"), patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(guard.main(), 0)
            self.assertEqual(output.getvalue(), "AWS release caller account verified\n")

    def test_all_cloud_writing_jobs_guard_after_oidc_and_before_release_tool(self):
        source = (Path(__file__).parent/".github/workflows/ci.yml").read_text()
        self.assertNotIn("allowed-account-ids:", source)
        jobs = dict(re.findall(r"(?ms)^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", source))
        expected = {"restore-native": "          python -m tools.restore_native_sources",
            "publish-aws": "tools/publish_aws_release.py",
            "publish-frontend-aws": "tools/publish_frontend_release.py",
            "publish-staging-candidate": "tools/publish_staging_candidate.py",
            "deploy-staging-aws": "tools/deploy_aws_release.py",
            "promote-accepted-aws": "tools/deploy_aws_release.py"}
        guarded = {name for name, body in jobs.items() if "configure-aws-credentials@" in body}
        self.assertEqual(guarded, set(expected))
        for name, tool in expected.items():
            body = jobs[name]
            self.assertIn("configure-aws-credentials@ff717079ee2060e4bcee96c4779b553acc87447c", body)
            self.assertEqual(body.count("run: python tools/check_aws_release_account.py"), 1)
            credential_pos = body.index("configure-aws-credentials@")
            guard_pos = body.index("run: python tools/check_aws_release_account.py")
            operation_pos = body.index(tool)
            self.assertLess(credential_pos, guard_pos)
            self.assertLess(guard_pos, operation_pos)
            guard_step = body[body.rfind("      - name:", 0, guard_pos):guard_pos]
            self.assertNotIn("if:", guard_step)
            self.assertNotIn("continue-on-error", body)


if __name__ == "__main__":
    unittest.main()

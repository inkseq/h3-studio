"""Synthetic managed secrets and isolated root filesystem; never call AWS/GPU."""
import base64
import contextlib
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.platform import migration_secrets_aws as target


def seed():
    return {"schema_version": 1,
        "providers": {"vastai": {"profile_id": "vastai--synthetic-test",
            "base_url": "https://console.vast.ai/api", "api_key": "SYNTHETIC_VAST_"+"x"*40}},
        "grafana_cloud": {"schema_version": 1, "enabled": True,
            "endpoint": "https://otlp-gateway-synthetic.grafana.net/otlp",
            "authorization": "Basic "+base64.b64encode(b"123:SYNTHETIC_ONLY").decode(),
            "service_name": "sixnine-worker", "service_version": "a"*40, "environment": "staging"}}


def initialized():
    value = seed()
    value["infrastructure"] = {"dstack_admin_token": "SYNTHETIC_"+"t"*40,
        "dstack_database_url": "postgresql+asyncpg://dstack_control:"+"p"*48+"@db:5432/sixnine_dstack",
        "hatchet_database_password": "d"*48, "hatchet_administrator_password": "a"*48}
    return value


class FakeAPI:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.version = "original-version"
        self.versions = {self.version: copy.deepcopy(value)}
        self.puts = []
        self.gets = 0
        self.lost_put_ack = False

    def get_secret_value(self, **kwargs):
        assert kwargs.get("SecretId") == target.SECRET and not set(kwargs) - {"SecretId", "VersionId"}
        self.gets += 1
        if "VersionId" in kwargs:
            if kwargs["VersionId"] not in self.versions:
                raise self.exceptions.ResourceNotFoundException()
            return {"SecretString": json.dumps(self.versions[kwargs["VersionId"]]), "VersionId": kwargs["VersionId"]}
        return {"SecretString": json.dumps(self.value), "VersionId": self.version}

    def put_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == target.SECRET
        assert len(kwargs["ClientRequestToken"]) == 36
        self.puts.append(kwargs)
        self.value = json.loads(kwargs["SecretString"])
        self.version = kwargs["ClientRequestToken"]
        self.versions[self.version] = copy.deepcopy(self.value)
        if self.lost_put_ack:
            raise TimeoutError("SYNTHETIC_PRIVATE_RESPONSE")
        return {"VersionId": self.version}


class MemoryIntent:
    def __init__(self):
        self.value = None

    def read(self):
        return copy.deepcopy(self.value)

    def create(self, value):
        assert self.value is None
        self.value = copy.deepcopy(value)
        return self.read()


class ManagedSecretTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryIntent()

    def load(self, api, **options):
        return target.load(api, intent_store=self.store, **options)

    def test_optional_stage_read_mount_preserves_every_existing_identity(self):
        from studio_platform.stage_metrics import read_config
        value = initialized()
        before = target.outputs(value)
        stage = {"schema_version": 1, "enabled": True,
            "endpoint": "https://logs-prod-synthetic.grafana.net",
            "authorization": "Basic "+base64.b64encode(b"123:SYNTHETIC_READ_ONLY").decode(),
            "scope": "logs:read"}
        value["grafana_stage_query"] = stage
        api = FakeAPI(value)
        self.assertEqual(self.load(api), value)
        self.assertEqual(api.puts, [])
        output = target.outputs(value)
        self.assertEqual({key: output[key] for key in before}, before)
        content, uid, gid, mode = output["grafana_stage_query"]
        self.assertEqual((uid, gid, mode), (10001, 10001, 0o600))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"reader.json"
            path.write_text(content, encoding="utf-8")
            path.chmod(mode)
            config = read_config(path)
            self.assertEqual(config.scope, "logs:read")
            self.assertEqual(config.endpoint, stage["endpoint"])
        for key, bad in (("scope", "logs:write"), ("endpoint", "https://outside.example"),
                ("endpoint", "https://logs-prod-synthetic.grafana.net/private"),
                ("authorization", "Basic invalid"), ("schema_version", True)):
            with self.subTest(key=key, bad=bad):
                changed = copy.deepcopy(value)
                changed["grafana_stage_query"][key] = bad
                with self.assertRaisesRegex(ValueError, "stage_query_configuration_invalid"):
                    target.validate(changed)

    def test_inert_import_and_existing_identity_never_rotates(self):
        api = FakeAPI(initialized())
        with patch.object(target, "client", side_effect=AssertionError("network prohibited")):
            self.assertEqual(self.load(api, initialize=True), initialized())
        self.assertEqual(api.puts, [])

    def test_explicit_fresh_initialization_retains_provider_cloud(self):
        api = FakeAPI(seed())
        checker = unittest.mock.Mock()
        actual = self.load(api, initialize=True, bootstrap_checks="/protected/receipt.json", checker=checker)
        checker.assert_called_once_with("/protected/receipt.json")
        self.assertEqual(actual["providers"], seed()["providers"])
        self.assertEqual(actual["grafana_cloud"], seed()["grafana_cloud"])
        self.assertEqual(len(api.puts), 1)
        self.assertEqual(actual, api.value)
        target.validate(actual)

    def test_missing_infrastructure_without_approval_never_puts(self):
        for options in ({}, {"initialize": True}):
            with self.subTest(options=options):
                api = FakeAPI(seed())
                with self.assertRaises((ValueError, RuntimeError)):
                    self.load(api, **options)
                self.assertEqual(api.puts, [])

    def test_partial_existing_infrastructure_is_not_regenerated(self):
        value = initialized()
        del value["infrastructure"]["hatchet_database_password"]
        api = FakeAPI(value)
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
        self.assertEqual(api.puts, [])

    def test_get_error_never_creates_replacement_credentials(self):
        api = FakeAPI(seed())
        for error in (api.exceptions.ResourceNotFoundException(), TimeoutError("private")):
            with self.subTest(kind=type(error).__name__):
                with patch.object(api, "get_secret_value", side_effect=error):
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
                self.assertEqual(api.puts, [])

    def test_lost_put_acknowledgement_reconciles_once(self):
        api = FakeAPI(seed()); api.lost_put_ack = True
        actual = self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
        self.assertEqual(actual, api.value)
        self.assertEqual(len(api.puts), 1)

    def test_unconfirmed_put_fails_without_second_mint(self):
        api = FakeAPI(seed())
        with patch.object(api, "put_secret_value", side_effect=TimeoutError("private")) as put:
            with self.assertRaisesRegex(RuntimeError, "initialization_unconfirmed"):
                self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
            self.assertEqual(put.call_count, 1)
        self.assertNotIn("infrastructure", api.value)

    def test_managed_version_changed_before_put_preserves_new_version(self):
        api = FakeAPI(seed())
        def changed(_):
            api.version = "concurrent-version"
        with self.assertRaisesRegex(RuntimeError, "managed_version_changed"):
            self.load(api, initialize=True, bootstrap_checks="unused", checker=changed)
        self.assertEqual(api.puts, [])

    def test_endpoint_mismatch_and_ambiguous_json_rejected(self):
        value = initialized()
        value["providers"]["vastai"]["base_url"] += "/v0"
        with self.assertRaisesRegex(ValueError, "endpoint_or_identity_invalid"):
            target.validate(value)
        api = FakeAPI(seed())
        with patch.object(api, "get_secret_value", return_value={"SecretString": '{"schema_version":1,"schema_version":1}'}):
            with self.assertRaisesRegex(ValueError, "duplicate_field"):
                self.load(api)

    def test_two_invocations_unknown_put_and_stale_get_reuse_original_identity(self):
        api = FakeAPI(seed())
        first_payload = []
        def lost_and_not_yet_visible(**kwargs):
            first_payload.append(copy.deepcopy(kwargs))
            raise TimeoutError("private")
        with patch.object(api, "put_secret_value", side_effect=lost_and_not_yet_visible):
            with self.assertRaisesRegex(RuntimeError, "initialization_unconfirmed"):
                self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
        pending = self.store.read()
        # New invocation: old AWSCURRENT seed remains visible, original exact
        # target version is not yet visible. No approval recheck or new mint.
        with patch.object(target.secrets, "token_urlsafe", side_effect=AssertionError("second mint prohibited")):
            actual = self.load(api, initialize=True, bootstrap_checks="expired", checker=lambda _: self.fail("must reconcile pending intent"))
        self.assertEqual(api.puts[0], first_payload[0])
        self.assertEqual(actual["infrastructure"], pending["infrastructure"])
        self.assertEqual(api.puts[0]["ClientRequestToken"], pending["client_request_token"])

    def test_unknown_put_then_get_failure_retains_intent_across_invocations(self):
        api = FakeAPI(seed())
        original_get = api.get_secret_value
        def put_then_failure(**kwargs):
            api.put_secret_value_original(**kwargs)
            raise TimeoutError("private")
        api.put_secret_value_original = api.put_secret_value
        def unavailable_after_put(**kwargs):
            if "VersionId" in kwargs and kwargs["VersionId"] != "original-version" and api.puts:
                raise TimeoutError("private")
            return original_get(**kwargs)
        with patch.object(api, "put_secret_value", side_effect=put_then_failure), patch.object(api, "get_secret_value", side_effect=unavailable_after_put):
            with self.assertRaises(TimeoutError):
                self.load(api, initialize=True, bootstrap_checks="unused", checker=lambda _: None)
        pending = self.store.read()
        with patch.object(target.secrets, "token_urlsafe", side_effect=AssertionError("second mint prohibited")):
            result = self.load(api)
        self.assertEqual(result["infrastructure"], pending["infrastructure"])
        self.assertEqual(len(api.puts), 1)

    def test_cloud_validator_matches_existing_exporter_destination_rules(self):
        from studio_platform.telemetry import CloudTelemetryConfig
        fields = {"endpoint", "authorization", "service_name", "service_version", "environment"}
        cases = [({}, True), ({"endpoint": "http://otlp-gateway-synthetic.grafana.net/otlp"}, False),
            ({"endpoint": "https://collector.example.org/otlp"}, False),
            ({"endpoint": "https://otlp-gateway-synthetic.grafana.net/otlp?key=x"}, False),
            ({"authorization": "Bearer SYNTHETIC"}, False),
            ({"service_name": "arbitrary"}, False), ({"service_version": "branch-main"}, False),
            ({"environment": "arbitrary"}, False)]
        for update, allowed in cases:
            with self.subTest(update=update):
                value = initialized(); value["grafana_cloud"].update(update)
                cloud = {key: value["grafana_cloud"][key] for key in fields}
                if allowed:
                    target.validate(value); CloudTelemetryConfig(**cloud)
                else:
                    with self.assertRaises(ValueError): target.validate(value)
                    with self.assertRaises(ValueError): CloudTelemetryConfig(**cloud)

    def test_output_mounts_match_cpu_users_and_contain_no_gpu_secret(self):
        value = initialized(); value["hatchet_api_token"] = "a"*16+"."+"b"*16+"."+"c"*16
        value["providers"]["runpod"] = {"profile_id": "runpod--synthetic-test",
            "base_url": target.ENDPOINTS["runpod"], "api_key": "SYNTHETIC_RUNPOD_"+"r"*40}
        result = target.outputs(value)
        self.assertEqual(result["grafana_cloud"][1:], (10001, 10001, 0o600))
        self.assertEqual(result["hatchet_api_token"][1:], (10001, 10001, 0o600))
        self.assertEqual(result["dstack_api_token"][1:], (0, 10001, 0o640))
        self.assertEqual(result["dstack_database_url"][1:], (0, 0, 0o600))
        server = json.loads(result["dstack-server-config.yml"][0])
        self.assertEqual({row["type"] for row in server["projects"][0]["backends"]}, {"vastai", "runpod"})
        self.assertEqual(result["dstack-server-config.yml"][1:], (0, 0, 0o600))
        self.assertNotIn("gpu_ssh_key", result)
        self.assertNotIn("app_database_url", result)

    def test_cli_suppresses_exception_and_accidental_argument_values(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(target, "hydrate", side_effect=RuntimeError("SYNTHETIC_PRIVATE_RESPONSE")):
            self.assertEqual(target.main([]), 1)
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            target.main(["--api-key", "SYNTHETIC_PRIVATE_ARGUMENT"])
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Linux root required for real UID/mode proof")
class ProtectedHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sixnine-hydrate-test-", dir="/root")
        self.root = Path(self.temporary.name)
        self.mounts = self.root / "secrets"
        self.patch_root = patch.object(target, "ROOT", self.mounts); self.patch_root.start()
        self.host = self.root / "instance-id"; self.host.write_text("i-0123456789abcdef0\n")
        self.patch_host = patch.object(target, "HOST_ID", self.host); self.patch_host.start()
        self.patch_intent = patch.object(target, "INTENT", self.root / "durable" / "intent.json"); self.patch_intent.start()
        self.now = datetime.now(timezone.utc)
        self.receipt = self.root / "receipt.json"
        self.checks = {"schema_version": 1, "infrastructure_database_absent": True,
            "checked_at": self.now.isoformat(), "host_id": self.host.read_text().strip(),
            "hatchet_postgres_volume": "synthetic-hatchet-db", "hatchet_config_volume": "synthetic-hatchet-config",
            "dstack_state_dir": str(self.root / "dstack"), "dstack_runtime_dir": str(self.root / "runtime")}
        self.write_receipt()

    def tearDown(self):
        self.patch_root.stop(); self.patch_host.stop(); self.patch_intent.stop(); self.temporary.cleanup()

    def write_receipt(self):
        self.receipt.write_text(json.dumps(self.checks)); self.receipt.chmod(0o600)

    def runner(self, output="", code=0):
        def run(command, **kwargs):
            self.assertEqual(command, ["docker", "volume", "ls", "--format", "{{.Name}}"])
            self.assertTrue(kwargs["capture_output"])
            return SimpleNamespace(returncode=code, stdout=output)
        return run

    def test_fresh_receipt_and_absent_host_state_accepts(self):
        target.check_fresh_bootstrap(self.receipt, runner=self.runner(), now=self.now)

    def test_existing_runtime_identity_forbids_initialization(self):
        target.write_outputs(target.outputs(initialized()))
        with self.assertRaisesRegex(RuntimeError, "existing_engine_recovery"):
            target.check_fresh_bootstrap(self.receipt, runner=self.runner(), now=self.now)

    def test_durable_intent_precedes_aws_put_and_survives_new_store(self):
        api = FakeAPI(seed())
        def assert_persisted(**kwargs):
            record = target.ProtectedIntent().read()
            info = target.INTENT.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o600))
            self.assertEqual(record["client_request_token"], kwargs["ClientRequestToken"])
            self.assertNotIn("providers", record); self.assertNotIn("grafana_cloud", record)
            self.assertEqual(record["infrastructure"], json.loads(kwargs["SecretString"])["infrastructure"])
            raise TimeoutError("private")
        with patch.object(api, "put_secret_value", side_effect=assert_persisted):
            with self.assertRaisesRegex(RuntimeError, "initialization_unconfirmed"):
                target.load(api, initialize=True, bootstrap_checks=self.receipt,
                    checker=lambda filename: target.check_fresh_bootstrap(filename, runner=self.runner(), now=self.now))
        record = target.ProtectedIntent().read()
        with patch.object(target.secrets, "token_urlsafe", side_effect=AssertionError("second mint prohibited")):
            result = target.load(api)
        self.assertEqual(result["infrastructure"], record["infrastructure"])
        self.assertEqual(len(api.puts), 1)

    def test_existing_volumes_failed_listing_and_existing_state_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "existing_engine_recovery"):
            target.check_fresh_bootstrap(self.receipt, runner=self.runner("synthetic-hatchet-db\n"), now=self.now)
        with self.assertRaisesRegex(RuntimeError, "state_unconfirmed"):
            target.check_fresh_bootstrap(self.receipt, runner=self.runner(code=1), now=self.now)
        state = self.root / "dstack"; state.mkdir(mode=0o700); (state / "existing-state").write_text("synthetic")
        with self.assertRaisesRegex(RuntimeError, "existing_engine_recovery"):
            target.check_fresh_bootstrap(self.receipt, runner=self.runner(), now=self.now)

    def test_stale_cross_host_false_db_and_unprotected_receipts_rejected(self):
        for update in ({"checked_at": "2020-01-01T00:00:00Z"}, {"host_id": "i-00000000000000000"},
                {"infrastructure_database_absent": False}):
            old = copy.deepcopy(self.checks); self.checks.update(update); self.write_receipt()
            with self.assertRaises(ValueError):
                target.check_fresh_bootstrap(self.receipt, runner=self.runner(), now=self.now)
            self.checks = old
        self.write_receipt(); self.receipt.chmod(0o644)
        with self.assertRaises(ValueError):
            target.check_fresh_bootstrap(self.receipt, runner=self.runner(), now=self.now)

    def test_permissions_and_bound_inode_preserved_on_rehydration(self):
        files = target.outputs(initialized()); target.write_outputs(files)
        inodes = {}
        for name, (content, uid, gid, mode) in files.items():
            path = self.mounts / name; info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (uid, gid, mode))
            self.assertEqual(path.read_text(), content); inodes[name] = info.st_ino
        target.write_outputs(files)
        self.assertEqual({name: (self.mounts / name).stat().st_ino for name in files}, inodes)

    def test_rotation_preflight_writes_nothing_new(self):
        files = target.outputs(initialized()); target.write_outputs(files)
        missing = self.mounts / "grafana_cloud"; missing.unlink()
        changed = copy.deepcopy(files)
        content, uid, gid, mode = changed["dstack_api_token"]
        changed["dstack_api_token"] = (content+"changed", uid, gid, mode)
        with self.assertRaisesRegex(RuntimeError, "rotation_requires"):
            target.write_outputs(changed)
        self.assertFalse(missing.exists())
        self.assertEqual((self.mounts / "dstack_api_token").read_text(), files["dstack_api_token"][0])

    def test_symlink_hardlink_and_wrong_owner_rejected(self):
        files = target.outputs(initialized()); target.write_outputs(files)
        token = self.mounts / "dstack_api_token"
        os.link(token, self.root / "linked")
        with self.assertRaisesRegex(RuntimeError, "file_invalid"): target.write_outputs(files)
        (self.root / "linked").unlink()
        os.chown(token, 10001, 10001)
        with self.assertRaisesRegex(RuntimeError, "file_invalid"): target.write_outputs(files)
        os.chown(token, 0, 10001)
        token.unlink(); token.symlink_to(self.root / "outside")
        with self.assertRaises(OSError): target.write_outputs(files)


if __name__ == "__main__":
    unittest.main()

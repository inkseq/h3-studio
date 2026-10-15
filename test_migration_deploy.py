"""Compose checks; opt-in isolated CPU proof with no provider backends."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tools.check_migration_deploy import DSTACK_IMAGE, PROBE, ROOT, check, local_inputs, main, memory_mib, validate


class MigrationPrivateProbeTests(unittest.TestCase):
    """Execute the shipped probe against bounded HTTP responses, with no network."""

    def run_probe(self, *, health=(200, b'{"status":"running"}'), unauth=(401, b''),
                  auth=(200, b'{"username":"admin"}'), ready=(200, b''), transport_error=None):
        calls, reads = [], []
        responses = iter((health, unauth, auth, ready))

        class Response(io.BytesIO):
            def __init__(self, status, body):
                super().__init__(body)
                self.status = status

            def read(self, limit=-1):
                reads.append(limit)
                return super().read(limit)

        def request(req, *, timeout):
            calls.append((req.full_url, req.method, req.get_header("Authorization"), timeout))
            if transport_error is not None:
                raise transport_error
            status, body = next(responses)
            if status >= 400:
                raise HTTPError(req.full_url, status, "PRIVATE-PROBE-SECRET", None, io.BytesIO(body))
            return Response(status, body)

        output, errors = io.StringIO(), io.StringIO()
        exit_code = None
        with patch("pathlib.Path.read_text", return_value="PRIVATE-PROBE-SECRET"), \
                patch("urllib.request.urlopen", side_effect=request), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                exec(PROBE, {})
            except SystemExit as error:
                exit_code = str(error)
        self.assertNotIn("PRIVATE-PROBE-SECRET", output.getvalue()+errors.getvalue()+str(exit_code))
        return exit_code, output.getvalue(), errors.getvalue(), calls, reads

    def assert_failed(self, **kwargs):
        code, output, errors, _, _ = self.run_probe(**kwargs)
        self.assertEqual(code, "migration_private_probe_failed")
        self.assertEqual(output+errors, "")

    def test_actual_probe_accepts_empty_hatchet_ready_and_checks_dstack_auth(self):
        code, output, errors, calls, reads = self.run_probe()
        self.assertIsNone(code)
        self.assertEqual(output, "migration_private_probe_passed\n")
        self.assertEqual(errors, "")
        self.assertEqual([call[1] for call in calls], ["GET", "POST", "POST", "GET"])
        self.assertEqual([call[2] is not None for call in calls], [False, False, True, False])
        self.assertTrue(all(call[3] == 5 for call in calls))
        self.assertEqual(reads, [262145, 262145, 262145])

    def test_hatchet_readiness_is_status_only(self):
        code, output, _, _, _ = self.run_probe(ready=(200, b'ready; not JSON'))
        self.assertIsNone(code)
        self.assertEqual(output, "migration_private_probe_passed\n")
        self.assert_failed(ready=(503, b'PRIVATE-PROBE-SECRET'))

    def test_dstack_json_remains_strict(self):
        for field in ("health", "auth"):
            with self.subTest(field=field):
                self.assert_failed(**{field: (200, b'PRIVATE-PROBE-SECRET; malformed JSON')})

    def test_dstack_auth_and_health_contracts_remain_required(self):
        for change in ({"health": (200, b'{"status":"stopped"}')},
                       {"unauth": (200, b'{"username":"admin"}')},
                       {"auth": (403, b'PRIVATE-PROBE-SECRET')},
                       {"auth": (200, b'{"username":"another-user"}')}):
            with self.subTest(change=next(iter(change))):
                self.assert_failed(**change)

    def test_response_limit_applies_to_json_and_status_only_endpoints(self):
        for field, body in (("health", b'{"status":"running"}'),
                            ("auth", b'{"username":"admin"}'), ("ready", b'')):
            with self.subTest(field=field):
                self.assert_failed(**{field: (200, body+b' '*(262145-len(body)))})

    def test_transport_exception_is_masked(self):
        self.assert_failed(transport_error=OSError("PRIVATE-PROBE-SECRET"))


def fixture_environment():
    environment = dict(os.environ)
    environment.pop("SIXNINE_STORYBOARD_OWNERS", None)
    names = ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE", "SIXNINE_IMAGE", "SIXNINE_DISPATCH_IMAGE")
    environment.update({name: "test/never-pulled@sha256:"+"a"*64 for name in names})
    environment["SIXNINE_DSTACK_SERVER_IMAGE"] = DSTACK_IMAGE
    for name in ("SIXNINE_DB_ADMIN_SECRET_FILE", "SIXNINE_APP_DSN_SECRET_FILE",
        "SIXNINE_EXECUTION_PROFILES_SECRET_FILE", "SIXNINE_GRAFANA_CONFIG_FILE", "SIXNINE_DSTACK_OPERATOR_CONFIG_FILE",
        "SIXNINE_DSTACK_RUNTIME_CONFIG_FILE", "SIXNINE_DSTACK_TOKEN_FILE", "SIXNINE_DSTACK_DSN_FILE",
        "SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE", "SIXNINE_GPU_SSH_KEY_FILE", "SIXNINE_HATCHET_BROKER_CONFIG_FILE",
        "SIXNINE_HATCHET_TOKEN_FILE", "SIXNINE_DSTACK_STATE_DIR", "SIXNINE_DSTACK_SERVER_CONFIG_FILE",
        "SIXNINE_DSTACK_RUNTIME_DIR", "SIXNINE_DSTACK_SOURCE_DIR", "SIXNINE_HATCHET_SECRET_DIR"):
        environment[name] = "/never-opened-fixtures/"+name.lower()
    environment.update(SIXNINE_HATCHET_POSTGRES_VOLUME="existing-hatchet-postgres",
        SIXNINE_HATCHET_CONFIG_VOLUME="existing-hatchet-config", SIXNINE_OPERATOR_CAPACITY_OWNERS="superdan,supervan",
        SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID="h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1",
        SIXNINE_MIGRATION_GENERATION_ENABLED="0", SIXNINE_MIGRATION_EXECUTION_BACKEND="disabled")
    return environment


class MigrationDeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker Compose CLI required for offline configuration parsing")
        import subprocess
        def capture(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if result.returncode == 0:
                cls.document = json.loads(result.stdout)
            return result
        cls.receipt = check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
            environment=fixture_environment(), runner=capture)

    def assert_rejected(self, mutate, code):
        value = copy.deepcopy(self.document); mutate(value)
        with self.assertRaisesRegex(ValueError, "^"+code+"$"):
            validate(value)

    def test_public_origin_and_proxy_only_phase_are_closed_and_consistent(self):
        self.assertEqual(self.document["services"]["app"]["environment"]["PUBLIC_ORIGIN"], "https://inkseq.com")
        legacy = copy.deepcopy(self.document)
        for name in ("app", "controller", "dispatch"):
            legacy["services"][name]["environment"].pop("PUBLIC_ORIGIN")
        legacy["services"]["app"]["environment"].pop("PUBLIC_ORIGIN_ALIASES")
        legacy["services"]["app"]["environment"]["SIXNINE_PUBLIC_ORIGIN"] = "https://www.sixnine.art"
        legacy["services"]["caddy"].pop("environment")
        validate(legacy)
        self.assert_rejected(lambda d: d["services"]["app"]["environment"].pop("PUBLIC_ORIGIN"),
            "migration_public_origin_split")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(PUBLIC_ORIGIN="https://www.sixnine.art"),
            "migration_public_origin_split")
        self.assert_rejected(lambda d: d["services"]["app"]["environment"].update(PUBLIC_ORIGIN_ALIASES="https://staging.inkseq.com"),
            "migration_public_origin_split")
        self.assert_rejected(lambda d: d["services"]["app"]["environment"].update(INKSEQ_LEGACY_REDIRECT="1"),
            "migration_domain_flag_outside_proxy")
        self.assert_rejected(lambda d: d["services"]["caddy"]["environment"].update(INKSEQ_LEGACY_REDIRECT="true"),
            "migration_domain_redirect_phase_invalid")

    def test_derivative_requires_the_explicit_immutable_artifact_binding(self):
        value = copy.deepcopy(self.document)
        reference = "sixnine-dstack:"+"b"*40+"@sha256:"+"c"*64
        value["services"]["dstack"]["image"] = reference
        validate(value, dstack_image=reference)
        with self.assertRaisesRegex(ValueError,"infrastructure_image_changed"):
            validate(value)
        with self.assertRaisesRegex(ValueError,"dstack_image_binding_invalid"):
            validate(value, dstack_image="some-other-image@sha256:"+"c"*64)

    def render_r2(self):
        environment = fixture_environment()
        environment.update(SIXNINE_STORAGE_PROVIDER="r2",
            SIXNINE_STORAGE_ENDPOINT="https://"+"a"*32+".r2.cloudflarestorage.com",
            SIXNINE_STORAGE_REGION="auto", SIXNINE_STORAGE_BUCKET="sixnine-synthetic-media",
            SIXNINE_STORAGE_PROFILE="cloudflare-r2--rig-root",
            SIXNINE_STORAGE_CREDENTIAL_FILE="/run/sixnine-secrets/r2_storage")
        rendered = {}
        def render(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if result.returncode == 0:
                rendered.update(json.loads(result.stdout))
            return result
        check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
            environment=environment, runner=render)
        return rendered

    def test_local_static_credential_alias_is_unused_and_reads_no_secret(self):
        from studio_platform.settings import Settings
        from studio_platform.fleet import create_store
        from studio_platform.storage import LocalObjectStore
        services = self.document["services"]
        self.assertEqual(self.document["secrets"]["r2_storage"]["file"],
            self.document["secrets"]["grafana_cloud"]["file"])
        with tempfile.TemporaryDirectory() as directory:
            for name in ("app", "controller"):
                env = {key: value for key, value in services[name]["environment"].items()
                    if key.startswith("SIXNINE_STORAGE_")}
                env["SIXNINE_DATA"] = directory
                with patch.dict(os.environ, env, clear=True), patch(
                        "studio_platform.storage_config.load_configured_storage_credentials",
                        side_effect=AssertionError("Local mode must never read alias or registry")):
                    settings = Settings.from_environment()
                    self.assertIsNone(settings.storage_credential_file)
                    self.assertIsInstance(create_store(settings), LocalObjectStore)

    def test_r2_identity_and_protected_file_reach_controller_worker_without_dispatch_keys(self):
        from studio_platform.settings import Settings
        from studio_platform.fleet import create_store
        from studio_platform.storage_config import S3Credentials
        document = self.render_r2()
        self.assertEqual(document["secrets"]["r2_storage"]["file"], "/run/sixnine-secrets/r2_storage")
        app, controller, dispatch = (document["services"][name]["environment"]
            for name in ("app", "controller", "dispatch"))
        fields = ("SIXNINE_STORAGE_PROVIDER", "SIXNINE_STORAGE_ENDPOINT", "SIXNINE_STORAGE_REGION",
            "SIXNINE_STORAGE_BUCKET", "SIXNINE_STORAGE_PROFILE")
        self.assertEqual({key: app[key] for key in fields}, {key: controller[key] for key in fields})
        self.assertEqual({key: app[key] for key in fields}, {key: dispatch[key] for key in fields})
        self.assertNotIn("SIXNINE_STORAGE_CREDENTIAL_FILE", dispatch)
        selected = {key: value for key, value in controller.items() if key.startswith("SIXNINE_STORAGE_")}
        self.assertEqual(selected["SIXNINE_STORAGE_CREDENTIAL_FILE"], "/run/secrets/r2_storage")
        if os.name == "nt":
            # The rendered contract targets Linux. Use a native absolute path
            # only for this Windows store-factory fixture (no file is opened).
            selected["SIXNINE_STORAGE_CREDENTIAL_FILE"] = str(ROOT / "never-opened-r2-runtime-fixture")
        # Slot children inherit controller's environment; their existing store
        # factory must select the mounted file, not a workstation loader.
        with patch.dict(os.environ, selected, clear=True), patch(
                "studio_platform.storage_config.load_configured_storage_credentials",
                return_value=S3Credentials("synthetic", "synthetic")) as credentials, patch(
                "studio_platform.storage.S3ObjectStore") as store:
            settings = Settings.from_environment()
            self.assertEqual(settings.storage_credential_file, Path(selected["SIXNINE_STORAGE_CREDENTIAL_FILE"]))
            create_store(settings)
            config = credentials.call_args.args[0]
            self.assertEqual((config.provider, config.profile, config.bucket),
                ("r2", "cloudflare-r2--rig-root", "sixnine-synthetic-media"))
            self.assertEqual(credentials.call_args.kwargs["credentials_file"], settings.storage_credential_file)
            self.assertIsNone(credentials.call_args.kwargs["registry_root"])
            store.assert_called_once()

    def test_staging_requires_exact_origin_bucket_profile_and_matching_workers(self):
        document = self.render_r2()
        for name in ('app', 'controller', 'dispatch'):
            document['services'][name]['environment'].update(PUBLIC_ORIGIN='https://staging.inkseq.com',
                SIXNINE_STORAGE_PROFILE='cloudflare-r2--sixnine-staging-20261015', SIXNINE_STORAGE_BUCKET='sixnine-staging-media')
        document['services']['app']['environment']['PUBLIC_ORIGIN_ALIASES'] = ''
        document['services']['caddy']['environment'] = {'INKSEQ_LEGACY_REDIRECT': '0'}
        mount = next(item for item in document['services']['caddy']['volumes'] if item['target'] == '/etc/caddy/Caddyfile')
        mount['source'] = str(ROOT/'deploy/platform/Caddyfile.staging')
        validate(document, target='staging')
        with self.assertRaises(ValueError): validate(document)
        for change in ('profile', 'bucket', 'origin'):
            broken = copy.deepcopy(document)
            for name in ('app', 'controller', 'dispatch'):
                broken['services'][name]['environment'].update({
                    'profile': {'SIXNINE_STORAGE_PROFILE': 'cloudflare-r2--rig-root'},
                    'bucket': {'SIXNINE_STORAGE_BUCKET': 'sixnine-media'},
                    'origin': {'PUBLIC_ORIGIN': 'https://inkseq.com'},
                }[change])
            with self.assertRaises(ValueError): validate(broken, target='staging')

    def test_checker_inherits_stage_identity_from_the_render_environment(self):
        document = copy.deepcopy(self.document)
        for name in ('app', 'controller', 'dispatch'):
            document['services'][name]['environment']['PUBLIC_ORIGIN'] = 'https://staging.inkseq.com'
        document['services']['app']['environment']['PUBLIC_ORIGIN_ALIASES'] = ''
        document['services']['caddy']['environment'] = {'INKSEQ_LEGACY_REDIRECT': '0'}
        mount = next(item for item in document['services']['caddy']['volumes'] if item['target'] == '/etc/caddy/Caddyfile')
        mount['source'] = str(ROOT/'deploy/platform/Caddyfile.staging')
        calls = []
        def render(command, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(returncode=0, stdout=json.dumps(document))
        with patch.dict(os.environ, {'INKSEQ_DEPLOYMENT_TARGET': 'staging', 'INKSEQ_LEGACY_REDIRECT': '0'}, clear=False):
            check(ROOT/'deploy/platform/compose.yaml', ROOT/'deploy/dstack/platform-overlay.yaml', runner=render)
        self.assertIsNone(calls[0]['env'])

    def test_r2_split_missing_opt_in_fallback_and_environment_keys_are_rejected(self):
        baseline = self.render_r2()
        for mutate, code in (
                (lambda d: d["services"]["controller"]["environment"].update(SIXNINE_STORAGE_BUCKET="another-bucket"),
                    "migration_storage_identity_split"),
                (lambda d: d["services"]["app"]["environment"].update(SIXNINE_STORAGE_CREDENTIAL_FILE=""),
                    "migration_storage_r2_configuration_invalid"),
                (lambda d: d["secrets"]["r2_storage"].update(file=d["secrets"]["grafana_cloud"]["file"]),
                    "migration_storage_r2_configuration_invalid"),
                (lambda d: d["services"]["dispatch"]["environment"].update(SIXNINE_STORAGE_CREDENTIAL_FILE="/run/secrets/r2_storage"),
                    "migration_storage_dispatch_credential"),
                (lambda d: d["services"]["controller"]["environment"].update(R2_SECRET_ACCESS_KEY="SYNTHETIC_PRIVATE"),
                    "migration_secret_value_in_compose"),
                (lambda d: d["services"]["app"]["environment"].update(R2_TOKEN="SYNTHETIC_PRIVATE"),
                    "migration_secret_value_in_compose"),
                (lambda d: d["services"]["app"]["environment"].update(SIXNINE_STORAGE_SECRET="SYNTHETIC_PRIVATE"),
                    "migration_storage_unexpected_environment")):
            value = copy.deepcopy(baseline); mutate(value)
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "^"+code+"$"):
                validate(value)
        self.assert_rejected(lambda d: d["secrets"]["r2_storage"].update(file="/run/sixnine-secrets/r2_storage"),
            "migration_storage_local_configuration_invalid")

    def test_real_compose_merge_shared_namespace_pins_and_memory(self):
        self.assertEqual(self.receipt["service_memory_cap_mib"], 3296)
        self.assertGreaterEqual(self.receipt["host_reserve_mib"], 512)
        self.assertEqual(self.receipt["gpu_operations"], 0)
        services = self.document["services"]
        self.assertEqual(memory_mib(services["dstack"]["mem_limit"]), 768)
        self.assertEqual(memory_mib(services["hatchet"]["mem_limit"]), 384)
        self.assertEqual(services["dstack"]["entrypoint"], ["python", "-c"])
        self.assertEqual(services["hatchet-db"]["networks"], {"database": None})
        self.assertEqual(services["controller"]["command"], ["python", "-m", "studio_platform.dstack_controller"])
        self.assertIn("app_database_url", {s["source"] for s in services["app"]["secrets"]})
        self.assertIn("google_titles", {s["source"] for s in services["app"]["secrets"]})

    def test_storyboard_rollout_reaches_only_app_and_preserves_disabled_default(self):
        self.assertEqual(self.document["services"]["app"]["environment"]["SIXNINE_STORYBOARD_OWNERS"], "")
        for name in ("dstack", "hatchet", "controller", "dispatch"):
            self.assertNotIn("SIXNINE_STORYBOARD_OWNERS", self.document["services"][name]["environment"])
        environment = fixture_environment()
        environment["SIXNINE_STORYBOARD_OWNERS"] = "superdan,supervan"
        rendered = {}
        def render(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if result.returncode == 0:
                rendered.update(json.loads(result.stdout))
            return result
        check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
            environment=environment, runner=render)
        self.assertEqual(rendered["services"]["app"]["environment"]["SIXNINE_STORYBOARD_OWNERS"], "superdan,supervan")
        for value in ("other", "superdan,superdan", None):
            invalid = copy.deepcopy(rendered)
            invalid["services"]["app"]["environment"]["SIXNINE_STORYBOARD_OWNERS"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "migration_storyboard_reference_invalid"):
                validate(invalid)
        invalid = copy.deepcopy(rendered)
        invalid["services"]["controller"]["environment"]["SIXNINE_STORYBOARD_OWNERS"] = "superdan"
        with self.assertRaisesRegex(ValueError, "migration_storyboard_worker_environment"):
            validate(invalid)

    def test_rendered_controller_settings_reach_its_exact_cpu_slot_child(self):
        from studio_platform.control import WorkerSpec
        from studio_platform.dstack_controller import HatchetProcesses
        from studio_platform.fleet import SlotConfig, read_config
        from studio_platform.runtime_catalog import engine_manifest, PROFILE_IDS
        from studio_platform.settings import Settings

        flags = ("SIXNINE_GENERATION_ENABLED", "SIXNINE_EXECUTION_BACKEND")
        for enabled, backend in ((None, None), ("0", "disabled"), ("1", "wangp-worker")):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                environment = fixture_environment()
                for name, value in (("SIXNINE_MIGRATION_GENERATION_ENABLED", enabled),
                                    ("SIXNINE_MIGRATION_EXECUTION_BACKEND", backend)):
                    environment.pop(name, None)
                    if value is not None:
                        environment[name] = value
                rendered = {}
                def render(command, **kwargs):
                    result = subprocess.run(command, **kwargs)
                    if result.returncode == 0:
                        rendered.update(json.loads(result.stdout))
                    return result
                check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
                      environment=environment, runner=render)
                selected = {name: {key: value for key, value in rendered["services"][name]["environment"].items()
                    if key in flags} for name in ("app", "controller")}
                with patch.dict(os.environ, selected["app"], clear=True):
                    app_settings = Settings.from_environment()
                self.assertEqual(app_settings.generation_enabled, enabled == "1")
                self.assertEqual(app_settings.execution_backend, backend or "disabled")
                self.assertEqual(selected["controller"], selected["app"])
                manifest = engine_manifest(PROFILE_IDS[0], "fl")
                spec = WorkerSpec("compose-child", "compose-pool", "vast", "original-instance", ("original-gpu",),
                    (manifest.document["generation_recipe_id"],), manifest.document["model_id"],
                    "compose-config", "wangp-worker", manifest.digest, dispatch_backend="hatchet-v1")
                slot = SlotConfig(spec, True, "http://127.0.0.1:8199", ("http://127.0.0.1:8199",),
                    runtime_config_file=str(Path(temporary)/"never-opened-native.json"))
                children = []
                def spawn(command, **kwargs):
                    # Popen's inherited environment is the child's actual Settings
                    # input. No real process, database, broker or GPU is started.
                    self.assertNotIn("env", kwargs)
                    child_settings = Settings.from_environment()
                    self.assertEqual((child_settings.generation_enabled, child_settings.execution_backend),
                        (app_settings.generation_enabled, app_settings.execution_backend))
                    self.assertEqual(command[1:4], ["-m", "studio_platform.hatchet_dispatch", "worker"])
                    fleet = read_config(command[command.index("--fleet-config")+1])
                    self.assertEqual(fleet.slot(spec.worker_id).spec, spec)
                    children.append(child_settings)
                    return SimpleNamespace(poll=lambda:0, wait=lambda:0)
                with patch.dict(os.environ, selected["controller"], clear=True), \
                        HatchetProcesses(Path(temporary)/"supervisor", Path(temporary)/"broker.json", popen=spawn) as processes:
                    processes.ensure("original-intent", slot)
                self.assertEqual(len(children), 1)

    def test_loopback_split_or_public_management_ports_rejected(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(network_mode="bridge"), "migration_loopback_namespace_split")
        self.assert_rejected(lambda d: d["services"]["dstack"].update(ports=["3000:3000"]), "migration_private_port_published")

    def test_presence_based_raw_otel_and_secret_environment_rejected(self):
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_OTEL_TRACES_ENABLED="0"),
                             "migration_raw_dstack_telemetry_enabled")
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_SERVER_ADMIN_TOKEN="SECRET"),
                             "migration_secret_value_in_compose")
        self.assert_rejected(lambda d: d["services"]["dstack"]["environment"].update(DSTACK_SERVER_LOG_LEVEL="INFO"),
                             "migration_dstack_auth_invalid")

    def test_existing_broker_volumes_and_business_ledger_cannot_be_replaced(self):
        self.assert_rejected(lambda d: d["volumes"]["hatchet-postgres"].update(external=False), "migration_blank_broker_volume")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(SIXNINE_DATABASE_URL_FILE="/tmp/new.db"),
                             "migration_business_authority_split")
        self.assert_rejected(lambda d: d["services"]["hatchet"]["environment"].update(SERVER_ALLOW_SIGNUP="true"),
                             "migration_hatchet_auth_invalid")

    def test_unpinned_images_gpu_access_and_memory_overcommit_rejected(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(image="test:latest"), "migration_image_unpinned")
        self.assert_rejected(lambda d: d["services"]["controller"].update(gpus="all"), "migration_unsafe_service")
        self.assert_rejected(lambda d: d["services"]["controller"].update(mem_limit="2g"), "migration_host_memory_overcommitted")

    def test_runtime_keys_config_and_cloud_mounts_are_not_optional(self):
        self.assert_rejected(lambda d: d["services"]["controller"].update(volumes=[]), "migration_assets_split")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(SIXNINE_TELEMETRY_CONFIG_FILE=""),
                             "migration_cloud_telemetry_missing")
        self.assert_rejected(lambda d: d["services"]["dispatch"]["environment"].update(SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID="another-profile"),
                             "migration_default_profile_missing_or_split")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].pop("SIXNINE_GENERATION_ENABLED"),
                             "migration_controller_execution_settings_split")
        self.assert_rejected(lambda d: d["services"]["controller"]["environment"].update(SIXNINE_EXECUTION_BACKEND="wangp-worker"),
                             "migration_controller_execution_settings_split")

    def test_checker_only_config_then_explicit_read_probe_no_secret_output(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if "config" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.document), stderr="SECRET should not print")
            self.assertEqual(command[-5:-1], ["-T", "app", "python", "-c"])
            return SimpleNamespace(returncode=0, stdout="migration_private_probe_passed\n", stderr="SECRET")
        output = check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml", runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET", json.dumps(output))
        self.assertIn("--env-file", calls[0])
        check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml", runner=runner, probe=True)
        self.assertFalse(any(any(action in cmd for action in ("up", "pull", "stop", "rm", "create")) for cmd in calls))

    def test_cli_masks_raw_compose_errors_and_missing_mounts(self):
        output = io.StringIO()
        with patch("tools.check_migration_deploy.check", side_effect=RuntimeError("SECRET DSN")), contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "failed", "code": "migration_deploy_check_failed"})
        # Rendered host paths are Linux absolute paths; on Windows they are
        # rejected before any file read, and on Linux they do not exist.
        with self.assertRaises((OSError,ValueError)):
            local_inputs(self.document)


@unittest.skipUnless(os.environ.get("SIXNINE_DSTACK_CPU_PROOF") == "1",
                     "Explicit isolated CPU proof opt-in required")
class DstackCPUProof(unittest.TestCase):
    """Start only tagged temporary CPU containers with synthetic credentials.

    Images must already exist locally; --pull=never forbids implicit downloads.
    No provider configuration, rental call, real credentials or live volume is
    reachable. Docker errors and authentication responses are never printed.
    """

    def test_pinned_server_postgres_auth_and_private_namespace(self):
        if os.name != "posix" or not shutil.which("docker") or os.geteuid() != 0:
            self.skipTest("Linux root and Docker required for root-owned protected temporary mounts")
        scope = "sixnine-dstack-cpu-proof-" + secrets.token_hex(8)
        names = [scope + "-server", scope + "-outside", scope + "-db"]
        label = "sixnine.cpu-proof=" + scope
        dstack_image = "dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8"
        postgres_image = "postgres:15@sha256:c961aa287d8698297cb26cdfadfbe9fd2cbaf77e53cfffe9636e8d8a1e4d842c"

        def docker(arguments, *, input=None, required=True, timeout=30):
            result = subprocess.run(["docker", *arguments], input=input, text=True,
                                    capture_output=True, timeout=timeout)
            if required and result.returncode:
                raise RuntimeError("cpu_proof_docker_" + arguments[0] + "_failed")
            return result

        def wait_ready(arguments, code, timeout=90, watch=None):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if docker(arguments, required=False, timeout=8).returncode == 0:
                    return
                if watch:
                    state = docker(["inspect", "--format", '{{.State.Status}}', watch]).stdout.strip()
                    if state in {"exited", "dead"}:
                        raise RuntimeError(code + "_server_exited")
                time.sleep(1)
            raise RuntimeError(code)

        def cleanup():
            for name in names:
                found = docker(["inspect", "--format", '{{json .Config.Labels}}', name], required=False)
                if found.returncode == 0:
                    if json.loads(found.stdout).get("sixnine.cpu-proof") != scope:
                        raise RuntimeError("cpu_proof_cleanup_ownership_mismatch")
                    docker(["rm", "--force", name])
            found = docker(["network", "inspect", "--format", '{{json .Labels}}', scope], required=False)
            if found.returncode == 0:
                if json.loads(found.stdout).get("sixnine.cpu-proof") != scope:
                    raise RuntimeError("cpu_proof_network_ownership_mismatch")
                docker(["network", "rm", scope])

        with tempfile.TemporaryDirectory(prefix=scope + "-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            state = directory / "state"; state.mkdir(mode=0o700)
            admin_password, database_password, token = (secrets.token_hex(32) for _ in range(3))
            protected = {
                "pg-password": admin_password,
                "dstack-token": token,
                "dstack-dsn": "postgresql+asyncpg://dstack_control:" + database_password + "@db:5432/sixnine_dstack",
            }
            for name, value in protected.items():
                path = directory / name; path.write_text(value + "\n"); path.chmod(0o600)
            try:
                version = docker(["run", "--rm", "--pull=never", "--network=none", "--label", label,
                                  "--entrypoint", "/root/.local/bin/dstack", dstack_image, "--version"])
                self.assertEqual(version.stdout.strip(), "0.22.3")
                docker(["network", "create", "--label", label, scope])
                docker(["run", "--detach", "--name", names[2], "--label", label, "--pull=never",
                        "--network", scope, "--network-alias", "db", "--memory", "256m", "--pids-limit", "128",
                        "--mount", f"type=bind,src={directory / 'pg-password'},dst=/run/secrets/password,readonly",
                        "--tmpfs", "/var/lib/postgresql/data:rw,size=268435456",
                        "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/password", postgres_image,
                        "postgres", "-c", "shared_buffers=32MB", "-c", "max_connections=40",
                        "-c", "log_min_error_statement=panic"])
                wait_ready(["exec", names[2], "pg_isready", "-U", "postgres"], "cpu_proof_postgres_timeout")
                docker(["exec", "--interactive", names[2], "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1"],
                       input=("CREATE ROLE dstack_control LOGIN PASSWORD '" + database_password + "' NOSUPERUSER;\n"
                              "CREATE DATABASE sixnine_dstack OWNER dstack_control;\n"))

                # Test the exact protected-file entrypoint rendered by the real
                # production overlay. Config parsing reads no fixture files.
                document = {}
                def capture(command, **kwargs):
                    result = subprocess.run(command, **kwargs)
                    if result.returncode == 0:
                        document.update(json.loads(result.stdout))
                    return result
                check(ROOT / "deploy/platform/compose.yaml", ROOT / "deploy/dstack/platform-overlay.yaml",
                      environment=fixture_environment(), runner=capture)
                service = document["services"]["dstack"]
                file_probe = docker(["run", "--rm", "--name", names[1], "--label", label,
                    "--pull=never", "--network=none", "--cap-drop=ALL", "--read-only", "--entrypoint", "python",
                    "--mount", f"type=bind,src={directory / 'dstack-token'},dst=/run/secrets/dstack_api_token,readonly",
                    "--mount", f"type=bind,src={directory / 'dstack-dsn'},dst=/run/secrets/dstack_database_url,readonly",
                    dstack_image, "-c", r"""
import pathlib,os,stat,json
result={}
for path in pathlib.Path('/run/secrets').iterdir():
    with path.open('rb') as stream:
        info=os.fstat(stream.fileno()); raw=stream.read(16385)
    text=raw.decode('utf-8').removesuffix('\n')
    result[path.name]={'regular':stat.S_ISREG(info.st_mode),'links':info.st_nlink,'mode':oct(info.st_mode&0o777),'length':len(raw),'stripped':text==text.strip()}
print(json.dumps(result))
"""])
                for properties in json.loads(file_probe.stdout).values():
                    self.assertEqual(properties["mode"], "0o600")
                    self.assertTrue(properties["regular"] and properties["stripped"])
                    self.assertEqual(properties["links"], 1)
                command = ["run", "--detach", "--name", names[0], "--label", label, "--pull=never",
                           "--network", "container:" + names[2], "--memory", str(service["mem_limit"]), "--pids-limit", "128",
                           "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
                           "--tmpfs", "/tmp:rw,size=67108864,mode=1777",
                           "--tmpfs", "/root/.cache:rw,size=1048576,mode=0700",
                           "--mount", f"type=bind,src={state},dst=/root/.dstack",
                           "--mount", f"type=bind,src={directory / 'dstack-token'},dst=/run/secrets/dstack_api_token,readonly",
                           "--mount", f"type=bind,src={directory / 'dstack-dsn'},dst=/run/secrets/dstack_database_url,readonly"]
                for key, value in service["environment"].items():
                    command.extend(["--env", key + "=" + value])
                command.extend(["--env", "DSTACK_SERVER_CONFIG_DISABLED=1",
                                "--env", "DSTACK_SERVER_BACKGROUND_PROCESSING_DISABLED=1",
                                "--entrypoint", "python", dstack_image, "-c", service["command"][0]])
                docker(command)
                health = "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:3000/healthcheck',timeout=3))['status']=='running'"
                wait_ready(["exec", names[0], "python", "-c", health], "cpu_proof_dstack_health_timeout", watch=names[0])
                authentication = """
import json,pathlib,urllib.request,urllib.error
url='http://127.0.0.1:3000/api/users/get_my_user'
def request(token=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    try:
        with urllib.request.urlopen(urllib.request.Request(url,data=b'{}',headers=headers),timeout=4) as response:
            return response.status,json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code,None
assert request()[0] in (401,403)
assert request('invalid-synthetic-token')[0] in (401,403)
status,value=request(pathlib.Path('/run/secrets/dstack_api_token').read_text().strip())
assert status==200 and value['username']=='admin'
"""
                docker(["exec", names[0], "python", "-c", authentication])
                database = docker(["exec", names[2], "psql", "-U", "postgres", "-d", "sixnine_dstack", "-tA", "-c",
                                   "SELECT json_build_object('superuser',(SELECT rolsuper FROM pg_roles WHERE rolname='dstack_control'),'tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public'));" ])
                proof = json.loads(database.stdout)
                self.assertFalse(proof["superuser"])
                self.assertGreater(proof["tables"], 5)
                for name in (names[0], names[2]):
                    self.assertFalse(json.loads(docker(["inspect", "--format", '{{json .HostConfig.PortBindings}}', name]).stdout))
                outside = """
import urllib.request,urllib.error,socket
try:
    urllib.request.urlopen('http://db:3000/healthcheck',timeout=3)
except (urllib.error.URLError,socket.timeout):
    pass
else:
    raise SystemExit('private_namespace_not_isolated')
"""
                docker(["run", "--rm", "--name", names[1], "--label", label, "--pull=never", "--network", scope,
                        "--entrypoint", "python", dstack_image, "-c", outside])
                logs = docker(["logs", names[0]])
                self.assertFalse(any(secret in logs.stdout + logs.stderr
                                     for secret in (*protected.values(), database_password)))
            finally:
                cleanup()
        print(json.dumps({"cpu_proof": "passed", "version": "0.22.3", "postgres_initialized": True,
                          "nonadmin_database_role": True, "anonymous_and_invalid_token_rejected": True,
                          "admin_token_authenticated": True, "private_namespace_only": True,
                          "provider_backends_configured": 0, "gpu_operations": 0,
                          "temporary_cpu_resources_removed": True}))


if __name__ == "__main__":
    unittest.main()

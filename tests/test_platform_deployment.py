"""Offline deployment policy and optional isolated local Docker integration tests.

No production paths are mounted, no images are pulled, no cloud/DNS operations.
Enable explicit ephemeral DB integration with SIXNINE_TEST_DOCKER_DB=1.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "platform"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load_module("platform_deployment_policy", "check_config.py")
bootstrap = load_module("platform_database_bootstrap", "init_database.py")


def docker(*args, input=None, check=True, timeout=60, env=None):
    result = subprocess.run(
        ["docker", *args],
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode:
        # Docker/driver diagnostics might contain a URL in general. Do not print
        # command output here even though these tests use only synthetic inputs.
        raise AssertionError(
            "Local Docker verification failed (captured output withheld)"
        )
    return result


class DeploymentPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker CLI unavailable")
        cls.temp = tempfile.TemporaryDirectory()
        empty = Path(cls.temp.name) / "empty.env"
        empty.write_text("", encoding="utf-8")
        # Only interpolation references are added, never real secret values.
        env = dict(
            os.environ,
            SIXNINE_IMAGE="sixnine-platform:" + "a" * 40,
            SIXNINE_POSTGRES_IMAGE="postgres@sha256:" + "b" * 64,
            SIXNINE_CADDY_IMAGE="caddy@sha256:" + "c" * 64,
            SIXNINE_DB_ADMIN_SECRET_FILE="/run/sixnine-secrets/db_admin_password",
            SIXNINE_APP_DSN_SECRET_FILE="/run/sixnine-secrets/app_database_url",
        )
        result = docker(
            "compose",
            "--env-file",
            str(empty),
            "-f",
            str(DEPLOY / "compose.yaml"),
            "config",
            "--format",
            "json",
            env=env,
        )
        cls.rendered = json.loads(result.stdout)
        cls.compose_version = docker(
            "compose", "version", "--short", env=env
        ).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def test_rendered_compose_has_strict_production_policy(self):
        self.assertTrue(
            policy.validate(self.rendered, compose_version=self.compose_version)
        )

    def test_browser_session_duration_is_bounded_and_retained_config_still_validates(
        self,
    ):
        for duration in (None, "604800", "900"):
            changed = copy.deepcopy(self.rendered)
            environment = changed["services"]["app"]["environment"]
            environment.pop("INKSEQ_BROWSER_SESSION_SECONDS", None)
            if duration is not None:
                environment["INKSEQ_BROWSER_SESSION_SECONDS"] = duration
            self.assertTrue(
                policy.validate(changed, compose_version=self.compose_version)
            )
        for duration in ("0", "31536001", " 900", "true", 604800):
            changed = copy.deepcopy(self.rendered)
            changed["services"]["app"]["environment"][
                "INKSEQ_BROWSER_SESSION_SECONDS"
            ] = duration
            with self.assertRaises(policy.ConfigurationError):
                policy.validate(changed, compose_version=self.compose_version)

    def test_staging_has_its_own_exact_origin_and_proxy_profile(self):
        candidate = copy.deepcopy(self.rendered)
        candidate['services']['app']['environment'].update(
            PUBLIC_ORIGIN='https://staging.inkseq.com', PUBLIC_ORIGIN_ALIASES='',
            INKSEQ_DEPLOYMENT_TARGET='staging',
        )
        candidate['services']['caddy']['environment'] = {'INKSEQ_LEGACY_REDIRECT': '0'}
        candidate['services']['caddy']['volumes'][0]['source'] = str(
            DEPLOY / 'Caddyfile.staging'
        )
        self.assertTrue(
            policy.validate(
                candidate, compose_version=self.compose_version, target='staging'
            )
        )
        with self.assertRaises(policy.ConfigurationError):
            policy.validate(candidate, compose_version=self.compose_version)
        for mutate in (
            lambda c: c['services']['app']['environment'].update(
                PUBLIC_ORIGIN_ALIASES='https://inkseq.com'
            ),
            lambda c: c['services']['caddy']['environment'].update(
                INKSEQ_LEGACY_REDIRECT='1'
            ),
            lambda c: c['services']['caddy']['volumes'][0].update(
                source=str(DEPLOY / 'Caddyfile')
            ),
        ):
            changed = copy.deepcopy(candidate)
            mutate(changed)
            with self.assertRaises(policy.ConfigurationError):
                policy.validate(
                    changed, compose_version=self.compose_version, target='staging'
                )

    def test_domain_phase_is_closed_and_retained_legacy_configuration_still_validates(
        self,
    ):
        self.assertEqual(
            self.rendered["services"]["app"]["environment"]["PUBLIC_ORIGIN"],
            "https://inkseq.com",
        )
        for phase in ("0", "1"):
            candidate = copy.deepcopy(self.rendered)
            candidate["services"]["caddy"]["environment"] = {
                "INKSEQ_LEGACY_REDIRECT": phase
            }
            self.assertTrue(
                policy.validate(candidate, compose_version=self.compose_version)
            )
        legacy = copy.deepcopy(self.rendered)
        environment = legacy["services"]["app"]["environment"]
        environment.pop("PUBLIC_ORIGIN")
        environment.pop("PUBLIC_ORIGIN_ALIASES")
        environment["SIXNINE_PUBLIC_ORIGIN"] = "https://www.sixnine.art"
        legacy["services"]["caddy"].pop("environment")
        self.assertTrue(policy.validate(legacy, compose_version=self.compose_version))
        for mutation in (
            lambda c: c["services"]["app"]["environment"].update(
                PUBLIC_ORIGIN="https://staging.inkseq.com"
            ),
            lambda c: c["services"]["app"]["environment"].update(
                PUBLIC_ORIGIN_ALIASES="https://*.sixnine.art"
            ),
            lambda c: c["services"]["app"]["environment"].update(
                SIXNINE_PUBLIC_ORIGIN="https://www.sixnine.art"
            ),
            lambda c: c["services"]["caddy"]["environment"].update(
                INKSEQ_LEGACY_REDIRECT="true"
            ),
            lambda c: c["services"]["caddy"]["environment"].update(
                PUBLIC_ORIGIN="https://inkseq.com"
            ),
        ):
            changed = copy.deepcopy(self.rendered)
            mutation(changed)
            with self.assertRaises(policy.ConfigurationError):
                policy.validate(changed, compose_version=self.compose_version)

    def test_title_secret_path_and_egress_are_a_complete_reviewed_pair(self):
        legacy = copy.deepcopy(self.rendered)
        app = legacy['services']['app']
        app['environment'].pop('SIXNINE_TITLE_CONFIG_FILE')
        app['networks'].pop('edge')
        app['secrets'] = [
            item for item in app['secrets'] if item['source'] != 'google_titles'
        ]
        legacy['secrets'].pop('google_titles')
        self.assertTrue(policy.validate(legacy, compose_version=self.compose_version))
        for mutate in (
            lambda c: c['services']['app']['environment'].update(
                SIXNINE_TITLE_CONFIG_FILE='/data/key'
            ),
            lambda c: c['services']['app']['networks'].pop('edge'),
            lambda c: (
                c['services']['db']
                .setdefault('secrets', [])
                .append(
                    {'source': 'google_titles', 'target': '/run/secrets/google_titles'}
                )
            ),
        ):
            broken = copy.deepcopy(self.rendered)
            mutate(broken)
            with self.assertRaises(policy.ConfigurationError):
                policy.validate(broken, compose_version=self.compose_version)

    def test_initial_four_gib_host_has_explicit_two_account_container_envelope(self):
        services = self.rendered["services"]
        expected = {
            "app": 2 * 1024**3,
            "db": 512 * 1024**2,
            "caddy": 128 * 1024**2,
            "db-init": 256 * 1024**2,
        }
        self.assertEqual(
            {name: int(row["mem_limit"]) for name, row in services.items()}, expected
        )
        self.assertEqual(
            sum(expected[name] for name in ("app", "db", "caddy")), 2688 * 1024**2
        )
        self.assertEqual(services["app"]["environment"]["SIXNINE_RENDER_ENABLED"], "0")
        for name, old_bytes in (
            ("app", 3 * 1024**3),
            ("db", 1024**3),
            ("caddy", 256 * 1024**2),
        ):
            changed = copy.deepcopy(self.rendered)
            changed["services"][name]["mem_limit"] = old_bytes
            with self.assertRaises(policy.ConfigurationError):
                policy.validate(changed, compose_version=self.compose_version)

    def test_insecure_configuration_changes_are_rejected(self):
        changes = [
            lambda c: c["services"]["app"]["environment"].update(
                SIXNINE_GENERATION_ENABLED="1"
            ),
            lambda c: c["services"]["app"]["environment"].update(
                SIXNINE_RENDER_ENABLED="1"
            ),
            lambda c: c["services"]["app"]["environment"].pop("SIXNINE_RENDER_ENABLED"),
            lambda c: c["services"]["app"].update(user="0:0"),
            lambda c: c["services"]["db"].update(
                ports=[{"published": "5432", "target": 5432}]
            ),
            lambda c: c["networks"]["database"].update(internal=False),
            lambda c: c["services"]["app"]["environment"].update(
                SIXNINE_DATABASE_URL="synthetic-dsn"
            ),
            lambda c: c["services"]["app"]["command"].append("*"),
            lambda c: c["services"]["caddy"].update(image="caddy:latest"),
        ]
        for change in changes:
            with self.subTest(change=change):
                broken = copy.deepcopy(self.rendered)
                change(broken)
                with self.assertRaises(policy.ConfigurationError):
                    policy.validate(broken, compose_version=self.compose_version)

    def test_only_public_proxy_publishes_ports_and_secrets_are_references(self):
        services = self.rendered["services"]
        self.assertEqual(
            {s for s, value in services.items() if value.get("ports")}, {"caddy"}
        )
        for value in services.values():
            self.assertNotIn("/root/.aws", json.dumps(value))
            self.assertNotIn("docker.sock", json.dumps(value))
        self.assertEqual(
            set(self.rendered["secrets"]),
            {"db_admin_password", "app_database_url", "google_titles"},
        )

    def test_mount_and_resource_policy_rejects_unreviewed_sources_or_privileges(self):
        changes = {
            "bootstrap host socket": lambda c: c["services"]["db-init"][
                "volumes"
            ].append(
                {
                    "type": "bind",
                    "source": "/var/run/docker.sock",
                    "target": "/var/run/docker.sock",
                }
            ),
            "same-name untrusted bootstrap": lambda c: c["services"]["db-init"][
                "volumes"
            ][0].update(source="/incoming/init_database.py"),
            "writable bootstrap": lambda c: c["services"]["db-init"]["volumes"][
                0
            ].update(read_only=False),
            "proxy secret file": lambda c: c["services"]["caddy"]["volumes"].append(
                {
                    "type": "bind",
                    "source": "/run/sixnine-secrets/app_database_url",
                    "target": "/secret",
                }
            ),
            "proxy extra port": lambda c: c["services"]["caddy"]["ports"].append(
                {
                    "mode": "ingress",
                    "target": 2019,
                    "published": "2019",
                    "protocol": "tcp",
                }
            ),
            "named volume host bind": lambda c: c["volumes"]["caddy_data"].update(
                driver_opts={"type": "none", "o": "bind", "device": "/"}
            ),
            "missing memory bound": lambda c: c["services"]["app"].pop("mem_limit"),
            "cpu quota exceeds smallest reviewed host": lambda c: c["services"][
                "app"
            ].update(cpus=3),
            "unlimited pids": lambda c: c["services"]["db"].update(pids_limit=-1),
            "unbounded logs": lambda c: c["services"]["caddy"]["logging"][
                "options"
            ].pop("max-size"),
            "database trust auth": lambda c: c["services"]["db"]["environment"].update(
                POSTGRES_INITDB_ARGS="--auth-host=trust"
            ),
            "database statement logging": lambda c: c["services"]["db"][
                "command"
            ].extend(["-c", "log_statement=all"]),
            "host process namespace": lambda c: c["services"]["app"].update(pid="host"),
            "entrypoint override": lambda c: c["services"]["db-init"].update(
                entrypoint=["sh", "-c"]
            ),
            "extra security option": lambda c: c["services"]["db-init"][
                "security_opt"
            ].append("seccomp=unconfined"),
            "secret target override": lambda c: c["services"]["app"]["secrets"][
                0
            ].update(target="/app/secret"),
            "multiple app workers": lambda c: c["services"]["app"][
                "command"
            ].__setitem__(10, "4"),
            "proxy address spoof": lambda c: c["services"]["app"]["networks"][
                "web"
            ].update(ipv4_address="172.29.69.2"),
            "db arbitrary health command": lambda c: c["services"]["db"][
                "healthcheck"
            ].update(test=["CMD-SHELL", "echo synthetic-command"]),
            "app additional health code": lambda c: c["services"]["app"]["healthcheck"][
                "test"
            ].__setitem__(
                3,
                c["services"]["app"]["healthcheck"]["test"][3]
                + "; print('synthetic-unreviewed-code')",
            ),
            "skip init readiness": lambda c: c["services"]["app"]["depends_on"][
                "db-init"
            ].update(condition="service_started"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                broken = copy.deepcopy(self.rendered)
                change(broken)
                with self.assertRaises(policy.ConfigurationError):
                    policy.validate(broken, compose_version=self.compose_version)

    def test_root_owned_controller_selects_exact_release_directory(self):
        # The controller chooses a reviewed absolute directory after copying the
        # flat bundle; this does not accept a client-provided mount directory.
        trusted = Path(self.temp.name) / "root-owned-release"
        config = copy.deepcopy(self.rendered)
        config["services"]["db-init"]["volumes"][0]["source"] = str(
            trusted / "init_database.py"
        )
        config["services"]["caddy"]["volumes"][0]["source"] = str(trusted / "Caddyfile")
        self.assertTrue(
            policy.validate(
                config,
                deployment_directory=trusted,
                compose_version=self.compose_version,
            )
        )
        with self.assertRaises(policy.ConfigurationError):
            policy.validate(config, compose_version=self.compose_version)
        with self.assertRaises(policy.ConfigurationError):
            policy.validate(
                config,
                deployment_directory="relative-directory",
                compose_version=self.compose_version,
            )

    def test_legacy_compose_2382_omitted_false_requires_trusted_exact_version(self):
        legacy = copy.deepcopy(self.rendered)
        for service in legacy["services"].values():
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    mount["bind"] = {}
        before = copy.deepcopy(legacy)
        for version in ("2.38.2", "v2.38.2"):
            self.assertTrue(policy.validate(legacy, compose_version=version))
        self.assertEqual(legacy, before)  # Do not mutate the approved rendered input.
        for version in (None, "", "2.38.1", "2.39.0", "5.5.1", "v5.5.1"):
            with (
                self.subTest(version=version),
                self.assertRaises(policy.ConfigurationError),
            ):
                policy.validate(legacy, compose_version=version)

    def test_bind_creation_cannot_be_enabled_or_hidden_as_legacy_default(self):
        explicit = copy.deepcopy(self.rendered)
        for service in explicit["services"].values():
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    mount["bind"] = {"create_host_path": False}
        self.assertTrue(policy.validate(explicit))
        for options in (
            None,
            {"create_host_path": True},
            {"create_host_path": 0},
            {"create_host_path": "false"},
            {"create_host_path": False, "propagation": "shared"},
        ):
            for version in (None, "2.38.2", "5.5.1"):
                broken = copy.deepcopy(explicit)
                broken["services"]["app"]["volumes"][0]["bind"] = options
                with (
                    self.subTest(options=options, version=version),
                    self.assertRaises(policy.ConfigurationError),
                ):
                    policy.validate(broken, compose_version=version)
        broken = copy.deepcopy(explicit)
        broken["services"]["app"]["volumes"][0].pop("bind")
        with self.assertRaises(policy.ConfigurationError):
            policy.validate(broken, compose_version="2.38.2")

    def test_caddy_adapts_without_network_or_tls_issuance(self):
        image = "caddy:2.10.2-alpine"
        if docker("image", "inspect", image, check=False).returncode:
            self.skipTest("Reviewed local Caddy image not available; no pull performed")
        for phase in ("0", "1"):
            result = docker(
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_BIND_SERVICE",
                "--env",
                "INKSEQ_LEGACY_REDIRECT=" + phase,
                "--mount",
                f"type=bind,source={DEPLOY / 'Caddyfile'},target=/etc/caddy/Caddyfile,readonly",
                "--tmpfs",
                "/data",
                "--tmpfs",
                "/config",
                "--tmpfs",
                "/tmp",
                image,
                "caddy",
                "adapt",
                "--validate",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            )
            document = json.loads(result.stdout)
            handlers = []

            def walk(value):
                if isinstance(value, dict):
                    if "handler" in value:
                        handlers.append(value)
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(document)
            redirects = [
                item
                for item in handlers
                if item["handler"] == "static_response"
                and "Location" in item.get("headers", {})
            ]
            # 308 is method/body preserving, and URI includes path plus query.
            # No request rewrite handler may discard either before redirect.
            self.assertTrue(redirects)
            self.assertTrue(
                all(str(item["status_code"]) == "308" for item in redirects)
            )
            self.assertTrue(
                any(
                    item["headers"]["Location"]
                    == ["https://inkseq.com{http.request.uri}"]
                    for item in redirects
                )
            )
            self.assertFalse(any(item["handler"] == "rewrite" for item in handlers))
            # Redirects apply to POST as well as GET; no method matcher narrows
            # them to a browser-only request or rewrites the submitted body.
            self.assertNotIn('"method":', json.dumps(document))


class BootstrapInputTests(unittest.TestCase):
    def test_dsn_must_select_private_non_admin_identity(self):
        fake_password = "only-a-synthetic-test-password-123456789"
        dsn = "postgresql+psycopg://sixnine_app:" + fake_password + "@db:5432/sixnine"
        settings = bootstrap.connection_settings(dsn)
        self.assertEqual(
            (settings["user"], settings["host"], settings["dbname"]),
            ("sixnine_app", "db", "sixnine"),
        )
        for bad in (
            dsn.replace("sixnine_app:", "postgres:"),
            dsn.replace("@db:", "@public.example:"),
            dsn.replace("/sixnine", "/another"),
            dsn + "?sslmode=require",
            "not a DSN",
        ):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.connection_settings(bad)

    def test_secret_loading_rejects_links_and_multiline_without_printing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("fake-secret-never-log\nsecond-line", encoding="utf-8")
            path.chmod(0o600)
            try:
                bootstrap.read_secret(path)
            except bootstrap.BootstrapError as error:
                self.assertNotIn(
                    "fake-secret", "".join(traceback.format_exception(error))
                )
            else:
                self.fail("Multiline secret must be rejected")

    def test_driver_errors_do_not_escape_as_connection_strings(self):
        connect = mock.Mock(
            side_effect=RuntimeError("postgresql://fake-secret-never-log@host")
        )
        settings = bootstrap.connection_settings(
            "postgresql+psycopg://sixnine_app:synthetic-test-password-12345678900@db/sixnine"
        )
        try:
            bootstrap.provision(
                "synthetic-admin-password-1234567890", settings, connector=connect
            )
        except bootstrap.BootstrapError as error:
            self.assertNotIn("fake-secret", "".join(traceback.format_exception(error)))
        else:
            self.fail("Expected sanitized bootstrap failure")

    def test_app_password_cannot_also_authenticate_as_admin(self):
        password = "synthetic-shared-password-1234567890"
        settings = bootstrap.connection_settings(
            "postgresql+psycopg://sixnine_app:" + password + "@db/sixnine"
        )
        connect = mock.Mock()
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.provision(password, settings, connector=connect)
        connect.assert_not_called()


@unittest.skipUnless(
    os.environ.get("SIXNINE_TEST_DOCKER_DB") == "1",
    "Ephemeral DB integration is explicit opt-in",
)
class LocalDockerDatabaseTests(unittest.TestCase):
    def test_private_role_bootstrap_is_idempotent_and_uses_protected_files(self):
        application, postgres = (
            "sixnine-platform:local-validation",
            "postgres:17-alpine",
        )
        for image in (application, postgres):
            if docker("image", "inspect", image, check=False).returncode:
                self.skipTest(
                    "Required local test image unavailable; no image pull performed"
                )
        prefix = "sixnine-deploy-test-" + uuid.uuid4().hex[:12]
        network, volume, database = prefix + "-net", prefix + "-secrets", prefix + "-pg"
        created = []
        try:
            docker("network", "create", "--internal", network)
            created.append(("network", network))
            docker("volume", "create", volume)
            created.append(("volume", volume))
            # Synthetic values travel via stdin directly to a disposable secret
            # volume, never command arguments, env, image or test output.
            fake = {
                "db_admin_password": "SYNTHETIC-ADMIN-ONLY-01234567890123456789",
                "app_database_url": "postgresql+psycopg://sixnine_app:SYNTHETIC-APP-ONLY-01234567890123456789@db:5432/sixnine",
            }
            setup = (
                "import json,os,sys,pathlib; values=json.load(sys.stdin); root=pathlib.Path('/run/secrets'); "
                "[( (root/k).write_text(v),os.chmod(root/k,0o440 if k=='app_database_url' else 0o400),"
                "os.chown(root/k,0,10001 if k=='app_database_url' else 0)) for k,v in values.items()]"
            )
            docker(
                "run",
                "--rm",
                "--pull=never",
                "-i",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,source={volume},target=/run/secrets",
                application,
                "python",
                "-c",
                setup,
                input=json.dumps(fake),
            )
            docker(
                "run",
                "-d",
                "--pull=never",
                "--name",
                database,
                "--network",
                network,
                "--network-alias",
                "db",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "SETUID",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/var/run/postgresql:size=16777216,mode=3775",
                "--tmpfs",
                "/tmp:size=67108864,mode=1777",
                "--mount",
                f"type=volume,source={volume},target=/run/secrets,readonly",
                "--tmpfs",
                "/var/lib/postgresql/data:uid=70,gid=70,mode=0700",
                "--env",
                "PGDATA=/var/lib/postgresql/data/pgdata",
                "--env",
                "POSTGRES_PASSWORD_FILE=/run/secrets/db_admin_password",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=peer",
                postgres,
                "postgres",
                "-c",
                "log_statement=none",
                "-c",
                "log_min_error_statement=panic",
            )
            created.append(("container", database))
            for _ in range(30):
                if (
                    docker(
                        "exec", database, "pg_isready", "-U", "postgres", check=False
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(0.5)
            else:
                self.fail("Disposable PostgreSQL did not become ready")
            command = (
                "run",
                "--rm",
                "--pull=never",
                "--network",
                network,
                "--user",
                "0:0",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--tmpfs",
                "/tmp",
                "--mount",
                f"type=volume,source={volume},target=/run/secrets,readonly",
                "--mount",
                f"type=bind,source={DEPLOY / 'init_database.py'},target=/bootstrap.py,readonly",
                application,
                "python",
                "/bootstrap.py",
            )
            for _ in range(2):
                output = docker(*command)
                self.assertIn("database is ready", output.stdout)
                self.assertNotIn("SYNTHETIC-", output.stdout + output.stderr)
            # A dangerous existing app role must be rejected, not silently changed.
            docker(
                "exec",
                "--user",
                "postgres",
                database,
                "psql",
                "-U",
                "postgres",
                "-c",
                "ALTER ROLE sixnine_app SUPERUSER",
            )
            rejected = docker(*command, check=False)
            self.assertEqual(rejected.returncode, 1)
            self.assertNotIn("SYNTHETIC-", rejected.stdout + rejected.stderr)
        finally:
            for kind, name in reversed(created):
                if not name.startswith(prefix):
                    raise AssertionError(
                        "Refusing to clean an unrelated Docker resource"
                    )
                if kind == "container":
                    docker("rm", "-f", name, check=False)
                else:
                    docker(kind, "rm", name, check=False)


if __name__ == "__main__":
    unittest.main()

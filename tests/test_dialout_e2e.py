"""Local end-to-end dial-out (#859): real app, Caddy route, chisel and boot script.

Opt-in: INKSEQ_DIALOUT_E2E=1 with Docker and a pinned chisel 1.12.0 binary at
INKSEQ_E2E_CHISEL (see Dockerfile.dispatch for the checksum). Production
topology on one Linux host: Caddy terminates TLS and routes /v1/worker-tunnel
to the controller's chisel server, everything else to the app. The "GPU host"
is a plain python:3.12-slim container running the run spec's own command and
env. Only dstack, object storage and the GPU are fakes.

    INKSEQ_DIALOUT_E2E=1 INKSEQ_E2E_CHISEL=~/bin/chisel \
        PYTHONPATH=tests:. python -m unittest test_dialout_e2e -v
"""

from contextlib import ExitStack
import copy
import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_dstack_factory import FactoryTests

ENABLED = os.environ.get("INKSEQ_DIALOUT_E2E") == "1"
CHISEL = os.path.expanduser(os.environ.get("INKSEQ_E2E_CHISEL", ""))
CADDY_IMAGE = "caddy:2.10.2"
HOST = None  # This host's IPv4 as containers see it; resolved per run.
WORKER_IMAGE = "python:3.12-slim"
# Transport-level refusals; anything else means the controller reached the worker.
NOT_CONNECTED = {"dstack_runtime_ssh_unavailable_or_host_key_untrusted"}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def certificate(directory):
    """Self-signed localhost certificate; the worker trusts it via SSL_CERT_FILE."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / "key.pem").chmod(0o644)  # Read by the Caddy container user.


class SignedFiles:
    """Content-addressed objects served over plain HTTP, like signed S3 URLs."""

    def __init__(self, root):
        self.root = root
        root.mkdir()

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def put(self, key, stream, *, max_bytes, expected_sha256):
        data = stream.read()
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        (self.root / key.replace("/", "_")).write_bytes(data)

    def presign_download(self, key, *, expires_seconds):
        from studio_platform.storage import SecretURL

        port = self.server.server_address[1]
        url = "http://%s:%d/%s" % (HOST, port, key.replace("/", "_"))
        return SecretURL(url, "GET", time.time() + expires_seconds, {})

    def close(self):
        self.server.shutdown()


def host_address():
    """Where containers reach this host's app, chisel server and signed files."""
    probe = docker(
        "run", "--rm", WORKER_IMAGE, "python", "-c",
        "import socket; print(socket.gethostbyname('host.docker.internal'))",
    )  # fmt: skip
    return probe.stdout.strip()


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=120
    )


@unittest.skipUnless(
    ENABLED
    and os.name == "posix"
    and os.path.isfile(CHISEL)
    and shutil.which("docker"),
    "opt-in: INKSEQ_DIALOUT_E2E=1, INKSEQ_E2E_CHISEL and Docker",
)
class DialoutEndToEnd(FactoryTests):
    def test_worker_boots_dials_out_and_the_controller_reaches_it(self):
        import uvicorn

        from studio_platform import worker_grant, worker_tunnel
        from studio_platform.api import create_app
        from studio_platform.dstack_capacity import DstackError
        from studio_platform.dstack_factory import create_runtime
        from studio_platform.dstack_operator import DstackOperator
        from studio_platform.settings import Settings

        global HOST
        HOST = host_address()
        stack = ExitStack()
        self.addCleanup(stack.close)
        tag = "inkseq-e2e-" + os.urandom(4).hex()
        certs = self.root / "certs"
        certs.mkdir()
        certificate(certs)
        app_port, edge_port = free_port(), free_port()
        origin = "https://localhost:%d" % edge_port
        data_dir = self.root / "api"
        grants = data_dir / "worker-tunnel"
        grants.mkdir(parents=True, mode=0o700)

        # The app, exactly as served: real middleware and the boot route.
        settings = Settings(
            data_dir,
            database_url=self.url,
            tenant_id=self.scope.tenant_id,
            deployment_target="staging",
            public_origin="https://staging.inkseq.com",  # The boot route ignores it.
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, repository=self.repo),
                host="0.0.0.0",
                port=app_port,
                log_level="warning",
            )
        )
        threading.Thread(target=server.run, daemon=True).start()
        stack.callback(setattr, server, "should_exit", True)

        # Caddy with the staging site's tunnel route and reverse proxy.
        (certs / "Caddyfile").write_text(
            "{\n admin off\n auto_https disable_redirects\n}\n"
            "https://localhost:%d {\n tls /certs/cert.pem /certs/key.pem\n"
            " encode zstd gzip\n"
            " handle /v1/worker-tunnel {\n  reverse_proxy %s:%d\n }\n"
            # The app's trusted-host check expects the staging hostname.
            " reverse_proxy %s:%d {\n  header_up Host staging.inkseq.com\n }\n}\n"
            % (edge_port, HOST, worker_tunnel.SERVER_PORT, HOST, app_port)
        )
        docker(
            "run", "-d", "--rm", "--name", tag + "-caddy",
            "-v", "%s:/certs:ro" % certs, CADDY_IMAGE,
            "caddy", "run", "--config", "/certs/Caddyfile",
        )  # fmt: skip
        stack.callback(docker, "rm", "-f", tag + "-caddy", check=False)

        # The controller: the real factory, chisel server and grant writer.
        files = SignedFiles(self.root / "objects")
        stack.callback(files.close)
        children = []
        stopped = threading.Event()

        def popen(command, **kwargs):
            if stopped.is_set():
                raise SystemExit  # Ends the server's restart loop at teardown.
            # WSL relays an IPv4 listener to containers, not Go's dual-stack "*".
            command = ["127.0.0.1" if part == "0.0.0.0" else part for part in command]
            child = subprocess.Popen(command, **kwargs)
            children.append(child)
            return child

        def serve(root, **kwargs):
            return original_serve(root, popen=popen)

        def stop_chisel():
            stopped.set()
            for child in children:
                child.kill()

        original_serve = worker_tunnel.serve
        stack.callback(stop_chisel)
        stack.enter_context(patch.object(worker_tunnel, "CHISEL", CHISEL))
        stack.enter_context(patch.object(worker_tunnel, "serve", serve))
        stack.enter_context(
            patch.dict(worker_grant.GrantWriter.__init__.__kwdefaults__, chisel=CHISEL)
        )
        stack.enter_context(
            patch("studio_platform.fleet.create_store", lambda _: files)
        )
        self.config["dialout_root"] = str(grants)
        self.save_config()
        runtime, _ = create_runtime(
            self.repo,
            self.store,
            configuration_path=self.config_path,
            settings=SimpleNamespace(public_origin=origin),
        )

        # Allocation through the operator: the observed run must stay owned.
        operator_settings = copy.copy(self.settings)
        operator_settings.public_origin = origin
        config = copy.deepcopy(self.operator.config)
        for profile in config["profiles"]:
            profile["transport"] = "dialout"
        self.operator = DstackOperator(
            self.repo, operator_settings, self.capacity, config
        )
        preview = self.operator.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        accepted = self.operator.start(
            self.owner, {"preview_id": preview["preview_id"]}, "e2e"
        )
        observed = self.store.load(accepted["node_id"])
        self.assertNotIn(
            observed.get("state"), {"creation_unknown", "observation_unknown"}
        )
        binding, run = self.allocation_from(observed, "e2e")

        # The "GPU host": the run spec's own command and env, nothing else.
        spec = binding["run_spec"]["configuration"]
        env = [
            arg
            for key, value in spec["env"].items()
            for arg in ("-e", key + "=" + value)
        ]
        docker(
            # Caddy's namespace: localhost:<edge> is the public edge here.
            "run", "-d", "--name", tag + "-worker",
            "--network", "container:" + tag + "-caddy",
            *env, "-e", "SSL_CERT_FILE=/ca.pem",
            "-v", "%s:/ca.pem:ro" % (certs / "cert.pem"),
            WORKER_IMAGE, "bash", "-c", spec["commands"][0],
        )  # fmt: skip
        stack.callback(docker, "rm", "-f", tag + "-worker", check=False)
        stack.callback(runtime.close)

        deadline, code = time.monotonic() + 120, None
        while time.monotonic() < deadline:
            try:
                runtime.readiness(self.store.load(binding["intent_id"]), run)
                code = "ready"
                break
            except DstackError as error:
                code = error.args[0] if error.args else repr(error)
                if code not in NOT_CONNECTED:
                    break
            time.sleep(3)
        logs = docker("logs", tag + "-worker", check=False)
        print("\nworker log:\n" + (logs.stdout + logs.stderr)[-3000:])
        print("controller result:", code)
        self.assertNotIn(code, NOT_CONNECTED | {None})
        self.assertIn("tunnel and status endpoint started", logs.stdout)
        # The worker reached the bootstrap launch; this image has no WanGP.
        self.assertNotIn("dstack_runtime_remote_identity_unconfirmed", code)

    def allocation_from(self, binding, key):
        run = self.client.runs[binding["run_name"]]
        run.update(
            status="running",
            latest_job_submission={
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "gpu-" + key,
                    "hostname": "198.51.100.8",
                    "ssh_port": 21882,
                    "username": "root",
                    "dockerized": False,
                    "ssh_proxy": None,
                    "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}},
                }
            },
        )
        self.store.record(
            binding["intent_id"],
            {
                "run_id": run["id"],
                "provider_instance_id": "gpu-" + key,
                "state": "runtime_unconfirmed",
                "observed_at": self.now,
            },
        )
        return self.store.load(binding["intent_id"]), self.client.get(
            run_name=binding["run_name"]
        )


def load_tests(loader, tests, pattern):
    # Reuses the factory fixtures, never re-runs the factory's own tests.
    return unittest.TestSuite(
        [DialoutEndToEnd("test_worker_boots_dials_out_and_the_controller_reaches_it")]
    )


if __name__ == "__main__":
    unittest.main()

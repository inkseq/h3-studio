"""Local end-to-end dial-out (#859): real app, Caddy route, chisel and boot script.

Opt-in: INKSEQ_DIALOUT_E2E=1 with Docker and a pinned chisel 1.12.0 binary at
INKSEQ_E2E_CHISEL (see Dockerfile.dispatch for the checksum). Production
topology: Caddy (container) terminates TLS and routes /v1/worker-tunnel to the
controller's chisel server, everything else to the app. The "GPU host" is a
plain python:3.12-slim container in Caddy's network namespace, running the run
spec's own command and env, either directly (dstack) or through the real RunPod
Serverless handler (deploy/runpod-serverless/handler.py). Only dstack, RunPod's
API, object storage and the GPU are fakes.

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
# RunPod's SDK loop, faked: the real handler gets the job input once.
RUNPOD_STUB = (
    "import json, os\n"
    "class serverless:\n"
    "    @staticmethod\n"
    "    def start(config):\n"
    "        job = {'input': json.loads(os.environ['JOB'])}\n"
    "        print(json.dumps(config['handler'](job)), flush=True)\n"
)


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


def await_reachable(*ports):
    """WSL relays a new host listener to containers only after a short delay."""
    probe = (
        "import socket, sys, time\n"
        "for port in map(int, sys.argv[2:]):\n"
        "    for _ in range(60):\n"
        "        try:\n"
        "            socket.create_connection((sys.argv[1], port), 1).close()\n"
        "            break\n"
        "        except OSError:\n"
        "            time.sleep(0.5)\n"
        "    else:\n"
        "        sys.exit('unreachable %d' % port)\n"
    )
    docker("run", "--rm", WORKER_IMAGE, "python", "-c", probe, HOST, *map(str, ports))


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
    def edge(self):
        """Caddy edge, the real app, and the controller's chisel server and runtime."""
        import uvicorn

        from studio_platform import worker_grant, worker_tunnel
        from studio_platform.api import create_app
        from studio_platform.dstack_factory import create_runtime
        from studio_platform.settings import Settings

        global HOST
        HOST = host_address()
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        self.tag = "inkseq-e2e-" + os.urandom(4).hex()
        certs = self.certs = self.root / "certs"
        certs.mkdir()
        certificate(certs)
        app_port, edge_port = free_port(), free_port()
        self.origin = "https://localhost:%d" % edge_port
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
            "run", "-d", "--rm", "--name", self.tag + "-caddy",
            "-v", "%s:/certs:ro" % certs, CADDY_IMAGE,
            "caddy", "run", "--config", "/certs/Caddyfile",
        )  # fmt: skip
        stack.callback(docker, "rm", "-f", self.tag + "-caddy", check=False)

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
        # A fresh tunnel port range per run: closed tunnels linger in TIME_WAIT.
        base = 20000 + int.from_bytes(os.urandom(2), "big") % 30000
        self.config.update(first_local_port=base, last_local_port=base + 2)
        self.save_config()
        runtime, _ = create_runtime(
            self.repo,
            self.store,
            configuration_path=self.config_path,
            settings=SimpleNamespace(public_origin=self.origin),
        )
        stack.callback(runtime.close)
        await_reachable(
            app_port, worker_tunnel.SERVER_PORT, files.server.server_address[1]
        )
        return runtime

    def start(self, capacity, **profile_changes):
        """Preview and start through the operator; the run must stay owned."""
        from studio_platform.dstack_operator import DstackOperator

        settings = copy.copy(self.settings)
        settings.public_origin = self.origin
        config = copy.deepcopy(self.operator.config)
        for profile in config["profiles"]:
            profile.update(transport="dialout", **profile_changes)
        self.operator = DstackOperator(self.repo, settings, capacity, config)
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
        return observed

    def worker(self, name, command, env, *extra):
        """A container in Caddy's namespace: localhost:<edge> is the public edge."""
        pairs = [arg for k, v in env.items() for arg in ("-e", k + "=" + v)]
        docker(
            "run", "-d", "--name", name,
            "--network", "container:" + self.tag + "-caddy",
            *pairs, "-e", "SSL_CERT_FILE=/ca.pem",
            "-v", "%s:/ca.pem:ro" % (self.certs / "cert.pem"),
            *extra, WORKER_IMAGE, *command,
        )  # fmt: skip
        self.stack.callback(docker, "rm", "-f", name, check=False)

    def wait(self, observe, worker):
        deadline, code = time.monotonic() + 120, None
        while time.monotonic() < deadline:
            code = observe()
            if code not in NOT_CONNECTED:
                break
            time.sleep(3)
        logs = docker("logs", worker, check=False)
        print("\nworker stdout:\n" + logs.stdout[-2000:])
        print("worker stderr:\n" + logs.stderr[-2000:])
        print("controller result:", code)
        self.assertNotIn(code, NOT_CONNECTED | {None})
        self.assertIn("tunnel and status endpoint started", logs.stdout)
        # The worker reached the bootstrap launch; this image has no WanGP.
        self.assertNotIn("dstack_runtime_remote_identity_unconfirmed", code)

    def test_worker_boots_dials_out_and_the_controller_reaches_it(self):
        from studio_platform.dstack_capacity import DstackError

        runtime = self.edge()
        binding, run = self.allocation_from(self.start(self.capacity), "e2e")
        spec = binding["run_spec"]["configuration"]
        name = self.tag + "-worker"
        self.worker(name, ["bash", "-c", spec["commands"][0]], spec["env"])

        def observe():
            try:
                runtime.readiness(self.store.load(binding["intent_id"]), run)
                return "ready"
            except DstackError as error:
                return error.args[0] if error.args else repr(error)

        self.wait(observe, name)

    def test_runpod_serverless_job_boots_through_the_real_handler(self):
        import httpx

        from studio_platform.dstack_capacity import DstackCapacity
        from studio_platform.runpod_serverless import (
            BACKEND,
            RoutedClient,
            RunPodServerlessClient,
        )

        runtime = self.edge()
        handler = Path(__file__).resolve().parents[1] / (
            "deploy/runpod-serverless/handler.py"
        )
        stub = self.root / "runpod"
        stub.mkdir()
        (stub / "__init__.py").write_text(RUNPOD_STUB)
        name = self.tag + "-serverless"
        jobs = {}

        def runpod_api(request):
            # RunPod's queue: /run starts one worker running the real handler.
            path = request.url.path
            if path.endswith("/run"):
                body = json.loads(request.content)
                self.worker(
                    name,
                    ["python", "/h/handler.py"],
                    {"JOB": json.dumps(body["input"])},
                    "-v", "%s:/h/handler.py:ro" % handler,
                    "-v", "%s:/h/runpod:ro" % stub,
                    "-e", "PYTHONPATH=/h",
                )  # fmt: skip
                jobs["j1"] = "IN_PROGRESS"
                return httpx.Response(200, json={"id": "j1", "status": "IN_QUEUE"})
            if "/status/" in path:
                return httpx.Response(
                    200, json={"id": "j1", "status": jobs["j1"], "workerId": name}
                )
            if "/cancel/" in path:
                jobs["j1"] = "CANCELLED"
                docker("rm", "-f", name, check=False)
                return httpx.Response(200, json={"status": "CANCELLED"})
            live = int(jobs.get("j1") == "IN_PROGRESS")
            return httpx.Response(
                200, json={"jobs": {"inQueue": 0, "inProgress": live}}
            )

        serverless = RunPodServerlessClient(
            {
                "api_key": "k" * 32,
                "endpoint_id": "e2e",
                "gpu_name": "RTX5090",
                "gpu_memory_gib": 32,
                "cpus": 12,
                "memory_gib": 96,
                "disk_gib": 128,
                "hourly_microusd": 700_000,
                "state_dir": str(self.root / "serverless"),
            },
            self.client.project,
            http=httpx.Client(transport=httpx.MockTransport(runpod_api)),
            clock=self.repo.clock,
        )
        capacity = DstackCapacity(
            RoutedClient(self.client, serverless),
            self.store,
            clock=self.repo.clock,
            readiness=runtime.readiness,
        )
        observed = self.start(capacity, backend=BACKEND)
        self.assertEqual(observed["backend"], BACKEND)
        intent = observed["intent_id"]
        self.wait(lambda: capacity.observe(intent).get("reason_code"), name)
        # Once drained, the capacity stop of this run cancels exactly this job.
        capacity.client.stop(self.store.load(intent)["run_name"])
        self.assertEqual(jobs["j1"], "CANCELLED")
        self.assertEqual(capacity.observe(intent)["state"], "stopped")

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
        [
            DialoutEndToEnd(name)
            for name in (
                "test_worker_boots_dials_out_and_the_controller_reaches_it",
                "test_runpod_serverless_job_boots_through_the_real_handler",
            )
        ]
    )


if __name__ == "__main__":
    unittest.main()

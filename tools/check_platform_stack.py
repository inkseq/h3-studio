"""Explicit, disposable local Docker HTTPS -> password app -> PG -> Local drill.

No cloud, pulls, host ports, installed CA, production paths, or real credentials.
Run from the reviewed source checkout. Runtime fixture values only cross stdin;
container logs are disabled. Each resource bears a unique label, checked again
before cleanup. This is deliberately separate from the production Compose name.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid

IMAGES = ("sixnine-platform:captions-precommit-20261004", "postgres:17-alpine", "caddy:2.10.2-alpine")
LABEL = "art.sixnine.isolated-stack-check"
ORIGIN = "https://stack.test"


class StackCheckError(Exception):
    """Only constant diagnostic codes may escape the fixture boundary."""


def require(condition, code):
    if not condition:
        raise StackCheckError(code)


def application_commit(image):
    """Never silently select a historical image for a final release check."""
    if image == IMAGES[0]:
        return None
    match = re.fullmatch(r"(?:sixnine|inkseq)-platform:([0-9a-f]{40})", image or "")
    require(match is not None, "explicit_reviewed_platform_image_required")
    return match[1]


def emit(value):
    print(json.dumps(value), flush=True)


def fixture_settings():
    sys.path.insert(0, "/app")
    from studio_platform.settings import Settings
    return Settings.from_environment()


def configure_fixture(payload):
    root = Path("/run/secrets")
    os.chmod(root, 0o750)
    os.chown(root, 0, 10001)
    for name in ("db_admin_password", "app_database_url"):
        path = root/name
        with path.open("x", encoding="utf-8") as output:
            output.write(payload[name])
        os.chmod(path, 0o440 if name == "app_database_url" else 0o400)
        os.chown(path, 0, 10001 if name == "app_database_url" else 0)
    os.chmod("/data", 0o700)
    os.chown("/data", 10001, 10001)
    os.chmod("/pgdata", 0o700)
    os.chown("/pgdata", 70, 70)
    emit({"stage": "fixture_configured"})


def provision_fixture(payload):
    result = subprocess.run([sys.executable, "/bootstrap.py"], capture_output=True, timeout=30)
    require(result.returncode == 0, "isolated_role_bootstrap_failed")
    settings = fixture_settings()
    from studio_platform.repository import Repository
    from studio_platform.auth import Auth
    repo = Repository(settings.database_url)
    try:
        repo.create_schema()
        auth = Auth(repo.engine)
        for username in ("superdan", "supervan"):
            auth.set_password(username, payload[username])
        require(auth.ready(), "isolated_accounts_not_ready")
    finally:
        repo.close()
    emit({"stage": "accounts_provisioned"})


def audit_fixture(payload):
    settings = fixture_settings()
    from sqlalchemy import select, text, func
    from studio_platform.repository import Repository, attempts, jobs
    from studio_platform.auth import login_limits, digest
    repo = Repository(settings.database_url)
    try:
        with repo.engine.connect() as conn:
            identity = conn.execute(text("SELECT current_user, current_database()")).one()
            require(tuple(identity) == ("sixnine_app", "sixnine"), "wrong_database_identity")
            flags = conn.execute(text("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
            require(not any(flags), "database_role_not_least_privilege")
            observed = set(conn.execute(select(login_limits.c.source_hash)).scalars())
            require(observed == {digest(ip) for ip in payload["client_ips"]}, "proxy_source_identity_incorrect")
            require(not any(digest(ip) in observed for ip in payload["spoof_ips"]), "spoofed_source_identity_trusted")
            require(conn.execute(select(func.count()).select_from(attempts)).scalar_one() == 0,
                    "unexpected_generation_attempt")
            require(set(conn.execute(select(jobs.c.status)).scalars()) == {"blocked"}, "unexpected_job_execution")
    finally:
        repo.close()
    emit({"stage": "ledger_audited", "least_privilege": True, "proxy_sources_verified": True,
          "attempt_count": 0, "execution_disabled": True})


def client_context(payload):
    import ssl
    import httpx
    context = ssl.create_default_context(cadata=payload["ca"])
    require(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname, "tls_not_verified")
    return httpx.Client(base_url=ORIGIN, verify=context, trust_env=False, follow_redirects=False,
                        timeout=15, headers={"Origin": ORIGIN})


def wait_http(client):
    deadline = time.monotonic()+60
    while time.monotonic() < deadline:
        try:
            response = client.get("/healthz")
            if response.status_code == 200 and response.json().get("auth_ready") is True:
                return response.json()
        except Exception:
            pass
        time.sleep(.25)
    raise StackCheckError("https_app_did_not_become_ready")


def client_scenario(payload):
    import io
    import ssl
    import httpx
    from PIL import Image
    with client_context(payload) as readiness:
        wait_http(readiness)
    # Also demonstrate that no default/host trust was installed: the temporary
    # internal CA must fail in a fresh ordinary client context.
    with httpx.Client(base_url=ORIGIN, trust_env=False, timeout=10) as untrusted:
        try:
            untrusted.get("/healthz")
        except httpx.ConnectError as error:
            require("CERTIFICATE_VERIFY_FAILED" in str(error), "untrusted_tls_failed_for_unexpected_reason")
        else:
            raise StackCheckError("temporary_ca_unexpectedly_globally_trusted")
    with client_context(payload) as client:
        health = wait_http(client)
        require(not health["generation_enabled"] and not health["render_enabled"]
                and not health["cloud_creation_enabled"] and health["execution_backend"] == "disabled", "execution_policy_not_disabled")
        require(client.get("/").status_code == client.get("/freestyle").status_code == 200, "frontend_not_served")
        require(client.get("/v1/projects").status_code == 401, "anonymous_projects_exposed")
        login = client.post("/api/auth/login", json={"username": "superdan", "password": payload["superdan"]})
        require(login.status_code == 200, "password_login_failed")
        require(all(flag in login.headers.get("set-cookie", "") for flag in ("HttpOnly", "Secure", "SameSite=lax")), "session_cookie_flags_missing")
        entity = lambda key, kind, parent=None: dict(id=key, type=kind, parentId=parent, title=key,
            description="Synthetic local stack test", version=1, order=0, status="draft", data={"seconds": 5} if kind == "shot" else {})
        project = dict(schemaVersion=4, id="stack-project", title="Synthetic stack project", logline="",
            entities=[entity("chapter", "chapter"), entity("scene", "scene", "chapter"), entity("shot", "shot", "scene")],
            links=[], jobs=[], layout={"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}})
        require(client.post("/v1/projects", json={"project": project}).status_code == 201, "project_create_failed")
        require(client.post("/v1/projects", json={"project": project}, headers={"Origin": "https://attacker.invalid"}).status_code == 403,
                "foreign_origin_not_rejected")
        require(client.get("/v1/projects", headers={"X-Expected-Account": "supervan"}).status_code == 409, "stale_tab_account_not_rejected")
        output = io.BytesIO()
        Image.new("RGB", (512, 512), "navy").save(output, format="PNG")
        original = output.getvalue()
        uploaded = client.post("/v1/assets", data={"client_project_id": project["id"], "client_asset_id": "image-entity:stack-file"},
            files={"file": ("synthetic.png", original, "image/png")})
        require(uploaded.status_code == 201 and uploaded.json()["metadata"]["model_ready"], "upload_or_normalization_failed")
        asset = uploaded.json()
        download = client.get(asset["content_url"])
        require(download.status_code == 200 and download.content == original, "private_media_bytes_differ")
        partial = client.get(asset["content_url"], headers={"Range": "bytes=2-6"})
        require(partial.status_code == 206 and partial.content == original[2:7]
                and partial.headers.get("content-range") == f"bytes 2-6/{len(original)}", "range_response_incorrect")
        head = client.head(asset["content_url"])
        require(head.status_code == 200 and not head.content and int(head.headers["content-length"]) == len(original), "head_response_incorrect")
        require(client.get(asset["content_url"], headers={"Range": f"bytes={len(original)}-"}).status_code == 416, "unsatisfied_range_not_rejected")
        attachment = client.get(asset["content_url"]+"?download=1")
        require(attachment.content == original and attachment.headers.get("content-disposition", "").startswith("attachment;"), "attachment_not_private_or_incorrect")
        request = {"client_ref": {"project_id": project["id"], "shot_id": "shot", "shot_version": 1},
            "recipe_id": "h3-base-fl2va-v1", "prompt": "Synthetic non-generated scene",
            "inputs": {"first_frame": asset["id"]}, "controls": {"resolution": "480P", "duration": 5}}
        plan = client.post("/v1/generation-plans", json=request)
        require(plan.status_code == 201 and plan.json()["status"] == "blocked", "generation_plan_not_blocked")
        job = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers={"Idempotency-Key": "stack-blocked-once"})
        require(job.status_code == 202 and job.json()["status"] == "blocked", "generation_job_not_blocked")
        duplicate = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers={"Idempotency-Key": "stack-blocked-once"})
        require(duplicate.json()["id"] == job.json()["id"], "job_idempotency_changed")
        with client_context(payload) as other:
            require(other.post("/api/auth/login", json={"username": "supervan", "password": payload["supervan"]}).status_code == 200, "second_user_login_failed")
            require(other.get("/v1/projects").json() == {"projects": []}, "other_user_project_exposed")
            for path in ("/v1/projects/"+project["id"], asset["content_url"], "/v1/jobs/"+job.json()["id"]):
                require(other.get(path).status_code == 404, "cross_user_resource_exposed")
            require(other.get(asset["content_url"], headers={"Range": "bytes=0-7"}).status_code == 404, "cross_user_range_exposed")
            require(other.post("/api/auth/logout").status_code == 200
                    and other.get("/api/auth/me").status_code == 401, "logout_not_revoked")
        emit({"stage": "ready_for_restart", "https_verified": True, "media_sha256": hashlib.sha256(original).hexdigest(),
              "cross_user_isolation": True, "range_and_head": True})
        require(json.loads(sys.stdin.readline()) == {"command": "after_restart"}, "unexpected_fixture_phase")
        wait_http(client)
        require(client.get("/api/auth/me").json().get("username") == "superdan", "session_lost_after_restart")
        require(client.get("/v1/projects/"+project["id"]).status_code == 200
                and client.get(asset["content_url"]).content == original, "persistent_data_lost_after_restart")
        require(client.get("/v1/jobs/"+job.json()["id"]).json()["status"] == "blocked", "blocked_job_changed_after_restart")
        with client_context(payload) as fresh:
            require(fresh.post("/api/auth/login", json={"username": "superdan", "password": payload["superdan"]}).status_code == 200,
                    "account_lost_after_restart")
        # Two initial logins plus one fresh login have consumed three slots.
        # Altering attacker-controlled XFF must not grant a new source budget.
        codes = []
        for suffix in range(4):
            response = client.post("/api/auth/login", json={"username": "superdan", "password": "deliberately-wrong-fixture"},
                headers={"X-Forwarded-For": f"203.0.113.{81+suffix}", "X-Real-IP": f"203.0.113.{81+suffix}"})
            codes.append(response.status_code)
        require(codes == [401, 401, 429, 429], "spoofed_xff_bypassed_login_limit")
        emit({"stage": "client_complete", "restart_persistence": True, "spoofed_xff_rejected": True})


def second_source(payload):
    import httpx
    with client_context(payload) as client:
        response = client.post("/api/auth/login", json={"username": "supervan", "password": payload["supervan"]},
            headers={"X-Forwarded-For": "203.0.113.87"})
        require(response.status_code == 200, "proxy_collapsed_distinct_client_sources")
    # A client attached to the backend test network is not the fixed trusted
    # proxy. Its forwarding headers must also be ignored by Uvicorn itself.
    with httpx.Client(base_url="http://app:8845", trust_env=False, timeout=10,
                      headers={"Host": "stack.test", "Origin": ORIGIN}) as direct:
        response = direct.post("/api/auth/login", json={"username": "supervan", "password": "deliberately-wrong-fixture"},
            headers={"X-Forwarded-For": "203.0.113.88", "X-Forwarded-Proto": "https"})
        require(response.status_code == 401, "direct_untrusted_client_check_failed")
    emit({"stage": "second_source_complete", "distinct_client_budget": True})


def child_main(action):
    try:
        payload = json.loads(sys.stdin.readline())
        {"configure": configure_fixture, "provision": provision_fixture, "audit": audit_fixture,
         "scenario": client_scenario, "second-source": second_source}[action](payload)
        return 0
    except Exception as error:
        # Never allow HTTP exceptions, cookies, driver DSNs or fixture input in
        # tracebacks. The diagnostic comes only from this module's constants.
        emit({"stage": "failed", "reason": str(error) if isinstance(error, StackCheckError) else "isolated_child_failed"})
        return 1


class DockerDrill:
    def __init__(self, directory, application_image):
        self.directory = directory
        self.application_image = application_image
        self.commit = application_commit(application_image)
        self.run_id = uuid.uuid4().hex
        self.prefix = "sixnine-stack-test-"+self.run_id[:12]
        self.resources = []
        self.process = None
        self.phase = "initializing"
        self.docker_host = "npipe:////./pipe/dockerDesktopLinuxEngine" if os.name == "nt" else "unix:///var/run/docker.sock"
        self.env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
        self.env["DOCKER_CONFIG"] = str(directory/"empty-docker-config")
        Path(self.env["DOCKER_CONFIG"]).mkdir()
        self.script = Path(__file__).resolve()

    def command(self, *args, input_data=None, timeout=90, check=True):
        try:
            result = subprocess.run(["docker", "--host", self.docker_host, *args], env=self.env,
                input=input_data, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            raise StackCheckError("isolated_docker_command_unavailable") from None
        if check and result.returncode:
            raise StackCheckError("isolated_docker_command_failed")
        return result

    def create(self, kind, suffix, *args, start=True):
        self.phase = "create_"+suffix
        name = self.prefix+"-"+suffix
        if kind == "container":
            result = self.command("create", "--pull=never", "--name", name,
                "--label", LABEL+"="+self.run_id, "--log-driver", "none", *args)
        else:
            result = self.command(kind, "create", "--label", LABEL+"="+self.run_id, *args, name)
        self.resources.append((kind, name))
        if kind == "container" and start:
            self.command("start", name)
        return name

    def mount(self, source, target, *, readonly=False, bind=False):
        return "type="+("bind" if bind else "volume")+",source="+str(source)+",target="+target+(",readonly" if readonly else "")

    def execute_child(self, container, action, payload):
        self.phase = "child_"+action
        result = self.command("exec", "-i", container, "python", "-B", str("/check_stack.py"), "--child", action,
                              input_data=json.dumps(payload)+"\n", check=False)
        value = json.loads(result.stdout)
        require(result.returncode == 0 and value.get("stage") != "failed", value.get("reason", "isolated_child_stage_failed"))
        return value

    def cleanup(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=15)
        failed = False
        for kind, name in reversed(self.resources):
            require(name.startswith(self.prefix+"-"), "refusing_unrelated_resource_cleanup")
            result = self.command(kind, "inspect", name, check=False)
            if result.returncode:
                failed = True
                continue
            info = json.loads(result.stdout)[0]
            labels = info.get("Config", {}).get("Labels", {}) if kind == "container" else info.get("Labels", {})
            require(labels.get(LABEL) == self.run_id, "refusing_resource_with_different_label")
            args = ("rm", "-f", name) if kind == "container" else (kind, "rm", name)
            result = self.command(*args, check=False)
            failed |= bool(result.returncode)
        for kind in ("container", "network", "volume"):
            arguments = ("ps", "-aq") if kind == "container" else (kind, "ls", "-q")
            result = self.command(*arguments, "--filter", "label="+LABEL+"="+self.run_id)
            failed |= bool(result.stdout.strip())
        require(not failed, "isolated_cleanup_not_fully_verified")

    def run(self):
        images = {}
        for image in (self.application_image, *IMAGES[1:]):
            self.phase = "inspect_local_images"
            info = json.loads(self.command("image", "inspect", image).stdout)[0]
            images[image] = info["Id"]
            if image == self.application_image and self.commit:
                require(info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == self.commit,
                        "application_image_revision_does_not_match_commit")
        require(json.loads(self.command("info", "--format", "{{json .OSType}}").stdout) == "linux", "linux_docker_required")
        web = self.create("network", "web", "--internal")
        database_net = self.create("network", "db", "--internal")
        subnet = json.loads(self.command("network", "inspect", web).stdout)[0]["IPAM"]["Config"][0]["Subnet"]
        addresses = ipaddress.ip_network(subnet)
        proxy_ip, app_ip, client_ip, second_ip = [str(addresses[x]) for x in (2, 3, 10, 11)]
        secret_volume = self.create("volume", "secrets", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=size=16777216,mode=0700")
        data_volume = self.create("volume", "media")
        pg_volume = self.create("volume", "pgdata")
        caddy_volume = self.create("volume", "tls")
        app_env = ["--env", "SIXNINE_DATA=/data", "--env", "SIXNINE_DATABASE_URL_FILE=/run/secrets/app_database_url",
            "--env", "SIXNINE_PUBLIC_ORIGIN="+ORIGIN, "--env", "SIXNINE_AUTH_MODE=password",
            "--env", "SIXNINE_GENERATION_ENABLED=0", "--env", "SIXNINE_RENDER_ENABLED=0", "--env", "SIXNINE_EXECUTION_BACKEND=disabled",
            "--env", "SIXNINE_CLOUD_CREATION_ENABLED=0", "--env", "SIXNINE_STORAGE_PROVIDER=local", "--env", "SIXNINE_FRONTEND_DIR=/app/yingxu-dist"]
        restricted = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--memory", "1g", "--tmpfs", "/tmp:size=67108864,mode=1777"]
        helper = self.create("container", "helper", "--network", database_net, "--user", "0:0", *restricted,
            "--cap-add", "CHOWN", *app_env, "--mount", self.mount(secret_volume, "/run/secrets"), "--mount", self.mount(data_volume, "/data"),
            "--mount", self.mount(pg_volume, "/pgdata"),
            "--mount", self.mount(self.script, "/check_stack.py", readonly=True, bind=True),
            "--mount", self.mount(self.script.parent.parent/"deploy/platform/init_database.py", "/bootstrap.py", readonly=True, bind=True),
            self.application_image, "python", "-c", "import time; time.sleep(1800)")
        passwords = {username: secrets.token_urlsafe(30) for username in ("superdan", "supervan")}
        fixture = {"db_admin_password": secrets.token_urlsafe(40),
            "app_database_url": "postgresql+psycopg://sixnine_app:"+secrets.token_urlsafe(40)+"@db:5432/sixnine"}
        self.execute_child(helper, "configure", fixture)
        del fixture
        database = self.create("container", "postgres", "--network", database_net, "--network-alias", "db", *restricted,
            "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER", "--cap-add", "SETGID", "--cap-add", "SETUID",
            "--tmpfs", "/var/run/postgresql:size=16777216,mode=3775", "--mount", self.mount(pg_volume, "/var/lib/postgresql/data"),
            "--mount", self.mount(secret_volume, "/run/secrets", readonly=True), "--env", "PGDATA=/var/lib/postgresql/data/pgdata",
            "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/db_admin_password", "--env", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            "--env", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=peer", IMAGES[1], "postgres",
            "-c", "password_encryption=scram-sha-256", "-c", "log_statement=none", "-c", "log_min_error_statement=panic")
        deadline = time.monotonic()+45
        while self.command("exec", database, "pg_isready", "-U", "postgres", check=False).returncode:
            require(time.monotonic() < deadline, "isolated_postgres_not_ready")
            time.sleep(.3)
        self.execute_child(helper, "provision", passwords)
        application = self.create("container", "app", "--network", web, "--ip", app_ip, "--network-alias", "app",
            "--user", "10001:10001", *restricted, *app_env, "--mount", self.mount(secret_volume, "/run/secrets", readonly=True),
            "--mount", self.mount(data_volume, "/data"), self.application_image, "python", "-m", "uvicorn", "platform_app:app",
            "--host", "0.0.0.0", "--port", "8845", "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", proxy_ip, start=False)
        self.command("network", "connect", database_net, application)
        self.command("start", application)
        self.command("exec", application, "python", "-c", "import os; assert os.getuid()==10001; "
            "assert os.access('/run/secrets/app_database_url',os.R_OK); "
            "assert not os.access('/run/secrets/db_admin_password',os.R_OK)")
        caddyfile = self.directory/"Caddyfile"
        caddyfile.write_text("{\n admin off\n auto_https disable_redirects\n skip_install_trust\n}\n"+ORIGIN+" {\n tls internal\n reverse_proxy app:8845 {\n header_up X-Forwarded-For {remote_host}\n header_up X-Forwarded-Proto https\n header_up X-Forwarded-Host stack.test\n }\n}\n", encoding="utf-8")
        proxy = self.create("container", "caddy", "--network", web, "--ip", proxy_ip, "--network-alias", "stack.test",
            *restricted, "--cap-add", "NET_BIND_SERVICE", "--tmpfs", "/config:size=16777216,mode=0700",
            "--mount", self.mount(caddy_volume, "/data"), "--mount", self.mount(caddyfile, "/etc/caddy/Caddyfile", readonly=True, bind=True), IMAGES[2])
        deadline = time.monotonic()+30
        while True:
            result = self.command("exec", proxy, "cat", "/data/caddy/pki/authorities/local/root.crt", check=False)
            if result.returncode == 0 and "BEGIN CERTIFICATE" in result.stdout:
                ca = result.stdout
                break
            require(time.monotonic() < deadline, "isolated_caddy_ca_not_ready")
            time.sleep(.3)
        clients = []
        for suffix, address in (("client-a", client_ip), ("client-b", second_ip)):
            clients.append(self.create("container", suffix, "--network", web, "--ip", address, "--user", "10001:10001", *restricted,
                "--mount", self.mount(self.script, "/check_stack.py", readonly=True, bind=True),
                self.application_image, "python", "-c", "import time; time.sleep(1800)"))
        self.process = subprocess.Popen(["docker", "--host", self.docker_host, "exec", "-i", clients[0], "python", "-B", "/check_stack.py", "--child", "scenario"],
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        messages = queue.Queue()
        threading.Thread(target=lambda: [messages.put(line) for line in self.process.stdout], daemon=True).start()
        self.process.stdin.write(json.dumps({**passwords, "ca": ca})+"\n")
        self.process.stdin.flush()
        def phase(expected):
            try:
                value = json.loads(messages.get(timeout=90))
            except Exception:
                raise StackCheckError("isolated_https_phase_timeout") from None
            require(value.get("stage") == expected, value.get("reason", "isolated_https_phase_failed"))
            return value
        first = phase("ready_for_restart")
        self.command("restart", "--time", "20", database, timeout=40)
        deadline = time.monotonic()+45
        while self.command("exec", database, "pg_isready", "-U", "postgres", check=False).returncode:
            require(time.monotonic() < deadline, "isolated_postgres_restart_not_ready")
            time.sleep(.3)
        self.command("restart", "--time", "20", application, timeout=40)
        self.process.stdin.write(json.dumps({"command": "after_restart"})+"\n")
        self.process.stdin.flush()
        second = phase("client_complete")
        self.process.stdin.close()
        require(self.process.wait(timeout=15) == 0, "isolated_client_exit_failed")
        third = self.execute_child(clients[1], "second-source", {**passwords, "ca": ca})
        del passwords
        audit = self.execute_child(helper, "audit", {"client_ips": [client_ip, second_ip], "spoof_ips": [proxy_ip, *["203.0.113."+str(n) for n in range(81, 89)]]})
        # No service has host port bindings and both fixture networks stay internal.
        for name in (application, database, proxy, helper, *clients):
            info = json.loads(self.command("container", "inspect", name).stdout)[0]
            require(not info["HostConfig"]["PortBindings"], "unexpected_host_port_exposed")
        for name in (web, database_net):
            require(json.loads(self.command("network", "inspect", name).stdout)[0]["Internal"] is True, "unexpected_external_network")
        return {"state": "passed", "run_id": self.run_id, "images": images, "release_commit": self.commit,
            "precommit_evidence": self.commit is None,
            "init_database_source_sha256": hashlib.sha256((self.script.parent.parent/"deploy/platform/init_database.py").read_bytes()).hexdigest(),
            "tls": "internal-ca-client-context-only",
            "host_ports": [], "external_network": False, "stages": [first, second, third, audit],
            "app_admin_secret_unreadable": True, "real_supplier_requests": False, "production_deployed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("configure", "provision", "audit", "scenario", "second-source"), help=argparse.SUPPRESS)
    parser.add_argument("--image", help="Required existing inkseq-platform:<40hex commit> (legacy sixnine-platform accepted), or explicit historical captions-precommit-20261004 tag")
    parser.add_argument("--report", type=Path, help="Optional NEW non-secret JSON result path")
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args.child)
    if not args.image:
        parser.error("--image is required; no historical image is selected by default")
    application_commit(args.image)
    if args.report:
        require(args.report.is_absolute() and not args.report.exists() and args.report.parent.is_dir(), "new_absolute_report_path_required")
    result, failure = None, None
    with tempfile.TemporaryDirectory(prefix="sixnine-stack-host-") as temporary:
        drill = DockerDrill(Path(temporary), args.image)
        try:
            result = drill.run()
        except Exception as error:
            failure = str(error) if isinstance(error, StackCheckError) else "isolated_stack_check_failed"
        finally:
            try:
                drill.cleanup()
            except Exception:
                failure = "isolated_cleanup_not_fully_verified"
        if failure:
            emit({"state": "failed", "reason": failure, "phase": drill.phase, "resource_label": LABEL+"="+drill.run_id})
            return 1
    result["cleanup_verified"] = True
    if args.report:
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2)
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

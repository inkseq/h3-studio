"""Read-only Compose migration check; never up/pull/create/stop or print config.

Run on the CPU release host with existing nonsecret path/image variables and
--local-inputs to validate protected mounts. Optional --probe checks existing
private HTTP services through app's namespace; it creates no infrastructure.
Default mode only renders/validates Compose and reads no mounted secret bytes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DSTACK_IMAGE = "dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8"
HATCHET_IMAGE = "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.110.5@sha256:cc81f078a5285d7d2051ec4a4fa459d033e1face4ca5983d82612b00da6e3fa3"
CPU_SHARED = ("dstack", "hatchet", "dispatch", "controller")
PROBE = """import json,pathlib,urllib.request,urllib.error
def request(url,token=None,*,parse_json=True):
    headers={'Authorization':'Bearer '+token} if token else {}
    method='POST' if url.endswith('/get_my_user') else 'GET'
    req=urllib.request.Request(url,headers=headers,data=b'{}' if method=='POST' else None,method=method)
    try:
        with urllib.request.urlopen(req,timeout=5) as response:
            body=response.read(262145)
            assert len(body)<=262144
            return response.status,json.loads(body) if parse_json else None
    except urllib.error.HTTPError as error:
        return error.code,None
try:
    token=pathlib.Path('/run/secrets/dstack_api_token').read_text().strip()
    assert request('http://127.0.0.1:3000/healthcheck')[1]['status']=='running'
    status,_=request('http://127.0.0.1:3000/api/users/get_my_user'); assert status in (401,403)
    status,user=request('http://127.0.0.1:3000/api/users/get_my_user',token)
    assert status==200 and user['username']=='admin'
    assert request('http://127.0.0.1:8888/api/ready',parse_json=False)[0]==200
except Exception:
    raise SystemExit('migration_private_probe_failed') from None
print('migration_private_probe_passed')
"""


def require(condition, code):
    if not condition:
        raise ValueError(code)


def memory_mib(value):
    if type(value) is int or isinstance(value, str) and value.isdecimal():
        require(int(value) > 0, "migration_memory_limit_missing")
        return int(value) / 1024**2
    match = re.fullmatch(r"([0-9]+)([kmg])", str(value).lower())
    require(match is not None, "migration_memory_limit_missing")
    return int(match[1]) * {"k": 1/1024, "m": 1, "g": 1024}[match[2]]


def secret_names(service):
    return {item if isinstance(item, str) else item["source"] for item in service.get("secrets", [])}


def validate(document, *, host_memory_mib=3840):
    require(type(document) is dict and type(document.get("services")) is dict, "migration_compose_invalid")
    services = document["services"]
    require({"app", "db", "db-init", "caddy", "hatchet-db", *CPU_SHARED} <= set(services), "migration_services_missing")
    require(services["dstack"]["image"] == DSTACK_IMAGE and services["hatchet"]["image"] == HATCHET_IMAGE,
            "migration_infrastructure_image_changed")
    for name, service in services.items():
        require(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", service.get("image", "")), "migration_image_unpinned")
        require(service.get("pull_policy") == "never", "migration_implicit_image_pull")
        require(not any(service.get(key) for key in ("gpus", "devices", "privileged", "env_file")), "migration_unsafe_service")
        environment = service.get("environment", {})
        require(type(environment) is dict, "migration_environment_invalid")
        require(not any(key.startswith("DSTACK_OTEL_") and key.endswith("_ENABLED") for key in environment),
                "migration_raw_dstack_telemetry_enabled")
        require(not any(key in environment for key in ("DSTACK_SERVER_ADMIN_TOKEN", "DSTACK_DATABASE_URL",
            "DATABASE_URL", "POSTGRES_PASSWORD", "ADMIN_PASSWORD", "SIXNINE_DATABASE_URL")),
                "migration_secret_value_in_compose")
        if name != "caddy":
            require(not service.get("ports"), "migration_private_port_published")
        logs = service.get("logging", {}).get("options", {})
        require(logs.get("max-size") and logs.get("max-file"), "migration_unbounded_logs")
    for name in CPU_SHARED:
        service = services[name]
        require(service.get("network_mode") == "service:app" and not service.get("networks"),
                "migration_loopback_namespace_split")
    for name in ("app", "dispatch", "controller"):
        service = services[name]
        env = service["environment"]
        default = env.get("SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID")
        require(type(default) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", default)
            and default == services["app"]["environment"].get("SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"),
            "migration_default_profile_missing_or_split")
        require(env.get("SIXNINE_DATABASE_URL_FILE") == "/run/secrets/app_database_url" and
            {"app_database_url", "execution_profiles", "grafana_cloud"} <= secret_names(service),
            "migration_business_authority_split")
        require(any(volume.get("source") == "/srv/sixnine/platform-data" and volume.get("target") == "/data"
            and not volume.get("read_only") for volume in service.get("volumes", [])), "migration_assets_split")
        require(env.get("SIXNINE_TELEMETRY_CONFIG_FILE") == "/run/secrets/grafana_cloud", "migration_cloud_telemetry_missing")
    controller = services["controller"]
    targets = {volume["target"] for volume in controller.get("volumes", [])}
    require({"/dstack-runtime", "/dstack-sources"} <= targets and
        {"gpu_ssh_key", "dstack_runtime_config", "hatchet_broker", "hatchet_api_token"} <= secret_names(controller),
        "migration_runtime_mounts_missing")
    require("gpu_ssh_key" not in secret_names(services["app"]) and
        "dstack_database_url" not in secret_names(services["controller"]), "migration_credential_boundary_invalid")
    dstack = services["dstack"]
    env = dstack["environment"]
    require(env.get("DSTACK_SERVER_HOST") == "127.0.0.1" and env.get("DSTACK_SERVER_PORT") == "3000"
        and env.get("DSTACK_SERVER_LOG_LEVEL") in ("WARNING", "ERROR")
        and {"dstack_api_token", "dstack_database_url"} <= secret_names(dstack), "migration_dstack_auth_invalid")
    require("--log-level" in str(dstack.get("command")) and "--host" in str(dstack.get("command")),
            "migration_dstack_entrypoint_invalid")
    hatchet = services["hatchet"]["environment"]
    require(hatchet.get("SERVER_ALLOW_SIGNUP") == "false" and hatchet.get("SERVER_ALLOW_INVITES") == "false"
        and hatchet.get("SERVER_GRPC_BIND_ADDRESS") == "127.0.0.1"
        and hatchet.get("SERVER_MSGQUEUE_KIND") == "postgres"
        and {"administrator_password", "database_password"} <= secret_names(services["hatchet"]),
        "migration_hatchet_auth_invalid")
    for name in ("hatchet-config", "hatchet-postgres"):
        require(document.get("volumes", {}).get(name, {}).get("external") is True, "migration_blank_broker_volume")
    app_env = services["app"]["environment"]
    require((app_env.get("SIXNINE_GENERATION_ENABLED"), app_env.get("SIXNINE_EXECUTION_BACKEND"))
        in {("0", "disabled"), ("1", "wangp-worker")}, "migration_admission_switch_invalid")
    total = sum(memory_mib(service.get("mem_limit")) for service in services.values())
    require(type(host_memory_mib) is int and host_memory_mib >= 1024 and total <= host_memory_mib-512,
            "migration_host_memory_overcommitted")
    return {"state": "migration_compose_verified", "service_memory_cap_mib": total,
        "host_reserve_mib": host_memory_mib-total, "namespace": "app", "gpu_operations": 0}


def local_inputs(document):
    """Metadata only; never read/provider-log secret contents."""
    for secret in document.get("secrets", {}).values():
        path = Path(secret.get("file", ""))
        require(path.is_absolute() and not path.is_symlink(), "migration_secret_path_invalid")
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            (os.name == "nt" or not info.st_mode & 0o027), "migration_secret_permissions_invalid")
    for service in document["services"].values():
        for volume in service.get("volumes", []):
            if volume.get("type") == "bind":
                path = Path(volume["source"])
                require(path.is_absolute() and path.exists() and not path.is_symlink(), "migration_bind_missing")
                require(volume.get("bind", {}).get("create_host_path") is False, "migration_implicit_bind_creation")


def check(base, overlay, *, environment=None, runner=subprocess.run, host_memory_mib=3840,
          check_inputs=False, probe=False):
    # An explicit empty env file suppresses automatic project .env discovery.
    with tempfile.TemporaryDirectory(prefix="sixnine-compose-check-") as directory:
        empty = Path(directory) / "empty.env"
        empty.write_bytes(b"")
        command = ["docker", "compose", "--env-file", str(empty), "-f", str(base), "-f", str(overlay)]
        result = runner(command+["config", "--format", "json"], env=environment,
            capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, "migration_compose_render_failed")
        try:
            document = json.loads(result.stdout)
        except ValueError:
            raise ValueError("migration_compose_render_invalid") from None
        receipt = validate(document, host_memory_mib=host_memory_mib)
        if check_inputs:
            local_inputs(document)
        if probe:
            result = runner(command+["exec", "-T", "app", "python", "-c", PROBE], env=environment,
                capture_output=True, text=True, timeout=25)
            require(result.returncode == 0 and result.stdout.strip() == "migration_private_probe_passed",
                    "migration_private_probe_failed")
            receipt.update(private_dstack_authenticated=True, hatchet_health_ready=True)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "deploy/platform/compose.yaml")
    parser.add_argument("--overlay", type=Path, default=ROOT / "deploy/dstack/platform-overlay.yaml")
    parser.add_argument("--host-memory-mib", type=int, default=3840)
    parser.add_argument("--local-inputs", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = check(args.base, args.overlay, host_memory_mib=args.host_memory_mib,
            check_inputs=args.local_inputs, probe=args.probe)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"migration_[a-z_]+", str(error)) else "migration_deploy_check_failed"
        print(json.dumps({"state": "failed", "code": code})); return 1
    print(json.dumps(receipt, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
try:
    import deployment_target
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from deploy.platform import deployment_target

ROOT = Path(__file__).resolve().parents[1]
DSTACK_IMAGE = "dstackai/dstack:0.22.5@sha256:8dc2c8a5ee348789b1ed1621bc75b31c156adf31d849fded29508cfc21b279e4"
LEGACY_DSTACK_IMAGE = "dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8"
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


def validate_workos(document):
    """Hosted sign-in is opt-in and its credential never reaches GPU control."""
    services, secrets = document['services'], document.get('secrets', {})
    selected = services['app'].get('environment', {}).get('INKSEQ_WORKOS_CONFIG_FILE', '')
    require(selected in {'', '/run/secrets/workos'}, 'migration_workos_configuration_invalid')
    for name, service in services.items():
        require(name == 'app' or ('INKSEQ_WORKOS_CONFIG_FILE' not in service.get('environment', {})
                and 'workos' not in secret_names(service)), 'migration_workos_outside_app')
    configured = secrets.get('workos')
    if configured is None:
        require(not selected, 'migration_workos_configuration_invalid')
        return
    expected = '/run/sixnine-secrets/workos' if selected else secrets['execution_profiles']['file']
    require(configured.get('file') == expected and 'workos' in secret_names(services['app']),
            'migration_workos_configuration_invalid')


def validate_error_monitoring(document, *, target):
    """API errors and closed controller reclaim events use the owner project."""
    approved = "https://7339f473c737f7b493b03da92a2bb925@o4512306171019264.ingest.us.sentry.io/4512306334728192"
    for name, service in document['services'].items():
        env = service.get('environment', {})
        require(name in {'app', 'controller'} or 'INKSEQ_SENTRY_DSN' not in env,
                'migration_api_error_monitoring_outside_app')
        if name == 'controller':
            selected = env.get('INKSEQ_SENTRY_DSN', '')
            require(type(selected) is str and selected in {'', approved},
                    'migration_api_error_monitoring_destination_invalid')
            require(not selected or env.get('INKSEQ_DEPLOYMENT_TARGET') == target,
                    'migration_api_error_monitoring_target_invalid')
    env = document['services']['app'].get('environment', {})
    selected = env.get('INKSEQ_SENTRY_DSN', '')
    require(type(selected) is str and selected in {'', approved},
            'migration_api_error_monitoring_destination_invalid')
    require(not selected or env.get('INKSEQ_DEPLOYMENT_TARGET') == target,
            'migration_api_error_monitoring_target_invalid')


def validate_stripe(document, *, target='production'):
    """Only API and the existing DBOS consumer receive the target's Stripe mode."""
    services, secrets = document['services'], document.get('secrets', {})
    app_env = services['app'].get('environment', {})
    selected = app_env.get('INKSEQ_STRIPE_CONFIG_FILE', '')
    mode = app_env.get('INKSEQ_STRIPE_MODE', 'off')
    require(
        selected in {'', '/run/secrets/stripe'}
        and mode in {'off', 'test', 'live'}
        and (mode == 'off' or bool(selected)),
        'migration_stripe_configuration_invalid',
    )
    require(
        (mode != 'test' or target == 'staging')
        and (mode != 'live' or target == 'production'),
        'migration_stripe_mode_target_invalid',
    )
    identity = deployment_target.profile(target)
    for name, service in services.items():
        environment = service.get('environment', {})
        if name in {'app', 'controller'}:
            require(
                environment.get('INKSEQ_STRIPE_CONFIG_FILE', '') == selected
                and environment.get('INKSEQ_STRIPE_MODE', 'off') == mode,
                'migration_stripe_configuration_split',
            )
            if selected:
                aliases = set(
                    filter(
                        None, environment.get('PUBLIC_ORIGIN_ALIASES', '').split(',')
                    )
                )
                allowed_aliases = set(filter(None, identity['aliases'].split(',')))
                require(
                    environment.get('INKSEQ_DEPLOYMENT_TARGET') == target
                    and environment.get('PUBLIC_ORIGIN') == identity['origin']
                    and aliases <= allowed_aliases
                    and environment.get('INKSEQ_GENERATION_QUEUE', 'postgres')
                    == 'dbos',
                    'migration_stripe_target_invalid',
                )
        else:
            require(
                not {'INKSEQ_STRIPE_CONFIG_FILE', 'INKSEQ_STRIPE_MODE'}
                & set(environment)
                and 'stripe' not in secret_names(service),
                'migration_stripe_outside_business',
            )
    configured = secrets.get('stripe')
    if configured is None:
        require(not selected, 'migration_stripe_configuration_invalid')
        return  # Retained, unconfigured older releases stay compatible.
    expected = '/run/sixnine-secrets/stripe' if selected else secrets['execution_profiles']['file']
    require(configured.get('file') == expected
            and all('stripe' in secret_names(services[name]) for name in ('app', 'controller')),
            'migration_stripe_configuration_invalid')


def validate_storage(document, *, target='production'):
    """One provider identity for API/worker; credentials never enter env/queue."""
    identity = deployment_target.profile(target)
    fields = {"SIXNINE_STORAGE_PROVIDER", "SIXNINE_STORAGE_ENDPOINT", "SIXNINE_STORAGE_REGION",
        "SIXNINE_STORAGE_BUCKET", "SIXNINE_STORAGE_PROFILE"}
    services = document["services"]
    selected = {key: services["app"]["environment"].get(key, "") for key in fields}
    selected["SIXNINE_STORAGE_PROVIDER"] = selected["SIXNINE_STORAGE_PROVIDER"] or "local"
    for name in ("app", "controller", *(("dispatch",) if "dispatch" in services else ())):
        env = services[name]["environment"]
        require(not {key for key in env if key.startswith("SIXNINE_STORAGE_")} - fields - {"SIXNINE_STORAGE_CREDENTIAL_FILE"},
            "migration_storage_unexpected_environment")
        require(all((env.get(key, "") or ("local" if key.endswith("_PROVIDER") else "")) == value
            for key, value in selected.items()), "migration_storage_identity_split")
    if "dispatch" in services:
        require(not services["dispatch"]["environment"].get("SIXNINE_STORAGE_CREDENTIAL_FILE")
            and "r2_storage" not in secret_names(services["dispatch"]), "migration_storage_dispatch_credential")
    secrets = document.get("secrets", {})
    require("r2_storage" in secrets and "grafana_cloud" in secrets, "migration_storage_mount_missing")
    for name in ("app", "controller"):
        require(any((item == "r2_storage" or isinstance(item, dict) and item.get("source") == "r2_storage"
                and item.get("target", "r2_storage") in {"r2_storage", "/run/secrets/r2_storage"})
            for item in services[name].get("secrets", [])),
            "migration_storage_mount_missing")
    if selected["SIXNINE_STORAGE_PROVIDER"] == "local":
        require(not any(value for key, value in selected.items() if key != "SIXNINE_STORAGE_PROVIDER")
            and all(not services[name]["environment"].get("SIXNINE_STORAGE_CREDENTIAL_FILE")
                for name in ("app", "controller"))
            and secrets["r2_storage"].get("file") == secrets["grafana_cloud"].get("file"),
            "migration_storage_local_configuration_invalid")
        return
    require(selected["SIXNINE_STORAGE_PROVIDER"] == "r2"
        and selected["SIXNINE_STORAGE_PROFILE"] == identity['r2_profile']
        and (target != 'staging' or selected['SIXNINE_STORAGE_BUCKET'] == identity['r2_bucket'])
        and selected["SIXNINE_STORAGE_REGION"] == "auto"
        and re.fullmatch(r"https://[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com",
            selected["SIXNINE_STORAGE_ENDPOINT"])
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", selected["SIXNINE_STORAGE_BUCKET"])
        and all(services[name]["environment"].get("SIXNINE_STORAGE_CREDENTIAL_FILE") == "/run/secrets/r2_storage"
            for name in ("app", "controller"))
        and secrets["r2_storage"].get("file") == "/run/sixnine-secrets/r2_storage",
        "migration_storage_r2_configuration_invalid")


def validate(document, *, host_memory_mib=3840, dstack_image=DSTACK_IMAGE, target='production', resource_environment=None):
    identity = deployment_target.profile(target)
    resources = deployment_target.resources({**(resource_environment or {}), 'INKSEQ_DEPLOYMENT_TARGET': target})
    require(type(document) is dict and type(document.get("services")) is dict, "migration_compose_invalid")
    services = document["services"]
    modern = document.get("x-sixnine-dispatch-layout") == "postgres-notify-v1"
    require(document.get("x-sixnine-dispatch-layout") in (None, "postgres-notify-v1"), "migration_layout_invalid")
    shared = ("dstack", "controller") if modern else CPU_SHARED
    business = ("app", "controller") if modern else ("app", "dispatch", "controller")
    expected_services = {"app", "db", "db-init", "caddy", *shared} | (set() if modern else {"hatchet-db"})
    require(set(services) == expected_services, "migration_services_missing")
    if modern:
        require(not {"hatchet_broker", "hatchet_api_token", "database_password", "administrator_password"} & set(document.get("secrets", {}))
            and not {"hatchet-config", "hatchet-postgres"} & set(document.get("volumes", {})), "migration_retired_broker_mount")
    public = services["app"].get("environment", {})
    if "PUBLIC_ORIGIN" in public:
        require(public.get("PUBLIC_ORIGIN") == identity['origin']
            and public.get("PUBLIC_ORIGIN_ALIASES") == identity['aliases']
            and "SIXNINE_PUBLIC_ORIGIN" not in public
            and all(services[name].get("environment", {}).get("PUBLIC_ORIGIN") == identity['origin']
                and "SIXNINE_PUBLIC_ORIGIN" not in services[name].get("environment", {})
                and "PUBLIC_ORIGIN_ALIASES" not in services[name].get("environment", {})
                for name in business if name != "app"), "migration_public_origin_split")
        require(services["caddy"].get("environment") in
            ({"INKSEQ_LEGACY_REDIRECT": "0"}, {"INKSEQ_LEGACY_REDIRECT": "1"}), "migration_domain_redirect_phase_invalid")
        require(target != 'staging' or services['caddy']['environment']['INKSEQ_LEGACY_REDIRECT'] == '0',
            'migration_staging_redirect_forbidden')
        if target == 'staging':
            mounts = [item for item in services['caddy'].get('volumes', []) if item.get('target') == '/etc/caddy/Caddyfile']
            require(len(mounts) == 1 and mounts[0].get('read_only') is True
                and Path(mounts[0].get('source', '')).name == identity['caddy'], 'migration_staging_proxy_profile_invalid')
    else:
        require(target == 'production', 'migration_staging_origin_required')
        require(public.get("SIXNINE_PUBLIC_ORIGIN") == "https://www.sixnine.art"
            and "PUBLIC_ORIGIN_ALIASES" not in public
            and all(not {"PUBLIC_ORIGIN", "PUBLIC_ORIGIN_ALIASES"} & set(services[name].get("environment", {}))
                for name in business if name != "app"), "migration_public_origin_split")
        require(not services["caddy"].get("environment"), "migration_domain_redirect_phase_invalid")
    require(dstack_image in {DSTACK_IMAGE, LEGACY_DSTACK_IMAGE} or re.fullmatch(r"sixnine-dstack:[0-9a-f]{40}@sha256:[0-9a-f]{64}", dstack_image),
            "migration_dstack_image_binding_invalid")
    require(services["dstack"]["image"] == dstack_image and (modern or services["hatchet"]["image"] == HATCHET_IMAGE),
            "migration_infrastructure_image_changed")
    for name, service in services.items():
        require(service.get('cgroup_parent', '') == resources['cgroup_parent'], 'migration_cpu_cgroup_parent_invalid')
        require(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", service.get("image", "")), "migration_image_unpinned")
        require(service.get("pull_policy") == "never", "migration_implicit_image_pull")
        require(not any(service.get(key) for key in ("gpus", "devices", "privileged", "env_file")), "migration_unsafe_service")
        environment = service.get("environment", {})
        require(type(environment) is dict, "migration_environment_invalid")
        require(name == "caddy" or "INKSEQ_LEGACY_REDIRECT" not in environment,
                "migration_domain_flag_outside_proxy")
        require(not any(key.startswith("DSTACK_OTEL_") and key.endswith("_ENABLED") for key in environment),
                "migration_raw_dstack_telemetry_enabled")
        require(not any(key in environment for key in ("DSTACK_SERVER_ADMIN_TOKEN", "DSTACK_DATABASE_URL",
            "DATABASE_URL", "POSTGRES_PASSWORD", "ADMIN_PASSWORD", "SIXNINE_DATABASE_URL",
            "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_SESSION_TOKEN", "R2TOKEN", "R2_TOKEN",
            "WORKOS_API_KEY", "INKSEQ_WORKOS_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_API_KEY",
            "STRIPE_WEBHOOK_SECRET", "INKSEQ_STRIPE_SECRET_KEY", "INKSEQ_STRIPE_API_KEY", "INKSEQ_STRIPE_WEBHOOK_SECRET",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")),
                "migration_secret_value_in_compose")
        if name != "caddy":
            require(not service.get("ports"), "migration_private_port_published")
        logs = service.get("logging", {}).get("options", {})
        require(logs.get("max-size") and logs.get("max-file"), "migration_unbounded_logs")
    for name in shared:
        service = services[name]
        require(service.get("network_mode") == "service:app" and not service.get("networks"),
                "migration_loopback_namespace_split")
    for name in business:
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
    validate_storage(document, target=target)
    validate_workos(document)
    validate_error_monitoring(document, target=target)
    validate_stripe(document, target=target)
    deployment_target.inky_mounts(document, target=target)
    controller = services["controller"]
    targets = {volume["target"] for volume in controller.get("volumes", [])}
    require({"/dstack-runtime", "/dstack-sources"} <= targets and
        ({"gpu_ssh_key", "dstack_runtime_config"} | (set() if modern else {"hatchet_broker", "hatchet_api_token"})) <= secret_names(controller),
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
    if not modern:
        hatchet = services["hatchet"]["environment"]
        require(hatchet.get("SERVER_ALLOW_SIGNUP") == "false" and hatchet.get("SERVER_ALLOW_INVITES") == "false"
            and hatchet.get("SERVER_GRPC_BIND_ADDRESS") == "127.0.0.1"
            and hatchet.get("SERVER_MSGQUEUE_KIND") == "postgres"
            and {"administrator_password", "database_password"} <= secret_names(services["hatchet"]),
            "migration_hatchet_auth_invalid")
        for name in ("hatchet-config", "hatchet-postgres"):
            require(document.get("volumes", {}).get(name, {}).get("external") is True, "migration_blank_broker_volume")
    app_env = services["app"]["environment"]
    queue = app_env.get('INKSEQ_GENERATION_QUEUE', 'postgres')
    film = app_env.get('SIXNINE_RENDER_ENABLED', '0')
    require(queue in {'postgres','dbos'} and film in {'0','1'} and (film == '0' or queue == 'dbos'),
        'migration_workflow_settings_invalid')
    require(controller['environment'].get('INKSEQ_GENERATION_QUEUE', 'postgres') == queue
        and controller['environment'].get('SIXNINE_RENDER_ENABLED', '0') == film,
        'migration_workflow_settings_split')
    storyboard = app_env.get("SIXNINE_STORYBOARD_OWNERS", "")
    require(type(storyboard) is str and (not storyboard or
        len(storyboard.split(",")) == len(set(storyboard.split(",")))
        and set(storyboard.split(",")) <= {"superdan", "supervan"}),
        "migration_storyboard_reference_invalid")
    require(all("SIXNINE_STORYBOARD_OWNERS" not in services[name]["environment"]
        for name in shared), "migration_storyboard_worker_environment")
    require((app_env.get("SIXNINE_GENERATION_ENABLED"), app_env.get("SIXNINE_EXECUTION_BACKEND"))
        in {("0", "disabled"), ("1", "wangp-worker")}, "migration_admission_switch_invalid")
    require(all(controller["environment"].get(key) == app_env.get(key)
        for key in ("SIXNINE_GENERATION_ENABLED", "SIXNINE_EXECUTION_BACKEND")),
        "migration_controller_execution_settings_split")
    total = sum(memory_mib(service.get("mem_limit")) for service in services.values())
    if resources['name'] == deployment_target.COMPACT_RESOURCES:
        require(set(services) == expected_services,
                'migration_compact_service_set_invalid')
        require({name: memory_mib(service.get('mem_limit')) for name, service in services.items()}
                == {name: cap for name, cap in deployment_target.COMPACT_SERVICE_MIB.items() if name in expected_services}, 'migration_compact_service_caps_changed')
        require(type(host_memory_mib) is int and host_memory_mib >= resources['minimum_host_mib']
                and resources['resident_mib'] <= host_memory_mib-512
                and total <= resources['resident_mib']+resources['swap_mib'],
                'migration_host_memory_overcommitted')
    else:
        require(type(host_memory_mib) is int and host_memory_mib >= 1024 and total <= host_memory_mib-512,
                "migration_host_memory_overcommitted")
    return {"state": "migration_compose_verified", "service_memory_cap_mib": total,
        "host_reserve_mib": host_memory_mib-(resources['resident_mib'] or total),
        **({'resource_profile': resources['name'], 'parent_resident_mib': resources['resident_mib'],
            'parent_swap_mib': resources['swap_mib']} if resources['swap_mib'] else {}),
        "namespace": "app", "gpu_operations": 0}


def local_inputs(document):
    """Metadata only; never read/provider-log secret contents."""
    for secret in document.get("secrets", {}).values():
        path = Path(secret.get("file", ""))
        require(path.is_absolute() and not path.is_symlink(), "migration_secret_path_invalid")
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            (os.name == "nt" or not info.st_mode & 0o027), "migration_secret_permissions_invalid")
    if document["services"]["app"]["environment"].get("SIXNINE_STORAGE_PROVIDER") == "r2":
        info = Path(document["secrets"]["r2_storage"]["file"]).stat()
        require(os.name == "nt" or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 10001, 0o440),
            "migration_storage_runtime_permissions_invalid")
    for service in document["services"].values():
        for volume in service.get("volumes", []):
            if volume.get("type") == "bind":
                path = Path(volume["source"])
                require(path.is_absolute() and path.exists() and not path.is_symlink(), "migration_bind_missing")
                require(volume.get("bind", {}).get("create_host_path") is False, "migration_implicit_bind_creation")


def check(base, overlay, *, environment=None, runner=subprocess.run, host_memory_mib=3840,
          check_inputs=False, probe=False):
    effective_environment = environment if environment is not None else os.environ
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
        receipt = validate(document, host_memory_mib=host_memory_mib,
            dstack_image=effective_environment.get("SIXNINE_DSTACK_SERVER_IMAGE", DSTACK_IMAGE),
            target=deployment_target.environment(effective_environment)['name'], resource_environment=effective_environment)
        if "PUBLIC_ORIGIN" in document["services"]["app"]["environment"]:
            require(document["services"]["caddy"]["environment"]["INKSEQ_LEGACY_REDIRECT"]
                == effective_environment.get("INKSEQ_LEGACY_REDIRECT", "0"),
                "migration_domain_redirect_phase_unbound")
        if check_inputs:
            local_inputs(document)
            if deployment_target.resources(effective_environment)['name'] == deployment_target.COMPACT_RESOURCES:
                sealed = deployment_target.host()
                require(deployment_target.resource_environment(sealed) == {
                    key: effective_environment.get(key) for key in ('INKSEQ_RESOURCE_PROFILE', 'INKSEQ_CPU_CGROUP_PARENT')},
                    'migration_compact_sealed_profile_mismatch')
                metadata = deployment_target.resource_metadata()
                require(metadata['memory_bytes']//1024**2 == host_memory_mib,
                        'migration_compact_observed_host_memory_changed')
                deployment_target.validate_resource_metadata(effective_environment, metadata,
                    memory_bytes=metadata['memory_bytes'])
                receipt['compact_host_resources_verified'] = True
        if probe:
            probe_code = PROBE if "hatchet" in document["services"] else PROBE.replace("    assert request('http://127.0.0.1:8888/api/ready',parse_json=False)[0]==200\n", "")
            result = runner(command+["exec", "-T", "app", "python", "-c", probe_code], env=environment,
                capture_output=True, text=True, timeout=25)
            require(result.returncode == 0 and result.stdout.strip() == "migration_private_probe_passed",
                    "migration_private_probe_failed")
            receipt.update(private_dstack_authenticated=True)
            if "hatchet" in document["services"]:
                receipt["hatchet_health_ready"] = True
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

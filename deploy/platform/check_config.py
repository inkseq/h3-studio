"""Check a rendered Compose JSON from stdin without displaying configuration.

Usage: docker compose ... config --format json | python check_config.py
No network requests or service starts. Secret source files are not read.
"""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import re
import sys
try:
    import deployment_target
except ImportError:
    from deploy.platform import deployment_target


class ConfigurationError(ValueError):
    pass


SENTRY_DSN = "https://7339f473c737f7b493b03da92a2bb925@o4512306171019264.ingest.us.sentry.io/4512306334728192"


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def validate(config, *, deployment_directory=None, compose_version=None, target='production', resource_environment=None):
    selected = deployment_target.profile(target)
    resources = deployment_target.resources({**(resource_environment or {}), 'INKSEQ_DEPLOYMENT_TARGET': target})
    require(compose_version is None or isinstance(compose_version, str), "Compose version must be trusted binary output")
    deployment = Path(deployment_directory) if deployment_directory is not None else Path(__file__).resolve().parent
    require(deployment.is_absolute(), "Deployment directory must be an explicit absolute trusted path")
    require(config.get("name") == "sixnine-platform", "Compose project identity differs")
    services = config.get("services", {})
    require(set(services) == {"app", "db", "db-init", "caddy"}, "Unexpected service set")
    allowed_env = {
        "app": {"SIXNINE_DATA", "SIXNINE_DATABASE_URL_FILE", "SIXNINE_PUBLIC_ORIGIN", "PUBLIC_ORIGIN", "PUBLIC_ORIGIN_ALIASES", "SIXNINE_AUTH_MODE", "INKSEQ_BROWSER_SESSION_SECONDS", "INKSEQ_DEPLOYMENT_TARGET", "INKSEQ_SENTRY_DSN",
                "SIXNINE_GENERATION_ENABLED", "SIXNINE_RENDER_ENABLED", "SIXNINE_EXECUTION_BACKEND", "SIXNINE_CLOUD_CREATION_ENABLED",
                "SIXNINE_STORAGE_PROVIDER", "SIXNINE_FRONTEND_DIR", "SIXNINE_FRONTEND_RELEASE_DIR", "SIXNINE_TITLE_CONFIG_FILE"},
        "db": {"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD_FILE", "POSTGRES_INITDB_ARGS",
               "POSTGRES_HOST_AUTH_METHOD", "PGDATA"}, "db-init": set(), "caddy": {"INKSEQ_LEGACY_REDIRECT"}}
    allowed_caps = {"app": set(), "db-init": set(), "caddy": {"NET_BIND_SERVICE"},
                    "db": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}}
    common_fields = {"image", "pull_policy", "restart", "read_only", "cap_drop", "cap_add", "security_opt",
                     "pids_limit", "mem_limit", "environment", "command", "entrypoint", "secrets", "volumes",
                     "tmpfs", "networks", "healthcheck", "logging", "depends_on", "user", "cgroup_parent"}
    extra_fields = {"app": {"cpus", "init", "stop_grace_period"}, "db": {"shm_size", "stop_grace_period"},
                    "db-init": set(), "caddy": {"ports"}}
    # Initial two-account control plane on a 4-GiB instance. Rendering/GPU
    # workers stay disabled and must receive a separately tested resource budget.
    bounds = {"app": (2*1024**3, 256), "db": (512*1024**2, 256),
              "db-init": (256*1024**2, 32), "caddy": (128*1024**2, 128)}
    for name, service in services.items():
        require(service.get('cgroup_parent', '') == resources['cgroup_parent'], 'Unreviewed CPU cgroup parent')
        require(set(service) <= common_fields | extra_fields[name], "Unreviewed container option is present")
        image = service.get("image", "")
        require(bool(re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image)
                     or (name in {"app", "db-init"} and re.fullmatch(r"sixnine-platform:[0-9a-f]{40}", image))),
                "Images must be reviewed immutable references")
        require(service.get("pull_policy") == "never" and not service.get("build"), "Release must reuse tested local images")
        require(service.get("read_only") is True and service.get("privileged", False) is False, "Container filesystem/privilege policy differs")
        require(service.get("cap_drop") == ["ALL"], "Capabilities must be explicitly dropped")
        require(set(service.get("cap_add", [])) == allowed_caps[name], "Added capabilities exceed the reviewed service needs")
        require(service.get("security_opt") == ["no-new-privileges:true"], "Security options differ from the reviewed policy")
        require(service.get("entrypoint") is None, "Service entrypoints must come from the reviewed image")
        expected_memory, expected_pids = bounds[name]
        require(str(service.get("mem_limit")) == str(expected_memory) and service.get("pids_limit") == expected_pids,
                "Memory/process resource bounds differ")
        require(service.get("logging") == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
                "Logs must use the bounded local configuration")
        require(service.get("restart") == ("no" if name == "db-init" else "unless-stopped"), "Restart policy differs")
        require(not service.get("network_mode") and not service.get("devices") and not service.get("gpus"), "Host/GPU access is forbidden")
        require(not service.get("env_file"), "Runtime environment files must not be mounted into services")
        require(set(service.get("environment", {})) <= allowed_env[name], "Unreviewed environment field is present")
        for field in service.get("environment", {}):
            require(field not in {"POSTGRES_PASSWORD", "PGPASSWORD", "SIXNINE_DATABASE_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"},
                    "A credential value was configured in the container environment")
        require(name == "caddy" or not service.get("ports"), "Only Caddy may publish ports")
    app, db, init, caddy = (services[name] for name in ("app", "db", "db-init", "caddy"))
    require(app["image"] == init["image"], "Bootstrap and app must use the same tested image")
    require(app.get("user") == "10001:10001", "App must run as its non-root identity")
    require(init.get("user") == "0:0" and db.get("user") is None and caddy.get("user") is None, "Service user policy differs")
    require(app.get("cpus") == 2 and app.get("init") is True, "App CPU/process supervision differs")
    require(str(db.get("shm_size")) == str(256*1024**2), "Database shared-memory bound differs")
    required = {"SIXNINE_GENERATION_ENABLED": "0", "SIXNINE_RENDER_ENABLED": "0", "SIXNINE_EXECUTION_BACKEND": "disabled",
                "SIXNINE_CLOUD_CREATION_ENABLED": "0", "SIXNINE_AUTH_MODE": "password",
                "SIXNINE_STORAGE_PROVIDER": "local",
                "SIXNINE_DATABASE_URL_FILE": "/run/secrets/app_database_url", "SIXNINE_DATA": "/data",
                "SIXNINE_FRONTEND_DIR": "/app/yingxu-dist"}
    # Accept retained old release bytes or this exact dual-origin successor,
    # never an arbitrary public host, alias list or mixed legacy/new setting.
    if "PUBLIC_ORIGIN" in app.get("environment", {}):
        required.update(PUBLIC_ORIGIN=selected['origin'], PUBLIC_ORIGIN_ALIASES=selected['aliases'])
        require(caddy.get("environment") in ({"INKSEQ_LEGACY_REDIRECT": "0"}, {"INKSEQ_LEGACY_REDIRECT": "1"}),
                "Domain redirect phase must be the explicit reviewed value")
        require(target != 'staging' or caddy['environment']['INKSEQ_LEGACY_REDIRECT'] == '0',
                'Staging cannot enable production domain redirects')
    else:
        require(target == 'production', 'Staging requires its explicit public origin')
        required["SIXNINE_PUBLIC_ORIGIN"] = "https://www.sixnine.art"
        require(not caddy.get("environment"), "Legacy proxy must not inherit new domain settings")
    # Only the exact legacy baseline or the complete reviewed frontend pair is
    # accepted; this preserves rollback without permitting arbitrary mounts.
    independent_frontend = "SIXNINE_FRONTEND_RELEASE_DIR" in app.get("environment", {})
    if independent_frontend:
        required["SIXNINE_FRONTEND_RELEASE_DIR"] = "/frontend"
    titles = "SIXNINE_TITLE_CONFIG_FILE" in app.get("environment", {})
    if titles:
        required["SIXNINE_TITLE_CONFIG_FILE"] = "/run/secrets/google_titles"
    # Retained rollback configs may omit this additive, non-secret setting.
    duration = app.get("environment", {}).get("INKSEQ_BROWSER_SESSION_SECONDS")
    if duration is not None:
        require(isinstance(duration, str) and re.fullmatch(r"[0-9]{1,8}", duration)
                and 1 <= int(duration) <= 365*86400, "Invalid browser session duration")
        required["INKSEQ_BROWSER_SESSION_SECONDS"] = duration
    if "INKSEQ_DEPLOYMENT_TARGET" in app.get("environment", {}):
        required["INKSEQ_DEPLOYMENT_TARGET"] = target
    if "INKSEQ_SENTRY_DSN" in app.get("environment", {}):
        dsn = app["environment"]["INKSEQ_SENTRY_DSN"]
        require(isinstance(dsn, str) and dsn in {"", SENTRY_DSN}, "API error monitoring destination differs")
        require(not dsn or app["environment"].get("INKSEQ_DEPLOYMENT_TARGET") == target,
                "API error monitoring requires an explicit deployment target")
        required["INKSEQ_SENTRY_DSN"] = dsn
    require(app.get("environment") == required, "App production safety settings differ")
    networks = config.get("networks", {})
    require(set(networks) == {"web", "database", "edge"}, "Unexpected network definition")
    for name, value in networks.items():
        require(set(value) <= {"name", "internal", "ipam"} and value.get("name") == "sixnine-platform_"+name,
                "Network driver/external configuration differs")
    require(networks.get("web", {}).get("internal") is True and networks.get("database", {}).get("internal") is True,
            "App/database networks must be private")
    require(set(app.get("networks", {})) == ({"web", "database", "edge"} if titles else {"web", "database"}),
            "App egress must match the reviewed Google title credential configuration")
    require(set(db.get("networks", {})) == {"database"} and set(init.get("networks", {})) == {"database"},
            "DB/bootstrap must only reach the private database network")
    require(set(caddy.get("networks", {})) == {"web", "edge"}, "Proxy network separation differs")
    require(all(not value for value in app.get("networks", {}).values())
            and all(not value for value in db.get("networks", {}).values())
            and all(not value for value in init.get("networks", {}).values()), "Backend network attachments differ")
    require(caddy["networks"] == {"web": {"ipv4_address": "172.29.69.2"}, "edge": {}}, "Proxy attachment options differ")
    require(caddy["networks"]["web"].get("ipv4_address") == "172.29.69.2", "Proxy address differs from trusted identity")
    require(networks["web"].get("ipam", {}).get("config") == [{"subnet": "172.29.69.0/28", "ip_range": "172.29.69.8/29"}],
            "Dynamic address allocation must exclude the trusted proxy address")
    require(networks["web"].get("ipam") == {"config": [{"subnet": "172.29.69.0/28", "ip_range": "172.29.69.8/29"}]}
            and not networks["database"].get("ipam") and not networks["edge"].get("ipam"),
            "Unreviewed IPAM driver/options are present")
    command = app.get("command", [])
    require(command == ["python", "-m", "uvicorn", "platform_app:app", "--host", "0.0.0.0", "--port", "8845",
            "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "172.29.69.2"],
            "App process/proxy trust differs; ingress limits require a single Uvicorn worker")
    require(init.get("command") == ["python", "/bootstrap/init_database.py"] and caddy.get("command") is None,
            "Bootstrap/proxy command differs")
    require(not init.get("healthcheck") and not caddy.get("healthcheck"), "Unexpected container health command")
    require(db.get("environment") == {"POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres",
            "POSTGRES_PASSWORD_FILE": "/run/secrets/db_admin_password", "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
            "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --auth-local=peer",
            "PGDATA": "/var/lib/postgresql/data/pgdata"}, "PostgreSQL authentication/data policy differs")
    expected_db_command = ["postgres"]
    for setting in ("password_encryption=scram-sha-256", "log_statement=none", "log_min_error_statement=panic",
                    "log_parameter_max_length=0", "log_parameter_max_length_on_error=0", "max_connections=64"):
        expected_db_command.extend(["-c", setting])
    require(db.get("command") == expected_db_command, "PostgreSQL security/logging/connection arguments differ")
    def secret_names(service):
        return {item["source"] if isinstance(item, dict) else item for item in service.get("secrets", [])}
    require(secret_names(app) == ({"app_database_url", "google_titles"} if titles else {"app_database_url"}) and secret_names(db) == {"db_admin_password"}
            and secret_names(init) == {"db_admin_password", "app_database_url"} and not secret_names(caddy),
            "Secret access is broader than required")
    require(set(config.get("secrets", {})) == ({"db_admin_password", "app_database_url", "google_titles"} if titles else {"db_admin_password", "app_database_url"}), "Unexpected secret references")
    for name, item in config.get("secrets", {}).items():
        require(set(item) == {"name", "file"} and item.get("name") == "sixnine-platform_"+name
                and item.get("file") == "/run/sixnine-secrets/" + name,
                "Production secret sources must be explicit protected /run files")
    for service in (app, db, init):
        for item in service.get("secrets", []):
            require(isinstance(item, dict) and set(item) == {"source", "target"}
                    and item["target"] == "/run/secrets/"+item["source"], "Secret mount target/options differ")
    healthy = {"condition": "service_healthy", "required": True}
    require(app.get("depends_on") == {"db": healthy, "db-init": {"condition": "service_completed_successfully", "required": True}}
            and init.get("depends_on") == {"db": healthy} and caddy.get("depends_on") == {"app": healthy}
            and not db.get("depends_on"), "Service readiness dependency policy differs")
    app_health = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and not h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='disabled'"
    require(app.get("healthcheck") == {"test": ["CMD", "python", "-c", app_health], "interval": "15s", "timeout": "6s",
                                       "retries": 4, "start_period": "30s"}, "App health command/timing differs")
    require(db.get("healthcheck") == {"test": ["CMD", "pg_isready", "-U", "postgres", "-d", "postgres"],
                                      "interval": "5s", "timeout": "3s", "retries": 20, "start_period": "30s"},
            "Database health command/timing differs")
    def bind(source, target, *, readonly=False):
        value = {"type": "bind", "source": str(source), "target": target, "bind": {"create_host_path": False}}
        if readonly:
            value["read_only"] = True
        return value
    for service_name, expected in {
        "app": [bind("/srv/sixnine/platform-data", "/data"), bind("/srv/sixnine/upload-spool", "/tmp")]
               + ([bind("/srv/sixnine/frontend", "/frontend", readonly=True)] if independent_frontend else []),
        "db": [bind("/srv/sixnine/postgres", "/var/lib/postgresql/data")],
        "db-init": [bind(deployment / "init_database.py", "/bootstrap/init_database.py", readonly=True)],
        "caddy": [bind(deployment / selected['caddy'], "/etc/caddy/Caddyfile", readonly=True),
                  {"type": "volume", "source": "caddy_data", "target": "/data", "volume": {}},
                  {"type": "volume", "source": "caddy_config", "target": "/config", "volume": {}}],
    }.items():
        mounts = services[service_name].get("volumes", [])
        require(isinstance(mounts, list), "Mounts must be a rendered list")
        normalized = []
        for item in mounts:
            require(isinstance(item, dict), "Mount must be a rendered object")
            item = dict(item)
            if item.get("type") == "bind":
                options = item.get("bind")
                # Compose 2.38.2 / compose-go 2.7.1 uses bool+omitempty:
                # explicit false serializes as bind:{}. New Compose uses an
                # OptOut type where omission can mean TRUE. The version must
                # come from the trusted installed binary, never the bundle.
                if compose_version in {"2.38.2", "v2.38.2"} and options == {}:
                    options = {"create_host_path": False}
                require(isinstance(options, dict) and options.get("create_host_path") is False,
                        "Bind must explicitly disable host path creation for this Compose version")
                item["bind"] = options
            normalized.append(item)
        require(normalized == expected, "Mount set/source/options differ from the reviewed trusted deployment")
    require(config.get("volumes") == {name: {"name": "sixnine-platform_"+name} for name in ("caddy_data", "caddy_config")},
            "Named volumes must not use external or driver-configured host sources")
    ports = caddy.get("ports", [])
    require(ports == [{"mode": "ingress", "target": number, "published": str(number), "protocol": protocol}
                      for number, protocol in ((80, "tcp"), (443, "tcp"), (443, "udp"))],
            "Proxy must only publish the reviewed HTTP/HTTPS ports")
    for name, expected in {"app": [], "db": ["/tmp:size=67108864,mode=1777", "/var/run/postgresql:size=16777216,mode=3775"],
                           "db-init": ["/tmp:size=16777216,mode=1777"], "caddy": ["/tmp:size=16777216,mode=1777"]}.items():
        require(services[name].get("tmpfs", []) == expected, "Temporary filesystem mounts/bounds differ")
    if resources['name'] == deployment_target.COMPACT_RESOURCES:
        require(sum(int(service['mem_limit']) for service in services.values())
                <= (resources['resident_mib']+resources['swap_mib'])*1024**2,
                'Staging base services exceed the bounded parent RAM and swap')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-version", help="Version obtained from the trusted installed Compose binary; never from bundle metadata")
    parser.add_argument('--target', choices=('production', 'staging'), default='production')
    args = parser.parse_args(argv)
    try:
        validate(json.load(sys.stdin), compose_version=args.compose_version, target=args.target,
                 resource_environment=os.environ)
        print("Production configuration policy passed; secret contents and online readiness were not checked")
        return 0
    except Exception:
        print("Production configuration policy failed; no configuration or secret values were printed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

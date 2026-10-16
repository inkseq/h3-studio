#!/usr/bin/python3
"""Hydrate CPU migration secrets from one protected managed secret.

Imports are inert. Provider keys stay on the CPU host. No configuration value
is accepted as an argument, put in the process environment or printed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

REGION = "ap-southeast-1"
SECRET = "/sixnine/platform/migration"
ROOT = Path("/run/sixnine-secrets")
LOCK = Path("/opt/sixnine-release/migration-secret-hydration.lock")
HOST_ID = Path("/var/lib/cloud/data/instance-id")
INTENT = Path("/var/lib/sixnine/migration-secret-initialization/intent.json")
ENDPOINTS = {"vastai": "https://console.vast.ai/api", "runpod": "https://api.runpod.io/graphql"}
INFRASTRUCTURE = frozenset({"dstack_admin_token", "dstack_database_url",
    "hatchet_database_password", "hatchet_administrator_password"})
POSTGRES_INFRASTRUCTURE = frozenset({"dstack_admin_token", "dstack_database_url"})
MAX_SECRET = 65536
try:
    from . import deployment_target
except ImportError:
    import deployment_target


def client():
    """Use the instance role and exact AWS endpoint; never accept overrides."""
    import boto3
    from botocore.config import Config
    return boto3.client("secretsmanager", region_name=REGION,
        endpoint_url="https://secretsmanager.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=5, read_timeout=20,
            retries={"mode": "standard", "max_attempts": 3}))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("migration_secret_duplicate_field")
        result[key] = value
    return result


def _opaque(value, minimum=32, maximum=4096):
    return (isinstance(value, str) and minimum <= len(value) <= maximum
        and all(33 <= ord(c) <= 126 for c in value))


def validate(payload, *, allow_uninitialized=False, target='production'):
    """Closed schema, exact endpoints and existing safe Cloud exporter contract."""
    if (type(payload) is not dict or not {"schema_version", "providers", "grafana_cloud"} <= set(payload)
            or set(payload) - {"schema_version", "providers", "grafana_cloud", "grafana_stage_query", "r2_storage", "infrastructure", "hatchet_api_token"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] not in (1, 2)
            or payload["schema_version"] == 2 and "hatchet_api_token" in payload):
        raise ValueError("migration_secret_structure_invalid")
    providers = payload["providers"]
    if type(providers) is not dict or not providers or set(providers) - set(ENDPOINTS):
        raise ValueError("migration_provider_invalid")
    selected = deployment_target.profile(target)
    if target == 'staging' and (set(providers) != {'vastai'}
            or type(providers['vastai']) is not dict
            or providers['vastai'].get('profile_id') != selected['vast_profile']):
        raise ValueError('staging_provider_identity_invalid')
    for name, config in providers.items():
        if (type(config) is not dict or set(config) != {"profile_id", "base_url", "api_key"}
                or config["base_url"] != ENDPOINTS[name] or not _opaque(config["api_key"], 16)
                or not isinstance(config["profile_id"], str)
                or not re.fullmatch(re.escape(name)+r"--[a-z0-9][a-z0-9-]{0,100}", config["profile_id"])):
            raise ValueError("migration_provider_endpoint_or_identity_invalid")
    cloud = payload["grafana_cloud"]
    expected = {"schema_version", "enabled", "endpoint", "authorization", "service_name", "service_version", "environment"}
    if (type(cloud) is not dict or set(cloud) != expected or type(cloud["schema_version"]) is not int
            or cloud["schema_version"] != 1 or type(cloud["enabled"]) is not bool):
        raise ValueError("migration_cloud_configuration_invalid")
    # Keep the host helper standalone. Mirror the closed exporter destination
    # and Basic-auth format, with parity tests against CloudTelemetryConfig.
    if cloud["enabled"]:
        try:
            endpoint = urlsplit(cloud["endpoint"])
            auth = cloud["authorization"]
            valid = (endpoint.scheme == "https" and endpoint.port in (None, 443)
                and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment
                and endpoint.path.rstrip("/") == "/otlp" and endpoint.hostname is not None
                and re.fullmatch(r"otlp-gateway-[a-z0-9-]+\.grafana\.net", endpoint.hostname)
                and isinstance(auth, str) and auth.startswith("Basic ") and len(auth) <= 8192
                and not any(c in auth for c in "\r\n\x00")
                and re.fullmatch(rb"[0-9]+:[!-~]+", base64.b64decode(auth[6:], validate=True))
                and cloud["service_name"] in {"sixnine-api", "sixnine-worker", "sixnine-controller", "sixnine-acceptance"}
                and (cloud["service_version"] == "unknown" or re.fullmatch(r"[0-9a-f]{12,40}", cloud["service_version"]))
                and cloud["environment"] in {"local", "staging", "production"})
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("migration_cloud_configuration_invalid")
    if "grafana_stage_query" in payload:
        _validate_stage_query(payload["grafana_stage_query"])
    if "r2_storage" in payload:
        _validate_r2_storage(payload["r2_storage"], target=target)
    if target == 'staging' and (cloud['enabled'] or cloud['endpoint'] or cloud['authorization']
            or cloud['environment'] != 'staging'
            or payload.get('grafana_stage_query', {}).get('enabled')
            or payload.get('grafana_stage_query', {}).get('endpoint')
            or payload.get('grafana_stage_query', {}).get('authorization')
            or 'r2_storage' not in payload):
        raise ValueError('staging_isolated_optional_credentials_required')
    if "infrastructure" not in payload:
        if not allow_uninitialized or "hatchet_api_token" in payload or payload["schema_version"] == 2:
            raise ValueError("migration_infrastructure_recovery_required")
        return
    infrastructure = payload["infrastructure"]
    _validate_infrastructure(infrastructure, schema_version=payload["schema_version"])
    if "hatchet_api_token" in payload:
        token = payload["hatchet_api_token"]
        if not _opaque(token, 32, 8192) or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ValueError("migration_hatchet_token_invalid")


def _validate_stage_query(value):
    # Standalone host helper; parity is checked against the detail reader.
    if (type(value) is not dict
            or set(value) != {"schema_version", "enabled", "endpoint", "authorization", "scope"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["enabled"]) is not bool):
        raise ValueError("migration_stage_query_configuration_invalid")
    if not value["enabled"]:
        return
    try:
        endpoint = urlsplit(value["endpoint"])
        auth = value["authorization"]
        valid = (endpoint.scheme == "https" and endpoint.port in (None, 443)
            and endpoint.hostname is not None
            and re.fullmatch(r"logs-prod-[a-z0-9-]+\.grafana\.net", endpoint.hostname)
            and endpoint.path in ("", "/") and not endpoint.username and not endpoint.password
            and not endpoint.query and not endpoint.fragment and value["scope"] == "logs:read"
            and isinstance(auth, str) and auth.startswith("Basic ") and len(auth) <= 8192
            and not any(c in auth for c in "\r\n\x00")
            and re.fullmatch(rb"[0-9]+:[!-~]+", base64.b64decode(auth[6:], validate=True)))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("migration_stage_query_configuration_invalid")


def _validate_r2_storage(value, *, target='production'):
    # Standalone host helper: mirror the protected runtime reader's closed
    # identity. Secrets stay in the managed payload/file, never Compose/env.
    required = {"schema_version", "service", "profile", "endpoint_url", "region",
        "bucket", "access_key_id", "secret_access_key"}
    if (type(value) is not dict or not required <= set(value)
            or set(value) - required - {"session_token"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["service"] != "cloudflare-r2" or value["profile"] != deployment_target.profile(target)['r2_profile']
            or target == 'staging' and value.get('bucket') != deployment_target.profile(target)['r2_bucket']
            or value["region"] != "auto"
            or not isinstance(value["endpoint_url"], str)
            or not re.fullmatch(r"https://[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com", value["endpoint_url"])
            or not isinstance(value["bucket"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", value["bucket"])):
        raise ValueError("migration_r2_storage_configuration_invalid")
    for field in ("access_key_id", "secret_access_key", "session_token"):
        if field in value and (not isinstance(value[field], str) or not 1 <= len(value[field]) <= 8192
                or any(ord(c) < 33 or ord(c) == 127 for c in value[field])):
            raise ValueError("migration_r2_storage_configuration_invalid")
    if len((json.dumps(value, separators=(",", ":"), sort_keys=True)+"\n").encode("utf-8")) > 16384:
        raise ValueError("migration_r2_storage_configuration_invalid")


def _validate_infrastructure(infrastructure, *, schema_version=1):
    fields = POSTGRES_INFRASTRUCTURE if schema_version == 2 else INFRASTRUCTURE
    if type(infrastructure) is not dict or set(infrastructure) != fields:
        raise ValueError("migration_infrastructure_recovery_required")
    if any(not _opaque(value) for value in infrastructure.values()):
        raise ValueError("migration_infrastructure_invalid")
    for key in ("hatchet_database_password", "hatchet_administrator_password"):
        if key in fields and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", infrastructure[key]):
            raise ValueError("migration_infrastructure_invalid")
    parsed = urlsplit(infrastructure["dstack_database_url"])
    if ((parsed.scheme, parsed.hostname, parsed.port, parsed.username, parsed.path)
            != ("postgresql+asyncpg", "db", 5432, "dstack_control", "/sixnine_dstack")
            or not parsed.password or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", parsed.password)
            or parsed.query or parsed.fragment):
        raise ValueError("migration_infrastructure_database_invalid")


def _managed(api, *, version_id=None, target='production'):
    try:
        name = deployment_target.profile(target)['secret_root'] + '/migration'
        response = api.get_secret_value(SecretId=name, **({"VersionId": version_id} if version_id else {}))
    except api.exceptions.ResourceNotFoundException:
        if version_id:
            return None, None
        # A missing or unversioned secret has no provider identity. The release
        # owner must seed it securely; absence never permits inventing keys.
        raise RuntimeError("migration_managed_secret_required") from None
    raw = response.get("SecretString")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SECRET:
        raise ValueError("migration_secret_structure_invalid")
    return json.loads(raw, object_pairs_hook=_unique), response.get("VersionId")


def _ancestors(path):
    for directory in reversed(path.parents):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
            raise RuntimeError("migration_protected_parent_required")


def _directory(path, *, create=False):
    _ancestors(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        path.mkdir(mode=0o700)
        info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("migration_protected_directory_required")
    return True


def _protected_json(path):
    if not path.is_absolute():
        raise ValueError("migration_bootstrap_checks_invalid")
    _ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_uid, info.st_gid) != (0, 0)
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            raise ValueError("migration_bootstrap_checks_invalid")
        return json.loads(os.read(fd, 16385), object_pairs_hook=_unique)
    finally:
        os.close(fd)


def check_fresh_bootstrap(path, *, runner=subprocess.run, now=None):
    """Require fresh root DB evidence AND independently inspect host state.

    The release owner checks the dedicated PostgreSQL catalog before writing
    this protected nonsecret receipt. This helper has no business DB admin key.
    It also checks Docker successfully, never interpreting failed/empty output
    from an unsuccessful command as proof that volumes are absent.
    """
    value = _protected_json(Path(path))
    expected = {"schema_version", "hatchet_postgres_volume", "hatchet_config_volume",
        "dstack_state_dir", "dstack_runtime_dir", "infrastructure_database_absent", "checked_at", "host_id"}
    if (type(value) is not dict or set(value) != expected or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["infrastructure_database_absent"] is not True
            or not isinstance(value["host_id"], str)
            or not re.fullmatch(r"i-[0-9a-f]{8,17}", value["host_id"])
            or HOST_ID.read_text().strip() != value["host_id"]):
        raise ValueError("migration_bootstrap_checks_invalid")
    try:
        checked = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        age = (current - checked).total_seconds()
        if checked.utcoffset() is None or not 0 <= age <= 300:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValueError("migration_bootstrap_checks_expired") from None
    names = [value["hatchet_postgres_volume"], value["hatchet_config_volume"]]
    if (len(set(names)) != 2 or any(not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name) for name in names)):
        raise ValueError("migration_bootstrap_checks_invalid")
    result = runner(["docker", "volume", "ls", "--format", "{{.Name}}"],
        capture_output=True, text=True, check=False, timeout=20)
    if result.returncode != 0 or not isinstance(result.stdout, str):
        raise RuntimeError("migration_engine_state_unconfirmed")
    if set(names) & set(result.stdout.splitlines()):
        raise RuntimeError("migration_existing_engine_recovery_required")
    directories = [Path(value[key]) for key in ("dstack_state_dir", "dstack_runtime_dir")]
    if any(not directory.is_absolute() for directory in directories) or directories[0] == directories[1]:
        raise ValueError("migration_bootstrap_checks_invalid")
    for directory in directories:
        if _directory(directory) and any(directory.iterdir()):
            raise RuntimeError("migration_existing_engine_recovery_required")
    # Runtime files from any earlier initializer also forbid minting another
    # infrastructure identity, even when its external volumes are missing.
    for relative in ("dstack_api_token", "dstack_database_url", "hatchet", "hatchet_api_token"):
        path = ROOT / relative
        if path.exists() or path.is_symlink():
            raise RuntimeError("migration_existing_engine_recovery_required")


def _validate_intent(record):
    if (type(record) is not dict or set(record) != {"schema_version", "source_version_id", "client_request_token", "infrastructure"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or not isinstance(record["source_version_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record["source_version_id"])
            or not isinstance(record["client_request_token"], str)):
        raise ValueError("migration_initialization_intent_invalid")
    try:
        if str(uuid.UUID(record["client_request_token"])) != record["client_request_token"]:
            raise ValueError()
    except ValueError:
        raise ValueError("migration_initialization_intent_invalid") from None
    _validate_infrastructure(record["infrastructure"])


class ProtectedIntent:
    """Durable provisional identity, never a second provider credential store."""
    def read(self):
        if not INTENT.parent.exists():
            _ancestors(INTENT.parent)
            return None
        _directory(INTENT.parent)
        try:
            record = _protected_json(INTENT)
        except FileNotFoundError:
            return None
        _validate_intent(record)
        return record

    def create(self, record):
        _validate_intent(record)
        _directory(INTENT.parent, create=True)
        temporary = INTENT.parent / (".intent-"+uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", closefd=False) as stream:
                json.dump(record, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(fd)
            # Publish exclusively, without replacing a competing original
            # intent. A crash leaving a temporary hardlink fails closed.
            os.link(temporary, INTENT)
        finally:
            os.close(fd)
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(INTENT.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return self.read()


def _reconcile_intent(api, record, current, current_version, *, target='production'):
    _validate_intent(record)
    source, _ = _managed(api, version_id=record["source_version_id"], target=target)
    if source is None or "infrastructure" in source:
        raise RuntimeError("migration_original_seed_recovery_required")
    validate(source, allow_uninitialized=True, target=target)
    expected = dict(source, infrastructure=record["infrastructure"])
    validate(expected, target=target)
    target_version = record["client_request_token"]
    persisted, _ = _managed(api, version_id=target_version, target=target)
    if persisted is not None:
        if persisted != expected:
            raise RuntimeError("migration_initialization_identity_conflict")
        return persisted
    if current_version != record["source_version_id"] or current != source:
        raise RuntimeError("migration_managed_version_changed")
    try:
        # AWS RequestToken is also the exact version ID. Retrying this payload
        # with the same token cannot create another infrastructure identity.
        api.put_secret_value(SecretId=deployment_target.profile(target)['secret_root']+'/migration', ClientRequestToken=target_version,
            SecretString=json.dumps(expected, separators=(",", ":"), sort_keys=True))
    except Exception:
        pass  # Reconcile the exact durable version below; never mint again.
    persisted, _ = _managed(api, version_id=target_version, target=target)
    if persisted != expected:
        raise RuntimeError("migration_initialization_unconfirmed") from None
    return persisted


def load(api, *, initialize=False, bootstrap_checks=None, checker=check_fresh_bootstrap, intent_store=None, target='production'):
    store = intent_store if intent_store is not None else ProtectedIntent()
    intent = store.read()
    payload, version = _managed(api, target=target)
    validate(payload, allow_uninitialized=initialize or intent is not None, target=target)
    if "infrastructure" in payload:
        if intent is not None:
            _validate_intent(intent)
            original = intent["infrastructure"]
            if payload["schema_version"] == 2:
                original = {key: original[key] for key in POSTGRES_INFRASTRUCTURE}
            if payload["infrastructure"] != original:
                raise RuntimeError("migration_initialization_identity_conflict")
        return payload
    if intent is not None:
        # Recovery never depends on a refreshed absence approval and never
        # generates a second credential set, even across host/process restarts.
        return _reconcile_intent(api, intent, payload, version, target=target)
    if not initialize or bootstrap_checks is None:
        raise RuntimeError("migration_explicit_bootstrap_checks_required")
    checker(bootstrap_checks)
    if not isinstance(version, str) or not version:
        raise RuntimeError("migration_managed_version_required")
    # A host lock prevents local initialization races. Re-read before putting
    # so a concurrently updated managed version is not silently overwritten.
    current, current_version = _managed(api, target=target)
    if current_version != version or current != payload:
        raise RuntimeError("migration_managed_version_changed")
    infrastructure = {
        "dstack_admin_token": secrets.token_urlsafe(48),
        "dstack_database_url": "postgresql+asyncpg://dstack_control:"+secrets.token_urlsafe(48)+"@db:5432/sixnine_dstack",
        "hatchet_database_password": secrets.token_urlsafe(48),
        "hatchet_administrator_password": secrets.token_urlsafe(48),
    }
    intent = store.create({"schema_version": 1, "source_version_id": version,
        "client_request_token": str(uuid.uuid4()), "infrastructure": infrastructure})
    return _reconcile_intent(api, intent, current, current_version, target=target)


def outputs(payload, *, target='production', transport=None):
    """Return mount bytes/UID/GID/mode, never expose this through CLI output."""
    validate(payload, target=target)
    if transport is None:
        transport = "postgres-notify" if payload["schema_version"] == 2 else "hatchet"
    if transport not in {"hatchet", "postgres-notify"} or transport == "hatchet" and payload["schema_version"] != 1:
        raise ValueError("migration_secret_transport_invalid")
    infrastructure = payload["infrastructure"]
    # JSON is YAML-compatible and avoids another host parser dependency. The
    # pinned server's WARNING log level remains in the reviewed Compose overlay.
    backends = [{"type": name, "creds": {"api_key": config["api_key"]}}
        for name, config in sorted(payload["providers"].items())]
    serialized = lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True)+"\n"
    result = {
        "dstack-server-config.yml": (serialized({"projects": [{"name": deployment_target.profile(target)['dstack_project'], "backends": backends}]}), 0, 0, 0o600),
        "grafana_cloud": (serialized(payload["grafana_cloud"]), 10001, 10001, 0o600),
        "dstack_api_token": (infrastructure["dstack_admin_token"]+"\n", 0, 10001, 0o640),
        "dstack_database_url": (infrastructure["dstack_database_url"]+"\n", 0, 0, 0o600),
    }
    if transport == "hatchet":
        result.update({
            "hatchet/database-password": (infrastructure["hatchet_database_password"]+"\n", 0, 0, 0o600),
            "hatchet/administrator-password": (infrastructure["hatchet_administrator_password"]+"\n", 0, 0, 0o600),
        })
    if transport == "hatchet" and "hatchet_api_token" in payload:
        result["hatchet_api_token"] = (payload["hatchet_api_token"]+"\n", 10001, 10001, 0o600)
    if "grafana_stage_query" in payload:
        result["grafana_stage_query"] = (serialized(payload["grafana_stage_query"]), 10001, 10001, 0o600)
    if "r2_storage" in payload:
        result["r2_storage"] = (serialized(payload["r2_storage"]), 0, 10001, 0o440)
    return result


def _verify_file(fd, content, uid, gid, mode):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode)):
        raise RuntimeError("migration_runtime_file_invalid")
    if info.st_size != len(content) or os.read(fd, len(content)+1) != content:
        raise RuntimeError("migration_secret_rotation_requires_explicit_release")


def write_outputs(files):
    """Check the complete set first; never replace already-bound inodes."""
    if set(files) - {"dstack-server-config.yml", "grafana_cloud", "grafana_stage_query", "dstack_api_token",
            "dstack_database_url", "hatchet/database-password", "hatchet/administrator-password", "hatchet_api_token", "r2_storage"}:
        raise ValueError("migration_unexpected_runtime_file")
    root_present = _directory(ROOT)
    broker_mounts = any(relative.startswith("hatchet/") for relative in files)
    if root_present and broker_mounts:
        _directory(ROOT / "hatchet")
    for relative, (content, uid, gid, mode) in files.items():
        path = ROOT / relative
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        try:
            _verify_file(fd, content.encode("utf-8"), uid, gid, mode)
        finally:
            os.close(fd)
    _directory(ROOT, create=True)
    if broker_mounts:
        _directory(ROOT / "hatchet", create=True)
    for relative, (content, uid, gid, mode) in files.items():
        path = ROOT / relative
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            # Recheck races without changing content/metadata or the inode.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                _verify_file(fd, content.encode("utf-8"), uid, gid, mode)
            finally:
                os.close(fd)
            continue
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(fd)
        except Exception:
            # A partial new mount must not be mistaken for persisted identity.
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)


@contextmanager
def host_lock():
    import fcntl
    _ancestors(LOCK)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("migration_initializer_lock_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def hydrate(*, initialize=False, bootstrap_checks=None):
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("migration_host_root_required")
    with host_lock():
        target = deployment_target.host()['name']
        payload = load(client(), initialize=initialize, bootstrap_checks=bootstrap_checks, target=target)
        write_outputs(outputs(payload, target=target, transport=hydration_transport()))


def hydration_transport():
    """The protected release layout selects mounts, never an ambient flag.

    Existing managed v1 values and on-disk broker files remain available for
    rollback. A v2 release stops consuming them without rotating any identity.
    """
    path = Path('/srv/sixnine/migration/paths.json')
    try:
        value = _protected_json(path)
    except FileNotFoundError:
        return None  # First bootstrap uses the managed schema's exact default.
    if (type(value) is not dict or set(value) != {'version', 'environment', 'host_memory_mib'}
            or type(value['version']) is not int or value['version'] not in (1, 2)
            or type(value['environment']) is not dict):
        raise ValueError('migration_secret_transport_invalid')
    return 'postgres-notify' if value['version'] == 2 else 'hatchet'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes accidental secrets supplied as arguments.
        self.exit(2, "migration_hydration_arguments_invalid\n")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--bootstrap-checks", type=Path,
        help="Protected nonsecret first-bootstrap evidence file; never a credential")
    args = parser.parse_args(argv)
    try:
        hydrate(initialize=args.initialize, bootstrap_checks=args.bootstrap_checks)
    except Exception:
        print("migration_secret_hydration_failed; details suppressed", file=sys.stderr)
        return 1
    print("migration_cpu_secret_mounts_hydrated; no values emitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

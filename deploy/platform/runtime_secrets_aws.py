#!/usr/bin/python3
"""Host-only Secrets Manager initialization/hydration. Never emit secret values."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import uuid
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
try:
    from . import deployment_target
except ImportError:
    import deployment_target

REGION = "ap-southeast-1"
DATABASE = "/sixnine/platform/database"
ACCOUNTS = "/sixnine/platform/bootstrap-accounts"
GOOGLE_TITLES = "/sixnine/platform/google-titles"
ROOT = Path("/run/sixnine-secrets")


def client():
    return boto3.client("secretsmanager", region_name=REGION,
        endpoint_url="https://secretsmanager.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "max_attempts": 3}))


def value(api, name, initialize=False, *, target='production'):
    prefix = deployment_target.profile(target)['secret_root']
    database, accounts = prefix+'/database', prefix+'/bootstrap-accounts'
    if name not in {database, accounts}:
        raise RuntimeError('Secret source differs from the protected deployment target')
    try:
        result = json.loads(api.get_secret_value(SecretId=name)["SecretString"])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException" or not initialize:
            raise RuntimeError("Required managed secret is unavailable") from None
        # The operator creates the named secret container first. Missing versions
        # may be initialized only before any production database was created.
        api.describe_secret(SecretId=name)
        if Path("/srv/sixnine/postgres/pgdata/PG_VERSION").exists():
            raise RuntimeError("Existing database requires recovery of original secrets")
        if name == database:
            result = {"db_admin_password": secrets.token_urlsafe(48),
                "app_database_url": "postgresql+psycopg://sixnine_app:"+secrets.token_urlsafe(48)+"@db:5432/sixnine"}
        elif name == accounts:
            result = {user: secrets.token_urlsafe(30) for user in ("superdan", "supervan")}
        else:
            raise RuntimeError("Unknown secret source")
        api.put_secret_value(SecretId=name, ClientRequestToken=str(uuid.uuid4()),
            SecretString=json.dumps(result))
    expected = {"db_admin_password", "app_database_url"} if name == database else {"superdan", "supervan"}
    if not isinstance(result, dict) or set(result) != expected or any(
            not isinstance(v, str) or not 32 <= len(v) <= 512 or any(c in v for c in "\n\r\0") for v in result.values()):
        raise RuntimeError("Managed secret structure is invalid")
    if name == database:
        parsed = urlsplit(result['app_database_url'])
        if (parsed.scheme != 'postgresql+psycopg' or parsed.hostname != 'db'
                or parsed.port != 5432 or parsed.path != '/sixnine'
                or parsed.username != 'sixnine_app' or not parsed.password
                or parsed.query or parsed.fragment):
            raise RuntimeError('Managed database endpoint is invalid')
    return result


def write_runtime(name, content):
    if name not in {"db_admin_password", "app_database_url", "google_titles"}:
        raise RuntimeError("Unexpected runtime secret")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("Runtime secret directory is not protected")
    path = ROOT / name
    # Preserve Docker's already-bound inode. Repeated hydration compares exact
    # bytes without truncating; an unexpected rotation requires separate work.
    mode, group = (0o400, 0) if name == 'db_admin_password' else (0o440, 10001)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise RuntimeError("Runtime secret file is invalid")
        os.fchmod(fd, mode)
        os.fchown(fd, 0, group)
        if info.st_size:
            if info.st_size > 16384 or os.read(fd, 16385) != content.encode('utf-8'):
                raise RuntimeError('Runtime secret changed; explicit rotation required')
            return
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def hydrate_titles(api, *, target='production'):
    # The host installs this dependency-free validator alongside this helper.
    # Unlike DB credentials, optional title config may be disabled/rotated at an
    # app restart. Atomic replacement preserves any existing mounted inode.
    from studio_platform.google_title_config import validate_config, unique_pairs
    try:
        if target == 'staging':
            raise RuntimeError('Staging title integration remains disabled')
        response = api.get_secret_value(SecretId=GOOGLE_TITLES)
        payload = json.loads(response["SecretString"], object_pairs_hook=unique_pairs)
        validate_config(payload)
    except Exception:
        payload = {"enabled": False}
        print("Google title configuration unavailable; naming disabled for the next app start")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("Runtime secret directory is not protected")
    path = ROOT / "google_titles"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise RuntimeError("Title runtime file is invalid")
    temporary = ROOT / (".google-titles-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    try:
        os.fchown(fd, 0, 10001)
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, "w", closefd=False) as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(fd)
        os.replace(temporary, path)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def hydrate(initialize=False):
    if os.geteuid() != 0:
        raise RuntimeError("Host operator privileges required")
    import fcntl
    # Single host initializer; never mint independent competing secret versions.
    with Path('/opt/sixnine-release/secret-hydration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        api = client()
        target = deployment_target.host()['name']
        prefix = deployment_target.profile(target)['secret_root']
        database = value(api, prefix+'/database', initialize, target=target)
        if initialize:
            value(api, prefix+'/bootstrap-accounts', True, target=target)
        for name, content in database.items():
            write_runtime(name, content)
        hydrate_titles(api, target=target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    try:
        hydrate(args.initialize)
        print("Runtime secret files hydrated; no values emitted")
    except Exception:
        print("Managed secret hydration failed; details suppressed", file=sys.stderr)
        raise SystemExit(1)

"""Publish a tested, immutable bundle through GitHub OIDC; never approve a host release."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = "sixnine-platform-releases-829135631045-ap-southeast-1"
REGION = "ap-southeast-1"
FILES = ("image.tar.gz", "compose.yaml", "Caddyfile", "Caddyfile.staging", "deployment_target.py", "init_database.py", "check_config.py", "release-manifest.json")


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(commit, directory):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("An exact commit is required")
    directory = Path(directory)
    manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    legacy = set(FILES) - {'Caddyfile.staging', 'deployment_target.py'}
    files = set(manifest['files']) | {'release-manifest.json'}
    if manifest["commit"] != commit or files not in (set(FILES), legacy):
        raise ValueError("Invalid release manifest")
    hashes = {}
    for name in sorted(files):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024**3:
            raise ValueError("Unexpected bundle file or size")
        hashes[name] = checksum(path)
        if name != "release-manifest.json" and hashes[name] != manifest["files"][name]:
            raise ValueError("Bundle hash mismatch")
    s3 = boto3.client("s3", region_name=REGION, endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=15, read_timeout=300, retries={"mode": "standard", "max_attempts": 2}))
    # Conditional writes make retries/reruns unable to replace an approved object.
    for name in sorted(files):
        key = "releases/" + commit + "/" + name
        try:
            with (directory / name).open("rb") as body:
                s3.put_object(Bucket=BUCKET, Key=key, Body=body, IfNoneMatch="*",
                    ServerSideEncryption="AES256", Metadata={"sha256": hashes[name]})
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = s3.head_object(Bucket=BUCKET, Key=key)
            if existing.get("Metadata", {}).get("sha256") != hashes[name] or existing["ContentLength"] != (directory/name).stat().st_size:
                raise ValueError("Existing immutable release differs; operator investigation required") from None
    print(json.dumps({"commit": commit, "bucket": BUCKET, "manifest_sha256": hashes["release-manifest.json"],
        "status": "published_pending_independent_host_approval"}))


def publish_migration(commit, directory, *, api=None):
    """Immutable additive upload only; independent host approval is still needed."""
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    trusted = root / "deploy/platform"
    previous = list(sys.path)
    sys.path.insert(0, str(trusted))
    try:
        spec = importlib.util.spec_from_file_location("sixnine_reviewed_migration_publication", root / "deploy/dstack/maintenance.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        directory = Path(directory)
        manifest = module.manifest(directory, commit)
        module.release.validate_image_archive(directory / "dispatch-image.tar.gz", {"commit": commit, **manifest["dispatch"]})
        if "dstack" in manifest:
            module.release.validate_image_archive(directory / "dstack-server-image.tar.gz", {"commit": commit, **manifest["dstack"]})
        names = module.artifact_files(manifest) | {module.MANIFEST}
    finally:
        sys.path[:] = previous
    api = api or boto3.client("s3", region_name=REGION, endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
        config=Config(connect_timeout=15, read_timeout=300, retries={"mode": "standard", "max_attempts": 2}))
    hashes = {name: checksum(directory / name) for name in names}
    for name in sorted(names):
        key = "releases/" + commit + "/migration/" + name
        try:
            with (directory / name).open("rb") as body:
                api.put_object(Bucket=BUCKET, Key=key, Body=body, IfNoneMatch="*",
                    ServerSideEncryption="AES256", Metadata={"sha256": hashes[name]})
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = api.head_object(Bucket=BUCKET, Key=key)
            if existing.get("Metadata", {}).get("sha256") != hashes[name] or existing["ContentLength"] != (directory / name).stat().st_size:
                raise ValueError("Existing immutable migration artifact differs") from None
    return {"commit": commit, "manifest_sha256": hashes[module.MANIFEST],
        "status": "migration_published_pending_independent_host_approval"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--migration-directory", type=Path)
    args = parser.parse_args(argv)
    try:
        publish(args.commit, args.directory)
        if args.migration_directory is not None:
            print(json.dumps(publish_migration(args.commit, args.migration_directory)))
        return 0
    except Exception:
        print("Release publication failed; no host deployment was requested", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

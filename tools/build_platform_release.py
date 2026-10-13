"""Bundle only the tested CPU image and reviewed deployment source. No credentials."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

FILES = ("compose.yaml", "Caddyfile", "init_database.py", "check_config.py")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.release_contract import build_contracts


def build(commit, destination):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A complete tested commit ID is required")
    destination = Path(destination)
    # Refuse an existing destination; never absorb private files in an artifact.
    destination.mkdir(parents=False, exist_ok=False)
    image = "sixnine-platform:"+commit
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], stderr=subprocess.DEVNULL))[0]
    if inspected.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != commit:
        raise ValueError("Tested image revision does not match requested release")
    for name in FILES:
        shutil.copyfile(ROOT / "deploy" / "platform" / name, destination / name)
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        with gzip.open(destination / "image.tar.gz", "wb", compresslevel=1) as target:
            shutil.copyfileobj(process.stdout, target, 1024*1024)
        if process.wait() != 0:
            raise RuntimeError("Could not export tested image")
    checksums = {}
    for name in (*FILES, "image.tar.gz"):
        digest = hashlib.sha256()
        with (destination / name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024*1024), b""):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    (destination / "release-manifest.json").write_text(json.dumps(
        {"commit": commit, "image": image, "image_id": inspected["Id"], "files": checksums,
         "contracts": build_contracts(ROOT)}, indent=2)+"\n", encoding="utf-8")
    print("Reviewable release manifest SHA-256: "+hashlib.sha256((destination / "release-manifest.json").read_bytes()).hexdigest())


def build_migration(commit, platform_directory, destination, selections, *,
        retain_native_sources_directory=None, retain_native_sources_commit=None,
        retain_native_sources_index_sha256=None, retain_native_sources_from_git=False):
    """Separate additive artifact; contains public source/config templates only.

    The root maintenance helper is included for review, never execution from an
    incoming directory. The platform artifact and its approval remain unchanged.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not selections:
        raise ValueError("Migration requires exact source and explicit profile/mode selections")
    platform = Path(platform_directory) / "release-manifest.json"
    value = json.loads(platform.read_text())
    if type(value) is not dict or value.get("commit") != commit:
        raise ValueError("Migration platform manifest source differs")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.build_dstack_sources import build as build_sources
    retained = (retain_native_sources_directory, retain_native_sources_commit,
        retain_native_sources_index_sha256)
    retained_bytes, source_commits = None, None
    if any(value is not None for value in retained):
        if not all(value is not None for value in retained):
            raise ValueError("Native retention requires an explicit directory, source commit and index digest")
        if retain_native_sources_from_git:
            from tools.native_source_retention import retain_from_git_or_extend
            retained_bytes, retained_index_digest, source_commits = retain_from_git_or_extend(
                *retained, selections, root=ROOT, current_commit=commit)
        else:
            from tools.native_source_retention import retain_or_extend
            retained_bytes, retained_index_digest = retain_or_extend(*retained, selections, root=ROOT)
    elif retain_native_sources_from_git:
        raise ValueError("Historical native sources require a complete explicit retention binding")
    destination = Path(destination)
    destination.mkdir(parents=False, exist_ok=False)
    image = "sixnine-dispatch:" + commit
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], stderr=subprocess.DEVNULL))[0]
    if (inspected.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != commit
            or inspected.get("Architecture") != "amd64" or inspected.get("Os") != "linux"):
        raise ValueError("Migration dispatch image is not the exact reviewed Linux source")
    paths = {"platform-overlay.yaml": "deploy/dstack/platform-overlay.yaml",
        "hatchet-entrypoint.sh": "deploy/hatchet/entrypoint.sh", "migration-check.py": "tools/check_migration_deploy.py",
        "maintenance.py": "deploy/dstack/maintenance.py", "runtime-config.example.json": "deploy/dstack/runtime-config.example.json",
        "server-config.example.yml": "deploy/dstack/server-config.example.yml", "broker.example.json": "deploy/hatchet/broker.example.json"}
    for name, source in paths.items():
        shutil.copyfile(ROOT / source, destination / name)
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        with gzip.open(destination / "dispatch-image.tar.gz", "wb", compresslevel=1) as target:
            shutil.copyfileobj(process.stdout, target, 1024 * 1024)
        if process.wait() != 0:
            raise RuntimeError("Could not export the reviewed CPU dispatch image")
    if retained_bytes is not None:
        # Preserve every existing four-file byte/hash for GPU receipts. An
        # additive selection gets a new index; old entries stay exact.
        (destination / "source-set.tar.gz").write_bytes(retained_bytes)
        index_digest = retained_index_digest
    else:
        with tempfile.TemporaryDirectory(prefix="sixnine-public-migration-sources-") as temporary:
            sources = Path(temporary) / "sources"
            source_index = build_sources(sources, selections)
            source_commits = {entry["source_sha256"]["wangp-package.tar.gz"]: commit
                for entry in source_index["sources"]}
            with tarfile.open(destination / "source-set.tar.gz", "w:gz") as archive:
                for path in sorted(sources.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.add(path, arcname=path.relative_to(sources).as_posix(), recursive=False)
            index_digest = hashlib.sha256((sources / "index.json").read_bytes()).hexdigest()
    names = {*paths, "dispatch-image.tar.gz", "source-set.tar.gz"}
    checksums = {}
    for name in names:
        digest = hashlib.sha256()
        with (destination / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    document = {"version": 1, "commit": commit,
        "platform_manifest_sha256": hashlib.sha256(platform.read_bytes()).hexdigest(),
        "dispatch": {"image": image, "image_id": inspected["Id"]},
        "source_index_sha256": index_digest, "files": checksums}
    if source_commits is not None:
        document["native_source_commits"] = source_commits
    (destination / "migration-manifest.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--migration-destination", type=Path)
    parser.add_argument("--migration-selection", action="append", metavar="PROFILE:MODE")
    parser.add_argument("--retain-native-sources-directory", type=Path)
    parser.add_argument("--retain-native-sources-commit")
    parser.add_argument("--retain-native-sources-index-sha256")
    parser.add_argument("--retain-native-sources-from-git", action="store_true")
    args = parser.parse_args()
    build(args.commit, args.destination)
    if args.migration_destination is not None:
        build_migration(args.commit, args.destination, args.migration_destination,
            [value.rsplit(":", 1) for value in args.migration_selection or []],
            retain_native_sources_directory=args.retain_native_sources_directory,
            retain_native_sources_commit=args.retain_native_sources_commit,
            retain_native_sources_index_sha256=args.retain_native_sources_index_sha256,
            retain_native_sources_from_git=args.retain_native_sources_from_git)
    elif args.migration_selection or any(value is not None for value in (
            args.retain_native_sources_directory, args.retain_native_sources_commit,
            args.retain_native_sources_index_sha256)) or args.retain_native_sources_from_git:
        parser.error("Migration selections and native retention require --migration-destination")
    print("CPU platform bundle created; no deployment or cloud changes performed")

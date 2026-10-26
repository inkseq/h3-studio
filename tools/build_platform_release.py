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

FILES = ("compose.yaml", "Caddyfile", "Caddyfile.staging", "deployment_target.py", "init_database.py", "check_config.py")
# Closed stock tuples: current build and the exact retained rollback only.
DSTACK_STOCK = {'0.22.3': ('009e2916e335d4c9e02d961bc68f704951f5db20', 'dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8', '731ee908299af3f4592b3bfd1542e8f00fa59bb3fc24506a9f32d73a5fc274ba'), '0.22.5': ('e59a6caa6d3d76f8b8c3430b7e8eeb7271138065', 'dstackai/dstack:0.22.5@sha256:8dc2c8a5ee348789b1ed1621bc75b31c156adf31d849fded29508cfc21b279e4', 'b12e1157da5a13102899132e41c4a7950b3a2dfad4a0a5b52960e1cd86078c5e')}
DSTACK_SHARED_SOURCE_HASHES = {'dstack/_internal/core/backends/vastai/api_client.py': '9a8a68d3bd00b995901af7a66b58511ff4451be8122135e6f7b1a534d4e3c685', 'dstack/_internal/core/backends/vastai/compute.py': '3d6466c87ad37ce3125d54feadff54287f82131425e80bbc57ccf0bec8acdea9', 'gpuhunt/providers/vastai.py': 'f3cefc3a20f6f05d2cce911cd8e61ff980c6af2a42ed16e4a8f290bc0e5f76db', 'dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py': 'a68386172ebdf5fe28bd08f61e18ecdfd6a4a1310e9782c8147ce3a96e8eeaac', 'dstack/_internal/server/background/scheduled_tasks/offers_catalog.py': '158bc7bee1a5cadc4d7a1efa2512701edda39707ae5dda33b8cd71bcd342d092'}
DSTACK_RUNPOD_SOURCE_HASHES = {'dstack/_internal/core/backends/runpod/api_client.py': 'ba0d804987d70242ecedca00938215e7d23474d1874da1b828827f65d5058a6d', 'dstack/_internal/core/backends/runpod/compute.py': '8b0ac5dc44c052bbb83a20fe9d394b8e0b61a1634eb8352786cfb26eb4a130c4'}
DSTACK_IMAGE_CHECK = r'''import hashlib,importlib.metadata,json
from pathlib import Path
root=Path('/root/.local/share/uv/tools/dstack/lib/python3.11/site-packages')
folder=Path('/opt/sixnine/native-vast')
stock={'0.22.3': ('009e2916e335d4c9e02d961bc68f704951f5db20', 'dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8', '731ee908299af3f4592b3bfd1542e8f00fa59bb3fc24506a9f32d73a5fc274ba'), '0.22.5': ('e59a6caa6d3d76f8b8c3430b7e8eeb7271138065', 'dstackai/dstack:0.22.5@sha256:8dc2c8a5ee348789b1ed1621bc75b31c156adf31d849fded29508cfc21b279e4', 'b12e1157da5a13102899132e41c4a7950b3a2dfad4a0a5b52960e1cd86078c5e')}
shared={'dstack/_internal/core/backends/vastai/api_client.py': '9a8a68d3bd00b995901af7a66b58511ff4451be8122135e6f7b1a534d4e3c685', 'dstack/_internal/core/backends/vastai/compute.py': '3d6466c87ad37ce3125d54feadff54287f82131425e80bbc57ccf0bec8acdea9', 'gpuhunt/providers/vastai.py': 'f3cefc3a20f6f05d2cce911cd8e61ff980c6af2a42ed16e4a8f290bc0e5f76db', 'dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py': 'a68386172ebdf5fe28bd08f61e18ecdfd6a4a1310e9782c8147ce3a96e8eeaac', 'dstack/_internal/server/background/scheduled_tasks/offers_catalog.py': '158bc7bee1a5cadc4d7a1efa2512701edda39707ae5dda33b8cd71bcd342d092'}
value=json.loads((folder/'installed-provenance.json').read_text())
assert set(value)=={'patched_sources','dstack_version','upstream_commit','original_sources','helper_sha256','sources'}
original=json.loads((folder/'native-vast-provenance.json').read_text())
assert set(original)=={'version','dstack_version','dstack_upstream_commit','base_image','sources'} and type(original['version']) is int and original['version'] in {1,2}
selected=stock[original['dstack_version']]
assert value['dstack_version']==original['dstack_version']==importlib.metadata.version('dstack')
assert value['upstream_commit']==original['dstack_upstream_commit']==selected[0] and original['base_image']==selected[1]
expected={**shared,'dstack/_internal/server/services/backends/__init__.py':selected[2]}
if original['version']==2:
 assert original['dstack_version']=='0.22.5'
 expected.update({'dstack/_internal/core/backends/runpod/api_client.py': 'ba0d804987d70242ecedca00938215e7d23474d1874da1b828827f65d5058a6d', 'dstack/_internal/core/backends/runpod/compute.py': '8b0ac5dc44c052bbb83a20fe9d394b8e0b61a1634eb8352786cfb26eb4a130c4'})
assert value['patched_sources']==len(expected)
assert original['sources']=={name:{'sha256':digest} for name,digest in expected.items()}
assert value['original_sources']==expected and set(value['sources'])==set(expected)
assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in value['sources'].items())
helper=hashlib.sha256((root/'dstack/_internal/core/backends/vastai/sixnine_safety.py').read_bytes()).hexdigest()
assert helper==value['helper_sha256']==hashlib.sha256((folder/'native_vast_safety.py').read_bytes()).hexdigest()
print(json.dumps({'installed':value,'helper_sha256':helper,
 'installer_sha256':hashlib.sha256((folder/'patch_native_vast.py').read_bytes()).hexdigest(),
 'source_provenance_sha256':hashlib.sha256((folder/'native-vast-provenance.json').read_bytes()).hexdigest()},sort_keys=True))
'''
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


def bundle_dstack(commit, destination):
    """Export only the tested, offline-verified CPU server derivative."""
    server_image = "sixnine-dstack:" + commit
    server = json.loads(subprocess.check_output(["docker", "image", "inspect", server_image], stderr=subprocess.DEVNULL))[0]
    if (server.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != commit
            or server.get("Architecture") != "amd64" or server.get("Os") != "linux"):
        raise ValueError("Migration dstack image is not the exact reviewed Linux source")
    checked = json.loads(subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--entrypoint",
        "/root/.local/share/uv/tools/dstack/bin/python", server_image, "-B", "-c", DSTACK_IMAGE_CHECK], stderr=subprocess.DEVNULL))
    for field, source in (("helper_sha256", "native_vast_safety.py"), ("installer_sha256", "patch_native_vast.py"),
                          ("source_provenance_sha256", "native-vast-provenance.json")):
        if checked.get(field) != hashlib.sha256((ROOT / "deploy/dstack" / source).read_bytes()).hexdigest():
            raise ValueError("Migration dstack installed source differs")
    proof = {"version": 1, "commit": commit, "base_image": json.loads(
        (ROOT / "deploy/dstack/native-vast-provenance.json").read_text())["base_image"], **checked}
    (destination / "dstack-native-provenance.json").write_text(json.dumps(proof, sort_keys=True, indent=2)+"\n", encoding="utf-8")
    with subprocess.Popen(["docker", "save", server_image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        with gzip.open(destination / "dstack-server-image.tar.gz", "wb", compresslevel=1) as target:
            shutil.copyfileobj(process.stdout, target, 1024 * 1024)
        if process.wait() != 0:
            raise RuntimeError("Could not export the reviewed CPU dstack image")
    return {"image": server_image, "image_id": server["Id"]}


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
        "migration-check.py": "tools/check_migration_deploy.py",
        "maintenance.py": "deploy/dstack/maintenance.py", "runtime-config.example.json": "deploy/dstack/runtime-config.example.json",
        "server-config.example.yml": "deploy/dstack/server-config.example.yml"}
    server = bundle_dstack(commit, destination)
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
    names = {*paths, "dispatch-image.tar.gz", "source-set.tar.gz", "dstack-server-image.tar.gz", "dstack-native-provenance.json"}
    checksums = {}
    for name in names:
        digest = hashlib.sha256()
        with (destination / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksums[name] = digest.hexdigest()
    document = {"version": 2, "layout": "postgres-notify-v1", "commit": commit,
        "platform_manifest_sha256": hashlib.sha256(platform.read_bytes()).hexdigest(),
        "dispatch": {"image": image, "image_id": inspected["Id"]},
        "dstack": {"image": server["image"], "image_id": server["image_id"],
                   "provenance_sha256": checksums["dstack-native-provenance.json"]},
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

"""Retain reviewed native bytes for a CPU-only release; never execute an archive.

An explicit successful preparation run, source commit and index digest are
required. Archive contents must equal current sources or exact pinned public Git
blobs under explicit historical-source mode. Retention cannot deploy/start GPUs.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

REPOSITORY = "inkseq/h3-studio"
WORKFLOW = ".github/workflows/ci.yml"
INPUTS = ("retain_native_sources_run", "retain_native_sources_commit", "retain_native_sources_index_sha256")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_ARCHIVE = 64 * 1024 * 1024


def binding(inputs):
    values = tuple(inputs.get(key) or "" for key in INPUTS)
    if not any(values):
        return None
    if (not all(isinstance(value, str) for value in values)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", values[0])
            or not SHA.fullmatch(values[1]) or not DIGEST.fullmatch(values[2])):
        raise ValueError("native_retention_binding_invalid")
    return dict(zip(INPUTS, values))


def verify_run_identity(run, inputs):
    selected = binding(inputs)
    if selected is None:
        raise ValueError("native_retention_binding_required")
    run_id, commit, _ = (selected[key] for key in INPUTS)
    if (type(run) is not dict or type(run.get("id")) is not int or run["id"] != int(run_id)
            or run.get("path") != WORKFLOW or run.get("event") != "workflow_dispatch"
            or run.get("head_sha") != commit or run.get("head_branch") != "main"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or (run.get("repository") or {}).get("full_name") != REPOSITORY
            or (run.get("head_repository") or {}).get("full_name") != REPOSITORY):
        raise ValueError("native_retention_run_identity_invalid")
    return selected


def verify_run(run, artifacts, inputs):
    selected = verify_run_identity(run, inputs)
    run_id, commit, _ = (selected[key] for key in INPUTS)
    if (type(artifacts) is not dict or type(artifacts.get("total_count")) is not int
            or not 0 <= artifacts["total_count"] <= 100 or type(artifacts.get("artifacts")) is not list
            or len(artifacts["artifacts"]) != artifacts["total_count"]):
        raise ValueError("native_retention_artifact_inventory_incomplete")
    matches = [item for item in artifacts["artifacts"] if type(item) is dict
        and item.get("name") == "sixnine-migration-" + commit]
    if len(matches) != 1:
        raise ValueError("native_retention_artifact_identity_invalid")
    artifact = matches[0]
    identity = artifact.get("workflow_run") or {}
    if (artifact.get("expired") is not False or type(artifact.get("id")) is not int
            or artifact["id"] <= 0 or identity.get("id") != int(run_id)
            or identity.get("head_sha") != commit or identity.get("head_branch") != "main"):
        raise ValueError("native_retention_artifact_identity_invalid")
    return artifact["id"]


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("native_retention_duplicate_json_key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def _regular(path, maximum):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("native_retention_link_forbidden")
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise ValueError("native_retention_file_invalid")
    return path.read_bytes()


def _archive(raw, expected, *, maximum=MAX_ARCHIVE):
    result, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if (member.name not in expected or member.name in result or not member.isfile()
                    or not 0 <= member.size <= expected[member.name]):
                raise ValueError("native_retention_archive_member_invalid")
            total += member.size
            if total > maximum:
                raise ValueError("native_retention_archive_limit")
            stream = archive.extractfile(member)
            data = stream.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError("native_retention_archive_truncated")
            result[member.name] = data
    if set(result) != set(expected):
        raise ValueError("native_retention_archive_incomplete")
    return result


def _git_sources(root, commit):
    """Read pinned public Git blobs; never check out, import or execute history."""
    if not SHA.fullmatch(commit or ""):
        raise ValueError("native_retention_source_commit_invalid")
    from deploy.wangp.package_tool import PRIVATE_FILES
    root = Path(root).absolute()
    # Disable replacement refs: the explicit commit must identify its own tree.
    environment = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, check=False, env=environment)
        if result.returncode:
            raise ValueError("native_retention_source_commit_unavailable")
        return result.stdout
    if git("cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("native_retention_source_commit_invalid")
    def blob(name, maximum):
        if (not re.fullmatch(r"[A-Za-z0-9_.\-/]+", name)
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("native_retention_source_path_invalid")
        entry = git("ls-tree", "-z", commit, "--", name)
        match = re.fullmatch(rb"100(?:644|755) blob ([0-9a-f]{40})\t" + re.escape(name.encode()) + rb"\x00", entry)
        if match is None:
            raise ValueError("native_retention_source_file_invalid")
        identity = match[1].decode("ascii")
        size_raw = git("cat-file", "-s", identity).strip()
        if not size_raw.isdigit() or not 0 < int(size_raw) <= maximum:
            raise ValueError("native_retention_source_file_invalid")
        raw = git("cat-file", "blob", identity)
        if len(raw) != int(size_raw):
            raise ValueError("native_retention_source_file_invalid")
        return raw
    tool = blob("deploy/wangp/package_tool.py", 1024 * 1024)
    try:
        assignments = [node.value for node in ast.parse(tool).body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PRIVATE_FILES" for target in node.targets)]
        names = ast.literal_eval(assignments[0]) if len(assignments) == 1 else None
    except (SyntaxError, ValueError, TypeError):
        names = None
    if (type(names) not in (tuple, list) or not 1 <= len(names) <= 128
            or any(type(name) is not str or name not in PRIVATE_FILES for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("native_retention_source_members_invalid")
    sources, total = {}, 0
    for name in names:
        sources[name] = blob(name, 16 * 1024 * 1024)
        total += len(sources[name])
        if total > MAX_ARCHIVE:
            raise ValueError("native_retention_source_limit")
    return sources, blob("deploy/wangp/bootstrap.py", 1024 * 1024)


def _source_commits(manifest, packages, fallback):
    origins = manifest.get("native_source_commits")
    if origins is None:
        return {digest: fallback for digest in packages}
    if (type(origins) is not dict or set(origins) != set(packages)
            or any(type(key) is not str or not DIGEST.fullmatch(key)
                or type(value) is not str or not SHA.fullmatch(value) for key, value in origins.items())):
        raise ValueError("native_retention_source_commits_invalid")
    return dict(origins)


def retain(directory, commit, index_sha256, selections, *, root, git_sources=False):
    """Return original public source archive bytes only after complete comparison.

    The caller separately establishes the GitHub artifact provenance. No files
    are extracted, imported or executed from the retained artifact.
    """
    if not SHA.fullmatch(commit or "") or not DIGEST.fullmatch(index_sha256 or ""):
        raise ValueError("native_retention_binding_invalid")
    from deploy.wangp.package_tool import PRIVATE_FILES
    from studio_platform.runtime_catalog import engine_manifest
    from tools.build_operator_sources import SOURCE_NAMES, _json as canonical, _runtime
    from tools.build_dstack_sources import PURPOSE
    selections = [tuple(item) for item in selections]
    if not 1 <= len(selections) <= 128 or len(set(selections)) != len(selections):
        raise ValueError("native_retention_selection_invalid")
    current = None if git_sources else {name: _regular(Path(root) / name, 16 * 1024 * 1024) for name in PRIVATE_FILES}
    bootstrap = None if git_sources else _regular(Path(root) / "deploy/wangp/bootstrap.py", 1024 * 1024)
    manifest = _json(_regular(Path(directory) / "migration-manifest.json", 1024 * 1024))
    raw = _regular(Path(directory) / "source-set.tar.gz", MAX_ARCHIVE)
    if (type(manifest) is not dict or manifest.get("version") != 1 or manifest.get("commit") != commit
            or manifest.get("source_index_sha256") != index_sha256
            or (manifest.get("files") or {}).get("source-set.tar.gz") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("native_retention_manifest_binding_changed")
    expected = {"index.json": 1024 * 1024}
    for profile, mode in selections:
        # engine_manifest validates the exact known profile and mode.
        engine_manifest(profile, mode)
        relative = f"{profile}/{mode}/gpu-0/"
        expected.update({relative + name: 16 * 1024 * 1024 for name in SOURCE_NAMES})
    contents = _archive(raw, expected)
    if hashlib.sha256(contents["index.json"]).hexdigest() != index_sha256:
        raise ValueError("native_retention_index_changed")
    packages = {hashlib.sha256(contents[f"{profile}/{mode}/gpu-0/wangp-package.tar.gz"]).hexdigest()
        for profile, mode in selections}
    origins = _source_commits(manifest, packages, commit) if git_sources else {}
    historical = {}
    entries = []
    for profile, mode in selections:
        relative = f"{profile}/{mode}/gpu-0"
        sources = {name: contents[relative + "/" + name] for name in SOURCE_NAMES}
        package_sha = hashlib.sha256(sources["wangp-package.tar.gz"]).hexdigest()
        if git_sources:
            origin = origins[package_sha]
            if origin not in historical:
                historical[origin] = _git_sources(root, origin)
            current, bootstrap = historical[origin]
        package = _archive(sources["wangp-package.tar.gz"], {name: len(data) for name, data in current.items()})
        if package != current:
            raise ValueError("native_retention_package_source_changed")
        native_manifest = engine_manifest(profile, mode)
        if (sources["wangp-bootstrap.py"] != bootstrap
                or sources["wangp-manifest.json"] != canonical(native_manifest.document)
                or sources["wangp-runtime.json"] != canonical(_runtime(profile, 0, 1, package_sha))):
            raise ValueError("native_retention_native_configuration_changed")
        entries.append({"runtime_profile_id": profile, "mode": mode, "gpu_count": 1,
            "profile_slot_index": 0, "directory": relative,
            "engine_manifest_digest": native_manifest.digest,
            "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}})
    expected_index = {"schema_version": 1, "purpose": PURPOSE, "production_adapter_verified": False,
        "sources": entries}
    if _json(contents["index.json"]) != expected_index:
        raise ValueError("native_retention_index_contract_changed")
    return raw


def _retain_or_extend(directory, commit, index_sha256, selections, *, root, git_sources):
    """Append exact catalog selections without changing an existing GPU receipt.

    The original index/order and every old four-file byte remain mandatory.
    Strict mode appends the fully verified old package. Historical-source mode
    verifies old Git blobs and appends a fresh current package instead.
    This builds public archive bytes only; it never extracts or executes code.
    """
    from studio_platform.runtime_catalog import engine_manifest
    from tools.build_operator_sources import SOURCE_NAMES, _json as canonical, _runtime
    from tools.build_dstack_sources import PURPOSE
    pairs = list(selections)
    if (not 1 <= len(pairs) <= 128 or any(type(item) not in (tuple, list) or len(item) != 2
            or not all(isinstance(value, str) for value in item) for item in pairs)):
        raise ValueError("native_retention_selection_invalid")
    pairs = [tuple(item) for item in pairs]
    if len(set(pairs)) != len(pairs):
        raise ValueError("native_retention_selection_invalid")
    for profile, mode in pairs:
        engine_manifest(profile, mode)
    if not SHA.fullmatch(commit or "") or not DIGEST.fullmatch(index_sha256 or ""):
        raise ValueError("native_retention_binding_invalid")
    manifest = _json(_regular(Path(directory) / "migration-manifest.json", 1024 * 1024))
    raw = _regular(Path(directory) / "source-set.tar.gz", MAX_ARCHIVE)
    if (type(manifest) is not dict or manifest.get("version") != 1 or manifest.get("commit") != commit
            or manifest.get("source_index_sha256") != index_sha256
            or (manifest.get("files") or {}).get("source-set.tar.gz") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("native_retention_manifest_binding_changed")
    # Read one bounded index only to discover the original selections. retain()
    # below then independently checks every member and the complete old index.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        indexes, total = [], 0
        for count, member in enumerate(archive, 1):
            total += member.size
            if (count > 513 or not member.isfile() or len(member.name) > 512
                    or not 0 <= member.size <= 16 * 1024 * 1024 or total > MAX_ARCHIVE):
                raise ValueError("native_retention_archive_member_invalid")
            if member.name == "index.json":
                indexes.append(member)
        if (len(indexes) != 1 or not indexes[0].isfile()
                or not 0 < indexes[0].size <= 1024 * 1024):
            raise ValueError("native_retention_index_contract_changed")
        index_raw = archive.extractfile(indexes[0]).read(indexes[0].size + 1)
    if hashlib.sha256(index_raw).hexdigest() != index_sha256:
        raise ValueError("native_retention_index_changed")
    index = _json(index_raw)
    if (type(index) is not dict or set(index) != {"schema_version", "purpose", "production_adapter_verified", "sources"}
            or type(index.get("sources")) is not list or not 1 <= len(index["sources"]) <= 128
            or any(type(entry) is not dict for entry in index["sources"])):
        raise ValueError("native_retention_index_contract_changed")
    original = [(entry.get("runtime_profile_id"), entry.get("mode")) for entry in index["sources"]]
    if original != pairs[:len(original)]:
        raise ValueError("native_retention_additive_selection_required")
    verified = retain(directory, commit, index_sha256, original, root=root, git_sources=git_sources)
    expected = {"index.json": 1024 * 1024}
    for profile, mode in original:
        expected.update({f"{profile}/{mode}/gpu-0/{name}": 16 * 1024 * 1024 for name in SOURCE_NAMES})
    contents = _archive(verified, expected)
    origins = _source_commits(manifest,
        {hashlib.sha256(contents[entry["directory"] + "/wangp-package.tar.gz"]).hexdigest()
            for entry in index["sources"]}, commit) if git_sources else {}
    if pairs == original:
        return verified, index_sha256, origins
    if git_sources:
        # Only appended immutable selections receive fresh source. The retained
        # artifact is never unpacked to disk or imported by this build.
        from deploy.wangp.package_tool import small_bundle, PRIVATE_FILES
        with tempfile.TemporaryDirectory(prefix="sixnine-new-native-source-") as temporary:
            path = Path(temporary) / "wangp-package.tar.gz"
            small_bundle(path)
            package = _regular(path, 16 * 1024 * 1024)
        current = {name: _regular(Path(root) / name, 16 * 1024 * 1024) for name in PRIVATE_FILES}
        if _archive(package, {name: len(data) for name, data in current.items()}) != current:
            raise ValueError("native_retention_fresh_package_source_changed")
    else:
        package = contents[index["sources"][0]["directory"] + "/wangp-package.tar.gz"]
    package_sha = hashlib.sha256(package).hexdigest()
    bootstrap = _regular(Path(root) / "deploy/wangp/bootstrap.py", 1024 * 1024)
    entries = list(index["sources"])
    for profile, mode in pairs[len(original):]:
        native = engine_manifest(profile, mode)
        relative = f"{profile}/{mode}/gpu-0"
        sources = {"wangp-bootstrap.py": bootstrap, "wangp-package.tar.gz": package,
            "wangp-manifest.json": canonical(native.document),
            "wangp-runtime.json": canonical(_runtime(profile, 0, 1, package_sha))}
        contents.update({relative + "/" + name: data for name, data in sources.items()})
        entries.append({"runtime_profile_id": profile, "mode": mode, "gpu_count": 1,
            "profile_slot_index": 0, "directory": relative, "engine_manifest_digest": native.digest,
            "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}})
    contents["index.json"] = canonical({"schema_version": 1, "purpose": PURPOSE,
        "production_adapter_verified": False, "sources": entries})
    if sum(len(data) for data in contents.values()) > MAX_ARCHIVE:
        raise ValueError("native_retention_archive_limit")
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for name, data in sorted(contents.items()):
            member = tarfile.TarInfo(name); member.size = len(data); member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))
    result = target.getvalue()
    if len(result) > MAX_ARCHIVE:
        raise ValueError("native_retention_archive_limit")
    return result, hashlib.sha256(contents["index.json"]).hexdigest(), origins


def retain_or_extend(directory, commit, index_sha256, selections, *, root):
    """Strict current-source retention, preserving the original two-value API."""
    raw, index, _ = _retain_or_extend(directory, commit, index_sha256, selections, root=root, git_sources=False)
    return raw, index


def retain_from_git_or_extend(directory, commit, index_sha256, selections, *, root, current_commit):
    """Preserve pinned historical bundles and append current immutable selections.

    Returns archive, index digest and package-SHA to public source-commit map.
    Caller must bind that map into the new migration manifest. No history runs.
    """
    if not SHA.fullmatch(current_commit or ""):
        raise ValueError("native_retention_source_commit_invalid")
    selections = list(selections)
    raw, index, origins = _retain_or_extend(directory, commit, index_sha256, selections, root=root, git_sources=True)
    contents = _archive(raw, {"index.json": 1024 * 1024, **{
        f"{profile}/{mode}/gpu-0/{name}": 16 * 1024 * 1024
        for profile, mode in selections for name in ("wangp-bootstrap.py", "wangp-package.tar.gz", "wangp-manifest.json", "wangp-runtime.json")}})
    for profile, mode in selections:
        relative = f"{profile}/{mode}/gpu-0/"
        digest = hashlib.sha256(contents[relative + "wangp-package.tar.gz"]).hexdigest()
        if digest not in origins:
            origins[digest] = current_commit
    # Verify the complete mixed archive against the exact commits before returning.
    # This also refuses a package reused with a bootstrap from a different source.
    historical = {}
    for profile, mode in selections:
        relative = f"{profile}/{mode}/gpu-0/"
        package = contents[relative + "wangp-package.tar.gz"]
        origin = origins[hashlib.sha256(package).hexdigest()]
        if origin not in historical:
            historical[origin] = _git_sources(root, origin)
        members, bootstrap = historical[origin]
        if (_archive(package, {name: len(data) for name, data in members.items()}) != members
                or contents[relative + "wangp-bootstrap.py"] != bootstrap):
            raise ValueError("native_retention_source_commit_bytes_changed")
    return raw, index, origins


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--artifacts-json", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--index-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        verify_run(_json(args.run_json.read_bytes()), _json(args.artifacts_json.read_bytes()),
            dict(zip(INPUTS, (args.run, args.commit, args.index_sha256))))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, "Native source retention provenance validation failed\n")
    print("Exact prior preparation run and migration artifact verified")


if __name__ == "__main__":
    main()

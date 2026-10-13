"""Retain reviewed native bytes for a CPU-only release; never execute an archive.

An explicit successful preparation run, source commit and index digest are
required. Archive contents must still equal the current native sources and
profile/runtime documents. Retention is not permission to deploy or start GPUs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

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


def verify_run(run, artifacts, inputs):
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


def retain(directory, commit, index_sha256, selections, *, root):
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
    current = {name: _regular(Path(root) / name, 16 * 1024 * 1024) for name in PRIVATE_FILES}
    bootstrap = _regular(Path(root) / "deploy/wangp/bootstrap.py", 1024 * 1024)
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
    entries = []
    for profile, mode in selections:
        relative = f"{profile}/{mode}/gpu-0"
        sources = {name: contents[relative + "/" + name] for name in SOURCE_NAMES}
        package = _archive(sources["wangp-package.tar.gz"], {name: len(data) for name, data in current.items()})
        if package != current:
            raise ValueError("native_retention_package_source_changed")
        package_sha = hashlib.sha256(sources["wangp-package.tar.gz"]).hexdigest()
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

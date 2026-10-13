"""Build local, unqualified operator source sets; no install or provider calls.

Each immutable profile/mode/GPU directory contains the four bootstrap inputs.
The index supplies hashes for a separately reviewed protected runtime binding;
it does not enable that binding, grant spending authority or certify readiness.
Run with ``python tools/build_operator_sources.py --output NEW_DIRECTORY``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.wangp.package_tool import small_bundle
from studio_platform.native_runtime_document import (
    MODEL_ROOT, PREPARED_ROOT, prepared_runtime_document as _runtime)
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile

SOURCE_NAMES = ("wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json", "wangp-package.tar.gz")


def _write(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    Path(path).chmod(0o600)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def build(output):
    """Create all catalog topologies once.

    A failed build can leave an incomplete new directory. It is never resumed
    or overwritten; only a completed index.json is a build receipt.
    """
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("operator_sources_output_exists")
    if not output.parent.is_dir():
        raise ValueError("operator_sources_parent_missing")
    for parent in output.parents:
        if parent.is_symlink():
            raise ValueError("operator_sources_linked_parent")
    profiles = [get_profile(identity) for identity in PROFILE_IDS]
    if any(len(p["gpu_count_options"]) != 1 or type(p["gpu_count_options"][0]) is not int
           or p["gpu_count_options"][0] not in (1, 2) for p in profiles):
        raise ValueError("operator_sources_topology_requires_review")
    bootstrap = (ROOT / "deploy/wangp/bootstrap.py").read_bytes()
    output.mkdir(mode=0o700, exist_ok=False)
    entries = []
    package = None
    for profile in profiles:
        count = profile["gpu_count_options"][0]
        for mode in (model["mode"] for model in profile["models"]):
            manifest = engine_manifest(profile["id"], mode)
            for index in range(count):
                relative = Path(profile["id"]) / mode / ("gpu-" + str(index))
                directory = output / relative
                directory.mkdir(mode=0o700, parents=True)
                archive = directory / "wangp-package.tar.gz"
                if package is None:
                    small_bundle(archive)
                    archive.chmod(0o600)
                    package = archive.read_bytes()
                else:
                    _write(archive, package)
                bundle_hash = hashlib.sha256(package).hexdigest()
                _write(directory / "wangp-bootstrap.py", bootstrap)
                _write(directory / "wangp-manifest.json", _json(manifest.document))
                _write(directory / "wangp-runtime.json", _json(_runtime(profile["id"], index, count, bundle_hash)))
                hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in SOURCE_NAMES}
                entries.append({
                    "runtime_profile_id": profile["id"], "mode": mode,
                    "gpu_count": count, "profile_slot_index": index,
                    "directory": relative.as_posix(), "source_sha256": hashes,
                    "engine_manifest_digest": manifest.digest,
                })
    receipt = {"schema_version": 1, "purpose": "local-unqualified-operator-source-set",
               "production_adapter_verified": False, "sources": entries}
    _write(output / "index.json", _json(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New directory under an existing local parent; never overwritten")
    args = parser.parse_args(argv)
    try:
        receipt = build(args.output)
    except (OSError, ValueError):
        print(json.dumps({"state": "source_build_failed"}))
        return 1
    print(json.dumps({"state": "built_unqualified", "slot_source_count": len(receipt["sources"]),
                      "production_adapter_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify a tested release bundle before upload; no Docker load or service IO."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def verify(directory, commit):
    # Import only the checked-out trusted validator, never code from the bundle.
    trusted = Path(__file__).resolve().parents[1]/"deploy"/"platform"
    previous = list(sys.path)
    sys.path.insert(0, str(trusted))
    try:
        spec = importlib.util.spec_from_file_location("sixnine_ci_release_validator", trusted/"release.py")
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
    finally:
        sys.path[:] = previous
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("bundle_directory_invalid")
    expected = release.manifest(directory, commit)
    if {entry.name for entry in directory.iterdir()} != set(expected['files']) | {"release-manifest.json"}:
        raise ValueError("bundle_contains_unreviewed_files")
    release.validate_image_archive(directory/"image.tar.gz", expected)
    return {"state": "bundle_manifest_and_archive_verified", "commit": commit,
            "manifest_sha256": hashlib.sha256((directory/"release-manifest.json").read_bytes()).hexdigest(),
            "image_loaded": False, "provenance_approved": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.bundle, args.commit)))
        return 0
    except Exception:
        print(json.dumps({"state": "bundle_verification_failed", "details": "Review trusted manifest and Docker archive; no image was loaded"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

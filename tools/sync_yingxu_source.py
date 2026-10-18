#!/usr/bin/env python3
"""Create/check the one-way, source-only Yingxu release snapshot.

Canonical edits remain in ../video-studio-design/studio-app. Never edit yingxu/
directly. Run --check before a release; --write explicitly refreshes owned files.
No user media, browser storage, environments, credentials or build output is read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT_FILES = ("index.html", "package.json", "package-lock.json", "vite.config.js",
              ".prettierignore", ".prettierrc.json", "eslint.config.js", "tools/check-changed.mjs")
SOURCE_SUFFIXES = {".js", ".jsx", ".css", ".json"}
SOURCE_DOCUMENTS = {"src/NOVICE-STORIES-AC.md", "src/v2/fonts/README.md", "src/v2/fonts/OFL-Inter.txt"}
# Only the reviewed, self-hosted prototype font is a binary source asset. Do
# not broaden the source snapshot to arbitrary fonts, media or text documents.
SOURCE_BINARIES = {"src/v2/fonts/inter-latin-v20.woff2"}
MANIFEST = "source-manifest.json"
SOURCE_REFERENCE = "../video-studio-design/studio-app"
IGNORED_BUILD_DIRS = {"node_modules", "dist", ".npm-cache"}


def _safe_relative(value):
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Snapshot manifest contains an unsafe path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or ":" in value:
        raise ValueError("Snapshot manifest contains an unsafe path")
    if value not in ROOT_FILES and value not in SOURCE_DOCUMENTS and value not in SOURCE_BINARIES and not (value.startswith("src/") and path.suffix in SOURCE_SUFFIXES):
        raise ValueError("Snapshot manifest path is outside the source allowlist")
    return value


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_bytes(path, *, binary=False):
    raw = path.read_bytes()
    if binary:
        return raw
    # Git and Linux releases use LF. Canonical Windows editor line endings do
    # not change text source/checksums. Binary font bytes must remain exact.
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def source_files(source):
    source = Path(source)
    result = {}
    for name in ROOT_FILES:
        _check_target_links(source, name)
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Required regular source file is missing: {name}")
        result[name] = path
    src = source / "src"
    if not src.is_dir() or src.is_symlink():
        raise ValueError("Source src must be a regular directory")
    for directory, directories, names in os.walk(src, followlinks=False):
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Source links/junctions are not followed")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(source).as_posix()
            if path.suffix not in SOURCE_SUFFIXES and relative not in SOURCE_DOCUMENTS and relative not in SOURCE_BINARIES:
                continue
            if path.is_symlink():
                raise ValueError("Source symlinks are not included")
            result[_safe_relative(relative)] = path
    return dict(sorted(result.items()))


def expected_manifest(files):
    return {"format": "yingxu-source-snapshot-v1", "canonical_source": SOURCE_REFERENCE,
            "direction": "canonical-to-release-only", "files": {name: hashlib.sha256(_source_bytes(path, binary=name in SOURCE_BINARIES)).hexdigest() for name, path in files.items()}}


def read_manifest(target):
    path = target / MANIFEST
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("Snapshot manifest cannot be a symlink")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "yingxu-source-snapshot-v1" or not isinstance(data.get("files"), dict):
        raise ValueError("Snapshot manifest format is invalid")
    for name, checksum in data["files"].items():
        _safe_relative(name)
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Snapshot checksum is invalid")
    return data


def _check_target_links(target, relative):
    # Inspect each path component before writing/removing; do not traverse links.
    path = target
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Snapshot contains a link/junction; no files were changed")


def verify_snapshot(target):
    target = Path(target)
    manifest = read_manifest(target)
    if manifest is None:
        raise ValueError("No source snapshot manifest; run --write from the canonical workspace")
    if b"\r" in (target / MANIFEST).read_bytes():
        raise ValueError("Snapshot manifest must use LF line endings")
    problems = []
    for name, checksum in manifest["files"].items():
        _check_target_links(target, name)
        path = target / name
        if not path.is_file() or (name not in SOURCE_BINARIES and b"\r" in path.read_bytes()) or _hash(path) != checksum:
            problems.append(name)
    if problems:
        raise ValueError("Snapshot has missing/modified managed files: " + ", ".join(problems))
    return manifest


def synchronize(source, target, *, write=False):
    source, target = Path(source).resolve(), Path(target).absolute()
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        raise ValueError("Snapshot target cannot be a link/junction")
    if source == target.resolve() or source in target.resolve().parents or target.resolve() in source.parents:
        raise ValueError("Canonical and snapshot directories must be separate")
    files = source_files(source)
    expected = expected_manifest(files)
    previous = read_manifest(target)
    if not write:
        actual = verify_snapshot(target)
        if actual != expected:
            changed = sorted(name for name in set(actual["files"]) | set(expected["files"])
                             if actual["files"].get(name) != expected["files"].get(name))
            raise ValueError("Canonical source differs from release snapshot: " + ", ".join(changed))
        return expected
    managed = set(previous["files"]) if previous else set()
    for name in set(files) | managed:
        _check_target_links(target, name)
        path = target / name
        if path.exists() and name not in managed:
            raise ValueError(f"Refusing to overwrite an unmanaged snapshot file: {name}")
    # All paths are validated before the first mutation. Only prior manifest-owned
    # stale files can be removed. No recursive delete and no source writes occur.
    target.mkdir(parents=True, exist_ok=True)
    for name, path in files.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_source_bytes(path, binary=name in SOURCE_BINARIES))
    for name in managed - set(files):
        path = target / name
        if path.is_file():
            path.unlink()
    (target / MANIFEST).write_bytes((json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    verify_snapshot(target)
    return expected


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=repo.parent / "video-studio-design" / "studio-app")
    parser.add_argument("--target", type=Path, default=repo / "yingxu")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="Check canonical source and committed snapshot")
    modes.add_argument("--write", action="store_true", help="Refresh only allowlisted snapshot files")
    modes.add_argument("--verify-snapshot", action="store_true", help="CI: verify snapshot without canonical checkout")
    args = parser.parse_args()
    try:
        manifest = verify_snapshot(args.target) if args.verify_snapshot else synchronize(args.source, args.target, write=args.write)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"Yingxu snapshot check failed: {error}\n")
    print(f"Yingxu source snapshot verified: {len(manifest['files'])} managed files")


if __name__ == "__main__":
    main()

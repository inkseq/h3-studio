"""Finite, offline repair of the measured Lium SSH package side effect.

This is not a general package manager or permission to change a runtime lock.
The source-bundle hash authorizes the optional kit. Official archive hashes below
were verified through signed Ubuntu snapshot 20250101T000000Z metadata.
"""

import json
import os
from pathlib import Path
import subprocess

from .wangp_environment import digest, regular_file, sha_file, system_packages

FORMAT = "sixnine-wangp-system-restore-v1"
DIRECTORY = "system-restore"
TARGET = "249.11-0ubuntu3.12"
OBSERVED = "249.11-0ubuntu3.22"
PINNED_DEBS = {
    "libnss-systemd": (
        133138,
        "d7668f0c871c6aaaa88be88a0c7086b07dfd345819603dc9567976e534a2dc5a",
    ),
    "libpam-systemd": (
        202680,
        "06feb193abc523382d02f87783f09f5192e8c42b8e5453acded27890b06ea794",
    ),
    "libsystemd0": (
        318720,
        "e1c90c12ce39edd04fd2648baad999e63e5fc122a5f94ddc88bd8f445f8a0b4b",
    ),
    "systemd-sysv": (
        10460,
        "c7aa3418e71c70f400a5970d5e931f175907016f84d19df2c84cd31d2d064f67",
    ),
    "systemd-timesyncd": (
        31184,
        "37444f527af703633239a13e6c1f0d297f60db932ee004def1d1a8eedbf40742",
    ),
    "systemd": (
        4580830,
        "e18e392f9a4c6fc1f0c2d1a749e3d1dba8953b5d31fb266f60a50901470df504",
    ),
}


def package_key(name):
    return name + ":amd64" if name.startswith("lib") else name


def validate_kit(root, lock):
    root = Path(root)
    manifest_path = regular_file(root, "manifest.json")
    if manifest_path.stat().st_size > 16384:
        raise ValueError("system_restore_manifest_invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "format",
            "base_image",
            "environment_lock_sha256",
            "from_version",
            "target_version",
            "packages",
        }
        or manifest["format"] != FORMAT
        or manifest["base_image"] != lock["base_image"]
        or manifest["environment_lock_sha256"] != digest(lock)
        or manifest["from_version"] != OBSERVED
        or manifest["target_version"] != TARGET
        or lock["system_packages"].get("libsystemd0:amd64") != TARGET
        or not isinstance(manifest["packages"], list)
        or len(manifest["packages"]) != len(PINNED_DEBS)
    ):
        raise ValueError("system_restore_manifest_invalid")
    files, seen = [], set()
    for item in manifest["packages"]:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "version",
            "architecture",
            "file",
            "size_bytes",
            "sha256",
        }:
            raise ValueError("system_restore_package_invalid")
        name = item["name"]
        if not isinstance(name, str) or name not in PINNED_DEBS or name in seen:
            raise ValueError("system_restore_package_invalid")
        size, sha = PINNED_DEBS[name]
        if (
            item["version"] != TARGET
            or item["architecture"] != "amd64"
            or item["file"] != f"{name}_{TARGET}_amd64.deb"
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] != size
            or item["sha256"] != sha
        ):
            raise ValueError("system_restore_package_invalid")
        # A provider-only dependency cannot replace another environment pin.
        if lock["system_packages"].get(package_key(name), TARGET) != TARGET:
            raise ValueError("system_restore_lock_conflict")
        path = regular_file(root, item["file"])
        if path.stat().st_size != size or sha_file(path) != sha:
            raise ValueError("system_restore_package_mismatch")
        files.append(path)
        seen.add(name)
    if {p.name for p in root.iterdir()} != {"manifest.json", *(p.name for p in files)}:
        raise ValueError("system_restore_extra_file")
    return manifest, files


def restore_system(root, lock):
    """Return a finite receipt; unchanged hosts perform no subprocess mutation."""
    manifest, files = validate_kit(root, lock)
    before = system_packages()
    mismatches = {
        name
        for name, version in lock["system_packages"].items()
        if before.get(name) != version
    }
    family = {package_key(name): before.get(package_key(name)) for name in PINNED_DEBS}
    if not mismatches and all(value in {None, TARGET} for value in family.values()):
        return {"state": "not_needed", "package_count": 0}
    if mismatches - {"libsystemd0:amd64"} or any(
        value not in {OBSERVED, TARGET} for value in family.values()
    ):
        raise ValueError("system_restore_unrecognized_drift")
    for item, path in zip(manifest["packages"], files):
        result = subprocess.run(
            ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        actual = dict(line.split(": ", 1) for line in result.stdout.splitlines())
        if actual != {
            "Package": item["name"],
            "Version": TARGET,
            "Architecture": "amd64",
        }:
            raise ValueError("system_restore_deb_metadata_mismatch")
    subprocess.run(
        ["dpkg", "--install", *(str(path) for path in files)],
        check=True,
        capture_output=True,
        timeout=180,
        env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"),
    )
    after = system_packages()
    if any(
        after.get(name) != version for name, version in lock["system_packages"].items()
    ) or any(after.get(package_key(name)) != TARGET for name in PINNED_DEBS):
        raise ValueError("system_restore_verification_failed")
    audit = subprocess.run(
        ["dpkg", "--audit"], check=True, capture_output=True, text=True, timeout=30
    )
    if audit.stdout.strip() or audit.stderr.strip():
        raise ValueError("system_restore_package_audit_failed")
    subprocess.run(["apt-get", "check"], check=True, capture_output=True, timeout=30)
    subprocess.run(
        ["/usr/sbin/sshd", "-t"], check=True, capture_output=True, timeout=15
    )
    return {
        "state": "restored",
        "package_count": len(files),
        "environment_lock_sha256": digest(lock),
    }

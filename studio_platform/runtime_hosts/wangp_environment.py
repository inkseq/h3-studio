"""Standard-library attestation of a separately built private runtime package.

No resolver, installer, model import or network action occurs here. Image identity
is verified by the controller/provider; a guest cannot attest its own OCI digest.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess

FORMAT = "sixnine-wangp-environment-v1"
PYTHON = "3.11.14"
REVISION = "0e58385fbde7ff102d276e4a9e490845de76b4ea"
REQUIREMENTS_SHA = "a9c4b97e100095e17302d27a2b8e35e5ec4b476a322ab2ed366cd30de97cc970"
PACKAGE_NAME_PATTERN = r"[a-z0-9][a-z0-9+.-]{0,99}(?::[a-z0-9-]{1,20})?"
PACKAGE_VERSION_PATTERN = r"[0-9][A-Za-z0-9.+:~\-]{0,99}"
MAX_SYSTEM_PACKAGES = 10000
MAX_PACKAGE_MISMATCHES = 16


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_relative(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("wangp_package_path_invalid")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise ValueError("wangp_package_path_invalid")
    if str(path) != name:
        raise ValueError("wangp_package_path_invalid")
    return Path(*path.parts)


def regular_file(root, name):
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError("wangp_package_link_forbidden")
    relative = safe_relative(name)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("wangp_package_link_forbidden")
    if not current.is_file() or current.stat().st_nlink != 1:
        raise ValueError("wangp_package_file_invalid")
    return current


def installed_packages():
    values = {}
    for item in importlib.metadata.distributions():
        name = re.sub(r"[-_.]+", "-", item.metadata["Name"]).lower()
        if name in values:
            raise ValueError("wangp_duplicate_distribution")
        values[name] = item.version
    return dict(sorted(values.items()))


def system_packages():
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(sorted(line.split("\t", 1) for line in result.stdout.splitlines()))


def safe_system_package_diagnostics(value):
    """Finite field/length whitelist for untrusted remote package diagnostics."""
    if not isinstance(value, dict) or not isinstance(value.get("mismatches"), list):
        return None
    total = value.get("total")
    if type(total) is not int or not 0 <= total <= MAX_SYSTEM_PACKAGES:
        return None
    rows = []
    for item in value["mismatches"][:MAX_PACKAGE_MISMATCHES]:
        if not isinstance(item, dict):
            continue
        name, expected, observed = (
            item.get(k) for k in ("package", "expected", "observed")
        )
        if (
            not isinstance(name, str)
            or not re.fullmatch(PACKAGE_NAME_PATTERN, name)
            or not isinstance(expected, str)
            or not re.fullmatch(PACKAGE_VERSION_PATTERN, expected)
            or observed is not None
            and (
                not isinstance(observed, str)
                or not re.fullmatch(PACKAGE_VERSION_PATTERN, observed)
            )
            or expected == observed
        ):
            continue
        rows.append({"package": name, "expected": expected, "observed": observed})
    if total < len(rows):
        return None
    return {
        "mismatches": rows,
        "total": total,
        "truncated": value.get("truncated") is True or total > len(rows),
    }


def system_package_diagnostics(expected, observed):
    mismatches = [
        {"package": name, "expected": version, "observed": observed.get(name)}
        for name, version in sorted(expected.items())
        if observed.get(name) != version
    ]
    return safe_system_package_diagnostics(
        {
            "mismatches": mismatches[:MAX_PACKAGE_MISMATCHES],
            "total": len(mismatches),
            "truncated": len(mismatches) > MAX_PACKAGE_MISMATCHES,
        }
    )


def validate_lock(lock):
    if (
        not isinstance(lock, dict)
        or lock.get("format") != FORMAT
        or lock.get("source_revision") != REVISION
        or not re.fullmatch(r"3\.11\.[0-9]+", lock.get("python", ""))
        or lock.get("requirements_sha256") != REQUIREMENTS_SHA
        or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", lock.get("base_image", ""))
    ):
        raise ValueError("wangp_environment_lock_invalid")
    for field in ("source_files", "installed_packages", "system_packages"):
        if not isinstance(lock.get(field), dict) or not lock[field]:
            raise ValueError("wangp_environment_lock_incomplete")
    if not isinstance(lock.get("wheels"), list) or not lock["wheels"]:
        raise ValueError("wangp_environment_lock_incomplete")
    if not re.fullmatch("[0-9a-f]{64}", lock.get("requirements_lock_sha256", "")):
        raise ValueError("wangp_environment_lock_invalid")

    def record(value):
        if (
            not isinstance(value, dict)
            or type(value.get("size_bytes")) is not int
            or value["size_bytes"] < 0
            or not isinstance(value.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", value["sha256"])
        ):
            raise ValueError("wangp_environment_file_record_invalid")

    for name, value in lock["source_files"].items():
        safe_relative(name)
        record(value)
    seen = set()
    for value in lock["wheels"]:
        record(value)
        name = value.get("file")
        relative = safe_relative(name)
        if (
            len(relative.parts) != 1
            or not name.endswith(".whl")
            or name in seen
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value.get("name", ""))
            or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", value.get("version", ""))
        ):
            raise ValueError("wangp_environment_wheel_record_invalid")
        seen.add(name)
    for field in ("installed_packages", "system_packages"):
        for name, version in lock[field].items():
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(version, str)
                or not version
                or any(c.isspace() for c in name + version)
            ):
                raise ValueError("wangp_environment_package_record_invalid")
    if not isinstance(lock.get("debs", []), list):
        raise ValueError("wangp_environment_deb_record_invalid")
    for value in lock.get("debs", []):
        record(value)
        relative = safe_relative(value.get("file"))
        if (
            len(relative.parts) != 1
            or not value["file"].endswith(".deb")
            or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", value.get("name", ""))
            or not re.fullmatch(r"[a-z0-9-]+", value.get("architecture", ""))
            or not isinstance(value.get("version"), str)
            or any(c.isspace() for c in value["version"])
        ):
            raise ValueError("wangp_environment_deb_record_invalid")
    return lock


def verify_source(root, lock):
    validate_lock(lock)
    for name, expected in lock["source_files"].items():
        path = regular_file(root, name)
        if (
            path.stat().st_size != expected["size_bytes"]
            or sha_file(path) != expected["sha256"]
        ):
            raise ValueError("wangp_runtime_source_modified")
    # No unrecorded source may shadow verified modules. Do not follow links.
    import os

    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(parent) / name).is_symlink():
                raise ValueError("wangp_runtime_source_link")
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or (
                path.suffix.lower() in {".py", ".so", ".pyd"}
                and relative not in lock["source_files"]
            ):
                raise ValueError("wangp_runtime_untracked_code")


def verify_environment(lock):
    validate_lock(lock)
    if (
        platform.system() != "Linux"
        or platform.machine() not in {"x86_64", "AMD64"}
        or platform.python_version() != lock["python"]
    ):
        raise ValueError("wangp_runtime_platform_mismatch")
    if installed_packages() != lock["installed_packages"]:
        raise ValueError("wangp_runtime_dependency_mismatch")
    observed_system = system_packages()
    if any(
        observed_system.get(name) != version
        for name, version in lock["system_packages"].items()
    ):
        raise ValueError("wangp_runtime_system_mismatch")
    return {
        "environment_lock_sha256": digest(lock),
        "python": lock["python"],
        "package_count": len(lock["installed_packages"]),
        "system_packages_sha256": digest(observed_system),
        "additional_system_packages": sorted(
            set(observed_system) - set(lock["system_packages"])
        ),
        "inference_verified": False,
    }


def verify_bound_environment(runtime_root, manifest):
    path = regular_file(runtime_root, ".sixnine-environment.json")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("wangp_environment_lock_limit")
    lock = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get(
        "runtime_digest_kind"
    ) != "sixnine-environment-lock-sha256" or digest(lock) != manifest.get(
        "runtime_digest"
    ):
        raise ValueError("wangp_runtime_environment_binding_mismatch")
    verify_source(runtime_root, lock)
    return verify_environment(lock)

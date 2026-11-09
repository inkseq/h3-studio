"""Scan new commits for leaked credentials with a pinned, sha256-checked gitleaks (#976).

CI runs it on every push and pull request:

    python tools/secret_scan.py --base <sha>   # scan <base>..HEAD
    python tools/secret_scan.py                # scan HEAD only

Findings are always redacted; the report never contains the matched value.
Rules: the gitleaks defaults plus `.gitleaks.toml` (content digests and the
public analytics key are allowed). A real finding means: rotate the key first
(`tools/rotate_key.py`), then remove it from the branch.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

VERSION = "8.30.1"
ASSET = f"gitleaks_{VERSION}_linux_x64.tar.gz"
URL = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{ASSET}"
SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
ROOT = Path(__file__).resolve().parents[1]
ZERO = re.compile(r"0{40}")


def fetch(*, opener=urllib.request.urlopen, sleep=time.sleep, attempts=5):
    for attempt in range(attempts):
        try:
            data = opener(URL, timeout=60).read()
            break
        except OSError:
            if attempt == attempts - 1:
                raise SystemExit("secret_scan_download_failed") from None
            sleep(3 * 2**attempt)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit("secret_scan_checksum_mismatch")
    return data


def install(directory, data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile():
            raise SystemExit("secret_scan_archive_invalid")
        target = Path(directory) / "gitleaks"
        target.write_bytes(archive.extractfile(member).read())
    os.chmod(target, 0o755)
    return target


def log_range(base):
    if base and not ZERO.fullmatch(base):
        if not re.fullmatch(r"[0-9a-f]{7,40}", base):
            raise SystemExit("secret_scan_base_invalid")
        return f"{base}..HEAD"
    return "-1"  # A new branch or manual run: the tip commit only.


def command(binary, base):
    return [
        str(binary),
        "git",
        str(ROOT),
        "--log-opts=" + log_range(base),
        "--config",
        str(ROOT / ".gitleaks.toml"),
        "--redact=100",
        "--no-banner",
        "--exit-code",
        "1",
    ]


def main(argv=None, *, run=subprocess.run, fetcher=fetch):
    parser = argparse.ArgumentParser(description="Scan new commits for secrets")
    parser.add_argument("--base", default="")
    parser.add_argument("--gitleaks", type=Path, help="An already verified binary")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as directory:
        binary = args.gitleaks or install(directory, fetcher())
        result = run(command(binary, args.base), check=False)
    if result.returncode:
        print(
            "secret_scan_findings: rotate the exposed key first, then remove it",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

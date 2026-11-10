"""Pinned chisel (MIT, github.com/jpillora/chisel) for dial-out GPU worker tunnels.

Used twice with the same pin (#1151):
- CI `--cache-only`: fill `.build-cache/` once on an actions-cache miss.
- `Dockerfile.dispatch`: install from the build-context copy when it matches the
  sha256, otherwise download from the GitHub release with retry and backoff.

The gzip release asset is verified before anything is written or installed.
Standard library only; it runs inside the image build.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import sys
import time
import urllib.request

VERSION = "1.12.0"
NAME = f"chisel_{VERSION}_linux_amd64.gz"
URL = f"https://github.com/jpillora/chisel/releases/download/v{VERSION}/{NAME}"
SHA256 = "f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6"
ATTEMPTS = 6


def verified(data):
    return data if hashlib.sha256(data).hexdigest() == SHA256 else None


def cached(directory):
    path = Path(directory) / NAME
    try:
        return verified(path.read_bytes()) if path.is_file() else None
    except OSError:
        return None


def download(*, opener=urllib.request.urlopen, sleep=time.sleep):
    for attempt in range(ATTEMPTS):
        try:
            data = opener(URL, timeout=120).read()
        except OSError:
            if attempt == ATTEMPTS - 1:
                raise SystemExit("chisel_download_failed") from None
            sleep(5 * 2**attempt)
            continue
        if verified(data) is None:
            raise SystemExit("chisel_checksum_mismatch")
        return data
    raise SystemExit("chisel_download_failed")


def main(argv=None, *, opener=urllib.request.urlopen, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args(argv)
    data = cached(args.cache)
    source = "cache"
    if data is None:
        data, source = download(opener=opener, sleep=sleep), "release"
        if args.cache_only:
            args.cache.mkdir(parents=True, exist_ok=True)
            (args.cache / NAME).write_bytes(data)
    if args.install is not None:
        args.install.write_bytes(gzip.decompress(data))
        os.chmod(args.install, 0o755)
    print(f"chisel {VERSION} verified from {source}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

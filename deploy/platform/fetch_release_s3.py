#!/usr/bin/python3
"""Root operator downloads a separately approved release; never self-approves."""
import json
import os
from pathlib import Path
import sys
import tempfile

import boto3
from botocore.config import Config
import release

BUCKET = 'sixnine-platform-releases-829135631045-ap-southeast-1'
REGION = 'ap-southeast-1'


def download(api, commit, name, target, maximum):
    response = api.get_object(Bucket=BUCKET, Key=f'releases/{commit}/{name}')
    stream = response['Body']
    try:
        release.require(type(response.get('ContentLength')) is int
                        and 0 < response['ContentLength'] <= maximum, 'release_object_size_invalid')
        total = 0
        with target.open('xb') as output:
            while block := stream.read(1024*1024):
                total += len(block)
                release.require(total <= maximum, 'release_object_size_exceeded')
                output.write(block)
            release.require(total == response['ContentLength'], 'release_object_truncated')
            output.flush()
            os.fsync(output.fileno())
    finally:
        stream.close()


def fetch(commit, *, root=release.ROOT, api=None):
    release.require(bool(release.SHA.fullmatch(commit)), 'invalid_commit')
    release.check_host(root)
    if api is None:
        api = boto3.client('s3', region_name=REGION, endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
            config=Config(connect_timeout=5, read_timeout=60, retries={'mode': 'standard', 'max_attempts': 3}))
    # This root-owned staging parent is outside the deployment-writable incoming.
    staging = Path(tempfile.mkdtemp(prefix='.download-', dir=root))
    target = root/'incoming'/commit
    try:
        download(api, commit, 'release-manifest.json', staging/'release-manifest.json', 16384)
        # Before the large image transfer, bind manifest to independent approval.
        release.approved_manifest(root, staging, commit)
        document = json.loads((staging/'release-manifest.json').read_text())
        names = set(document.get('files', {}))
        release.require(names in (release.FILES, release.TARGET_FILES), 'invalid_release_manifest')
        for name in sorted(names):
            download(api, commit, name, staging/name, 2*1024**3 if name == 'image.tar.gz' else 1024**2)
        expected = release.manifest(staging, commit)
        release.approved_manifest(root, staging, commit)
        if target.exists() or target.is_symlink():
            release.require(not target.is_symlink() and target.is_dir()
                            and release.manifest(target, commit) == expected, 'existing_download_differs')
            release.approved_manifest(root, target, commit)
            return
        staging.rename(target)
        release.sync_directory(target.parent)
    finally:
        if staging.exists():
            # Fresh flat private directory only; never remove other releases.
            for name in release.TARGET_FILES | {'release-manifest.json'}:
                (staging/name).unlink(missing_ok=True)
            staging.rmdir()


def main():
    try:
        release.require(len(sys.argv) == 2, 'one_commit_argument_required')
        import fcntl
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fetch(sys.argv[1])
        print('Approved bundle downloaded; no services started')
        return 0
    except Exception:
        print('Approved bundle download failed; details suppressed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Fixed SSM target for approved static UI releases; no service lifecycle calls."""
import json
from pathlib import Path
import sys
import tempfile

import boto3
from botocore.config import Config

import fetch_release_s3
import frontend_release
import release


def fetch(commit, root, *, api=None):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_frontend_commit')
    api = api or boto3.client('s3', region_name=fetch_release_s3.REGION,
        endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        config=Config(connect_timeout=10, read_timeout=60, retries={'mode': 'standard', 'total_max_attempts': 3}))
    # Existing bounded downloader already closes response bodies and fsyncs.
    staging = Path(tempfile.mkdtemp(prefix='.frontend-download-', dir=root))
    try:
        fetch_release_s3.download(api, commit, 'frontend/frontend-manifest.json',
            staging / 'frontend-manifest.json', 256 * 1024)
        frontend_release.approved(root, staging, commit)
        fetch_release_s3.download(api, commit, 'frontend/frontend.tar.gz',
            staging / 'frontend.tar.gz', 64 * 1024**2)
        return frontend_release.install_locked(root, staging, commit)
    finally:
        # Only files created in this exclusive flat private staging directory.
        for name in ('frontend-manifest.json', 'frontend.tar.gz'):
            (staging / name).unlink(missing_ok=True)
        staging.rmdir()


def deploy(commit, *, root=release.ROOT, api=None):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_frontend_commit')
    frontend_release.protected_directory(root)
    import fcntl
    with (root / 'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = fetch(commit, root, api=api)
        release.post_release_retention(root)
        return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        release.require(len(argv) == 1, 'one_frontend_commit_argument_required')
        print(json.dumps(deploy(argv[0])))
        return 0
    except Exception:
        print('Frontend deployment incomplete; details suppressed; previous API and GPU services were not restarted', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

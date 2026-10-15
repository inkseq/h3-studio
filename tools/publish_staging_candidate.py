"""Publish the test-gated main artifact tuple. This cannot approve production."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.platform import release_candidate as candidate
from tools.publish_aws_release import BUCKET, REGION


def build(commit, kind, run_id, run_attempt, *, platform=None, migration=None, frontend):
    paths = {'frontend': Path(frontend)/'frontend-manifest.json'}
    if kind == 'platform':
        paths.update(platform=Path(platform)/'release-manifest.json', migration=Path(migration)/'migration-manifest.json')
    result = {'version': 1, 'commit': commit, 'kind': kind, 'run_id': int(run_id),
        'run_attempt': int(run_attempt), 'manifests': {name: candidate.digest(path.read_bytes()) for name, path in paths.items()}}
    candidate.validate(result)
    for name, path in paths.items():
        value = json.loads(path.read_bytes())
        if value['commit'] != commit:
            raise ValueError('candidate_commit_split')
        if name == 'migration' and value['platform_manifest_sha256'] != result['manifests']['platform']:
            raise ValueError('candidate_platform_migration_split')
    return result


def publish(value, *, api=None):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    candidate.validate(value)
    # This prefix is writable only by the main/test-gated publication role;
    # the stage deployment role may invoke its fixed SSM document, not write it.
    api = api or boto3.client('s3', region_name=REGION, endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        config=Config(connect_timeout=10, read_timeout=30, retries={'mode': 'standard', 'total_max_attempts': 2}))
    raw = candidate.encode(value)
    key = 'staging-candidates/'+value['commit']+'.json'
    try:
        api.put_object(Bucket=BUCKET, Key=key, Body=raw, IfNoneMatch='*',
            ServerSideEncryption='AES256', Metadata={'sha256': candidate.digest(raw)})
    except ClientError as error:
        if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 412:
            raise
        response = api.get_object(Bucket=BUCKET, Key=key)
        try:
            previous = response['Body'].read(16385)
        finally:
            response['Body'].close()
        original = candidate.validate(json.loads(previous))
        if ({key: item for key, item in original.items() if key not in {'run_id', 'run_attempt'}}
                != {key: item for key, item in value.items() if key not in {'run_id', 'run_attempt'}}
                or candidate.encode(original) != previous):
            raise ValueError('immutable_staging_candidate_differs') from None
        raw = previous  # Same bytes on rerun; retain the original CI provenance.
    return {'commit': value['commit'], 'candidate_sha256': candidate.digest(raw), 'state': 'published_for_staging_only'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('--kind', choices=('platform', 'frontend'), required=True)
    parser.add_argument('--platform')
    parser.add_argument('--migration')
    parser.add_argument('--frontend', required=True)
    args = parser.parse_args(argv)
    if os.environ.get('GITHUB_EVENT_NAME') != 'push' or os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_SHA') != args.commit:
        raise SystemExit('staging_candidate_requires_tested_main_push')
    value = build(args.commit, args.kind, os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'],
        platform=args.platform, migration=args.migration, frontend=args.frontend)
    print(json.dumps(publish(value)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Target-specific entrypoints around the existing approved deployers; no builds.

Staging trusts only the restricted, immutable CI-publication prefix. Production
additionally requires the operator's independent protected acceptance approval.
Install this with the existing reviewed helpers, never execute it from a bundle.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import deploy_approved
import deploy_frontend
import deployment_target
import fetch_release_s3
import frontend_release
import release
import release_candidate


def read_object(api, key, maximum=16384):
    response = api.get_object(Bucket=fetch_release_s3.BUCKET, Key=key)
    try:
        raw = response['Body'].read(maximum+1)
        release.require(type(response.get('ContentLength')) is int
            and len(raw) == response['ContentLength'] and 0 < len(raw) <= maximum, 'candidate_object_invalid')
        return raw
    finally:
        response['Body'].close()


def publication(api, commit):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_commit')
    raw = read_object(api, 'staging-candidates/'+commit+'.json')
    value = release_candidate.validate(json.loads(raw))
    release.require(value['commit'] == commit and release_candidate.encode(value) == raw, 'candidate_commit_or_encoding_invalid')
    return value


def approvals(root, value, api):
    """Approve only exact manifest bytes from the immutable candidate tuple."""
    names = {'platform': ('release-manifest.json', 'approved-releases'),
        'migration': ('migration/migration-manifest.json', 'approved-migrations'),
        'frontend': ('frontend/frontend-manifest.json', 'approved-frontends')}
    commit = value['commit']
    for name, digest in value['manifests'].items():
        remote, folder = names[name]
        raw = read_object(api, 'releases/'+commit+'/'+remote, 256*1024)
        release.require(release_candidate.digest(raw) == digest and json.loads(raw).get('commit') == commit,
            'candidate_manifest_changed')
        directory = root/folder
        frontend_release.protected_directory(directory, create=True)
        # Existing exact approvals are reusable; no different approval is replaced.
        frontend_release.immutable_file(directory/(commit+'.sha256'), (digest+'\n').encode())


def retention_intent(root, value, *, completed=False):
    """Only a retention pin for the existing deployers, never deployment approval."""
    path = root / 'environment-release-intent.json'
    expected = {'version': 1, 'commit': value['commit'],
        'candidate_sha256': release_candidate.digest(release_candidate.encode(value)), 'state': 'in_progress'}
    if path.exists() or path.is_symlink():
        prior = release._protected_json(path)
        release.require(set(prior) == set(expected) and type(prior['version']) is int and prior['version'] == 1
            and type(prior['commit']) is str and release.SHA.fullmatch(prior['commit'])
            and type(prior['candidate_sha256']) is str and release.DIGEST.fullmatch(prior['candidate_sha256'])
            and prior['state'] in {'in_progress', 'completed'}, 'retention_promotion_intent_unknown')
        release.require(prior['state'] == 'completed' and not completed or prior == expected,
            'another_environment_release_requires_reconciliation')
    else:
        release.require(not completed, 'retention_promotion_intent_missing')
    frontend_release.atomic_file(path, (json.dumps({**expected,
        'state': 'completed' if completed else 'in_progress'}, sort_keys=True) + '\n').encode())


def run(target, commit, *, root=release.ROOT, api=None):
    sealed = deployment_target.host(root)
    release.require(target in {'staging', 'production'} and sealed['name'] == target, 'environment_release_wrong_host')
    if api is None:
        import boto3
        from botocore.config import Config
        api = boto3.client('s3', region_name=fetch_release_s3.REGION, endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
            config=Config(connect_timeout=10, read_timeout=30, retries={'mode': 'standard', 'total_max_attempts': 3}))
    value = publication(api, commit)
    if target == 'production':
        path = root/'promotion-candidates'/(commit+'.json')
        acceptance = release._protected_json(path)
        release_candidate.acceptance(acceptance, value)
        approval = root/'approved-promotions'/(commit+'.sha256')
        release.regular(approval, root_owned=True, maximum=128)
        release.require(approval.read_text().strip() == release.checksum(path), 'independent_promotion_approval_required')
    else:
        # A stage host has no authority to install production approvals.
        release.require(sealed['origin'] == 'https://staging.inkseq.com', 'staging_target_invalid')
    import fcntl
    with (root/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        approvals(root, value, api)
        retention_intent(root, value)
    if value['kind'] == 'platform':
        deploy_approved.deploy(commit, root=root)
    deploy_frontend.deploy(commit, root=root, api=api)
    with (root/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retention_intent(root, value, completed=True)
        release.post_release_retention(root)
    return {'state': 'staging_release_healthy' if target == 'staging' else 'accepted_artifacts_promoted',
        'commit': commit, 'candidate_sha256': release_candidate.digest(release_candidate.encode(value))}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        release.require(len(argv) == 2, 'environment_and_commit_required')
        print(json.dumps(run(*argv)))
        return 0
    except Exception:
        print('Environment release incomplete; preserve and inspect the original candidate/command', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""OIDC runner submits only the reviewed fixed SSM document; no host approval."""
import argparse
import json
import re
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = 'ap-southeast-1'
INSTANCE = 'i-03d81d2d153b5e2fd'
DOCUMENT = 'Sixnine-DeployApprovedRelease'
STAGING_INSTANCE = 'i-07805df7bd6984bd9'
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
COMMAND_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
# The host helper's only stderr shape: one closed code and a fixed sentence.
HOST_REFUSAL = re.compile(r'([a-z][a-z0-9_]{0,79}); preserve and inspect the original candidate/command\s')
LOCK_BUSY = 'environment_release_lock_busy'
LOCK_RETRY_SECONDS = 60


def host_code(invocation):
    found = HOST_REFUSAL.match(invocation.get('StandardErrorContent') or '')
    return found.group(1) if found else 'unknown'


def deploy(commit, *, resume_command=None, frontend=False, target='production', promote=False,
        timeout=1900, api=None, clock=time.monotonic, sleep=time.sleep):
    if not COMMIT.fullmatch(commit) or resume_command is not None and not COMMAND_ID.fullmatch(resume_command):
        raise ValueError('Exact commit and valid optional command ID required')
    document = 'Sixnine-DeployApprovedFrontend' if frontend else DOCUMENT
    plugin = 'deployApprovedFrontend' if frontend else 'deployApprovedCommit'
    receipt_state = 'approved_frontend_release_healthy' if frontend else 'approved_application_release_healthy'
    if target not in {'production', 'staging'} or promote and (target != 'production' or frontend):
        raise ValueError('Invalid closed deployment target/action')
    instance = INSTANCE
    environment_action = target == 'staging' or promote
    document_version = '1' if environment_action else '2'
    if target == 'staging':
        if frontend or not isinstance(STAGING_INSTANCE, str) or not re.fullmatch(r'i-[0-9a-f]{17}', STAGING_INSTANCE):
            raise ValueError('Dedicated staging target is not bound')
        instance, document, plugin, receipt_state = (STAGING_INSTANCE, 'Inkseq-DeployStagingRelease',
            'deployStagingCommit', 'staging_release_healthy')
    elif promote:
        document, plugin, receipt_state = ('Inkseq-PromoteAcceptedRelease', 'promoteAcceptedCommit', 'accepted_artifacts_promoted')
    api = api or boto3.client('ssm', region_name=REGION, endpoint_url='https://ssm.ap-southeast-1.amazonaws.com',
        config=Config(connect_timeout=10, read_timeout=30, retries={'mode': 'standard', 'total_max_attempts': 1}))
    def submit():
        # SendCommand has no idempotency token. Do not retry a lost response
        # automatically: the same approved commit is idempotent at the host.
        try:
            response = api.send_command(InstanceIds=[instance], DocumentName=document, DocumentVersion=document_version,
                Parameters={'Commit': [commit]}, TimeoutSeconds=120, MaxConcurrency='1', MaxErrors='0',
                Comment='Deploy independently approved Sixnine commit '+commit)
            submitted = response['Command']['CommandId']
        except Exception:
            raise RuntimeError('Submission outcome unknown; operator must reconcile the same commit before resubmission') from None
        if not COMMAND_ID.fullmatch(submitted):
            raise RuntimeError('Deployment command response invalid; reconcile host state')
        return submitted

    command_id = resume_command or submit()
    print(json.dumps({'state': 'deployment_command_pending', 'commit': commit, 'command_id': command_id}), flush=True)
    deadline = clock()+timeout
    while clock() < deadline:
        try:
            invocation = api.get_command_invocation(CommandId=command_id, InstanceId=instance,
                                                    PluginName=plugin)
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') != 'InvocationDoesNotExist':
                raise RuntimeError('Command status unavailable; resume the same command ID') from None
            sleep(5)
            continue
        status = invocation.get('Status')
        if status == 'Success':
            # Never print arbitrary SSM stdout/stderr. Validate the fixed receipt
            # so --resume-command cannot claim success for another commit/task.
            if invocation.get('DocumentName') != document or invocation.get('DocumentVersion') != document_version:
                raise RuntimeError('Command is not the reviewed deployment document version')
            try:
                receipt = json.loads(invocation.get('StandardOutputContent', ''))
            except ValueError:
                raise RuntimeError('Deployment success receipt invalid') from None
            expected_keys = {'state', 'commit', 'candidate_sha256'} if environment_action else {'state', 'commit'}
            if (type(receipt) is not dict or set(receipt) != expected_keys or receipt['state'] != receipt_state
                    or receipt['commit'] != commit or environment_action
                    and not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('candidate_sha256')))):
                raise RuntimeError('Deployment success receipt does not match this commit')
            result = {'state': 'deployment_completed', 'commit': commit, 'command_id': command_id}
            print(json.dumps(result))
            return result
        if status in {'Cancelled', 'TimedOut', 'Failed', 'Cancelling'}:
            code = host_code(invocation) if status == 'Failed' else status.lower()
            if code == LOCK_BUSY and target == 'staging' and clock()+LOCK_RETRY_SECONDS < deadline:
                # The helper refused before any change: another release or a
                # maintenance run holds release.lock. Queue behind it instead.
                print(json.dumps({'state': 'release_lock_busy_waiting', 'commit': commit}), flush=True)
                sleep(LOCK_RETRY_SECONDS)
                command_id = submit()
                print(json.dumps({'state': 'deployment_command_pending', 'commit': commit,
                                  'command_id': command_id}), flush=True)
                continue
            raise RuntimeError(f'Deployment did not complete ({code}); preserve host state and reconcile the same commit')
        if status not in {'Pending', 'InProgress', 'Delayed'}:
            raise RuntimeError('Unknown deployment status; resume the same command ID')
        sleep(5)
    raise RuntimeError('Polling deadline reached; command was not cancelled; resume the same command ID')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('--resume-command')
    parser.add_argument('--frontend', action='store_true', help='Deploy approved static frontend without restarting services')
    parser.add_argument('--target', choices=('production', 'staging'), default='production')
    parser.add_argument('--promote', action='store_true', help='Promote independently accepted staging artifact bytes; never build')
    args = parser.parse_args(argv)
    try:
        deploy(args.commit, resume_command=args.resume_command, frontend=args.frontend, target=args.target, promote=args.promote)
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Deployment status unavailable; details suppressed; reconcile original command', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

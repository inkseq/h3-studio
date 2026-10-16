#!/usr/bin/python3
"""Daily private snapshot and off-device copy; never starts app/provider work.

Install in its own protected directory. Host role is available only to the
short-lived transport container; the application retains its existing identity.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

ROOT = Path('/srv/sixnine')
STATE = Path('/var/lib/inkseq-business-backup')
CONFIG = Path('/etc/inkseq/business-backup.json')
HELPERS = Path('/opt/inkseq-release-staging-v1')
HOSTS = {'staging': 'i-07805df7bd6984bd9', 'production': 'i-03d81d2d153b5e2fd'}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}

def require(condition, code):
    if not condition:
        raise ValueError(code)

def protected(path, directory=False):
    path = Path(path)
    for parent in path.parents:
        s = parent.lstat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022, 'backup_parent_unprotected')
    s = path.lstat()
    require((stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1)
        and s.st_uid == 0 and not s.st_mode & 0o022, 'backup_path_unprotected')
    return path

def read(path):
    p = protected(path)
    require(p.stat().st_size < 65536, 'backup_json_oversize')
    return json.loads(p.read_bytes())

def record(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    fd = os.open(Path(path).parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)

def new_directory(path):
    path.mkdir(mode=0o700)
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)

def docker(args, *, timeout=30):
    r = subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', *args],
        env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    require(r.returncode == 0 and len(r.stdout) < 65536, 'backup_docker_failed_or_unknown')
    return r.stdout.decode()

def validate_config(value, host):
    require(type(value) is dict and type(value.get('version')) is int and value['version'] in (1, 2, 3), 'backup_config_fields')
    fields = {'version', 'environment', 'instance_id', 'target'} | ({'allow_missing_media'} if value['version'] >= 2 else set())
    if value['version'] == 3: fields.add('classify_expected_deletions')
    require(value['version'] != 3 or value.get('classify_expected_deletions') is True, 'backup_config_fields')
    require(set(value) == fields and (value['version'] == 1 or value['allow_missing_media'] is True), 'backup_config_fields')
    require(type(value['version']) is int and value['version'] in (1, 2, 3) and value['environment'] in HOSTS
        and value['instance_id'] == host == HOSTS[value['environment']], 'backup_host_mismatch')
    t = value['target']
    require(type(t) is dict and set(t) == {'account_id', 'region', 'bucket', 'kms_key_arn', 'role_arn'}, 'backup_target_fields')
    require(t['account_id'] == '829135631045' and t['region'] == 'ap-southeast-1', 'backup_account_region')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', t['bucket'])
        and 'business-backup' in t['bucket'], 'backup_bucket')
    require(re.fullmatch(r'arn:aws:kms:ap-southeast-1:829135631045:key/[0-9a-f-]{36}', t['kms_key_arn']), 'backup_key')
    require(re.fullmatch(r'arn:aws:iam::829135631045:role/[A-Za-z0-9/+=,.@_-]+', t['role_arn']), 'backup_role')
    return value

def coverage(value):
    c = value.get('coverage')
    if value.get('version', 1) == 1 and value.get('format', 'sixnine-private-backup-copy-v1') == 'sixnine-private-backup-copy-v1':
        require(c is None, 'backup_coverage_invalid'); return {}
    classified = value.get('version') == 3 or value.get('format') == 'sixnine-private-backup-copy-v3'
    fields = {'status','referenced_objects','available_objects','missing_objects','referenced_bytes','available_bytes','missing_bytes'}
    if classified: fields |= {'expected_missing_objects','expected_missing_bytes','unexpected_missing_objects','unexpected_missing_bytes','loss_alert_required'}
    require(type(c) is dict and set(c) == fields and all(type(c[k]) is int and c[k] >= 0 for k in fields-{'status','loss_alert_required'}), 'backup_coverage_invalid')
    require(c['referenced_objects'] == c['available_objects']+c['missing_objects'] and c['referenced_bytes'] == c['available_bytes']+c['missing_bytes']
        and c['status'] == ('incomplete' if c['missing_objects'] else 'complete') and (c['missing_objects']==0)==(c['missing_bytes']==0), 'backup_coverage_invalid')
    if classified:
        require(type(c['loss_alert_required']) is bool and c['loss_alert_required'] == (c['unexpected_missing_objects'] > 0)
            and c['missing_objects'] == c['expected_missing_objects']+c['unexpected_missing_objects']
            and c['missing_bytes'] == c['expected_missing_bytes']+c['unexpected_missing_bytes']
            and all((c[k+'_objects']==0)==(c[k+'_bytes']==0) for k in ('expected_missing','unexpected_missing')), 'backup_coverage_invalid')
    if 'objects' in value or 'bytes' in value:
        require(type(value.get('objects')) is int and type(value.get('bytes')) is int
            and c['available_objects'] == value['objects'] and c['available_bytes'] <= value['bytes'], 'backup_coverage_invalid')
    return {'coverage': c}


def copy_receipt(path, target):
    for p in [path, *path.parents]:
        s = p.lstat()
        require(not p.is_symlink() and s.st_uid in (0, 10001) and not s.st_mode & 0o022,
            'backup_receipt_path_unprotected')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size <= 16384, 'backup_receipt_invalid')
        value = json.loads(f.read(16385))
    require(set(value) == {'format', 'target', 'phase', 'completion_version_id', 'completion_sha256',
        'completion_bytes', 'snapshot_manifest_sha256', 'objects', 'bytes', 'restore_verified'} | set(coverage(value)), 'backup_receipt_fields')
    require(value['target'] == target and value['format'] in ('sixnine-private-backup-copy-v1', 'sixnine-private-backup-copy-v2', 'sixnine-private-backup-copy-v3')
        and value['phase'] == 'copied_not_restored' and value['restore_verified'] is False, 'backup_completion_mismatch')
    require(all(type(value[k]) is int and value[k] > 0 for k in ('bytes','completion_bytes'))
        and type(value['objects']) is int and value['objects'] >= (0 if value['format'] != 'sixnine-private-backup-copy-v1' else 1)
        and all(isinstance(value[k],str) and re.fullmatch('[0-9a-f]{64}', value[k])
            for k in ('completion_sha256','snapshot_manifest_sha256'))
        and isinstance(value['completion_version_id'], str) and 1 <= len(value['completion_version_id']) <= 1024,
        'backup_receipt_values')
    return value

def freeze_transport_snapshot(snapshot):
    """Private transport-readable copy, inaccessible through the host parent.

    The app shares uid10001 with the restricted transport, so the enclosing
    root-owned 0700 operation directory must remain a traversal barrier.
    Only the exact snapshot is mounted read-only into the transport container.
    """
    protected(snapshot, True)
    protected(snapshot.parent, True)
    require(stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700, 'backup_operation_not_private')
    entries = [snapshot]
    for parent, directories, files in os.walk(snapshot, followlinks=False):
        for name in directories + files:
            p = Path(parent)/name; info = p.lstat(); entries.append(p)
            require(len(entries) <= 100005 and info.st_uid == 0 and not p.is_symlink()
                and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
                'backup_private_copy_invalid')
    # Validate the entire copied tree before changing ownership or modes.
    for p in reversed(entries):
        directory = p.is_dir()
        os.chown(p, 10001, 10001)
        p.chmod(0o500 if directory else 0o400)


def transport(image, name, mounts, args):
    command = ['run', '--pull', 'never', '--name', name, '--network', 'host', '--read-only',
        '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--memory', '512m', '--pids-limit', '64', '--tmpfs', '/tmp:size=67108864,mode=1777']
    for source, target, readonly in mounts:
        command += ['--mount', 'type=bind,src='+str(source)+',dst='+target+(',readonly' if readonly else '')]
    return command + ['--env', 'AWS_EC2_METADATA_DISABLED=false', '--env', 'AWS_CONFIG_FILE=/nonexistent',
        '--env', 'AWS_SHARED_CREDENTIALS_FILE=/nonexistent', image,
        'python', '-B', '-m', 'studio_platform.backup_remote_cli', *args, '--instance-role-only']

def approved_image(release, directory, manifest, commit, *, pinned_image=None):
    """Reuse release validation for both OCI manifest and Docker config IDs."""
    identities = release.validate_image_archive(directory/'image.tar.gz', manifest)
    reference = pinned_image or manifest['image']
    value = json.loads(docker(['image', 'inspect', '--format',
        '{"id":{{json .Id}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}', reference]))
    require(value.get('id') in identities and value.get('revision') == commit
        and (pinned_image is None or value.get('id') == pinned_image), 'backup_image_not_in_approved_archive')
    return value['id']

def check_history(directory, day):
    """No new prefix while any earlier upload outcome remains uncertain."""
    for prior in directory.glob('????-??-??'):
        protected(prior, True)
        need = prior/'completed.json'
        require(need.exists(), 'backup_prior_outcome_requires_reconciliation')
        value = read(need)
        require(type(value.get('version')) is int and value['version'] in (1, 2, 3) and value.get('day') == prior.name
            and all(isinstance(value.get(k), str) and re.fullmatch('[0-9a-f]{64}', value[k])
                for k in ('completion_sha256', 'receipt_sha256')),
            'backup_prior_receipt_invalid')
        coverage(value)
    return (directory/day).exists()

def completed_summary(operation, day):
    return {'state': 'already_completed', 'day': day, **coverage(read(operation/'completed.json'))}


def run():
    require(os.geteuid() == 0, 'backup_root_required')
    cfg = validate_config(read(CONFIG), Path('/var/lib/cloud/data/instance-id').read_text().strip())
    protected(STATE, True); protected(HELPERS, True)
    # Load only already installed root-controlled release validation helpers.
    sys.path.insert(0, str(HELPERS))
    import release
    day = datetime.now(timezone.utc).date().isoformat()
    lockfd = os.open(protected(ROOT/'release.lock'), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(ROOT/'release-state.json')
        commit = state.get('current')
        require(isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}', commit)
            and not state.get('pending') and state.get('status') == 'app_ready', 'backup_release_not_ready')
        directory = ROOT/'releases'/commit
        release.approved_manifest(ROOT, directory, commit)
        manifest = release.manifest(directory, commit)
        # A previous unknown transfer blocks new prefixes: reconciliation first.
        operation = STATE/day
        if check_history(STATE, day):
            return completed_summary(operation, day)
        require(shutil.disk_usage(ROOT).free >= 5*1024**3, 'backup_disk_reserve_required')
        ids = docker(['ps', '--quiet', '--filter', 'label=com.docker.compose.project=sixnine-platform',
            '--filter', 'label=com.docker.compose.service=app']).split()
        require(len(ids) == 1 and re.fullmatch('[0-9a-f]{12,64}', ids[0]), 'backup_app_identity')
        template = '{"image":{{json .Image}},"running":{{json .State.Running}},"mounts":{{json .Mounts}}}'
        app = json.loads(docker(['inspect', '--format', template, ids[0]]))
        image = approved_image(release, directory, manifest, commit)
        require(app['image'] == image and app['running'] is True, 'backup_app_image_mismatch')
        require(any(m['Destination'] == '/data' and m['Source'] == str(ROOT/'platform-data')
            and m['Type'] == 'bind' for m in app['mounts']), 'backup_data_mount_mismatch')
        nonce = str(uuid.uuid4())
        new_directory(operation)
        record(operation/'intent.json', {'version': cfg['version'], 'day': day, 'commit': commit, 'image': image,
            'operation_id': nonce, 'automatic_retry': False})
        snapshot_host = ROOT/'platform-data'/'business-backups'/nonce
        snapshot_container = '/data/business-backups/'+nonce
        # Parent is created by the existing app user; snapshot itself is exclusive.
        docker(['exec', ids[0], 'python', '-B', '-c',
            "from pathlib import Path;p=Path('/data/business-backups');p.mkdir(mode=0o700,exist_ok=True);assert p.is_dir() and not p.is_symlink()"])
        docker(['exec', ids[0], 'python', '-B', '-m', 'studio_platform.backup_cli', 'configured',
            '--destination', snapshot_container] + (['--allow-missing-media'] if cfg['version'] >= 2 else []) + (['--classify-expected-deletions'] if cfg['version'] == 3 else []), timeout=900)
        # Private handoff: cp without -L retains symlinks, which are rejected.
        # The app cannot replace this root-controlled transport mount source.
        snapshot_host = operation/'snapshot'
        new_directory(snapshot_host)
        docker(['cp', ids[0]+':'+snapshot_container+'/.', str(snapshot_host)], timeout=900)
        freeze_transport_snapshot(snapshot_host)
        target = {**cfg['target'], 'prefix': 'business-backups/'+nonce}
        record(operation/'target.json', target)
        (operation/'target.json').chmod(0o644)  # Explicit public identifiers only.
        # Root-owned operation metadata is mounted read-only. Transfer output is
        # a separate, new directory; no app DB, R2 identity or host socket mounts.
        transfer = operation/'output'; new_directory(transfer)
        os.chown(transfer, 10001, 10001)
        name = 'inkseq-backup-transfer-'+nonce
        docker(transport(image, name, [(snapshot_host, '/snapshot', True),
            (operation/'target.json', '/target.json', True), (transfer, '/output', False)],
            ['copy', '--execute', '--target', '/target.json', '--backup', '/snapshot', '--receipts', '/output/transfer'] + (['--allow-incomplete'] if cfg['version'] >= 2 else [])), timeout=1800)
        receipt = copy_receipt(transfer/'transfer'/'completion.json', target)
        require(receipt['format'] == 'sixnine-private-backup-copy-v'+str(cfg['version']), 'backup_coverage_config_mismatch')
        record(operation/'receipt.json', receipt)
        (operation/'receipt.json').chmod(0o644)  # Nonsecret hash/version/target identities only.
        record(operation/'completed.json', {'version': cfg['version'], 'day': day, 'commit': commit,
            'completion_sha256': receipt['completion_sha256'], 'completion_version_id': receipt['completion_version_id'],
            'receipt_sha256': hashlib.sha256((operation/'receipt.json').read_bytes()).hexdigest(),
            'restore_verified': False, **coverage(receipt)})
        return {'state': 'copied_not_restored', 'day': day, **coverage(receipt)}
    finally:
        os.close(lockfd)

def restore(day, *, allow_incomplete=False):
    """Explicit recovery drill: remote pinned versions to a new private copy.

    No database credentials, app data mount, API/worker or live writes. Retain
    intent/output on both success and uncertainty. No automatic cleanup/replay.
    """
    require(os.geteuid() == 0 and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day), 'backup_restore_day_invalid')
    cfg = validate_config(read(CONFIG), Path('/var/lib/cloud/data/instance-id').read_text().strip())
    require(cfg['environment'] == 'staging', 'restore_drill_requires_staging')
    protected(STATE, True); protected(HELPERS, True)
    sys.path.insert(0, str(HELPERS))
    import release
    operation = protected(STATE/day, True)
    complete = read(operation/'completed.json'); intent = read(operation/'intent.json')
    receipt = read(operation/'receipt.json'); target = read(operation/'target.json')
    require(hashlib.sha256((operation/'receipt.json').read_bytes()).hexdigest() == complete['receipt_sha256']
        and receipt['completion_sha256'] == complete['completion_sha256']
        and receipt['completion_version_id'] == complete['completion_version_id']
        and receipt['target'] == target and target == {**cfg['target'], 'prefix': 'business-backups/'+intent['operation_id']},
        'restore_receipt_identity_mismatch')
    require(type(allow_incomplete) is bool and (allow_incomplete or coverage(complete).get('coverage', {}).get('status') != 'incomplete'), 'restore_incomplete_requires_explicit_opt_in')
    require(coverage(complete) == coverage(receipt), 'restore_coverage_mismatch')
    commit = intent['commit']; directory = ROOT/'releases'/commit
    require(re.fullmatch('[0-9a-f]{40}', commit), 'restore_commit_invalid')
    release.approved_manifest(ROOT, directory, commit)
    manifest = release.manifest(directory, commit)
    approved_image(release, directory, manifest, commit, pinned_image=intent['image'])
    require(shutil.disk_usage(STATE).free > 3*receipt['bytes']+512*1024**2, 'restore_disk_reserve_required')
    destination = STATE/('restore-'+day)
    new_directory(destination)  # Existing or uncertain operation never replays.
    record(destination/'intent.json', {'version': 1, 'day': day, 'receipt_sha256': complete['receipt_sha256'],
        'commit': commit, 'image': intent['image'], 'live_database_used': False})
    output = destination/'output'; new_directory(output); os.chown(output, 10001, 10001)
    docker(transport(intent['image'], 'inkseq-backup-restore-'+day, [
        (operation/'target.json','/target.json',True), (operation/'receipt.json','/receipt.json',True),
        (output,'/output',False)], ['restore','--execute','--target','/target.json','--receipt','/receipt.json',
        '--download','/output/download','--destination','/output/restored'] + (['--allow-incomplete'] if allow_incomplete else [])), timeout=1800)
    record(destination/'completed.json', {'state':'verified_isolated_restore', 'day':day,
        'receipt_sha256':complete['receipt_sha256'], 'live_database_used':False, 'execution_enabled':False, **coverage(complete)})
    return {'state':'verified_isolated_restore','day':day,'execution_enabled':False, **coverage(complete)}

if __name__ == '__main__':
    try:
        require(len(sys.argv) == 1 or len(sys.argv) in (3, 4) and sys.argv[1] == '--restore-day' and (len(sys.argv) == 3 or sys.argv[3] == '--allow-incomplete'), 'backup_fixed_invocation_required')
        print(json.dumps(run() if len(sys.argv) == 1 else restore(sys.argv[2], allow_incomplete=len(sys.argv) == 4)))
    except Exception:
        print('{"state":"backup_failed_or_unknown","automatic_retry":false}', file=sys.stderr)
        raise SystemExit(1) from None

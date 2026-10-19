#!/usr/bin/python3
"""Daily private snapshot and off-device copy; never starts app/provider work.

Install in its own protected directory. Host role is available only to the
short-lived transport container; the application retains its existing identity.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
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


def require_hosted_coverage(operation):
    """Warn only after the original business transfer is durably recorded."""
    manifest = operation/'snapshot'/'manifest.json'
    if manifest.exists():
        hosted = read(manifest).get('hosted_users')
        require(hosted is None or isinstance(hosted, dict) and hosted.get('status') == 'complete',
            'backup_hosted_users_unavailable_business_confirmed')


def finish_daily(operation, summary, cfg, image):
    result = {**summary, 'retention':retention_after_success(cfg,image)}
    require_hosted_coverage(operation)
    return result



MAX_RETENTION_JSON = 16 * 1024**2
MAX_RETENTION_FILES = 100003
UUID4 = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def unique_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, 'backup_retention_duplicate_json_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: require(False, 'backup_retention_nonfinite_json'))


def retention_json(path, *, root_only=False, maximum=MAX_RETENTION_JSON):
    # The outer operation remains a root-owned traversal barrier. The original
    # transfer intent/report is uid10001-owned, never a host credential mount.
    anchor = STATE if path.is_relative_to(STATE) else ROOT if path.is_relative_to(ROOT) else None
    require(anchor is not None,'backup_retention_json_scope')
    for parent in [path.parent, *path.parent.parents]:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in ((0,) if root_only else (0,10001))
            and not info.st_mode & 0o022, 'backup_retention_json_parent')
        if parent == anchor: break  # Trusted root was checked before taking the release lock.
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
        and before.st_uid in ((0,) if root_only else (0,10001)) and not before.st_mode & 0o022
        and 0 < before.st_size <= maximum,'backup_retention_json_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        opened = os.fstat(source.fileno())
        require(identity(opened) == identity(before) and stat.S_ISREG(opened.st_mode)
            and opened.st_nlink == 1 and opened.st_uid in ((0,) if root_only else (0,10001))
            and not opened.st_mode & 0o022 and 0 < opened.st_size <= maximum, 'backup_retention_json_invalid')
        data = source.read(maximum+1)
        require(len(data) == opened.st_size and identity(os.fstat(source.fileno())) == identity(opened)
            and identity(path.lstat()) == identity(before), 'backup_retention_json_changed')
    return {'value': unique_json(data), 'sha256': hashlib.sha256(data).hexdigest(), 'identity': identity(before)}


def retention_operation(operation, cfg):
    day = operation.name
    require(datetime.strptime(day, '%Y-%m-%d').date().isoformat() == day, 'backup_retention_day')
    protected(operation, True)
    require(stat.S_IMODE(operation.stat().st_mode) == 0o700, 'backup_operation_not_private')
    paths = {name: retention_json(operation/name, root_only=True)
        for name in ('intent.json','completed.json','target.json','receipt.json')}
    intent, complete, target, receipt = (paths[name]['value'] for name in
        ('intent.json','completed.json','target.json','receipt.json'))
    require(type(intent) is dict and set(intent) == {'version','day','commit','image','operation_id','automatic_retry'}
        and type(intent['version']) is int and intent['version'] in (1,2,3)
        and intent['day'] == complete.get('day') == day and complete.get('commit') == intent['commit']
        and isinstance(intent['commit'],str) and re.fullmatch('[0-9a-f]{40}',intent['commit'])
        and isinstance(intent['image'],str) and re.fullmatch('sha256:[0-9a-f]{64}',intent['image'])
        and isinstance(intent['operation_id'],str) and UUID4.fullmatch(intent['operation_id'])
        and intent['automatic_retry'] is False, 'backup_retention_original_intent')
    require(type(complete) is dict and set(complete) == {'version','day','commit','completion_sha256',
        'completion_version_id','receipt_sha256','restore_verified'} | set(coverage(complete))
        and type(complete['version']) is int and complete['version'] == intent['version']
        and complete['restore_verified'] is False and complete['receipt_sha256'] == paths['receipt.json']['sha256']
        and complete['completion_sha256'] == receipt.get('completion_sha256')
        and complete['completion_version_id'] == receipt.get('completion_version_id'), 'backup_retention_original_completion')
    require(target == receipt.get('target') == {**cfg['target'],'prefix':'business-backups/'+intent['operation_id']}
        and copy_receipt(operation/'receipt.json',target) == receipt
        and receipt['format'] == 'sixnine-private-backup-copy-v'+str(intent['version'])
        and coverage(complete) == coverage(receipt), 'backup_retention_original_receipt')
    return {'day':day,'operation_id':intent['operation_id'],'operation':operation,'metadata':paths,
        'target':target,'receipt':receipt,
        'app':ROOT/'platform-data'/'business-backups'/intent['operation_id'], 'transport':operation/'snapshot'}


def retention_inventory(item):
    original = retention_json(item['operation']/'output'/'transfer'/'intent.json')
    value = original['value']; receipt = item['receipt']
    require(type(value) is dict and value.get('target') == item['target']
        and value.get('format') == receipt['format'] and value.get('phase') == 'before_remote_write'
        and isinstance(value.get('operation_id'),str) and UUID4.fullmatch(value['operation_id'])
        and value.get('snapshot_manifest_sha256') == receipt['snapshot_manifest_sha256']
        and value.get('objects') == receipt['objects'] and value.get('bytes') == receipt['bytes']
        and coverage(value) == coverage(receipt), 'backup_retention_transfer_identity')
    files = value.get('files')
    require(type(files) is list and 2 <= len(files) <= MAX_RETENTION_FILES, 'backup_retention_inventory_limit')
    result = {}
    for entry in files:
        require(type(entry) is dict and set(entry) == {'name','size_bytes','sha256'}, 'backup_retention_inventory')
        name = entry['name']
        require(isinstance(name,str) and (name in ('manifest.json','database.sqlite3','workflow-state.json','workos-users.json')
            or re.fullmatch(r'media/[0-9a-f]{64}\.bin',name)) and name not in result
            and type(entry['size_bytes']) is int and 0 < entry['size_bytes'] <= 512*1024**2
            and isinstance(entry['sha256'],str) and re.fullmatch('[0-9a-f]{64}',entry['sha256']), 'backup_retention_inventory')
        require(name != 'manifest.json' or entry['size_bytes'] <= MAX_RETENTION_JSON,'backup_retention_manifest_limit')
        result[name] = entry
    require({'manifest.json','database.sqlite3'} <= set(result)
        and len(result) == receipt['objects']+2+('workflow-state.json' in result)+('workos-users.json' in result)
        and sum(v['size_bytes'] for v in result.values()) == receipt['bytes']
        and files[-1]['name'] == 'manifest.json' and result['manifest.json']['sha256'] == receipt['snapshot_manifest_sha256'],
        'backup_retention_inventory_identity')
    item['metadata']['output/transfer/intent.json'] = original
    item['files'] = result
    return original


def retention_remote(item, image, evidence):
    original = retention_inventory(item)
    output = evidence/'output'; new_directory(output); os.chown(output,10001,10001)
    docker(transport(image,'inkseq-backup-retention-'+str(uuid.uuid4()),[
        (item['operation']/'target.json','/target.json',True),
        (item['operation']/'receipt.json','/receipt.json',True),
        (item['operation']/'output'/'transfer','/receipts',True), (output,'/output',False)],
        ['reconcile','--execute','--target','/target.json','--receipt','/receipt.json',
         '--receipts','/receipts','--reconciliation','/output/reconciliation']),timeout=1800)
    report = retention_json(output/'reconciliation'/'reconciliation.json')
    value = report['value']; inventory = value.get('verified_files')
    require(value.get('classification') == 'complete' and value.get('code') == 'backup_copy_complete_bytes_verified'
        and value.get('cloud_write_operations') == 0 and value.get('automatic_retry') is False
        and value.get('restore_verified') is False and value.get('target') == item['target']
        and value.get('operation_id') == original['value']['operation_id']
        and value.get('original_intent_sha256') == hashlib.sha256(json.dumps(original['value'],
            sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        and value.get('snapshot_manifest_sha256') == item['receipt']['snapshot_manifest_sha256']
        and value.get('transfer_receipt') == item['receipt'] and value.get('missing_current_files') == []
        and type(inventory) is list and len(inventory) == len(item['files']), 'backup_retention_remote_unverified')
    require(all(type(v) is dict and set(v) == {'name','size_bytes','sha256','version_id'}
        and isinstance(v['version_id'],str) and 1 <= len(v['version_id']) <= 1024 for v in inventory)
        and [{k:v for k,v in entry.items() if k!='version_id'} for entry in inventory] == original['value']['files'],
        'backup_retention_remote_inventory')
    item['remote_report_sha256'] = report['sha256']


def snapshot_scope(path):
    if path.parent == ROOT/'platform-data'/'business-backups' and UUID4.fullmatch(path.name):
        return ROOT
    if path.parent.parent == STATE and path.name == 'snapshot' and re.fullmatch(r'\d{4}-\d{2}-\d{2}',path.parent.name):
        return STATE
    raise ValueError('backup_retention_snapshot_scope')


def open_snapshot(path):
    base = snapshot_scope(path); handles = []; chain = []
    try:
        fd = os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); handles.append(fd)
        device = os.fstat(fd).st_dev
        for part in path.relative_to(base).parts:
            before = os.stat(part,dir_fd=fd,follow_symlinks=False)
            child = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); handles.append(child)
            info = os.fstat(child)
            require(identity(info) == identity(before) and info.st_dev == device and info.st_uid in (0,10001)
                and not info.st_mode & 0o022, 'backup_retention_snapshot_parent')
            chain.append((part,info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid))
            fd = child
        return handles,chain
    except Exception:
        for fd in reversed(handles): os.close(fd)
        raise


def snapshot_tree(path, files, *, hash_files=True):
    handles,chain = open_snapshot(path)
    try:
        fd = handles[-1]; info = os.fstat(fd); entries = {'':{'identity':identity(info),'kind':'directory'}}
        require(set(os.listdir(fd)) == {'manifest.json','database.sqlite3','media'}
            | ({'workflow-state.json'} if 'workflow-state.json' in files else set())
            | ({'workos-users.json'} if 'workos-users.json' in files else set()), 'backup_retention_snapshot_extra')
        media = os.open('media',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); handles.append(media)
        info = os.fstat(media)
        require(info.st_dev == os.fstat(fd).st_dev and info.st_uid in (0,10001) and not info.st_mode & 0o022,
            'backup_retention_snapshot_media')
        entries['media'] = {'identity':identity(info),'kind':'directory'}
        require(set(os.listdir(media)) == {name.split('/')[1] for name in files if name.startswith('media/')},
            'backup_retention_snapshot_extra')
        for name, expected in files.items():
            parent,leaf = (media,name.split('/')[1]) if name.startswith('media/') else (fd,name)
            before = os.stat(leaf,dir_fd=parent,follow_symlinks=False)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid in (0,10001) and not before.st_mode & 0o077
                and before.st_size == expected['size_bytes'],'backup_retention_snapshot_file')
            handle = os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                opened = os.fstat(handle)
                require(identity(before) == identity(opened) and stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                    and opened.st_dev == os.fstat(fd).st_dev and opened.st_uid in (0,10001)
                    and not opened.st_mode & 0o077 and opened.st_size == expected['size_bytes'], 'backup_retention_snapshot_file')
                if hash_files:
                    digest = hashlib.sha256(); size = 0
                    while block := os.read(handle,1024*1024):
                        size += len(block); require(size <= expected['size_bytes'],'backup_retention_snapshot_changed'); digest.update(block)
                    require(size == expected['size_bytes'] and digest.hexdigest() == expected['sha256'], 'backup_retention_snapshot_hash')
                require(identity(os.fstat(handle)) == identity(opened)
                    and identity(os.stat(leaf,dir_fd=parent,follow_symlinks=False)) == identity(before), 'backup_retention_snapshot_changed')
                entries[name] = {'identity':identity(opened),'kind':'file','sha256':expected['sha256']}
            finally: os.close(handle)
        return {'path':str(path),'chain':chain,'entries':entries,'bytes':sum(v['size_bytes'] for v in files.values())}
    finally:
        for fd in reversed(handles): os.close(fd)


def unchanged(item):
    for name, expected in item['metadata'].items():
        actual = retention_json(item['operation']/name,root_only=not name.startswith('output/'))
        require(actual == expected,'backup_retention_metadata_changed')


def remove_snapshot(tree, files):
    path = Path(tree['path'])
    require(snapshot_tree(path,files,hash_files=False) == tree,'backup_retention_tree_changed')
    handles,chain = open_snapshot(path)
    try:
        require(chain == tree['chain'],'backup_retention_tree_changed')
        parent,fd = handles[-2:]; entries = tree['entries']
        for name in sorted(files):
            require(identity(os.stat(path.name,dir_fd=parent,follow_symlinks=False))[:5] == entries['']['identity'][:5],
                'backup_retention_delete_parent_changed')
            directory,leaf = (fd,name)
            media = None
            if name.startswith('media/'):
                media = os.open('media',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                require(identity(os.fstat(media))[:5] == entries['media']['identity'][:5]
                    and identity(os.stat('media',dir_fd=fd,follow_symlinks=False))[:5] == entries['media']['identity'][:5],
                    'backup_retention_delete_parent_changed')
                directory,leaf = media,name.split('/')[1]
            try:
                before = os.stat(leaf,dir_fd=directory,follow_symlinks=False)
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and identity(before) == entries[name]['identity'],'backup_retention_delete_changed')
                handle = os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                try:
                    require(identity(os.fstat(handle)) == entries[name]['identity']
                        and identity(os.stat(leaf,dir_fd=directory,follow_symlinks=False)) == entries[name]['identity'],
                        'backup_retention_delete_changed')
                    digest = hashlib.sha256(); size = 0
                    while block := os.read(handle,1024*1024):
                        size += len(block); require(size <= files[name]['size_bytes'],'backup_retention_delete_hash'); digest.update(block)
                    require(size == files[name]['size_bytes'] and digest.hexdigest() == files[name]['sha256']
                        and identity(os.fstat(handle)) == entries[name]['identity']
                        and identity(os.stat(leaf,dir_fd=directory,follow_symlinks=False)) == entries[name]['identity'],
                        'backup_retention_delete_hash')
                    os.unlink(leaf,dir_fd=directory)
                finally: os.close(handle)
            finally:
                if media is not None: os.close(media)
        require(set(os.listdir(fd)) == {'media'},'backup_retention_delete_extra')
        require(os.stat('media',dir_fd=fd,follow_symlinks=False).st_ino == entries['media']['identity'][1], 'backup_retention_delete_changed')
        os.rmdir('media',dir_fd=fd)
        require(not os.listdir(fd) and os.stat(path.name,dir_fd=parent,follow_symlinks=False).st_ino == entries['']['identity'][1],
            'backup_retention_delete_changed')
        os.rmdir(path.name,dir_fd=parent); os.fsync(parent)
    finally:
        for fd in reversed(handles): os.close(fd)


def retain_backups(cfg, image, *, apply=False):
    """Only original completed scheduled copies; manual/unknown data is retained."""
    require(type(apply) is bool,'backup_retention_mode')
    evidence = STATE/('retention-'+str(uuid.uuid4())); new_directory(evidence)
    retained = []; operations = []
    for path in sorted(STATE.glob('????-??-??'),reverse=True):
        try: operations.append(retention_operation(path,cfg))
        except Exception: retained.append({'day':path.name,'reason':'original_operation_unconfirmed'})
    require(operations,'backup_retention_no_completed_operation')
    latest = operations[0]
    require(not any(v['day'] > latest['day'] for v in retained),'backup_retention_newest_unconfirmed')
    require(latest['app'].exists() or latest['app'].is_symlink(),'backup_retention_newest_app_missing')
    candidates = []; latest_tree = None; checked = []; total_entries = 0
    for item in operations:
        paths = [path for path in (item['app'],item['transport']) if path.exists() or path.is_symlink()]
        if not paths: continue
        try:
            folder = evidence/item['day']; new_directory(folder)
            retention_remote(item,image,folder)
            trees = [snapshot_tree(path,item['files']) for path in paths]
            total_entries += sum(len(tree['entries']) for tree in trees)
            require(total_entries <= MAX_RETENTION_FILES,'backup_retention_plan_limit')
            if item is latest:
                latest_tree = next(tree for tree in trees if tree['path'] == str(item['app']))
            checked.append(item)
            for tree in trees:
                if tree is not latest_tree: candidates.append((item,tree))
        except Exception:
            retained.append({'day':item['day'],'reason':'remote_or_local_unverified'})
            if item is latest: raise ValueError('backup_retention_newest_unverified') from None
    require(latest_tree is not None,'backup_retention_newest_unverified')
    plan = {'version':1,'state':'backup_retention_plan','apply':apply,'newest_day':latest['day'],
        'kept_app_operation_id':latest['operation_id'],'retained':retained,
        'candidates':[{'day':item['day'],'operation_id':item['operation_id'],'path':tree['path'],
            'bytes':tree['bytes'],'inventory_sha256':hashlib.sha256(json.dumps(tree,sort_keys=True).encode()).hexdigest(),
            'remote_report_sha256':item['remote_report_sha256']} for item,tree in candidates],
        'candidate_bytes':sum(tree['bytes'] for _,tree in candidates),'cloud_write_operations':0,
        'original_metadata_removed':False,'remote_versions_removed':False}
    record(evidence/'plan.json',plan)  # Durable evidence precedes the first unlink.
    if not apply: return {**plan,'evidence':str(evidence)}
    for item in checked: unchanged(item)
    require(snapshot_tree(latest['app'],latest['files']) == latest_tree,'backup_retention_newest_changed')
    for item,tree in candidates:
        require(snapshot_tree(Path(tree['path']),item['files']) == tree,'backup_retention_tree_changed')
    removed = []
    for item,tree in candidates:
        unchanged(item); unchanged(latest)
        require(snapshot_tree(latest['app'],latest['files'],hash_files=False) == latest_tree,'backup_retention_newest_changed')
        remove_snapshot(tree,item['files']); removed.append({'path':tree['path'],'bytes':tree['bytes']})
    result = {**plan,'state':'backup_retention_applied','removed':removed,'removed_bytes':sum(v['bytes'] for v in removed)}
    record(evidence/'completed.json',result)
    return {**result,'evidence':str(evidence)}


def retention_after_success(cfg, image):
    try: return retain_backups(cfg,image,apply=True)
    except Exception: return {'state':'backup_retention_deferred','automatic_retry':False}


def run(mode="daily"):
    require(os.geteuid() == 0 and mode in ('daily','retention-plan','retention-apply'), 'backup_root_or_mode_required')
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
        ids = docker(['ps', '--quiet', '--filter', 'label=com.docker.compose.project=sixnine-platform',
            '--filter', 'label=com.docker.compose.service=app']).split()
        require(len(ids) == 1 and re.fullmatch('[0-9a-f]{12,64}', ids[0]), 'backup_app_identity')
        template = '{"image":{{json .Image}},"running":{{json .State.Running}},"mounts":{{json .Mounts}}}'
        app = json.loads(docker(['inspect', '--format', template, ids[0]]))
        image = approved_image(release, directory, manifest, commit)
        require(app['image'] == image and app['running'] is True, 'backup_app_image_mismatch')
        require(any(m['Destination'] == '/data' and m['Source'] == str(ROOT/'platform-data')
            and m['Type'] == 'bind' for m in app['mounts']), 'backup_data_mount_mismatch')
        if mode != 'daily':
            return retain_backups(cfg,image,apply=mode == 'retention-apply')
        # A previous unknown transfer blocks new prefixes: reconciliation first.
        operation = STATE/day
        if check_history(STATE, day):
            return finish_daily(operation,completed_summary(operation,day),cfg,image)
        require(shutil.disk_usage(ROOT).free >= 5*1024**3, 'backup_disk_reserve_required')
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
        return finish_daily(operation,{'state':'copied_not_restored','day':day,**coverage(receipt)},cfg,image)
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
        retention = {'--retention-plan':'retention-plan','--retention-apply':'retention-apply'}
        require(len(sys.argv) == 1 or len(sys.argv) == 2 and sys.argv[1] in retention
            or len(sys.argv) in (3,4) and sys.argv[1] == '--restore-day' and (len(sys.argv) == 3 or sys.argv[3] == '--allow-incomplete'), 'backup_fixed_invocation_required')
        print(json.dumps(run() if len(sys.argv) == 1 else run(retention[sys.argv[1]]) if len(sys.argv) == 2
            else restore(sys.argv[2],allow_incomplete=len(sys.argv) == 4)))
    except Exception as error:
        state = 'backup_hosted_users_unavailable_business_confirmed' if str(error) == 'backup_hosted_users_unavailable_business_confirmed' else 'backup_failed_or_unknown'
        print(json.dumps({'state':state,'automatic_retry':False}), file=sys.stderr)
        raise SystemExit(1) from None

#!/usr/bin/python3
"""Explicit #655 recovery of the proven 2026-10-26 capture OOM, never a retry.

No app files/limits change. A reviewed backup module runs in a separate Python
process in the existing app container. Original partial bytes remain untouched;
the first remote copy retains the original nonce and conditional-write protocol.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import business_backup as h
import business_backup_recovery as old

DAY = '2026-10-26'
NONCE = '9f8335d4-2da3-42ac-81c0-4f4104af2ee8'
RUNTIME = Path('/opt/inkseq-business-backup-v1/backup_capture_runtime.py')
SOURCE_SHA = 'dbd53b915a7d9fb795fa1ea6d8b1362986ac81822aaaac4bb33c9c54af1ac32b'
RUNTIME_SHA = 'fed5e0c78078d0c833f983871e5da01960071f756f9453a7552976bda93ba98a'
ORIGINAL = {
    'intent.json': 'f824f9af7af93a691a9168b8f8ccb7c414b443807b76eb3666997045e5193b6a',
    'phase-snapshot-started.json': 'dd2007a76d2c1afed33d1dc440726c585c50cf77129f0c6da0d2200e7d460a7d',
    'phase-snapshot-unknown.json': '93f32b65caee41c6c1be9f134a937c68cbc43d2fadb4fc3df51c867592650eea',
}
PARTIAL = {
    'database.sqlite3': (42176512, '24cc1051576b6928773eae6ec1172d7c942b1a3db4728d7001426f7ee11645c6'),
    'database.sqlite3-journal': (403616, '35a7a8b80aaed74b628de8eb8cb864fe71e213f69c16af805198e5e4b4807977'),
}
CAPTURE = '/data/business-backups/' + NONCE + '-oom-recapture'

# Input is reviewed source, not a credential or an arbitrary operator command.
# It does not replace the imported module or the serving application's files.
LOADER = """import hashlib,json,sys
from pathlib import Path
try:
 assert hashlib.sha256(Path('/app/studio_platform/backup.py').read_bytes()).hexdigest()==%r
 source=sys.stdin.buffer.read(131073)
 assert len(source)<=131072 and hashlib.sha256(source).hexdigest()==%r
 namespace={'__name__':'studio_platform.backup','__package__':'studio_platform'}
 exec(compile(source,'<reviewed-backup-capture>','exec'),namespace)
 value=namespace['backup_configured'](Path(%r),allow_missing_media=True,classify_expected_deletions=True)
 print(json.dumps({'state':'fresh_capture_returned','coverage':value.get('coverage')}))
except Exception:
 print(json.dumps({'state':'capture_failed_or_unknown','automatic_retry':False}))
 raise SystemExit(1) from None
""" % (SOURCE_SHA, RUNTIME_SHA, CAPTURE)


def partial_unchanged():
    path = h.ROOT/'platform-data'/'business-backups'/NONCE
    for parent in [path, *path.parents]:
        info = parent.lstat()
        h.require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, 10001)
                  and not info.st_mode & 0o022, 'oom_original_parent_changed')
    h.require(set(x.name for x in path.iterdir()) == set(PARTIAL), 'oom_original_tree_changed')
    for name, (size, digest) in PARTIAL.items():
        file = path/name
        before = file.lstat()
        h.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                  and before.st_uid == 10001 and not before.st_mode & 0o022
                  and before.st_size == size, 'oom_original_file_changed')
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            h.require(h.identity(os.fstat(stream.fileno())) == h.identity(before), 'oom_original_changed')
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            h.require(actual == digest and h.identity(os.fstat(stream.fileno())) == h.identity(before)
                      and h.identity(file.lstat()) == h.identity(before), 'oom_original_changed')


def qualify(operation):
    h.protected(operation, True)
    h.require(set(p.name for p in operation.iterdir()) == set(ORIGINAL), 'oom_already_attempted')
    for name, digest in ORIGINAL.items():
        h.require(hashlib.sha256(h.protected(operation/name).read_bytes()).hexdigest() == digest,
                  'oom_original_metadata_changed')
    intent = h.read(operation/'intent.json')
    h.require(intent == dict(version=3, day=DAY, commit=old.TRANSPORT_COMMIT,
                            image=old.TRANSPORT_IMAGE, operation_id=NONCE, automatic_retry=False),
              'oom_original_identity_changed')
    partial_unchanged()
    capture_host = h.ROOT/'platform-data'/'business-backups'/(NONCE+'-oom-recapture')
    h.require(not capture_host.exists() and not capture_host.is_symlink(), 'oom_capture_exists')
    source = h.protected(RUNTIME).read_bytes()
    h.require(len(source) <= 131072 and hashlib.sha256(source).hexdigest() == RUNTIME_SHA,
              'oom_runtime_changed')
    h.require(h.docker(['exec',old.APP,'sha256sum','/app/studio_platform/backup.py']).split()[0] == SOURCE_SHA,
              'oom_source_changed')
    quiet = json.loads(h.docker(['exec',old.APP,'python','-B','-c',old.QUIET],timeout=60))
    h.require(type(quiet) is dict and type(quiet.get('nonterminal_jobs')) is int
              and quiet == {'nonterminal_jobs':0}, 'oom_zero_work_required')
    processes = h.docker(['top',old.APP,'-eo','pid,args'])
    h.require(not any(s in processes for s in ('studio_platform.backup_', '<reviewed-backup-capture>')),
              'oom_capture_writer_active')
    names = h.docker(['ps','-a','--format','{{.Names}}']).splitlines()
    h.require('inkseq-backup-transfer-'+NONCE not in names, 'oom_transfer_exists')
    service = subprocess.run(['systemctl','show','inkseq-business-backup.service','-p','ActiveState','--value'],
                             capture_output=True,text=True,timeout=30)
    h.require(service.returncode == 0 and service.stdout.strip() == 'failed', 'oom_service_changed')
    return source


def recover(operation, cfg, source):
    # Fences before any capture; failures are preserved and never auto-resumed.
    h.record(operation/'recovery-intent.json', dict(version=1, reason='confirmed_capture_cgroup_oom',
             original_nonce=NONCE, original_files=ORIGINAL, runtime_sha256=RUNTIME_SHA,
             capture_started_at=h.datetime.now(h.timezone.utc).isoformat(),
             capture=CAPTURE, automatic_retry=False, original_partial_deleted=False))
    evidence = operation/'oom-recovery'
    h.new_directory(evidence)
    with h.phase(evidence, 'snapshot'):
        result = subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock',
            'exec','-i',old.APP,'python','-B','-c',LOADER],input=source,
            capture_output=True,env=h.ENV,timeout=h.SNAPSHOT_TIMEOUT)
        h.require(result.returncode == 0 and len(result.stdout) < 65536, 'backup_docker_failed_or_unknown')
        h.require(json.loads(result.stdout).get('state') == 'fresh_capture_returned', 'oom_capture_unconfirmed')
    partial_unchanged()
    snapshot = operation/'snapshot'
    with h.phase(evidence, 'handoff'):
        h.new_directory(snapshot)
        h.docker(['cp',old.APP+':'+CAPTURE+'/.',str(snapshot)],timeout=900)
        h.freeze_transport_snapshot(snapshot)
    target = {**cfg['target'], 'prefix':'business-backups/'+NONCE}
    h.record(operation/'target.json',target)
    (operation/'target.json').chmod(0o644)
    output = operation/'output'
    h.new_directory(output)
    os.chown(output,10001,10001)
    with h.phase(evidence, 'transfer'):
        h.docker(h.transport(old.TRANSPORT_IMAGE,'inkseq-backup-transfer-'+NONCE,
            [(snapshot,'/snapshot',True),(operation/'target.json','/target.json',True),
             (output,'/output',False)],['copy','--execute','--target','/target.json',
            '--backup','/snapshot','--receipts','/output/transfer','--allow-incomplete']),timeout=1800)
    receipt = h.copy_receipt(output/'transfer'/'completion.json',target)
    h.require(receipt['format'] == 'sixnine-private-backup-copy-v3', 'backup_completion_mismatch')
    h.record(operation/'receipt.json',receipt)
    (operation/'receipt.json').chmod(0o644)
    h.record(operation/'completed.json',dict(version=3,day=DAY,commit=old.TRANSPORT_COMMIT,
        completion_sha256=receipt['completion_sha256'],completion_version_id=receipt['completion_version_id'],
        receipt_sha256=hashlib.sha256((operation/'receipt.json').read_bytes()).hexdigest(),
        restore_verified=False,**h.coverage(receipt)))
    h.require_hosted_coverage(operation)
    return dict(state='fresh_copy_completed_not_restored',day=DAY,automatic_retry=False,**h.coverage(receipt))


def run(*, execute=False):
    h.require(os.geteuid() == 0 and type(execute) is bool, 'oom_root_mode_required')
    cfg = h.validate_config(h.read(h.CONFIG),Path('/var/lib/cloud/data/instance-id').read_text().strip())
    h.require(cfg['environment'] == 'production' and cfg['version'] == 3, 'oom_config_changed')
    h.require(hashlib.sha256(h.CONFIG.read_bytes()).hexdigest() ==
              '1aaeafedec23c373d5be1fbce00da9600674eba5fbf18ba71a67f0da8cc16467', 'oom_config_changed')
    h.require(h.datetime.now(h.timezone.utc).date().isoformat() == DAY, 'oom_capture_day_changed')
    h.require(shutil.disk_usage(h.ROOT).free >= 5*1024**3, 'backup_disk_reserve_required')
    h.protected(h.STATE,True)
    h.protected(h.HELPERS,True)
    sys.path.insert(0,str(h.HELPERS))
    import release
    fd = os.open(h.protected(h.ROOT/'release.lock'),os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state = h.read(h.ROOT/'release-state.json')
        h.require(state.get('current') == old.TRANSPORT_COMMIT and state.get('status') == 'app_ready'
                  and not state.get('pending'), 'oom_release_changed')
        directory = h.ROOT/'releases'/old.TRANSPORT_COMMIT
        release.approved_manifest(h.ROOT,directory,old.TRANSPORT_COMMIT)
        manifest = release.manifest(directory,old.TRANSPORT_COMMIT)
        h.approved_image(release,directory,manifest,old.TRANSPORT_COMMIT,pinned_image=old.TRANSPORT_IMAGE)
        identity = json.loads(h.docker(['inspect','--format','{"image":{{json .Image}},"running":{{json .State.Running}}}',old.APP]))
        h.require(identity == dict(image=old.TRANSPORT_IMAGE,running=True), 'oom_app_changed')
        source = qualify(h.STATE/DAY)
        if not execute:
            return dict(state='qualified_only',day=DAY,original_nonce=NONCE,runtime_sha256=RUNTIME_SHA)
        return recover(h.STATE/DAY,cfg,source)
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        h.require(sys.argv[1:] in ([],['--execute']), 'oom_fixed_invocation_required')
        print(json.dumps(run(execute=sys.argv[1:] == ['--execute'])))
    except Exception:
        print(json.dumps(dict(state='oom_recovery_failed_or_unknown',automatic_retry=False)))
        raise SystemExit(1) from None

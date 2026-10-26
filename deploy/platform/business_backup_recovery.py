#!/usr/bin/python3
"""One reviewed #655 recovery: original production 2026-10-19 snapshot only.

Default is a bounded offline qualification, never a recapture. Execute once only
after exact helper adoption and the operator zero-work gate. Unknown output is
preserved for read-only reconciliation; this command cannot resume or retry it.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import business_backup as host

DAY = '2026-10-19'
NONCE = '55f97645-d81e-493a-9dd7-f45926c36f80'
ORIGINAL_COMMIT = '5d97d5db2a9893df9981b2d6b4b3d18fdf0c4b79'
ORIGINAL_IMAGE = 'sha256:77593f139e8bea04ab7268addb557660899b2bdd4cab62b4ec4ecfaa211d859f'
TRANSPORT_COMMIT = 'b78510e6991f5acccb0996981e8016be5434693f'
TRANSPORT_IMAGE = 'sha256:375b439ff88697761abe1318871027d3a3e1976dfd014c7499a8dbc5100de705'
MANIFEST_SHA = '37a5e05898b9288f58b7eaa802b6bcb880d3935054bcbe6ee51b401a363fa965'
TOTAL_BYTES = 1359916299
APP = 'sixnine-platform-app-1'

VERIFY = """import json
from pathlib import Path
from studio_platform.backup import verify_local
from studio_platform.backup_remote import _files
m=verify_local(Path('/snapshot'),allow_incomplete=True)
f=_files(Path('/snapshot'),m)
print(json.dumps({'version':m['version'],'coverage':m['coverage'],
 'files':len(f),'bytes':sum(x['size_bytes'] for x in f),'manifest_sha256':f[-1]['sha256']}))
"""
QUIET = """import json
from sqlalchemy import create_engine,text
from studio_platform.settings import Settings
engine=create_engine(Settings.from_environment().database_url)
with engine.connect() as c:
 c.execute(text('SET TRANSACTION READ ONLY'))
 n=c.execute(text("SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled')")).scalar_one()
print(json.dumps({'nonterminal_jobs':n}))
engine.dispose()
"""


def pristine(operation):
    host.protected(operation, True)
    host.require(stat.S_IMODE(operation.stat().st_mode) == 0o700,'backup_operation_not_private')
    host.require(set(p.name for p in operation.iterdir()) == {'intent.json'},
        'backup_recovery_prior_outcome_requires_reconciliation')
    intent = host.read(operation/'intent.json')
    host.require(intent == {'version':3,'day':DAY,'commit':ORIGINAL_COMMIT,
        'image':ORIGINAL_IMAGE,'operation_id':NONCE,'automatic_retry':False},
        'backup_recovery_original_identity_changed')
    return intent


def quiet():
    # No schema initialization, task cancellation, provider or money operation.
    value = json.loads(host.docker(['exec', APP, 'python', '-B', '-c', QUIET],timeout=60))
    host.require(type(value.get('nonterminal_jobs')) is int and value['nonterminal_jobs'] == 0,
        'backup_recovery_zero_work_required')
    processes = host.docker(['top', APP, '-eo', 'args'])
    host.require('studio_platform.backup_cli' not in processes
        and 'studio_platform.backup_remote_cli' not in processes,
        'backup_recovery_snapshot_writer_active')
    # An existing named transfer (even exited) is original-outcome evidence.
    containers = host.docker(['ps','-a','--format','{{.Names}}']).splitlines()
    host.require(not any(n.startswith('inkseq-backup-transfer-') and NONCE in n
        for n in containers), 'backup_recovery_transfer_already_exists')


def verify(snapshot):
    # No app/DB/credential mounts, metadata service, networking, or writes.
    raw = host.docker(['run','--rm','--pull','never','--network','none','--read-only',
        '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','512m','--pids-limit','64','--tmpfs','/tmp:size=67108864,mode=1777',
        '--mount','type=bind,src='+str(snapshot)+',dst=/snapshot,readonly',
        TRANSPORT_IMAGE,'python','-B','-c',VERIFY],timeout=600)
    value = json.loads(raw)
    host.require(value.get('version') == 3 and value.get('files') == 1937
        and value.get('bytes') == TOTAL_BYTES and value.get('manifest_sha256') == MANIFEST_SHA,
        'backup_recovery_snapshot_changed')
    c = host.coverage({'version':3,'coverage':value.get('coverage')})['coverage']
    host.require(c['available_objects'] == 1935 and c['expected_missing_objects'] == 2
        and c['expected_missing_bytes'] == 1986479 and c['unexpected_missing_objects'] == 0
        and c['loss_alert_required'] is False, 'backup_recovery_coverage_changed')
    return value


def transfer_original(operation, cfg, evidence):
    """Caller holds release lock; exclusive recovery intent fences every write."""
    pristine(operation)
    quiet()
    host.record(operation/'recovery-intent.json', {'version':1,'operation_id':NONCE,
        'original_day':DAY,'original_commit':ORIGINAL_COMMIT,'original_image':ORIGINAL_IMAGE,
        'transport_commit':TRANSPORT_COMMIT,'transport_image':TRANSPORT_IMAGE,
        'snapshot_manifest_sha256':MANIFEST_SHA,'automatic_retry':False,
        'recapture':False,'delete':False})
    snapshot = operation/'snapshot'
    with host.phase(operation,'handoff'):
        host.new_directory(snapshot)
        host.docker(['cp',APP+':/data/business-backups/'+NONCE+'/.',str(snapshot)],timeout=900)
        host.freeze_transport_snapshot(snapshot)
        host.require(verify(snapshot) == evidence,'backup_recovery_handoff_changed')
    quiet()
    target = {**cfg['target'],'prefix':'business-backups/'+NONCE}
    host.record(operation/'target.json',target)
    (operation/'target.json').chmod(0o644)
    output = operation/'output'
    host.new_directory(output); os.chown(output,10001,10001)
    with host.phase(operation,'transfer'):
        # Existing copy protocol checks ALL version history at this exact prefix
        # before PUT, uses If-None-Match, and preserves intent on unknown outcome.
        host.docker(host.transport(TRANSPORT_IMAGE,'inkseq-backup-transfer-'+NONCE,
            [(snapshot,'/snapshot',True),(operation/'target.json','/target.json',True),
             (output,'/output',False)],['copy','--execute','--target','/target.json',
             '--backup','/snapshot','--receipts','/output/transfer','--allow-incomplete']),timeout=1800)
    receipt = host.copy_receipt(output/'transfer'/'completion.json',target)
    host.require(receipt['format'] == 'sixnine-private-backup-copy-v3'
        and receipt['snapshot_manifest_sha256'] == MANIFEST_SHA
        and receipt['objects'] == 1935 and receipt['bytes'] == TOTAL_BYTES
        and host.coverage(receipt)['coverage'] == evidence['coverage'],
        'backup_recovery_receipt_changed')
    host.record(operation/'receipt.json',receipt)
    (operation/'receipt.json').chmod(0o644)
    # Preserve the original snapshot day/commit. Uploading old data today never
    # satisfies today's daily freshness alarm or creates a restore claim.
    host.record(operation/'completed.json',{'version':3,'day':DAY,'commit':ORIGINAL_COMMIT,
        'completion_sha256':receipt['completion_sha256'],
        'completion_version_id':receipt['completion_version_id'],
        'receipt_sha256':hashlib.sha256((operation/'receipt.json').read_bytes()).hexdigest(),
        'restore_verified':False,**host.coverage(receipt)})
    return {'state':'original_copy_completed_not_restored','snapshot_day':DAY,
        'new_daily_copy_required':True,'automatic_retry':False}


def run(*, execute=False):
    host.require(type(execute) is bool and os.geteuid() == 0,'backup_root_or_mode_required')
    cfg = host.validate_config(host.read(host.CONFIG),Path('/var/lib/cloud/data/instance-id').read_text().strip())
    host.require(cfg['environment'] == 'production' and cfg['version'] == 3,
        'backup_recovery_production_v3_required')
    host.protected(host.STATE,True); host.protected(host.HELPERS,True)
    sys.path.insert(0,str(host.HELPERS))
    import release
    lockfd = os.open(host.protected(host.ROOT/'release.lock'),os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state = host.read(host.ROOT/'release-state.json')
        host.require(state.get('current') == TRANSPORT_COMMIT and state.get('status') == 'app_ready'
            and not state.get('pending'),'backup_recovery_release_changed')
        directory = host.ROOT/'releases'/TRANSPORT_COMMIT
        release.approved_manifest(host.ROOT,directory,TRANSPORT_COMMIT)
        host.approved_image(release,directory,release.manifest(directory,TRANSPORT_COMMIT),
            TRANSPORT_COMMIT,pinned_image=TRANSPORT_IMAGE)
        app = json.loads(host.docker(['inspect','--format',
            '{"image":{{json .Image}},"running":{{json .State.Running}},"mounts":{{json .Mounts}}}',APP]))
        host.require(app['image'] == TRANSPORT_IMAGE and app['running'] is True
            and any(m['Destination'] == '/data' and m['Source'] == str(host.ROOT/'platform-data')
                and m['Type'] == 'bind' for m in app['mounts']),'backup_app_image_mismatch')
        operation = host.STATE/DAY
        pristine(operation); quiet()
        source = host.ROOT/'platform-data'/'business-backups'/NONCE
        host.require(host.retention_json(source/'manifest.json')['sha256'] == MANIFEST_SHA,
            'backup_recovery_snapshot_changed')
        evidence = verify(source)
        host.require(host.shutil.disk_usage(host.STATE).free > TOTAL_BYTES+5*1024**3,
            'backup_disk_reserve_required')
        if not execute:
            return {'state':'offline_recovery_qualified','snapshot_day':DAY,
                'evidence':evidence,'cloud_operations':0,'writes':0}
        return transfer_original(operation,cfg,evidence)
    finally:
        os.close(lockfd)


if __name__ == '__main__':
    try:
        host.require(sys.argv[1:] in ([],['--execute']),'backup_fixed_invocation_required')
        print(json.dumps(run(execute=bool(sys.argv[1:]))))
    except Exception as error:
        print(json.dumps({'state':'backup_recovery_failed_or_unknown',
            'code':host.diagnostic_code(error),'automatic_retry':False}),file=sys.stderr)
        raise SystemExit(1) from None

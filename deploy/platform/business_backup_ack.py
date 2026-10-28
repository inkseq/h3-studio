#!/usr/bin/python3
"""Root-only capture-failure acknowledgment. Never invoked by the daily timer.

Plan is read-only. Execute repeats qualification under release.lock, reads the
original S3 prefix, then exclusively writes one acknowledgment. Unknown write
outcomes must be reconciled by reading that file, never by repeating execution.
"""
import argparse
from datetime import timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import business_backup as h

APP = 'sixnine-platform-app-1'
QUIET = """import json
from sqlalchemy import create_engine,text
from studio_platform.settings import Settings
e=create_engine(Settings.from_environment().database_url)
with e.connect() as c:
 c.execute(text('SET TRANSACTION READ ONLY'))
 n=c.execute(text("SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled')")).scalar_one()
print(json.dumps({'nonterminal_jobs':n}))
e.dispose()
"""

# Uses the existing host-to-backup role path, with no app mounts or credentials.
# No Put/Delete operation, snapshot invocation, or application network request.
LIST = """import json,sys
from datetime import datetime,timezone
from studio_platform.backup_remote import BackupTarget
from studio_platform.backup_remote_cli import aws_client
t=BackupTarget(**json.loads(sys.argv[1]))
c=aws_client(t,instance_role_only=True)
started=datetime.now(timezone.utc).isoformat()
pages=0;last=None
for p in c.get_paginator('list_object_versions').paginate(Bucket=t.bucket,Prefix=t.prefix+'/',ExpectedBucketOwner=t.account_id):
 if p.get('Versions') or p.get('DeleteMarkers'):raise ValueError('original_prefix_not_empty')
 if type(p.get('IsTruncated')) is not bool:raise ValueError('listing_incomplete')
 pages+=1;last=p['IsTruncated']
if pages<1 or last is not False:raise ValueError('listing_incomplete')
print(json.dumps({'bucket':t.bucket,'prefix':t.prefix+'/', 'versions':0,'delete_markers':0,
 'listing_complete':True,'listing_started_at':started,'checked_at':datetime.now(timezone.utc).isoformat()}))
c.close()
"""


def digest(path):
    path = h.protected(path)
    h.require(path.stat().st_size <= 262144, 'backup_ack_pin_oversize')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pins(expected):
    paths = {'config_sha256':h.CONFIG, 'release_state_sha256':h.ROOT/'release-state.json',
             'helper_sha256':Path(h.__file__), 'operator_sha256':Path(__file__)}
    h.require(set(expected) == set(paths) | {'release','image'}, 'backup_ack_pin_fields')
    h.require(all(type(expected.get(k)) is str and re.fullmatch('[0-9a-f]{64}', expected[k]) for k in paths),
              'backup_ack_pin_invalid')
    h.require(all(digest(p) == expected[k] for k,p in paths.items()), 'backup_ack_pin_changed')
    state = h.read(h.ROOT/'release-state.json')
    h.require(type(expected.get('release')) is str and re.fullmatch('[0-9a-f]{40}', expected['release'])
              and type(expected.get('image')) is str and re.fullmatch('sha256:[0-9a-f]{64}', expected['image'])
              and state.get('current') == expected['release'] and state.get('status') == 'app_ready'
              and not state.get('pending'), 'backup_ack_release_changed')
    return state


def quiet(nonce):
    result = json.loads(h.docker(['exec',APP,'python','-B','-c',QUIET],timeout=60))
    h.require(type(result.get('nonterminal_jobs')) is int and result == {'nonterminal_jobs':0},
              'backup_ack_zero_work_required')
    processes = h.docker(['top',APP,'-eo','pid,args'])
    h.require(not any(x in processes for x in ('studio_platform.backup_', '<reviewed-backup-capture>')),
              'backup_ack_writer_active')
    names = h.docker(['ps','-a','--format','{{.Names}}']).splitlines()
    h.require(not any(nonce in n and ('backup' in n or 'capture' in n) for n in names),
              'backup_ack_container_evidence')
    service = subprocess.run(['/usr/bin/systemctl','show','inkseq-business-backup.service',
                              '-p','ActiveState','--value'],env=h.ENV,capture_output=True,text=True,timeout=30)
    h.require(service.returncode == 0 and service.stdout.strip() in ('inactive','failed'),
              'backup_ack_service_active')


def qualify(prior, expected):
    pins(expected)
    cfg = h.validate_config(h.read(h.CONFIG),Path('/var/lib/cloud/data/instance-id').read_text().strip())
    h.require(cfg['environment'] == 'production', 'backup_ack_production_only')
    h.protected(h.HELPERS, True)
    sys.path.insert(0,str(h.HELPERS))
    import release
    directory = h.ROOT/'releases'/expected['release']
    release.approved_manifest(h.ROOT,directory,expected['release'])
    manifest = release.manifest(directory,expected['release'])
    h.approved_image(release,directory,manifest,expected['release'],pinned_image=expected['image'])
    app = json.loads(h.docker(['inspect','--format',
        '{"image":{{json .Image}},"running":{{json .State.Running}}}',APP]))
    h.require(app == {'image':expected['image'],'running':True}, 'backup_ack_app_changed')
    original = h.capture_evidence(prior)
    quiet(original[0]['operation_id'])
    return cfg, original


def negative_listing(cfg, nonce, image):
    target = {**cfg['target'],'prefix':'business-backups/'+nonce}
    command = ['run','--rm','--pull','never','--network','host','--read-only',
        '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','256m','--cpus','0.5','--pids-limit','64',
        '--env','AWS_EC2_METADATA_DISABLED=false','--env','AWS_CONFIG_FILE=/nonexistent',
        '--env','AWS_SHARED_CREDENTIALS_FILE=/nonexistent',image,'python','-B','-c',LIST,
        json.dumps(target,sort_keys=True)]
    value = json.loads(h.docker(command,timeout=180))
    h.require(set(value) == {'bucket','prefix','versions','delete_markers','listing_complete','listing_started_at','checked_at'}
              and value['bucket'] == target['bucket'] and value['prefix'] == target['prefix']+'/'
              and all(type(value[k]) is int and value[k] == 0 for k in ('versions','delete_markers'))
              and value['listing_complete'] is True, 'backup_ack_listing_invalid')
    return value


def adopt(prior, expected, *, execute=False):
    """Caller owns the exclusive release lock. Plan never lists S3 or writes."""
    cfg, original = qualify(prior, expected)
    intent, evidence, _ = original
    if not execute:
        return {'state':'qualified_only','day':prior.name,'operation_id':intent['operation_id'],
                'evidence':evidence,'automatic_retry':False}
    listing = negative_listing(cfg,intent['operation_id'],expected['image'])
    # Slow AWS reads cannot turn a prior zero-work or release check into authority.
    h.require(qualify(prior, expected) == (cfg, original), 'backup_ack_qualification_changed')
    value = {'version':2,'state':'failed_capture_reconciled_not_completed','day':prior.name,
        'operation_id':intent['operation_id'],'evidence':evidence,**listing,**expected,
        'acknowledged_at':h.datetime.now(h.timezone.utc).isoformat(), 'zero_work':True,
        'no_writer_or_transport':True,'partials_preserved':True,'automatic_retry':False}
    # Validate with the same reader without adding a temporary file to history.
    validate_candidate(prior, cfg, value)
    h.record(prior/h.FAILED_CAPTURE_ACK,value)
    h.require(h.read(prior/h.FAILED_CAPTURE_ACK) == value, 'backup_ack_readback_changed')
    future = (h.datetime.strptime(prior.name,'%Y-%m-%d')+timedelta(days=1)).date().isoformat()
    h.reconciled_capture_v2(prior,future,cfg,value)
    return {'state':value['state'],'day':prior.name,'operation_id':intent['operation_id'],
            'ack_sha256':digest(prior/h.FAILED_CAPTURE_ACK),'completed':False,'automatic_retry':False}


def validate_candidate(prior, cfg, value):
    # All provenance fields are computed above, but refuse stale time before the
    # irreversible exclusive write. The timer applies the full schema afterwards.
    _, evidence, failure = h.capture_evidence(prior)
    start, checked, adopted = (h.capture_timestamp(value[k]) for k in ('listing_started_at','checked_at','acknowledged_at'))
    h.require(value['evidence'] == evidence and failure <= start <= checked <= adopted
              and (adopted-start).total_seconds() <= 300, 'backup_ack_listing_stale')
    h.require(cfg['target']['bucket'] == 'sixnine-business-backup-829135631045-ap-southeast-1',
              'backup_ack_target_refused')


def run(day, expected, *, execute=False):
    h.require(os.geteuid() == 0 and type(execute) is bool, 'backup_ack_root_required')
    h.require(h.datetime.strptime(day,'%Y-%m-%d').date().isoformat() == day
              and day <= h.datetime.now(h.timezone.utc).date().isoformat(), 'backup_ack_day_invalid')
    h.protected(h.STATE,True)
    fd = os.open(h.protected(h.ROOT/'release.lock'),os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return adopt(h.STATE/day,expected,execute=execute)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day',required=True)
    for name in ('release','image','config-sha256','release-state-sha256','helper-sha256','operator-sha256'):
        parser.add_argument('--expected-'+name,required=True)
    parser.add_argument('--execute',action='store_true')
    args = parser.parse_args()
    expected = {k.removeprefix('expected_'):v for k,v in vars(args).items() if k.startswith('expected_')}
    try:
        print(json.dumps(run(args.day,expected,execute=args.execute)))
        return 0
    except Exception:
        print(json.dumps({'state':'backup_ack_refused_or_unknown','automatic_retry':False}))
        return 1


if __name__ == '__main__':
    sys.exit(main())

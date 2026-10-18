"""Offline retention safety tests: no Docker, AWS, database or real host actions."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_business_backup_schedule as schedule

host = schedule.load('business_backup')


def write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value,sort_keys=True).encode())
    path.chmod(0o600)


class ContractTests(unittest.TestCase):
    def test_service_adds_only_exact_backup_payload_write_scope(self):
        service=(Path(__file__).parent/'deploy/platform/inkseq-business-backup.service').read_text()
        value=next(line.split('=',1)[1] for line in service.splitlines() if line.startswith('ReadWritePaths='))
        self.assertEqual(set(value.split()),{'/var/lib/inkseq-business-backup','/srv/sixnine/release.lock',
            '/srv/sixnine/platform-data/business-backups'})
        self.assertIn('ProtectSystem=strict',service)

    def test_json_rejects_duplicate_keys_and_nonfinite(self):
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":Infinity}'):
            with self.assertRaises(ValueError): host.unique_json(raw)

    def test_retention_failure_does_not_replay_or_invalidate_successful_backup(self):
        with mock.patch.object(host,'retain_backups',side_effect=ValueError('unknown')) as retention:
            self.assertEqual(host.retention_after_success({},'image'),
                {'state':'backup_retention_deferred','automatic_retry':False})
        retention.assert_called_once_with({},'image',apply=True)

    def test_fixed_catchup_modes_use_retention_only_and_same_day_preserves_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); (root/'release.lock').write_bytes(b'lock')
            cfg=schedule.ScheduleTests().config(); image='sha256:'+'a'*64; commit='b'*40
            release=mock.Mock(); release.manifest.return_value={}
            def read(path):
                return cfg if path==host.CONFIG else {'current':commit,'status':'app_ready','pending':None}
            def docker(args,**kwargs):
                return 'a'*12 if args[0]=='ps' else json.dumps({'image':image,'running':True,
                    'mounts':[{'Type':'bind','Source':str(root/'platform-data'),'Destination':'/data'}]})
            with mock.patch.object(host,'ROOT',root),mock.patch.object(host,'protected',side_effect=lambda p,*a:p), \
                    mock.patch.object(host,'read',side_effect=read),mock.patch.object(host,'validate_config',return_value=cfg), \
                    mock.patch.object(Path,'read_text',return_value=cfg['instance_id']), \
                    mock.patch.object(host.os,'geteuid',return_value=0,create=True), \
                    mock.patch.object(host.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0),create=True), \
                    mock.patch.dict(sys.modules,{'release':release}),mock.patch.object(host,'docker',side_effect=docker), \
                    mock.patch.object(host,'approved_image',return_value=image), \
                    mock.patch.object(host.fcntl,'flock'),mock.patch.object(host.fcntl,'LOCK_EX',2),mock.patch.object(host.fcntl,'LOCK_NB',4),mock.patch.object(host,'retain_backups',return_value={'state':'plan'}) as retention, \
                    mock.patch.object(host,'new_directory') as create:
                self.assertEqual(host.run('retention-plan'),{'state':'plan'}); retention.assert_called_with(cfg,image,apply=False)
                self.assertEqual(host.run('retention-apply'),{'state':'plan'}); retention.assert_called_with(cfg,image,apply=True)
                with mock.patch.object(host,'check_history',return_value=True), \
                        mock.patch.object(host,'completed_summary',return_value={'state':'already_completed'}), \
                        mock.patch.object(host,'retention_after_success',return_value={'state':'backup_retention_deferred'}):
                    self.assertEqual(host.run(),{'state':'already_completed','retention':{'state':'backup_retention_deferred'}})
                create.assert_not_called()  # Neither a fresh snapshot nor another upload prefix.

    def test_reconciliation_requires_full_exact_proof_and_read_only_original_mounts(self):
        target={'prefix':'business-backups/original'}; receipt={'proof':'original'}
        original={'operation_id':'inner-id','files':[{'name':'manifest.json','size_bytes':1,'sha256':'a'*64}]}
        source={'value':original}; checksum=hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        item={'operation':Path('/private/day'),'target':target,'receipt':{**receipt,'snapshot_manifest_sha256':'a'*64},
            'files':{'manifest.json':{}},'metadata':{}}
        report={'classification':'complete','code':'backup_copy_complete_bytes_verified','cloud_write_operations':0,
            'automatic_retry':False,'restore_verified':False,'target':target,'operation_id':'inner-id',
            'original_intent_sha256':checksum,'snapshot_manifest_sha256':'a'*64,'transfer_receipt':item['receipt'],
            'verified_files':[{**original['files'][0],'version_id':'immutable-version'}],'missing_current_files':[]}
        variants=[None,{'classification':'partial'},{'transfer_receipt':{}},{'verified_files':[]},
            {'original_intent_sha256':'b'*64},{'cloud_write_operations':1},{'missing_current_files':['manifest.json']}]
        for delta in variants:
            with self.subTest(delta=delta),mock.patch.object(host,'retention_inventory',return_value=source), \
                    mock.patch.object(host,'retention_json',return_value={'value':{**report,**(delta or {})},'sha256':'c'*64}), \
                    mock.patch.object(host,'new_directory'),mock.patch.object(host.os,'chown',create=True), \
                    mock.patch.object(host,'docker') as docker:
                if delta:
                    with self.assertRaises(ValueError): host.retention_remote(copy.deepcopy(item),'approved-image',Path('/evidence'))
                else:
                    host.retention_remote(item,'approved-image',Path('/evidence'))
                    args=docker.call_args.args[0]
                    self.assertIn('reconcile',args);self.assertNotIn('copy',args);self.assertIn('--instance-role-only',args)
                    mounts=[args[i+1] for i,v in enumerate(args) if v=='--mount']
                    self.assertIn('type=bind,src='+str(Path('/private/day')/'output'/'transfer')+',dst=/receipts,readonly',mounts)
                    self.assertTrue(all('/data' not in m and 'docker.sock' not in m for m in mounts))


@unittest.skipIf(os.name!='posix','Linux no-follow descriptor and mode semantics required')
class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.base=Path(self.temporary.name)
        self.root=self.base/'root'; self.state=self.base/'state'
        self.root.mkdir(mode=0o700);self.state.mkdir(mode=0o700)
        self.cfg=schedule.ScheduleTests().config();self.calls=[];self.remote_delta={}
        self.patches=[mock.patch.object(host,'ROOT',self.root),mock.patch.object(host,'STATE',self.state),
            mock.patch.object(host,'docker',side_effect=self.remote),mock.patch.object(host.os,'chown')]
        # CI runners need not be root. Only emulate trusted fixture ownership and
        # ancestors outside this test boundary; preserve real inode/mode/time/link
        # metadata for every file whose deletion is under test.
        original_stat=os.stat; original_fstat=os.fstat; uid=os.getuid()
        def value(info,path=None):
            fields={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
            if fields['st_uid']==uid: fields['st_uid']=0
            if path is not None and Path(path).is_absolute() and Path(path) in self.base.parents:
                fields['st_mode'] &= ~0o022
            return SimpleNamespace(**fields)
        self.patches.extend([mock.patch.object(host.os,'stat',side_effect=lambda path,*a,**kw:value(original_stat(path,*a,**kw),path)),
            mock.patch.object(host.os,'fstat',side_effect=lambda fd:value(original_fstat(fd)))])
        for patch in self.patches:patch.start()
        self.items=[self.make_day(day) for day in ('2026-10-16','2026-10-17','2026-10-18')]

    def tearDown(self):
        for patch in reversed(self.patches):patch.stop()
        self.temporary.cleanup()

    def make_day(self,day):
        outer='00000000-0000-4000-8000-'+day.replace('-','').zfill(12)
        inner='11111111-1111-4111-8111-'+day.replace('-','').zfill(12)
        operation=self.state/day;operation.mkdir(mode=0o700)
        app=self.root/'platform-data'/'business-backups'/outer
        data={'database.sqlite3':('db'+day).encode(),'media/'+'a'*64+'.bin':('original'+day).encode(),
            'manifest.json':json.dumps({'created_at':datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()}).encode()}
        for directory in (app,operation/'snapshot'):
            (directory/'media').mkdir(mode=0o700,parents=True)
            directory.chmod(0o700)
            for name,body in data.items():(directory/name).write_bytes(body);(directory/name).chmod(0o600)
        files=[dict(name=name,size_bytes=len(body),sha256=hashlib.sha256(body).hexdigest()) for name,body in data.items()]
        target={**self.cfg['target'],'prefix':'business-backups/'+outer};sha=files[-1]['sha256'];total=sum(len(b) for b in data.values())
        transfer=dict(format='sixnine-private-backup-copy-v1',target=target,files=files,snapshot_manifest_sha256=sha,
            objects=1,bytes=total,staging_included=False,authentication_included=False,operation_id=inner,phase='before_remote_write')
        receipt=dict(format=transfer['format'],target=target,phase='copied_not_restored',completion_version_id='v'+day,
            completion_sha256='b'*64,completion_bytes=321,snapshot_manifest_sha256=sha,objects=1,bytes=total,restore_verified=False)
        intent=dict(version=1,day=day,commit='c'*40,image='sha256:'+'d'*64,operation_id=outer,automatic_retry=False)
        write(operation/'intent.json',intent);write(operation/'target.json',target);write(operation/'receipt.json',receipt)
        write(operation/'completed.json',dict(version=1,day=day,commit=intent['commit'],completion_sha256=receipt['completion_sha256'],
            completion_version_id=receipt['completion_version_id'],receipt_sha256=hashlib.sha256((operation/'receipt.json').read_bytes()).hexdigest(),restore_verified=False))
        write(operation/'output'/'transfer'/'intent.json',transfer);write(operation/'output'/'transfer'/'completion.json',receipt)
        return dict(day=day,operation=operation,app=app,transport=operation/'snapshot',files={v['name']:v for v in files},receipt=receipt,transfer=transfer)

    def remote(self,args,**kwargs):
        self.calls.append(args)
        mounts={v.split(',dst=')[1].split(',')[0]:v.split('src=')[1].split(',')[0]
            for i,x in enumerate(args) if x=='--mount' for v in [args[i+1]]}
        target=json.loads(Path(mounts['/target.json']).read_bytes());receipt=json.loads(Path(mounts['/receipt.json']).read_bytes())
        original=json.loads((Path(mounts['/receipts'])/'intent.json').read_bytes());day=Path(mounts['/receipts']).parents[1].name
        report=dict(classification='complete',code='backup_copy_complete_bytes_verified',cloud_write_operations=0,automatic_retry=False,
            restore_verified=False,target=target,operation_id=original['operation_id'],original_intent_sha256=hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            snapshot_manifest_sha256=receipt['snapshot_manifest_sha256'],transfer_receipt=receipt,missing_current_files=[],
            verified_files=[dict(v,version_id='verified-'+str(i)) for i,v in enumerate(original['files'])])
        report.update(self.remote_delta.get(day,{}));write(Path(mounts['/output'])/'reconciliation'/'reconciliation.json',report)
        return '{}'

    def test_plan_is_non_destructive_and_apply_keeps_only_newest_app_payload_and_all_metadata(self):
        before={str(p):p.read_bytes() for p in self.state.rglob('*.json') if 'snapshot' not in p.parts}
        manual=self.root/'platform-data'/'business-backups'/'manual-aborted';manual.mkdir();(manual/'unique').write_bytes(b'keep')
        plan=host.retain_backups(self.cfg,'approved-image',apply=False)
        self.assertEqual(len(plan['candidates']),5);self.assertEqual(len(self.calls),3)
        self.assertTrue(all(v['app'].exists() and v['transport'].exists() for v in self.items))
        result=host.retain_backups(self.cfg,'approved-image',apply=True)
        self.assertEqual(len(result['removed']),5);self.assertTrue(self.items[-1]['app'].exists())
        self.assertTrue(all(not v['transport'].exists() for v in self.items));self.assertTrue(all(not v['app'].exists() for v in self.items[:-1]))
        self.assertTrue((manual/'unique').exists());self.assertEqual({name:Path(name).read_bytes() for name in before},before)
        self.assertTrue((Path(result['evidence'])/'plan.json').is_file());self.assertTrue((Path(result['evidence'])/'completed.json').is_file())

    def test_newest_remote_unknown_or_newer_partial_never_allows_old_deletion(self):
        self.remote_delta[self.items[-1]['day']]={'classification':'unknown'}
        with self.assertRaisesRegex(ValueError,'newest_unverified'):host.retain_backups(self.cfg,'image',apply=True)
        self.assertTrue(all(v['app'].exists() and v['transport'].exists() for v in self.items))
        self.remote_delta.clear();(self.state/'2026-10-19').mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError,'newest_unconfirmed'):host.retain_backups(self.cfg,'image',apply=True)
        self.assertTrue(all(v['app'].exists() and v['transport'].exists() for v in self.items))

    def test_older_unverified_snapshot_is_retained_without_blocking_proven_other_copy(self):
        self.remote_delta[self.items[0]['day']]={'classification':'partial'}
        result=host.retain_backups(self.cfg,'image',apply=True)
        self.assertTrue(self.items[0]['app'].exists());self.assertTrue(self.items[0]['transport'].exists())
        self.assertFalse(self.items[1]['app'].exists());self.assertEqual(len(result['removed']),3)

    def test_malformed_latest_binding_receipt_and_extra_file_fail_closed(self):
        latest=self.items[-1];path=latest['operation']/'target.json';original=path.read_bytes()
        target=json.loads(original);target['prefix']='business-backups/../../outside';write(path,target)
        with self.assertRaisesRegex(ValueError,'newest_unconfirmed'):host.retain_backups(self.cfg,'image',apply=True)
        path.write_bytes(original);(latest['app']/'extra').write_bytes(b'unique')
        with self.assertRaisesRegex(ValueError,'newest_unverified'):host.retain_backups(self.cfg,'image',apply=True)
        self.assertTrue(all(v['transport'].exists() for v in self.items))

    def test_symlink_hardlink_outside_scope_and_mutated_tree_never_unlink_payload(self):
        item=self.items[0];name='database.sqlite3';path=item['app']/name;body=path.read_bytes();outside=self.base/'outside';outside.write_bytes(body)
        path.unlink();path.symlink_to(outside)
        with self.assertRaises((OSError,ValueError)):host.snapshot_tree(item['app'],item['files'])
        path.unlink();os.link(outside,path)
        with self.assertRaisesRegex(ValueError,'snapshot_file'):host.snapshot_tree(item['app'],item['files'])
        path.unlink();path.write_bytes(body);path.chmod(0o600)
        tree=host.snapshot_tree(item['app'],item['files']);before=path.stat();path.write_bytes(b'x'*len(body))
        original_identity=host.identity
        def same_timestamp(info):
            result=original_identity(info)
            return (*result[:7],before.st_mtime_ns,before.st_ctime_ns) if info.st_ino==before.st_ino else result
        with mock.patch.object(host,'identity',side_effect=same_timestamp),self.assertRaisesRegex(ValueError,'delete_hash'):
            host.remove_snapshot(tree,item['files'])
        self.assertTrue(path.exists());self.assertEqual(outside.read_bytes(),body)
        with self.assertRaisesRegex(ValueError,'snapshot_scope'):host.snapshot_tree(self.base,item['files'])

    def test_receipt_mismatch_unexpected_transport_file_and_cross_device_are_retained(self):
        latest=self.items[-1];completed=latest['operation']/'completed.json';original=completed.read_bytes()
        data=json.loads(original);data['receipt_sha256']='f'*64;write(completed,data)
        with self.assertRaisesRegex(ValueError,'newest_unconfirmed'):host.retain_backups(self.cfg,'image',apply=True)
        completed.write_bytes(original);(latest['transport']/'unexpected').write_bytes(b'unique')
        with self.assertRaisesRegex(ValueError,'newest_unverified'):host.retain_backups(self.cfg,'image',apply=True)
        (latest['transport']/'unexpected').unlink()
        original_fstat=host.os.fstat
        def device(fd):
            info=original_fstat(fd)
            if stat.S_ISREG(info.st_mode):return SimpleNamespace(**{**vars(info),'st_dev':info.st_dev+1})
            return info
        with mock.patch.object(host.os,'fstat',side_effect=device),self.assertRaisesRegex(ValueError,'snapshot_file'):
            host.snapshot_tree(latest['app'],latest['files'])
        self.assertTrue(all(v['app'].exists() and v['transport'].exists() for v in self.items))

    def test_parent_swap_during_removal_refuses_original_payload_and_keeps_outside(self):
        item=self.items[0];tree=host.snapshot_tree(item['app'],item['files']);original_open=host.os.open
        outside=self.base/'outside';outside.mkdir();(outside/'unique').write_bytes(b'keep');changed=False
        def swap(path,*args,**kwargs):
            nonlocal changed
            if path=='database.sqlite3' and kwargs.get('dir_fd') is not None and not changed:
                changed=True;item['app'].rename(item['app'].with_name(item['app'].name+'-moved'));item['app'].symlink_to(outside,target_is_directory=True)
            return original_open(path,*args,**kwargs)
        with mock.patch.object(host.os,'open',side_effect=swap),self.assertRaises((ValueError,OSError)):
            host.remove_snapshot(tree,item['files'])
        self.assertEqual((outside/'unique').read_bytes(),b'keep')
        self.assertTrue((item['app'].with_name(item['app'].name+'-moved')/'database.sqlite3').exists())

    def test_fifo_leaves_are_refused_before_open_without_waiting_for_a_writer(self):
        item=self.items[0];path=item['app']/'database.sqlite3';path.unlink();os.mkfifo(path,0o600)
        with self.assertRaisesRegex(ValueError,'snapshot_file'):host.snapshot_tree(item['app'],item['files'])
        record=item['operation']/'receipt.json';record.unlink();os.mkfifo(record,0o600)
        with self.assertRaisesRegex(ValueError,'json_invalid'):host.retention_json(record,root_only=True)

    def test_changed_metadata_after_remote_read_refuses_before_any_unlink(self):
        original=host.record
        def record(path,value):
            original(path,value)
            if path.name=='plan.json':
                p=self.items[0]['operation']/'target.json';p.write_bytes(p.read_bytes()+b' ')
        with mock.patch.object(host,'record',side_effect=record),self.assertRaisesRegex(ValueError,'metadata_changed'):
            host.retain_backups(self.cfg,'image',apply=True)
        self.assertTrue(all(v['app'].exists() and v['transport'].exists() for v in self.items))


if __name__=='__main__':unittest.main()

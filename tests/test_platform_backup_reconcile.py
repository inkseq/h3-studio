"""Actual synthetic SQLite/PNG snapshots with read-only fake S3; never AWS."""
import base64
import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import backup_remote as remote
from studio_platform.backup_reconcile import reconcile_copy, validate_intent, _json
from studio_platform.backup_remote_cli import main
from test_platform_backup_remote import FakeS3, ProviderFailure
import test_platform_backup_remote as fixtures


class NoSuchKey(Exception):pass


class ReadOnlyS3(FakeS3):
    exceptions=SimpleNamespace(NoSuchKey=NoSuchKey)
    readonly=False
    denied=False
    def put_object(self,**args):
        if self.readonly:raise AssertionError('reconcile must never PUT')
        return super().put_object(**args)
    def delete_object(self,**args):raise AssertionError('reconcile must never DELETE')
    def list_object_versions(self,**args):
        if self.readonly:raise AssertionError('reconcile must never use list absence')
        return super().list_object_versions(**args)
    def get_object(self,**args):
        if self.denied:raise ProviderFailure(403)
        if args['Key'] not in self.objects:
            self.reads.append(args);raise NoSuchKey()
        return super().get_object(**args)


class ReconcileTests(unittest.TestCase):
    setUp=fixtures.RemoteBackupTests.setUp
    backup=fixtures.RemoteBackupTests.backup
    target=fixtures.RemoteBackupTests.target

    def copied(self,*,lost_final=False,partial=False):
        source=self.backup();target=self.target();client=ReadOnlyS3(target)
        receipts=self.root/'copy-receipts'
        if partial:client.fail_before='database.sqlite3'
        original=remote._record
        def record(path,value):
            if lost_final and path.name=='completion.json':raise OSError('private secret cannot be shown')
            return original(path,value)
        if lost_final or partial:
            with patch.object(remote,'_record',side_effect=record),self.assertRaises(remote.RemoteBackupError):
                remote.copy_backup(source,target,receipts,client=client)
            receipt=None
        else:receipt=remote.copy_backup(source,target,receipts,client=client)
        intent=json.loads((receipts/'intent.json').read_bytes())
        self.original_bytes={p.name:p.read_bytes() for p in receipts.iterdir()}
        client.readonly=True;client.reads=[];client.closed_bodies=0
        self.before_objects=copy.deepcopy(client.objects);self.before_writes=len(client.writes)
        return target,client,intent,receipt

    def unchanged(self,client):
        self.assertEqual(len(client.writes),self.before_writes)
        receipts=self.root/'copy-receipts'
        self.assertEqual({p.name:p.read_bytes() for p in receipts.iterdir()},self.original_bytes)

    def change_json(self,client,name,change):
        obj=client.objects[self.target().prefix+'/'+name]
        value=json.loads(obj['data']);change(value);data=remote.canonical(value)
        checksum=hashlib.sha256(data).hexdigest()
        obj.update(data=data,ContentLength=len(data),ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode())
        obj['Metadata']={**obj['Metadata'],'sha256':checksum}

    def test_lost_final_receipt_recovers_same_versions_and_real_disabled_restore(self):
        target,client,intent,_=self.copied(lost_final=True)
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'complete');self.assertFalse(result['restore_verified'])
        self.assertFalse(result['automatic_retry']);self.assertEqual(result['cloud_write_operations'],0)
        self.assertEqual(result['operation_id'],intent['operation_id'])
        self.assertEqual(len(result['verified_files']),len(intent['files']))
        reads={v['Key']:v for v in client.reads if 'VersionId' in v}
        for item in result['verified_files']:
            self.assertEqual(reads[target.prefix+'/'+item['name']]['VersionId'],item['version_id'])
        restored=remote.restore_copy(target,result['transfer_receipt'],self.root/'download',self.root/'restored',client=client)
        self.assertFalse(restored['execution_enabled']);self.assertEqual(restored['held_jobs'],1)
        self.assertEqual(client.objects,self.before_objects);self.unchanged(client)
        self.assertEqual(len(client.reads),client.closed_bodies)

    def test_known_completion_pin_never_falls_back_to_latest(self):
        target,client,intent,receipt=self.copied()
        receipt['completion_version_id']='missing-exact-version'
        result=reconcile_copy(target,intent,receipt=receipt,client=client)
        self.assertEqual(result['classification'],'unknown');self.assertNotIn('transfer_receipt',result)
        completion=[v for v in client.reads if v['Key'].endswith('/complete.json')]
        self.assertEqual(len(completion),1);self.assertEqual(completion[0]['VersionId'],'missing-exact-version')
        self.unchanged(client)

    def test_partial_prefix_is_only_observation_and_no_new_completion_is_fabricated(self):
        target,client,intent,_=self.copied(partial=True)
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'partial')
        self.assertEqual(set(result['missing_current_files']),{v['name'] for v in intent['files']})
        self.assertNotIn('transfer_receipt',result);self.assertFalse(result['automatic_retry'])
        self.assertEqual(client.objects,self.before_objects);self.unchanged(client)

    def test_all_payloads_without_transport_completion_are_still_partial(self):
        target,client,intent,_=self.copied(lost_final=True)
        del client.objects[target.prefix+'/complete.json']  # Synthetic interrupted-write fixture only.
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'partial');self.assertEqual(result['missing_current_files'],[])
        self.assertEqual(len(result['verified_files']),len(intent['files']))
        self.assertNotIn('transfer_receipt',result);self.unchanged(client)

    def test_missing_original_claim_cannot_adopt_other_objects(self):
        target,client,intent,_=self.copied()
        del client.objects[target.prefix+'/claim.json']
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'unknown')
        self.assertEqual(result['code'],'backup_reconcile_original_claim_unobserved')
        self.assertEqual(len(client.reads),1);self.unchanged(client)

    def test_ambiguous_or_denied_read_is_not_missing(self):
        target,client,intent,_=self.copied();client.denied=True
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'unknown')
        self.assertNotIn('private.invalid',json.dumps(result));self.assertEqual(result['missing_current_files'],[])
        self.assertNotIn('transfer_receipt',result);self.unchanged(client)

    def test_corrupt_pinned_payload_keeps_known_completion_and_never_reuploads(self):
        target,client,intent,_=self.copied()
        client.corrupt='database.sqlite3'
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'unknown');self.assertEqual(result['code'],'backup_object_content_mismatch')
        self.assertIn('observed_completion',result);self.assertNotIn('transfer_receipt',result)
        self.assertEqual(len(client.reads),client.closed_bodies);self.unchanged(client)

    def test_completion_wrong_pinned_version_cannot_use_matching_latest_bytes(self):
        target,client,intent,_=self.copied()
        self.change_json(client,'complete.json',lambda value:value['files'][0].update(version_id='not-this-version'))
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'unknown')
        selected=[v for v in client.reads if v['Key'].endswith('/database.sqlite3')]
        self.assertEqual(len(selected),1);self.assertEqual(selected[0]['VersionId'],'not-this-version')
        self.unchanged(client)

    def test_completion_inventory_or_operation_mismatch_is_not_adopted(self):
        target,client,intent,_=self.copied()
        original=copy.deepcopy(client.objects)
        changes=(lambda v:v.update(operation_id='10000000-0000-4000-8000-000000000000'),
                 lambda v:v['files'][0].update(sha256='0'*64),lambda v:v.update(staging_included=0))
        for change in changes:
            client.objects=copy.deepcopy(original);self.change_json(client,'complete.json',change)
            result=reconcile_copy(target,intent,client=client)
            self.assertEqual(result['classification'],'unknown');self.assertNotIn('transfer_receipt',result)
        self.unchanged(client)

    def test_wrong_key_or_checksum_closes_body_and_is_unknown(self):
        target,client,intent,_=self.copied();client.bad_key=True
        result=reconcile_copy(target,intent,client=client)
        self.assertEqual(result['classification'],'unknown');self.assertEqual(result['code'],'backup_object_key_mismatch')
        self.assertEqual(client.closed_bodies,1);self.unchanged(client)

    def test_malformed_or_other_prefix_intent_refuses_before_any_read(self):
        target,client,intent,_=self.copied()
        original=copy.deepcopy(intent)
        candidates=[]
        for key,value in (('operation_id','invalid'),('objects',True),('authentication_included',True)):
            candidate=copy.deepcopy(original);candidate[key]=value;candidates.append(candidate)
        candidate=copy.deepcopy(original);candidate['files'][0]['name']='../credential';candidates.append(candidate)
        candidate=copy.deepcopy(original);candidate['files'].append(candidate['files'][0]);candidates.append(candidate)
        for candidate in candidates:
            with self.assertRaises(remote.RemoteBackupError):reconcile_copy(target,candidate,client=client)
        foreign=replace(target,prefix='business-backups/22a950cd-8258-4308-9c53-8b85a2f8ea57')
        with self.assertRaises(remote.RemoteBackupError):reconcile_copy(foreign,intent,client=client)
        self.assertEqual(client.reads,[]);self.unchanged(client)

    def test_duplicate_and_nonfinite_json_fail_closed(self):
        for raw in (b'{"operation_id":"one","operation_id":"two"}',b'{"bytes":NaN}'):
            with self.assertRaises(remote.RemoteBackupError):_json(raw)

    def test_cli_requires_execute_then_retains_original_evidence_and_creates_new_receipt(self):
        target,client,intent,_=self.copied(lost_final=True)
        config=self.root/'target.json';config.write_text(json.dumps(remote.asdict(target)))
        args=['reconcile','--target',str(config),'--receipts',str(self.root/'copy-receipts'),
              '--reconciliation',str(self.root/'reconciled')]
        with (patch('studio_platform.backup_remote_cli.aws_client',return_value=client) as factory,
              patch('sys.stdout',new_callable=io.StringIO) as output):
            self.assertEqual(main(args),1);factory.assert_not_called()
            self.assertEqual(main(args+['--execute']),0)
        self.assertEqual(factory.call_count,1);self.assertNotIn('role/',output.getvalue())
        recovered=json.loads((self.root/'reconciled/completion.json').read_bytes())
        self.assertEqual(recovered['snapshot_manifest_sha256'],intent['snapshot_manifest_sha256'])
        self.assertTrue((self.root/'reconciled/read-intent.json').exists())
        self.assertTrue((self.root/'reconciled/reconciliation.json').exists());self.unchanged(client)
        with (patch('studio_platform.backup_remote_cli.aws_client',side_effect=AssertionError('no read on reused output')),
              patch('sys.stdout',new_callable=io.StringIO)):
            self.assertEqual(main(args+['--execute']),1)

    def test_cli_unknown_is_nonzero_and_never_writes_completion(self):
        target,client,intent,_=self.copied();client.denied=True
        config=self.root/'target.json';config.write_text(json.dumps(remote.asdict(target)))
        with (patch('studio_platform.backup_remote_cli.aws_client',return_value=client),
              patch('sys.stdout',new_callable=io.StringIO) as output):
            code=main(['reconcile','--execute','--target',str(config),'--receipts',str(self.root/'copy-receipts'),
                       '--reconciliation',str(self.root/'uncertain-read')])
        self.assertEqual(code,2);self.assertEqual(json.loads(output.getvalue())['classification'],'unknown')
        self.assertFalse((self.root/'uncertain-read/completion.json').exists());self.unchanged(client)


if __name__=='__main__':unittest.main()

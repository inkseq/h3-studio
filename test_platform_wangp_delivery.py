"""Synthetic native delivery protocol/recovery fixtures; no provider or GPU calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import httpx
from fastapi.testclient import TestClient
from studio_platform.inference.protocol import BackendError, NotReady
from studio_platform.inference.wangp_contract import EngineManifest, PreparedRequest, RuntimeObservation, RuntimeOutput, canonical_json
from studio_platform.inference.wangp_delivery import POLICY, delivery_fingerprint, validate_delivery_receipt, object_key
from studio_platform.inference.wangp_http import HTTPWanGPTransport
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_delivery import GPUDelivery, file_descriptor
from studio_platform.runtime_hosts.wangp_http import StagedInputs, create_app
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from test_platform_wangp_receipts import manifest
from test_platform_wangp_host import FakeSession

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.output=self.root/'output';self.output.mkdir()
        self.manifest=EngineManifest.from_dict({**manifest().document,'output_delivery':POLICY})
        self.prepared=PreparedRequest('job-test','attempt-test','1'*64,self.manifest.digest,
            canonical_json({'prompt':'PRIVATE_PROMPT'}),canonical_json({'width':64,'height':64,'frames':124,'actual_duration':124/24}),True)
        self.journal=ReceiptJournal(self.root/'journal'/'operations.sqlite',slot_key='slot-1',manifest_digest=self.manifest.digest,create=True)
        self.session=FakeSession();self.exports=0;self.puts=[];self.outcomes=[]
        self.engine=GPUDelivery(self.root/'sealed',exporter=self.export,uploader=self.upload)
        self.host=WanGPHost(session=self.session,journal=self.journal,manifest=self.manifest,
            output_root=self.output,sealed_root=self.root/'sealed',delivery_processor=self.engine)
        self.addCleanup(self.host.close)
        self.host.submit(self.prepared)
        video=self.output/'raw.mp4';video.write_bytes(b'raw-native-video')
        audio=self.output/'raw.wav';audio.write_bytes(b'raw-native-waveform')
        self.session.handle.observation=RuntimeObservation('succeeded',True,{'video':RuntimeOutput(video,'video/mp4'),'audio':RuntimeOutput(audio,'audio/wav')})
        self.original=self.host.inspect(self.prepared.operation_id)
        self.identity={name:getattr(self.original,name) for name in ('operation_id','job_id','attempt_tag','request_hash','manifest_digest','slot_key','incarnation')}
        self.spec={'policy':POLICY,'fps':24,'frame_count':124,'duration_s':124/24,'requested_duration_s':5}
        self.request={'version':1,'identity':self.identity,'owner_id':'owner-test','delivery_spec':self.spec,'export_crf':18,'generate_audio':True,
            'fingerprint':delivery_fingerprint(self.identity,self.spec,18,True,'owner-test')}
    def export(self,request,original,width,height,folder):
        self.exports+=1;roles={}
        for kind in ('video','audio'):
            path=folder/('verified.mp4' if kind=='video' else 'verified.flac');path.write_bytes(('validated-'+kind).encode())
            evidence={'duration_s':124/24,'has_audio':True,'delivery_spec':self.spec}
            if kind=='video':evidence.update(width=width,height=height,fps=24,frame_count=124,container_duration_s=124/24,audio_duration_s=124/24)
            roles[kind]=file_descriptor(kind,path,self.root/'sealed',evidence)
        return roles
    def upload(self,path,target,role):
        # The original journal commits an immutable PUT intent first.
        current=self.journal.delivery(self.prepared.operation_id)
        self.assertEqual(current['objects'][role['kind']]['state'],'uploading')
        self.assertFalse(current['objects'][role['kind']]['stop_proven'])
        self.puts.append(role['kind'])
        return self.outcomes.pop(0) if self.outcomes else 'uploaded'
    def targets(self,receipt,kinds=None):
        values={}
        for kind in kinds or receipt['roles']:
            role=receipt['roles'][kind];key=object_key('owner-test','attempt-test',kind)
            values[kind]={'key':key,'size_bytes':role['size_bytes'],'sha256':role['sha256'],'content_md5':role['content_md5'],
                'content_type':role['content_type'],'storage_binding':'3'*64,'expires_at':time.time()+300,
                'url':'https://'+'a'*32+'.r2.cloudflarestorage.com/bucket/'+key+'?X-Amz-Signature='+'b'*64+'&X-Amz-Expires=300',
                'headers':{'Content-Type':role['content_type'],'Content-Length':str(role['size_bytes']),'Content-MD5':role['content_md5'],'If-None-Match':'*'}}
        return values
    def prepare(self):return self.host.prepare_delivery(self.request)
    def test_prepare_concurrent_reuses_same_verified_files_without_generation(self):
        before=self.original.to_dict();prepared_hash=self.prepared.identity_digest
        with ThreadPoolExecutor(3) as pool:values=list(pool.map(lambda _:self.prepare(),range(3)))
        self.assertEqual(values,[values[0]]*3);self.assertEqual(self.exports,1);self.assertEqual(self.session.calls,1)
        self.assertEqual(self.host.inspect(self.prepared.operation_id).to_dict(),before)
        self.assertEqual(self.prepared.identity_digest,prepared_hash)
        self.assertEqual(values[0]['objects'],{})
    def test_put_intent_then_both_roles_uploaded_without_url_persistence(self):
        value=self.prepare();targets=self.targets(value)
        result=self.host.upload_delivery(self.identity,value['fingerprint'],targets)
        self.assertEqual(result['state'],'uploaded');self.assertEqual(self.puts,['video','audio'])
        self.assertTrue(all(v['stop_proven'] for v in result['objects'].values()))
        db=sqlite3.connect(self.journal.path)
        try:raw=db.execute('SELECT receipt FROM operations').fetchone()[0]
        finally:db.close()
        self.assertNotIn('X-Amz-',raw);self.assertNotIn('https://',raw);self.assertNotIn('PRIVATE_PROMPT',raw)
        self.host.upload_delivery(self.identity,value['fingerprint'],targets)
        self.assertEqual(len(self.puts),2);self.assertEqual(self.session.calls,1)
    def test_unknown_response_retains_key_and_refuses_another_put(self):
        value=self.prepare();self.outcomes=['unknown']
        result=self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value,('video',)))
        self.assertEqual(result['state'],'unknown');self.assertFalse(result['objects']['video']['stop_proven'])
        with self.assertRaises(NotReady):self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value,('video',)))
        self.assertEqual(self.puts,['video']);self.assertEqual(self.exports,1)

    def test_first_unknown_later_not_started_can_finish_without_repeating_first_put(self):
        value=self.prepare();self.outcomes=['unknown']
        first=self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value))
        self.assertEqual(first['objects']['video']['state'],'unknown')
        self.assertEqual(first['objects']['audio']['state'],'not_started')
        self.assertTrue(first['objects']['audio']['stop_proven'])
        self.assertEqual(first['objects']['audio']['key'],object_key('owner-test','attempt-test','audio'))
        second=self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value,('audio',)))
        self.assertEqual(second['objects']['audio']['state'],'uploaded')
        self.assertEqual(second['objects']['video']['state'],'unknown')
        self.assertEqual(second['state'],'unknown');self.assertEqual(self.puts,['video','audio'])
        self.assertEqual(self.exports,1);self.assertEqual(self.session.calls,1)

    def test_explicit_rejection_permits_same_key_renewal_only(self):
        value=self.prepare();self.outcomes=['rejected']
        first=self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value,('video',)))
        self.assertTrue(first['objects']['video']['stop_proven']);self.assertEqual(first['state'],'rejected')
        target=self.targets(value,('video',));target['video']['storage_binding']='4'*64
        with self.assertRaises(NotReady):self.host.upload_delivery(self.identity,value['fingerprint'],target)
        second=self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value,('video',)))
        self.assertEqual(second['objects']['video']['state'],'uploaded');self.assertEqual(self.puts,['video','video'])
    def test_identity_audio_and_owner_changes_cannot_rebind_original_delivery(self):
        self.prepare()
        changed=json.loads(canonical_json(self.request));changed['owner_id']='other-owner'
        changed['fingerprint']=delivery_fingerprint(changed['identity'],self.spec,18,True,'other-owner')
        with self.assertRaises(NotReady):self.host.prepare_delivery(changed)
        changed=json.loads(canonical_json(self.request));changed['identity']['incarnation']='f'*32
        changed['fingerprint']=delivery_fingerprint(changed['identity'],self.spec,18,True,'owner-test')
        with self.assertRaises(NotReady):self.host.prepare_delivery(changed)
        changed=json.loads(canonical_json(self.request));changed['generate_audio']=False
        changed['fingerprint']=delivery_fingerprint(self.identity,self.spec,18,False,'owner-test')
        with self.assertRaises(NotReady):self.host.prepare_delivery(changed)
        self.assertEqual(self.exports,1)
    def test_original_geometry_rejects_changed_timeline(self):
        changed=json.loads(canonical_json(self.request));changed['delivery_spec']['frame_count']=125;changed['delivery_spec']['duration_s']=125/24
        changed['fingerprint']=delivery_fingerprint(self.identity,changed['delivery_spec'],18,True,'owner-test')
        with self.assertRaises(BackendError):self.host.prepare_delivery(changed)
        self.assertEqual(self.exports,0)
    def test_wrong_key_expiry_headers_or_origin_never_put(self):
        value=self.prepare()
        for mutation in ('key','expiry','md5','origin','size'):
            with self.subTest(mutation=mutation):
                targets=self.targets(value,('video',));target=targets['video']
                if mutation=='key':target['key']=target['key'].replace('owner-test','other-owner')
                elif mutation=='expiry':target['expires_at']=time.time()-1
                elif mutation=='md5':target['headers']['Content-MD5']='not-md5'
                elif mutation=='origin':target['url']=target['url'].replace('a'*32,'evil.subdomain')
                else:target['size_bytes']+=1
                with self.assertRaises(ValueError):self.host.upload_delivery(self.identity,value['fingerprint'],targets)
        self.assertEqual(self.puts,[]);self.assertEqual(self.journal.delivery(self.prepared.operation_id)['objects'],{})

    def test_existing_partial_source_copy_is_not_removed_by_failed_new_open(self):
        folder=self.engine._folder(self.prepared.operation_id)
        partial=folder/'source-video.mp4.part';partial.write_bytes(b'original-unknown-partial')
        with self.assertRaises(FileExistsError):self.engine._source(self.original,'video',folder)
        self.assertEqual(partial.read_bytes(),b'original-unknown-partial')
        self.assertEqual(self.session.calls,1)

    def test_validated_file_tamper_cannot_be_reprepared_or_uploaded(self):
        value=self.prepare();(self.root/'sealed'/self.prepared.operation_id/'delivery'/'verified.mp4').write_bytes(b'changed')
        with self.assertRaises(BackendError):self.prepare()
        with self.assertRaises(BackendError):self.host.upload_delivery(self.identity,value['fingerprint'],self.targets(value))
        self.assertEqual(self.exports,1);self.assertEqual(self.puts,[])
    def test_old_manifest_refuses_delivery_and_old_wire_has_no_extension(self):
        self.host.manifest=manifest()
        with self.assertRaises(NotReady):self.prepare()
        self.assertNotIn('delivery',self.original.to_dict());self.assertNotIn('_delivery',self.original.to_dict())

    @unittest.skipUnless(__import__('shutil').which('ffmpeg') and __import__('shutil').which('ffprobe'),'CPU media utilities required')
    def test_real_cpu_export_preserves_numbered_native_frames_and_generated_audio_tail(self):
        from test_platform_native_delivery import fixture,frame_numbers,audio_samples
        paths=fixture(self.output)
        journal=ReceiptJournal(self.root/'real-journal'/'operations.sqlite',slot_key='real-slot',manifest_digest=self.manifest.digest,create=True)
        session=FakeSession()
        host=WanGPHost(session=session,journal=journal,manifest=self.manifest,output_root=self.output,sealed_root=self.root/'real-sealed')
        self.addCleanup(host.close)
        prepared=replace(self.prepared,attempt_tag='attempt-real',output_spec_json=canonical_json({'width':256,'height':256,'frames':124,'actual_duration':124/24}))
        host.submit(prepared)
        session.handle.observation=RuntimeObservation('succeeded',True,{'video':RuntimeOutput(paths['video'],'video/mp4'),'audio':RuntimeOutput(paths['audio'],'audio/wav')})
        original=host.inspect(prepared.operation_id);before=original.to_dict()
        identity={name:getattr(original,name) for name in self.identity}
        request={**self.request,'identity':identity,'fingerprint':delivery_fingerprint(identity,self.spec,18,True,'owner-test')}
        delivered=host.prepare_delivery(request)
        folder=self.root/'real-sealed'/prepared.operation_id/'delivery'
        self.assertEqual(frame_numbers(folder/'verified.mp4'),list(range(124)))
        samples=audio_samples(folder/'verified.flac')
        self.assertGreater(max(abs(v) for v in samples[320000:]),5000)
        self.assertEqual(delivered['roles']['video']['evidence']['frame_count'],124)
        self.assertAlmostEqual(delivered['roles']['audio']['evidence']['duration_s'],124/24,delta=.1)
        self.assertEqual(host.inspect(prepared.operation_id).to_dict(),before)
        self.assertEqual(session.calls,1)

    def test_http_auth_identity_and_response_never_expose_grants(self):
        token='test-only-'+'x'*48
        client=TestClient(create_app(self.host,StagedInputs(self.root/'inputs'),token=token));self.addCleanup(client.close)
        response=client.post('/v1/operations/'+self.prepared.operation_id+'/delivery/prepare',json=self.request)
        self.assertEqual(response.status_code,401);self.assertEqual(self.exports,0)
        def dispatch(req):
            response=client.request(req.method,req.url.raw_path.decode(),headers=dict(req.headers),content=req.read())
            return httpx.Response(response.status_code,content=response.content,headers=dict(response.headers))
        transport=HTTPWanGPTransport('http://127.0.0.1:8199',token,transport=httpx.MockTransport(dispatch));self.addCleanup(transport.close)
        value=transport.prepare_delivery(self.prepared.operation_id,self.request)
        self.assertEqual(value['state'],'prepared')
        uploaded=transport.upload_delivery(self.prepared.operation_id,self.identity,value['fingerprint'],self.targets(value))
        self.assertEqual(uploaded['state'],'uploaded')
        status=transport.delivery_status(self.prepared.operation_id,self.identity)
        self.assertEqual(status,uploaded);self.assertNotIn('https://',canonical_json(status));self.assertEqual(self.session.calls,1)

if __name__=='__main__':unittest.main()

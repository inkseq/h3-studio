"""Controls API acceptance without GPU, credentials, HTTP network or user DATA.
Run with .venv/Scripts/python.exe -m unittest -v test_controls_server.py.
"""
import gc
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient

class ControlApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='h3-controls-api-')
        cls.data=Path(cls.temp.name)
        spec=importlib.util.spec_from_file_location('_h3_controls_offline_server',(Path(__file__).resolve().parents[1] / 'server.py'))
        cls.server=importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ,{'H3_STUDIO_DATA':str(cls.data),'H3_COMFY_URL':'http://127.0.0.1:8189'}):spec.loader.exec_module(cls.server)
        cls.network=mock.patch('httpx.HTTPTransport.handle_request',side_effect=AssertionError('Offline control test attempted network'))
        cls.network.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.STOP.set();cls.network.stop()
        schema_connection=getattr(cls.server,'c',None)
        if schema_connection is not None:schema_connection.close()
        gc.collect();cls.temp.cleanup()
    def setUp(self):
        with self.server.db() as c:c.execute('DELETE FROM jobs');c.execute('DELETE FROM uploads')
        self.client=TestClient(self.server.app)
        login=self.client.post('/api/auth/login',json={'username':'superdan'})
        self.assertEqual(login.status_code,200,login.text)
    def tearDown(self):self.client.close()
    def request(self,**extra):
        return dict(backend='comfy-local',model=self.server.MODEL,mode='fl',prompt='A cinematic landscape.',duration=5,
            resolution='768P',aspect_ratio='16:9',generate_audio=False,seed=42,inputs={},**extra)
    def preview(self,raw):return self.client.post('/api/workflow-preview',json=raw)
    def upload_record(self,kind='image',duration=None,has_audio=False):
        uid=('a' if kind=='image' else 'b' if kind=='video' else 'c')*32
        path=self.data/'uploads'/(uid+'.png' if kind=='image' else uid+'.mp4' if kind=='video' else uid+'.wav')
        path.write_bytes(b'offline media fixture: metadata test only')
        meta=dict(id=uid,kind=kind,model_path=str(path),path=str(path),duration=duration,has_audio=has_audio)
        if kind=='video':meta.update(source_duration=2,fps=24,frame_count=56,duration=56/24)
        if kind=='audio':meta.update(source_duration=duration)
        with self.server.db() as c:c.execute('INSERT OR REPLACE INTO uploads (id,metadata) VALUES(?,?)',(uid,json.dumps(meta)))
        return uid
    def test_preview_does_not_check_health_enqueue_or_dispatch(self):
        raw=self.request();raw.update(duration=4,resolution='custom',width=320,height=256,seed=str(2**64-1))
        with mock.patch.object(self.server,'capability',side_effect=AssertionError('Preview must not probe GPU')):
            response=self.preview(raw)
        self.assertEqual(response.status_code,200,response.text)
        body=response.json();self.assertFalse(body['gpu_submitted'])
        self.assertEqual(body['request']['seed'],str(2**64-1))
        noise=next(n for n in body['graph'].values() if n['class_type']=='RandomNoise')
        self.assertEqual(noise['inputs']['noise_seed'],2**64-1)
        self.assertEqual(body['native_spec']['width'],320)
        self.assertTrue(body['warnings'])
        self.assertEqual(self.client.get('/api/jobs').json(),{'jobs':[]})
    def test_controls_preserved_in_job_and_idempotent_replay(self):
        raw=self.request();raw.update(sampler_name='euler',scheduler='karras',denoise=.5,export_crf=7,steps=25,encoder_device='cpu')
        cap={'backends':[{'available':True,'reason':''}]}
        with mock.patch.object(self.server,'capability',return_value=cap):
            first=self.client.post('/api/jobs',json=raw,headers={'Idempotency-Key':'controls-valid-1'})
        self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(first.json()['request']['export_crf'],7)
        self.assertTrue(first.json()['warnings'])
        with mock.patch.object(self.server,'capability',side_effect=AssertionError('Replay must not probe GPU')):
            replay=self.client.post('/api/jobs',json=raw,headers={'Idempotency-Key':'controls-valid-1'})
        self.assertEqual(replay.json()['id'],first.json()['id'])
        self.assertEqual(len(self.client.get('/api/jobs').json()['jobs']),1)
    def test_invalid_or_unimplemented_controls_are_not_silently_ignored(self):
        cases=[{'resolution':'1080P'},{'negative_prompt':'bad anatomy'},{'cfg':7},{'controlnet':'depth'},
            {'sampler_name':'not-installed'},{'scheduler':'mystery'},{'export_crf':52},{'seed':str(2**64)},
            {'resolution':'custom','width':320,'height':257},{'resolution':'custom','width':1536,'height':1536},
            {'video_decode':'tiled','video_tile_size':64,'video_overlap':64}]
        for change in cases:
            with self.subTest(change=change):
                raw=self.request();raw.update(change);r=self.preview(raw);self.assertEqual(r.status_code,422,r.text)
    def test_incompatible_audio_tiled_is_rejected_before_any_gpu_access(self):
        raw=self.request();raw.update(audio_decode='tiled')
        with mock.patch.object(self.server,'capability',side_effect=AssertionError('Invalid audio decoder must not probe GPU')):
            preview=self.preview(raw)
            queued=self.client.post('/api/jobs',json=raw)
        for response in (preview,queued):
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(response.json()['detail'],'当前H3音频VAE分块解码已实测不兼容，请使用完整解码')
        self.assertEqual(self.client.get('/api/jobs').json(),{'jobs':[]})
        raw['audio_decode']='normal';raw.update(video_decode='tiled')
        valid=self.preview(raw)
        self.assertEqual(valid.status_code,200,valid.text)
        self.assertTrue(any(n['class_type']=='VAEDecodeTiled' for n in valid.json()['graph'].values()))

    def test_guides_can_reuse_reference_and_do_not_allow_tail_loss(self):
        uid=self.upload_record('image')
        raw=self.request();raw.update(mode='ref',inputs={'images':[uid]},guides=[{'media_id':uid,'time_seconds':2,'use_audio':False}])
        r=self.preview(raw);self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(any(n['class_type']=='MiniMaxH3AddGuide' for n in r.json()['graph'].values()))
        video=self.upload_record('video')
        raw=self.request();raw.update(guides=[{'media_id':video,'time_seconds':3,'use_audio':False}])
        r=self.preview(raw);self.assertEqual(r.status_code,422,r.text)
        self.assertIn('remaining output',r.json()['detail'])
    def test_guide_only_media_uploaded_once_before_queue_check(self):
        uid=self.upload_record('image');raw=self.request();raw.update(guides=[{'media_id':uid,'time_seconds':0,'use_audio':False}])
        request,uploads=self.server.validate_job(raw)
        jid='d'*32;j=dict(id=jid,status='running',request=request)
        with self.server.db() as c:c.execute('INSERT INTO jobs (id,status,created,record,idem) VALUES(?,?,?,?,?)',(jid,'running',0,json.dumps(j),None))
        class Response:
            def __init__(self,value):self.value=value
            def raise_for_status(self):pass
            def json(self):return self.value
        class Client:
            def __init__(self):self.uploaded=[]
            def post(self,url,**kwargs):
                if not url.endswith('/upload/image'):raise AssertionError('Must not submit GPU prompt')
                self.uploaded.append(kwargs['files']['image'][0]);return Response({'name':self.uploaded[-1]})
            def get(self,url):return Response({'queue_running':[['another-job']], 'queue_pending':[]})
        client=Client()
        with self.assertRaisesRegex(RuntimeError,'队列已有'):self.server.execute_job(j,client)
        self.assertEqual(client.uploaded,[Path(uploads[uid]['model_path']).name])
    def test_export_crf_reaches_actual_export_without_affecting_upload(self):
        raw=self.request();raw.update(duration=4,resolution='custom',width=320,height=256,export_crf=7)
        request,_=self.server.validate_job(raw)
        jid='e'*32;j=dict(id=jid,status='running',request=request)
        with self.server.db() as c:c.execute('INSERT INTO jobs (id,status,created,record,idem) VALUES(?,?,?,?,?)',(jid,'running',0,json.dumps(j),None))
        class Response:
            def __init__(self,value=None):self.value=value
            def raise_for_status(self):pass
            def json(self):return self.value
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def iter_bytes(self):yield b'fake encoded raw data'
        class Client:
            def post(self,url,**kwargs):
                if not url.endswith('/prompt'):raise AssertionError('Unexpected upload in pure text fixture')
                self.graph=kwargs['json']['prompt'];return Response({'prompt_id':'offline-prompt'})
            def get(self,url):
                if url.endswith('/queue'):return Response({'queue_running':[],'queue_pending':[]})
                save=next(k for k,n in self.graph.items() if n['class_type']=='SaveVideo')
                return Response({'offline-prompt':{'status':{'completed':True},'outputs':{
                    save:{'videos':[{'filename':jid+'_00001_.mp4','subfolder':'h3-studio','type':'output'}]}}}})
            def stream(self,*a,**kw):return Response()
        calls=[]
        def local_ffmpeg(args):
            calls.append(args)
            if args[-1]!='-':Path(args[-1]).write_bytes(b'offline exported video')
        metadata={'streams':[{'codec_type':'video','width':320,'height':256,'avg_frame_rate':'24/1','duration':'4'}]}
        with mock.patch.object(self.server,'run_ffmpeg',side_effect=local_ffmpeg),mock.patch.object(self.server,'probe',return_value=metadata):
            self.server.execute_job(j,Client())
        export=next(a for a in calls if '-crf' in a)
        self.assertEqual(export[export.index('-crf')+1],'7')
        self.assertEqual(j['status'],'succeeded')
        self.assertEqual(export[export.index('-frames:v')+1],'96')

    def test_api_schema_and_capability_are_control_complete_offline(self):
        schema=self.client.get('/openapi.json').json()
        props=schema['paths']['/api/jobs']['post']['requestBody']['content']['application/json']['schema']['properties']
        self.assertEqual(props['duration']['minimum'],4)
        for field in ('sampler_name','scheduler','denoise','guides','shift_video','video_audio','video_decode','encoder_device','export_crf'):
            self.assertIn(field,props)
        self.assertEqual(len(props['sampler_name']['enum']),45)
        with mock.patch.object(self.server,'comfy_info',return_value=None):cap=self.server.capability()
        self.assertEqual(cap['expert_url'],'http://127.0.0.1:8189/')
        self.assertEqual(len(cap['controls']['samplers']),45)

if __name__=='__main__':unittest.main()

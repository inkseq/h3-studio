"""Offline profile boundaries; no provider, weights, or model imports."""
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from comfy_workflow import native_output_spec
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_contract import EngineManifest, InputDescriptor, canonical_json
from studio_platform.inference.wangp_profile_compiler import (
    H3ProfileCompiler, compile_settings, control_schema, normalize_request, validate_prepared)
from studio_platform.runtime_catalog import PROFILE_IDS as ALL_PROFILE_IDS, engine_manifest, get_profile, runtime_config
# These fixtures assert the retained v1 compiler bytes and semantics. New v2
# profiles are covered by test_platform_full_controls and the native v2 compiler.
PROFILE_IDS = tuple(p for p in ALL_PROFILE_IDS if get_profile(p).get("compiler_epoch") != "full-controls-v2")
from studio_platform.runtime_hosts.wangp_http import StagedInputs
from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs
from studio_platform.runtime_hosts.wangp_session import _check_config, _audit_profile_runtime, PinnedWanGPSession


def example(profile_id, case):
    inputs = {'images':[], 'videos':[], 'audios':[], 'first_frame':None, 'last_frame':None}
    metadata = {}
    for role in case['input_roles']:
        kind = 'image' if role in ('first_frame','last_frame') else role
        if role in ('first_frame','last_frame'):
            inputs[role] = role
        else:
            inputs[{'image':'images','video':'videos','audio':'audios'}[kind]] = [role]
        metadata[role] = {'kind':kind,'model_ready':True,'width':832,'height':480,
            'fps':24,'frame_count':56,'duration':56/24 if kind=='video' else 5,
            'source_duration':56/24,'has_audio':False,'sample_rate':32000,'channels':2}
    request = {'model':get_profile(profile_id)['model_id'],'mode':case['mode'],'prompt':'Synthetic offline test',
        'duration':5,'resolution':str(case['height'])+'P','aspect_ratio':'16:9','seed':'42',
        'steps':case['steps'],'inputs':inputs,'video_audio':{k:False for k in inputs['videos']}}
    return request, metadata


def job_for(profile_id, case):
    request, metadata = example(profile_id, case)
    manifest = engine_manifest(profile_id,case['mode'])
    job = {'id':'job-1','owner_id':'owner','request_hash':'b'*64,
        'execution_plan':{'engine_manifest_digest':manifest.digest,'deployment_profile_id':profile_id},
        'request':{'request':request,'output_spec':native_output_spec(request),
            'deployment_profile_id':profile_id,'recipe_id':manifest.document['generation_recipe_id'],
            'assets':{key:{'metadata':meta,'model':{'key':f'owners/owner/assets/{key}/file',
                'sha256':'a'*64,'size_bytes':4}} for key,meta in metadata.items()}}}
    return job,manifest


class ProfileCompilerTests(unittest.TestCase):
    def test_advertised_native_canvas_shapes_survive_host_template_validation(self):
        identity=PROFILE_IDS[0]
        schema=control_schema(identity,'fl')
        for resolution in schema['resolution']['enum']:
            for aspect in schema['aspect_ratio']['enum']:
                job,manifest=job_for(identity,{'mode':'fl','height':480,'steps':20,'input_roles':[]})
                job['request']['request'].update(resolution=resolution,aspect_ratio=aspect)
                if resolution=='custom': job['request']['request'].update(width=768,height=1024)
                job['request']['output_spec']=native_output_spec(job['request']['request'])
                with self.subTest(resolution=resolution,aspect=aspect):
                    prepared=H3ProfileCompiler(manifest,lambda *a:self.fail('no assets'))(
                        job,'canvas',None,lambda:None)
                    validate_prepared(prepared,manifest)

    def test_unmeasured_first_only_last_only_and_changed_controls_keep_exact_profile(self):
        for identity in PROFILE_IDS:
            for roles in ([],['first_frame'],['last_frame'],['first_frame','last_frame']):
                for controls in ({'steps':30,'duration':6,'resolution':'576P','aspect_ratio':'1:1'},
                        {'steps':50,'duration':15,'resolution':'custom','width':512,'height':512,
                         'sampler_name':'res_multistep'}):
                    case={'mode':'fl','height':480,'steps':20,'input_roles':roles}
                    job,manifest=job_for(identity,case)
                    job['request']['request'].update(controls)
                    job['request']['output_spec']=native_output_spec(job['request']['request'])
                    staged=[]
                    compiler=H3ProfileCompiler(manifest,lambda item,*a,**k:staged.append(item) or item)
                    with self.subTest(profile=identity,roles=roles,controls=controls):
                        prepared=compiler(job,'unmeasured',NS(open=lambda _:io.BytesIO(b'data')),lambda:None)
                        validate_prepared(prepared,manifest)
                        settings=prepared.settings
                        self.assertEqual(settings['config'],get_profile(identity)['runtime']['task_config'])
                        self.assertEqual(settings['num_inference_steps'],controls['steps'])
                        self.assertEqual(settings['video_length'],job['request']['output_spec']['frames'])
                        self.assertEqual(settings['image_prompt_type'],('S' if 'first_frame' in roles else 'T')+
                            ('E' if 'last_frame' in roles else ''))
                        self.assertEqual(len(staged),len(roles))

    def test_multiple_reference_handles_map_in_order_and_host_consumes_every_input_once(self):
        identity=PROFILE_IDS[0]
        job,manifest=job_for(identity,{'mode':'ref','height':480,'steps':20,'input_roles':['image','video','audio']})
        request=job['request']['request']; assets=job['request']['assets']
        for plural,kind in (('images','image'),('videos','video'),('audios','audio')):
            request['inputs'][plural]=[]
            for index in range(3):
                key=kind+str(index); request['inputs'][plural].append(key)
                assets[key]=copy.deepcopy(assets[kind])
                assets[key]['model']['key']=f'owners/owner/assets/{key}/file'
                if kind=='audio': assets[key]['metadata']['duration']=4
            del assets[kind]
        request['video_audio']={key:False for key in request['inputs']['videos']}
        request.update(steps=30,duration=8)
        job['request']['output_spec']=native_output_spec(request)
        prepared=H3ProfileCompiler(manifest,lambda item,*a,**k:item)(job,'multiple',
            NS(open=lambda _:io.BytesIO(b'data')),lambda:None)
        consumed=[]
        def resolve(item,**kwargs): consumed.append(item.asset_id); return '/private/'+item.asset_id
        aggregate=[]
        resolved=resolve_inputs(prepared,NS(image_path=resolve,video_path=resolve,audio_path=resolve,
            validate_reference_totals=lambda values,**kw:aggregate.append(values)),manifest)
        self.assertEqual(resolved['video_prompt_type'],'IV+*-U')
        self.assertEqual(resolved['audio_prompt_type'],'ABD')
        self.assertEqual(resolved['video_guide3'],'/private/video2')
        self.assertEqual(resolved['audio_guide2'],'/private/audio1')
        self.assertEqual(resolved['image_refs'],['/private/image0','/private/image1','/private/image2'])
        self.assertCountEqual(consumed,assets)
        self.assertEqual(len(consumed),len(set(consumed)))
        self.assertEqual(len(aggregate),1)
        self.assertCountEqual([d.asset_id for d in aggregate[0]],assets)
        for change in ('aggregate_audio','aggregate_video','missing_visual','mixed_mode'):
            bad=copy.deepcopy(job); req=bad['request']['request']; meta=bad['request']['assets']
            if change=='aggregate_audio':
                for a in req['inputs']['audios']: meta[a]['metadata']['duration']=6
            elif change=='aggregate_video':
                for v in req['inputs']['videos']: meta[v]['metadata'].update(frame_count=124,duration=124/24)
            elif change=='missing_visual':
                for plural in ('images','videos'):
                    for key in req['inputs'][plural]: del meta[key]
                    req['inputs'][plural]=[]
                req['video_audio']={}
            else:
                req['inputs']['first_frame']='image0'
            with self.subTest(change=change),self.assertRaises(BackendError):
                H3ProfileCompiler(manifest,lambda *a,**k:self.fail('invalid request transferred'))(
                    bad,'invalid',None,lambda:None)

    def test_all_22_verified_joint_cases_compile_without_precision_or_role_substitution(self):
        for identity in PROFILE_IDS:
            profile = get_profile(identity)
            for case in profile['verified_cases']:
                with self.subTest(profile=identity,case=case['id']):
                    request, metadata = example(identity,case)
                    original = copy.deepcopy(request)
                    settings = compile_settings(request,metadata,native_output_spec(request),
                        {key:'handle-'+key for key in metadata},identity)
                    self.assertEqual(request,original)
                    self.assertEqual(settings['config'],profile['runtime']['task_config'])
                    self.assertEqual(settings['override_profile'],profile['runtime']['memory_profile'])
                    self.assertEqual(settings['num_inference_steps'],case['steps'])
                    self.assertEqual(settings['video_length'],124)
                    self.assertEqual(settings['force_fps'],'24')
                    if 'video' in case['input_roles']:
                        self.assertEqual(settings['video_prompt_type'],'IV-U')
                        self.assertIsNone(settings['video_source'])
                    if 'audio' in case['input_roles']:
                        self.assertEqual(settings['audio_prompt_type'],'A')
                        self.assertIsNone(settings['audio_source'])

    def test_invalid_or_unmapped_controls_and_frame_trimming_fail_before_staging(self):
        identity = PROFILE_IDS[1]
        case = get_profile(identity)['verified_cases'][3]
        request, metadata = example(identity,case)
        for changes in ({'steps':101},{'steps':True},
                {'model':'MiniMax-H3-Base-BF16'},{'duration':16},{'generate_audio':False},
                {'shift_audio':4},{'guides':[]},{'video_audio':{'video':True}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                value = {**request,**changes}
                normalize_request(value,metadata,native_output_spec(value),identity)
        for frame_count in (39,48,74):
            changed = copy.deepcopy(metadata)
            changed['video'].update(frame_count=frame_count,duration=frame_count/24)
            with self.assertRaisesRegex(ValueError,'aligned_video_required'):
                normalize_request(request,changed,native_output_spec(request),identity)
        request['inputs']['videos'] = []
        request['video_audio'] = {}
        metadata.pop('video')
        normalized = normalize_request(request,metadata,native_output_spec(request),identity)
        self.assertEqual(normalized['inputs']['images'],['image'])

    def test_compiler_binds_both_profile_identities_and_owner_before_any_upload(self):
        identity = PROFILE_IDS[2]
        job,manifest = job_for(identity,get_profile(identity)['verified_cases'][3])
        uploaded = []
        def stage(item,source,**kwargs):
            uploaded.append(item)
            return item
        compiler = H3ProfileCompiler(manifest,stage)
        store = NS(open=lambda key:io.BytesIO(b'data'))
        prepared = compiler(job,'attempt-1',store,lambda:None)
        validate_prepared(prepared,manifest)
        self.assertEqual(len(uploaded),3)
        for location in ('execution_plan','request'):
            bad = copy.deepcopy(job)
            bad[location]['deployment_profile_id'] = PROFILE_IDS[1]
            with self.assertRaisesRegex(BackendError,'binding_mismatch'):
                compiler(bad,'attempt-2',store,lambda:None)
        job['request']['assets']['audio']['model']['key'] = 'owners/other/assets/a/file'
        with self.assertRaisesRegex(BackendError,'owner_mismatch'):
            compiler(job,'attempt-2',store,lambda:None)
        self.assertEqual(len(uploaded),3)

    def test_host_rejects_changed_complete_settings_before_resolving_paths(self):
        identity = PROFILE_IDS[0]
        job,manifest = job_for(identity,get_profile(identity)['verified_cases'][-1])
        prepared = H3ProfileCompiler(manifest,lambda item,*a,**k:item)(job,'attempt-1',NS(open=lambda _:io.BytesIO(b'data')),lambda:None)
        paths = []
        inputs = NS(image_path=lambda *a,**kw:paths.append('image') or '/private/image.png',
            video_path=lambda *a,**kw:paths.append(kw['deployment_profile_id']) or '/private/video.mp4',
            audio_path=lambda *a,**kw:paths.append(kw['deployment_profile_id']) or '/private/audio.wav')
        resolved = resolve_inputs(prepared,inputs,manifest)
        self.assertEqual(resolved['video_guide'],'/private/video.mp4')
        for changes in ({'config':'bf16,bf16'},{'num_inference_steps':101},{'repeat_generation':2},
                {'video_prompt_type':'VP'},{'audio_source':'secret'},{'prompt_enhancer':'anything'},
                {'sliding_window_trim_first_frames':1},{'unknown':1}):
            altered = replace(prepared,settings_json=canonical_json({**prepared.settings,**changes}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve_inputs(altered,inputs,manifest)
        self.assertEqual(len(paths),3)
        with self.assertRaises(ValueError):
            validate_prepared(prepared,engine_manifest(PROFILE_IDS[1],'ref'))

    def test_distinct_factory_model_and_profile_dispatch(self):
        from studio_platform.inference.wangp_factory import create_backend
        manifest = engine_manifest(PROFILE_IDS[0],'fl')
        slot = NS(spec=NS(backend='wangp-worker',configuration_id='c',model_id='MiniMax-H3-Pruned-Rank8-INT8',
            engine_manifest_digest=manifest.digest),runtime_config_file='unused',endpoint='http://127.0.0.1:8199')
        config = {'version':1,'enabled':True,'slot_key':'slot','configuration_id':'c','manifest_file':'m',
            'token_file':'t','runtime_incarnation':'a'*32}
        with patch('studio_platform.inference.wangp_factory.read_document',side_effect=[config,manifest.document]), \
             patch('studio_platform.inference.wangp_factory.private_token_file',return_value='synthetic-token'), \
             patch('studio_platform.inference.wangp_factory.HTTPWanGPTransport',return_value=NS(stage_input=lambda:None)):
            backend = create_backend(slot,None)
        self.assertIsInstance(backend.compiler,H3ProfileCompiler)
        slot.spec.model_id = 'MiniMax-H3-Base-BF16'
        with patch('studio_platform.inference.wangp_factory.read_document',side_effect=[config,manifest.document]), \
             self.assertRaisesRegex(ValueError,'binding_mismatch'):
            create_backend(slot,None)

    def test_host_profile_branch_preserves_one_dispatch_and_durable_duplicate_identity(self):
        from studio_platform.runtime_hosts.wangp import WanGPHost
        from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
        identity = PROFILE_IDS[0]
        job,manifest = job_for(identity,get_profile(identity)['verified_cases'][0])
        prepared = H3ProfileCompiler(manifest,lambda *a:None)(job,'attempt-1',None,lambda:None)
        calls = []
        handle = NS(observe=lambda:None,cancel=lambda:None)
        session = NS(is_idle=lambda:True,submit_task=lambda settings:calls.append(settings) or handle)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);output = root/'outputs';output.mkdir()
            journal = ReceiptJournal(root/'journal.sqlite3',slot_key='native',manifest_digest=manifest.digest,create=True)
            host = WanGPHost(session=session,journal=journal,manifest=manifest,
                output_root=output,sealed_root=root/'sealed')
            try:
                self.assertEqual(host.submit(prepared).state,'running')
                self.assertEqual(host.submit(prepared).state,'running')
                self.assertEqual(len(calls),1)
                changed = {**prepared.settings,'num_inference_steps':101}
                wrong = replace(prepared,attempt_tag='attempt-2',settings_json=canonical_json(changed))
                with self.assertRaisesRegex(BackendError,'settings_resolution_failed'):
                    host.submit(wrong)
                self.assertIsNone(journal.get(wrong.operation_id))
                self.assertEqual(len(calls),1)
            finally:
                host.close()


class ProfileSessionTests(unittest.TestCase):
    def test_profile_config_is_explicit_and_legacy_stays_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'wgp_config.json'
            for identity in PROFILE_IDS:
                config = runtime_config(identity,directory)
                path.write_text(json.dumps(config))
                self.assertEqual(_check_config(path,directory,identity),config)
                with self.assertRaisesRegex(ValueError,'config_mismatch'):
                    _check_config(path,directory)
                config['profile'] = 9
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError,'config_mismatch'):
                    _check_config(path,directory,identity)

    def test_pruned_session_is_only_allowed_with_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream = NS(active_job=None,get_default_settings=lambda m:{'model_type':m},submit_task=lambda v:NS())
            plain = PinnedWanGPSession(upstream,directory,quiesce=lambda:None,worker_alive=lambda:False)
            with self.assertRaisesRegex(ValueError,'model_unsupported'):
                plain.submit_task({'model_type':'minimax_h3_fl2va_pruned'})
            calls = []
            native = PinnedWanGPSession(upstream,directory,quiesce=lambda:None,worker_alive=lambda:False,
                manifest=engine_manifest(PROFILE_IDS[0],'fl'),before_submit=lambda:calls.append(1))
            native.submit_task({'model_type':'minimax_h3_fl2va_pruned'})
            self.assertEqual(calls,[1])
            with self.assertRaisesRegex(ValueError,'model_unsupported'):
                native.submit_task({'model_type':'minimax_h3_ref2va_pruned'})

    def test_effective_loader_rejects_bf16_unsplit_or_quantization_fallback(self):
        identity = PROFILE_IDS[2]
        manifest = engine_manifest(identity,'fl')
        profile = get_profile(identity)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = runtime_config(identity,root)
            for component in manifest.document['components'].values():
                for record in component['files']:
                    file = root/record['path'];file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'fake')
            components = manifest.document['components']
            transformer = components['transformer']['files'][0]['path']
            encoder = components['text_encoder']['files'][0]['path']
            definition = {'architecture':'minimax_h3_fl2va','qkv_splitting':True,
                'video_vae_file':components['video_vae']['files'][0]['path']}
            attention = NS(q_proj=1,k_proj=1,v_proj=1)
            module = NS(server_config=config,transformer_quantization='bf16',text_encoder_quantization='bf16',
                default_profile_video=3,int8_backend=NS(_backend='pytorch'),
                get_model_config_groups=lambda *a:[],model_config_groups=NS(selected_model_configs=lambda *a:[]),
                get_model_filename=lambda model,**kwargs:'https://example/'+(encoder if 'URLs' in kwargs else transformer),
                fl=NS(get_local_model_filename=lambda url,**kw:root/(encoder if encoder in url else transformer),
                    locate_file=lambda name:root/name),loaded_profile=3,loaded_config='bf16,bf16',
                transformer_type='minimax_h3_fl2va',wan_model=NS(transformer=NS(blocks=[NS(attn=attention)],
                    split_linear_modules_map={'qkv':'q'},h3_checkpoint_info={'compressed_modulation':False,'time_embed_dim':2688})))
            session = NS(_ensure_runtime=lambda:NS(module=module),get_model_def=lambda _:definition)
            _audit_profile_runtime(session,manifest,config,loaded=True)
            attention.qkv_proj = 1
            with self.assertRaisesRegex(ValueError,'qkv_changed'):
                _audit_profile_runtime(session,manifest,config,loaded=True)
            del attention.qkv_proj
            module.int8_backend._backend = 'triton'
            with self.assertRaisesRegex(ValueError,'backend_changed'):
                _audit_profile_runtime(session,manifest,config)

    def test_staged_media_profile_uses_native_reference_bounds_and_rejects_unaligned_video(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = StagedInputs(directory)
            audio = InputDescriptor('a','ha','audio','a'*64,4)
            stream = {'codec_name':'pcm_s16le','sample_rate':'32000','channels':2}
            with patch.object(inputs,'_probe_reference',return_value=(stream,{'duration':'5.0'})), \
                 patch.object(inputs,'_typed_copy',return_value=Path(directory)/'audio.wav'):
                with self.assertRaises(ValueError): inputs.audio_path(audio)
                inputs.audio_path(audio,deployment_profile_id=PROFILE_IDS[1])
            video = InputDescriptor('v','hv','video','b'*64,4)
            stream = {'codec_name':'h264','width':832,'height':480,'nb_read_frames':'73',
                'duration':str(73/24),'avg_frame_rate':'24/1','r_frame_rate':'24/1'}
            with patch.object(inputs,'_probe_reference',return_value=(stream,{})), \
                 patch.object(inputs,'_typed_copy',return_value=Path(directory)/'video.mp4'):
                inputs.video_path(video)
                inputs.video_path(video,deployment_profile_id=PROFILE_IDS[1])
                stream.update(nb_read_frames='74',duration=str(74/24))
                with self.assertRaises(ValueError): inputs.video_path(video,deployment_profile_id=PROFILE_IDS[1])
                stream.update(nb_read_frames='56',duration=str(56/24))
                inputs.video_path(video,deployment_profile_id=PROFILE_IDS[1])

    def test_host_checks_actual_aggregate_not_placeholder_metadata_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs=StagedInputs(directory)
            descriptors=[InputDescriptor('v'+str(n),'hv'+str(n),'video',str(n)*64,4) for n in (1,2)]
            descriptors += [InputDescriptor('a'+str(n),'ha'+str(n),'audio',str(n+2)*64,4) for n in (1,2)]
            video_frames,audio_seconds=124,5
            def probe(descriptor,kind):
                return ({'nb_read_frames':str(video_frames)},{}) if kind=='video' else ({},{'duration':str(audio_seconds)})
            with patch.object(inputs,'_probe_reference',side_effect=probe):
                inputs.validate_reference_totals(descriptors,deployment_profile_id=PROFILE_IDS[0])
                video_frames=192
                with self.assertRaisesRegex(ValueError,'total_video_duration_exceeded'):
                    inputs.validate_reference_totals(descriptors,deployment_profile_id=PROFILE_IDS[0])
                video_frames=124;audio_seconds=8
                with self.assertRaisesRegex(ValueError,'total_audio_duration_exceeded'):
                    inputs.validate_reference_totals(descriptors,deployment_profile_id=PROFILE_IDS[0])

    def test_native_source_attestation_rejects_other_install_metadata_before_weights(self):
        from studio_platform.runtime_hosts.wangp_session import verify_runtime
        from studio_platform.inference.wangp_contract import UPSTREAM_REVISION
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = engine_manifest(PROFILE_IDS[2],'fl')
            path = root/'manifest.json';path.write_text(manifest.document_json)
            config = root/'wgp_config.json';config.write_text(json.dumps(runtime_config(PROFILE_IDS[2],root)))
            (root/'requirements.txt').write_text('different requirements')
            results = [NS(stdout=UPSTREAM_REVISION,returncode=0),NS(returncode=0),NS(returncode=0)]
            with patch('studio_platform.runtime_hosts.wangp_session.subprocess.run',side_effect=results), \
                 self.assertRaisesRegex(ValueError,'requirements_mismatch'):
                verify_runtime(root,config,path,root)


if __name__ == '__main__':
    unittest.main()

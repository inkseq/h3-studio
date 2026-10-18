"""V2 compiler/host boundaries and a bounded real CPU media fixture; no GPU/provider."""
import copy
from dataclasses import replace
import io
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from comfy_workflow import native_output_spec
from studio_platform.inference import wangp_full_compiler as compiler
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_contract import EngineManifest, InputDescriptor, PreparedRequest, canonical_json
from studio_platform.runtime_catalog import engine_manifest, get_profile
from studio_platform.runtime_hosts.wangp_http import StagedInputs
from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs

PROFILE='h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4'


def meta(kind,**changes):
    return {'kind':kind,'model_ready':True,'width':832,'height':480,'frame_count':56,'fps':24,
        'duration':56/24 if kind=='video' else 4,'source_duration':56/24,'has_audio':False,
        'sample_rate':32000,'channels':2,**changes}


def case(mode='ref',images=(),videos=(),audios=(),**changes):
    metadata={i:meta(kind) for ids,kind in ((images,'image'),(videos,'video'),(audios,'audio')) for i in ids}
    request={'model':get_profile(PROFILE)['model_id'],'mode':mode,'prompt':'Synthetic compiler boundary',
        'duration':5,'resolution':'480P','seed':'42',
        'inputs':{'images':list(images),'videos':list(videos),'audios':list(audios)},**changes}
    return request,metadata


def settings(request,metadata):
    return compiler.compile_settings(request,metadata,native_output_spec(request),{i:'handle-'+i for i in metadata},PROFILE)


def manifest(mode):
    doc=engine_manifest(PROFILE,mode).document
    doc['compiler_id']=compiler.COMPILER_ID
    return EngineManifest.from_dict(doc)


def prepare(request,metadata):
    s=settings(request,metadata); s['output_filename']='sixnine-test'
    m=manifest(request['mode'])
    p=PreparedRequest('job','test','a'*64,m.digest,canonical_json(s),canonical_json(native_output_spec(request)),
        request.get('generate_audio',True),tuple(InputDescriptor(i,'handle-'+i,v['kind'],'b'*64,4) for i,v in metadata.items()))
    return p,m


class FullCompilerTests(unittest.TestCase):
    def host(self,prepared,m,metadata=None):
        # Catalog routing/new immutable profiles are integration-owner scope.
        # Use an exact v2 manifest and actual pinned profile payload here.
        with patch.object(compiler,'validate_manifest',return_value=get_profile(PROFILE)):
            compiler.validate_prepared(prepared,m)
            if metadata is not None: compiler.validate_prepared_media(prepared,m,metadata)

    def test_additive_v6_manifest_uses_actual_catalog_without_legacy_patch(self):
        identity='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        r,m=case(images=['i'],videos=['v1','v2'],video_audio={'v1':True,'v2':False})
        m['v1']['has_audio']=True
        binding=engine_manifest(identity,'ref')
        self.assertEqual(binding.document['compiler_id'],compiler.COMPILER_ID)
        job={'id':'job','owner_id':'owner','request_hash':'a'*64,
            'request':{'request':r,'assets':{k:{'metadata':v,'model':{'key':'owners/owner/assets/'+k+'/file',
                'sha256':'b'*64,'size_bytes':4}} for k,v in m.items()},'output_spec':native_output_spec(r),
                'deployment_profile_id':identity,'recipe_id':binding.document['generation_recipe_id']},
            'execution_plan':{'deployment_profile_id':identity,'engine_manifest_digest':binding.digest}}
        p=compiler.H3ProfileCompiler(binding,lambda item,*args,**kw:item)(job,'test',
            SimpleNamespace(open=lambda _:io.BytesIO(b'data')),lambda:None)
        compiler.validate_prepared(p,binding)
        compiler.validate_prepared_media(p,binding,m)

    def test_scalar_controls_map_to_real_native_fields_and_roundtrip(self):
        r,m=case(images=['image'],steps=73,sampler_name='er_sde',shift_video=8.75,
            image_refs_relative_size=275,audio_refinement='enabled',export_crf=32,generate_audio=False)
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual((p.settings['num_inference_steps'],p.settings['sample_solver'],p.settings['flow_shift']),
                         (73,'er_sde',8.75))
        self.assertEqual(p.settings['image_refs_relative_size'],275)
        self.assertEqual(p.settings['custom_settings']['audio_refinement'],'enabled')
        self.assertTrue(p.settings['_api']['return_audio'])
        self.assertFalse(p.generate_audio)

    def test_ref_start_end_injection_reference_reuse_binds_one_descriptor(self):
        r,m=case(images=['shared'],inputs={'images':['shared'],'first_frame':'shared','last_frame':'end'},
                 guides=[{'media_id':'shared','time_seconds':1}])
        m['end']=meta('image')
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(len(p.inputs),2)
        self.assertEqual(p.settings['image_prompt_type'],'SE')
        self.assertEqual(p.settings['image_refs'],['handle-shared','handle-shared'])
        self.assertEqual(p.settings['frames_positions'],'25')

    def test_fl_images_are_injections_not_silent_reference_inputs(self):
        r,m=case('fl',images=['i'],guides=[{'media_id':'i','time_seconds':2}])
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['video_prompt_type'],'KFI')
        r['guides']=[]
        with self.assertRaisesRegex(ValueError,'fl_reference_image'): settings(r,m)

    def test_guide_uses_native_tail_frame_and_keeps_anchor_collision(self):
        r,m=case('fl',images=['i'],guides=[{'media_id':'i','time_seconds':123/24}])
        self.assertEqual(native_output_spec(r)['frames'],124)
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['frames_positions'],'124')
        r['guides'][0]['time_seconds']=124/24
        with self.assertRaisesRegex(ValueError,'guide_outside_output'): settings(r,m)
        r['guides'][0]['time_seconds']=123/24; r['inputs']['last_frame']='i'
        with self.assertRaisesRegex(ValueError,'conflicting_image_anchors'): settings(r,m)

    def test_mixed_video_audio_and_audio_reference_use_actual_audio_roles(self):
        r,m=case(images=['i'],videos=['v1','v2'],audios=['a'],video_audio={'v1':True,'v2':False})
        m['v1']['has_audio']=True
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['audio_prompt_type'],'AB')
        self.assertEqual(p.settings['audio_guide'],'handle-a')
        self.assertEqual(p.settings['audio_guide2'],'handle-v1')
        self.assertEqual(compiler.audio_source_handles(p.settings),{'handle-a','handle-v1'})
        self.assertEqual(len(p.inputs),4)
        m['v1']['has_audio']=False
        with self.assertRaisesRegex(ValueError,'has_no_audio'): settings(r,m)

    def test_all_video_tracks_use_native_combined_k_count_not_duplicate_rows(self):
        r,m=case(images=['i'+str(n) for n in range(9)],videos=['v1','v2','v3'],
            video_audio={'v1':True,'v2':True,'v3':True})
        for key in r['inputs']['videos']: m[key]['has_audio']=True
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['audio_prompt_type'],'K')
        self.assertEqual(p.settings['audio_guide'],None)
        r['video_audio']['v3']=False
        with self.assertRaisesRegex(ValueError,'reference_count_exceeded'): settings(r,m)

    def test_kept_video_soundtrack_requires_the_single_native_control_source(self):
        r,m=case(images=['i'],videos=['v1','v2'],audio_conditioning='soundtrack',
            video_audio={'v1':False,'v2':True})
        m['v2']['has_audio']=True
        with self.assertRaisesRegex(ValueError,'video_soundtrack_requires_single_video'): settings(r,m)
        r['inputs']['videos']=['v2']; r['video_audio']={'v2':True}; del m['v1']
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['audio_prompt_type'],'KS')
        self.assertEqual(p.settings['video_guide'],'handle-v2')

    def test_effective_excerpts_are_native_rows_even_when_uploaded_once(self):
        r,m=case(images=['i'+str(n) for n in range(9)],videos=['v'],
            video_excerpts=[{'time_seconds':2,'duration_seconds':56/24},
                {'time_seconds':4,'duration_seconds':56/24},
                {'time_seconds':6,'duration_seconds':56/24}])
        m['v'].update(frame_count=192,duration=8,source_duration=8)
        self.assertEqual(compiler.reference_usage(r,m,native_output_spec(r),PROFILE)['reference_rows'],12)
        p,binding=prepare(r,m); self.host(p,binding,m)
        r['inputs']['audios']=['a']; m['a']=meta('audio',duration=2)
        with self.assertRaisesRegex(ValueError,'reference_count_exceeded'): settings(r,m)

    def test_true_native_reference_counts_12_and_visual_audio_limits(self):
        for images,videos,audios in ((9,3,0),(9,0,3),(3,3,3)):
            r,m=case(images=['i'+str(n) for n in range(images)],videos=['v'+str(n) for n in range(videos)],
                audios=['a'+str(n) for n in range(audios)])
            p,binding=prepare(r,m); self.host(p,binding,m)
        r,m=case(images=['i'+str(n) for n in range(9)],videos=['v'+str(n) for n in range(3)],
            audios=['a'+str(n) for n in range(3)])
        with self.assertRaisesRegex(ValueError,'reference_count_exceeded'): settings(r,m)
        r,m=case(images=['i'],audios=['a','b'])
        with self.assertRaisesRegex(ValueError,'visual_reference'): settings(r,m)

    def test_native_usage_excludes_fi_anchors_and_kept_audio_from_twelve_rows(self):
        r,m=case(images=['i'+str(n) for n in range(9)],videos=['v1','v2','v3'],audios=['kept'],
            audio_conditioning='soundtrack',guides=[{'media_id':'g'+str(n),'time_seconds':.25*(n+1)} for n in range(8)])
        r['inputs'].update(first_frame='first',last_frame='last')
        m.update({i:meta('image') for i in ['first','last']+['g'+str(n) for n in range(8)]})
        self.assertEqual(len(m),23)
        self.assertEqual(compiler.reference_usage(r,m,native_output_spec(r),PROFILE),
            {'reference_rows':12,'reference_images':9,'reference_videos':3,'reference_audios':0})
        p,binding=prepare(r,m); self.host(p,binding,m)

    def test_fl_control_denoise_mask_and_kept_soundtrack_are_explicit(self):
        r,m=case('fl',videos=['v'],video_conditioning='denoise',audio_conditioning='soundtrack',
            video_audio={'v':True},denoise=.45,mask_mode='shared_timestep')
        m['v'].update(frame_count=124,duration=124/24,source_duration=5,has_audio=True)
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual((p.settings['video_prompt_type'],p.settings['audio_prompt_type']),('GV','K'))
        self.assertEqual(p.settings['denoising_strength'],.45)
        r['video_conditioning']='in_context'
        with self.assertRaisesRegex(ValueError,'denoise_requires'): settings(r,m)
        r.pop('video_conditioning')
        with self.assertRaisesRegex(ValueError,'conditioning_required'): settings(r,m)

    def test_time_zero_media_guides_and_from_video_audio_are_not_timed_fake_refs(self):
        r,m=case('fl',video_conditioning='denoise',audio_conditioning='from_video',
            guides=[{'media_id':'v','time_seconds':0}])
        m['v']=meta('video',frame_count=124,duration=124/24,source_duration=5)
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['audio_prompt_type'],'2')
        r,m=case(images=['i'],audio_conditioning='soundtrack',guides=[{'media_id':'a','time_seconds':0}])
        m['a']=meta('audio'); p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['audio_prompt_type'],'AS')
        r['guides'][0]['time_seconds']=1
        with self.assertRaisesRegex(ValueError,'timed_audio_guide_unsupported'): settings(r,m)

    def test_excerpt_source_bounds_and_independent_audio_positions(self):
        r,m=case(videos=['v'],video_audio={'v':True},
            video_excerpts=[{'time_seconds':3,'duration_seconds':56/24}],
            audio_excerpts=[{'time_seconds':4,'duration_seconds':2}])
        m['v'].update(frame_count=175,duration=175/24,source_duration=7,has_audio=True)
        p,binding=prepare(r,m); self.host(p,binding,m)
        self.assertEqual(p.settings['video_prompt_type'],'V1-U')
        self.assertEqual(p.settings['audio_prompt_type'],'K1')
        self.assertEqual(p.settings['custom_settings']['h3_audio_excerpt_positions'],'4.0s/2.0s')
        r['video_excerpts'][0]['time_seconds']=0
        with self.assertRaisesRegex(ValueError,'outside_selected_source'): settings(r,m)
        r['video_excerpts'][0].update(time_seconds=3,duration_seconds=2)
        normalized=compiler.normalize_request(r,m,native_output_spec(r),PROFILE)
        self.assertEqual(normalized['video_excerpts'][0]['duration_seconds'],56/24)
        p,binding=prepare(r,m); self.host(p,binding,m)

    def test_ignored_controls_conflicts_and_unbound_native_settings_rejected(self):
        base,m=case(images=['i'])
        for changes in ({'shift_audio':4},{'video_tile_size':512},{'audio_decode':'tiled'},
                {'guidance_phases':2},{'denoise':.5},{'mask_mode':'shared_timestep'},
                {'inputs':{'images':['i'],'guides':[]},'guides':[]}):
            r={**copy.deepcopy(base),**changes}
            with self.subTest(changes=changes),self.assertRaises(ValueError): settings(r,m)
        r=copy.deepcopy(base); r['inputs']['first_frame']='i'; r['guides']=[{'media_id':'i','time_seconds':0}]
        with self.assertRaisesRegex(ValueError,'conflicting_image_anchors'): settings(r,m)
        p,binding=prepare(base,m)
        for key,value in (('override_attention','flash'),('video_prompt_type','KIGV'),('video_guide','unbound')):
            changed=p.settings; changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.host(replace(p,settings_json=canonical_json(changed)),binding)

    def test_class_checks_every_owner_before_any_transfer_and_stages_once(self):
        r,m=case(images=['i'],inputs={'images':['i'],'first_frame':'i'})
        binding=manifest('ref'); profile=get_profile(PROFILE)
        assets={k:{'metadata':v,'model':{'key':'owners/owner/assets/'+k+'/file','sha256':'b'*64,'size_bytes':4}}
                for k,v in m.items()}
        job={'id':'job','owner_id':'owner','request_hash':'a'*64,
            'request':{'request':r,'assets':assets,'output_spec':native_output_spec(r),
                'deployment_profile_id':PROFILE,'recipe_id':binding.document['generation_recipe_id']},
            'execution_plan':{'deployment_profile_id':PROFILE,'engine_manifest_digest':binding.digest}}
        staged=[]
        with patch.object(compiler,'validate_manifest',return_value=profile):
            actor=compiler.H3ProfileCompiler(binding,lambda item,*args,**kw:staged.append(item) or item)
            p=actor(job,'test',SimpleNamespace(open=lambda _:io.BytesIO(b'data')),lambda:None)
            self.assertEqual(len(staged),1); self.host(p,binding,m)
            staged.clear(); job['request']['assets']['i']['model']['key']='owners/foreign/assets/i/file'
            with self.assertRaisesRegex(BackendError,'owner_mismatch'):
                actor(job,'test',SimpleNamespace(open=lambda _:self.fail('no read')),lambda:None)
            self.assertEqual(staged,[])


class FullHostInputsTests(unittest.TestCase):
    profile='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'

    def prepared(self,request,metadata):
        binding=engine_manifest(self.profile,request['mode'])
        s=compiler.compile_settings(request,metadata,native_output_spec(request),
            {i:'handle-'+i for i in metadata},self.profile)
        s['output_filename']='sixnine-test'
        p=PreparedRequest('job','test','a'*64,binding.digest,canonical_json(s),canonical_json(native_output_spec(request)),True,
            tuple(InputDescriptor(i,'handle-'+i,v['kind'],'b'*64,4) for i,v in metadata.items()))
        return p,binding

    @staticmethod
    def video(audio=True):
        streams=[{'codec_type':'video','codec_name':'h264','width':832,'height':480,
            'nb_read_frames':'56','duration':str(56/24),'avg_frame_rate':'24/1','r_frame_rate':'24/1'}]
        if audio: streams.append({'codec_type':'audio','codec_name':'aac','sample_rate':'32000','channels':2,'duration':str(56/24)})
        return {'streams':streams,'format':{'duration':str(56/24)}}

    def test_v2_resolves_reused_image_once_and_selected_video_as_private_pcm(self):
        r,m=case(images=['i'],videos=['v1','v2'],inputs={'images':['i'],'videos':['v1','v2'],'first_frame':'i'},
            video_audio={'v1':True,'v2':False},guides=[{'media_id':'i','time_seconds':1}])
        m['v1']['has_audio']=True
        p,binding=self.prepared(r,m)
        resolved=[]; extracted=[]
        def full(descriptor,**kwargs):
            resolved.append(descriptor.asset_id)
            return Path('/private/'+descriptor.asset_id),m[descriptor.asset_id]
        def audio(descriptor,**kwargs):
            extracted.append(descriptor.asset_id); return Path('/private/derived.wav')
        value=resolve_inputs(p,SimpleNamespace(full_input=full,audio_from_video_path=audio),binding)
        self.assertCountEqual(resolved,['i','v1','v2'])
        self.assertEqual(extracted,['v1'])
        self.assertEqual(value['image_start'],value['image_refs'][0])
        self.assertEqual(value['audio_guide'],str(Path('/private/derived.wav')))
        self.assertEqual(value['video_guide'],str(Path('/private/v1')))
        bad=copy.deepcopy(m); bad['v1']['has_audio']=False
        with self.assertRaisesRegex(ValueError,'has_no_audio'):
            resolve_inputs(p,SimpleNamespace(full_input=lambda d,**kw:(Path('/unused'),bad[d.asset_id]),
                audio_from_video_path=lambda *a,**kw:self.fail('extract after invalid actual facts')),binding)

    def test_v2_actual_media_probe_rejects_extra_streams_and_preserves_old_silent_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            staged=StagedInputs(directory)
            raw=b'original-immutable-video'; d=InputDescriptor('v','hv','video',hashlib.sha256(raw).hexdigest(),len(raw))
            staged.save(d,io.BytesIO(raw))
            data=self.video()
            def tool(*args,**kwargs): return subprocess.CompletedProcess([],0,json.dumps(data).encode())
            with patch('studio_platform.runtime_hosts.wangp_http.run_media_process',side_effect=tool):
                actual=staged.reference_metadata(d,deployment_profile_id=self.profile)
                self.assertTrue(actual['has_audio'])
                with self.assertRaisesRegex(ValueError,'media_probe_rejected'):
                    staged.video_path(d,deployment_profile_id=PROFILE)
                for extra in ({'codec_type':'subtitle'},{'codec_type':'audio','codec_name':'aac'},
                              {'codec_type':'data'}):
                    data['streams'].append(extra)
                    with self.subTest(extra=extra),self.assertRaisesRegex(ValueError,'normalized_media_required'):
                        staged.reference_metadata(d,deployment_profile_id=self.profile)
                    data['streams'].pop()
                data['streams'][1]['sample_rate']='44100'
                with self.assertRaisesRegex(ValueError,'normalized_media_required'):
                    staged.reference_metadata(d,deployment_profile_id=self.profile)
            staged.store._directory(staged.key(d)).joinpath('blob').write_bytes(b'X'*len(raw))
            with patch('studio_platform.runtime_hosts.wangp_http.run_media_process',side_effect=AssertionError('no probe before hash')):
                with self.assertRaisesRegex(ValueError,'staged_input_mismatch'):
                    staged.reference_metadata(d,deployment_profile_id=self.profile)

    def test_video_audio_extraction_uses_exact_original_and_bounded_private_derivative(self):
        with tempfile.TemporaryDirectory() as directory:
            staged=StagedInputs(directory)
            raw=b'original-immutable-video'; d=InputDescriptor('v','hv','video',hashlib.sha256(raw).hexdigest(),len(raw))
            staged.save(d,io.BytesIO(raw)); commands=[]
            audio_data={'streams':[{'codec_type':'audio','codec_name':'pcm_s16le','sample_rate':'32000','channels':2}],
                'format':{'duration':str(56/24)}}
            def tool(argv,**kwargs):
                commands.append((argv,kwargs))
                if argv[0]=='ffmpeg':
                    self.assertEqual(Path(argv[argv.index('-i')+1]).read_bytes(),raw)
                    Path(argv[-1]).write_bytes(b'closed-synthetic-pcm-fixture')
                    return subprocess.CompletedProcess([],0)
                return subprocess.CompletedProcess([],0,json.dumps(self.video() if '-f' in argv and argv[argv.index('-f')+1]=='mov' else audio_data).encode())
            with patch('studio_platform.runtime_hosts.wangp_http.run_media_process',side_effect=tool):
                first=staged.audio_from_video_path(d,deployment_profile_id=self.profile)
                second=staged.audio_from_video_path(d,deployment_profile_id=self.profile)
                self.assertEqual(first,second)
                self.assertIn(d.sha256,first.name)
                self.assertIn(hashlib.sha256(first.read_bytes()).hexdigest(),first.name)
                self.assertEqual(staged.resolve(d).read_bytes(),raw)
                self.assertEqual(first.stat().st_nlink,1)
                self.assertFalse(list(first.parent.glob('*.pending.wav')))
                ffmpeg=[row for row in commands if row[0][0]=='ffmpeg']
                self.assertEqual(len(ffmpeg),2)
                for argv,kwargs in ffmpeg:
                    self.assertEqual(argv[argv.index('-protocol_whitelist')+1],'file,pipe')
                    self.assertEqual(argv[argv.index('-map')+1],'0:a:0')
                    self.assertEqual(kwargs['timeout'],30)
                    self.assertEqual(kwargs['memory_bytes'],512*1024**2)
                audio_data['streams'][0]['channels']=1
                with self.assertRaisesRegex(ValueError,'extraction_rejected'):
                    staged.audio_from_video_path(d,deployment_profile_id=self.profile)
                self.assertEqual(first.read_bytes(),b'closed-synthetic-pcm-fixture')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'CPU ffmpeg/ffprobe required')
    def test_real_audiovisual_staging_and_pcm_derivative_preserve_hash_and_template(self):
        """Exercise real demux/probe rather than accepting fabricated metadata."""
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='sixnine-controls-media-') as directory:
            root=Path(directory); video=root/'source.mp4'; picture=root/'picture.png'
            subprocess.run(['ffmpeg','-v','error','-nostdin','-y',
                '-f','lavfi','-i','color=c=blue:s=832x480:r=24',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=32000',
                '-frames:v','56','-t',str(56/24),'-c:v','libx264','-threads','1',
                '-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-ar','32000','-ac','2',str(video)],
                check=True,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            Image.new('RGB',(832,480),(10,20,30)).save(picture)
            staged=StagedInputs(root/'staged')
            values={}; originals={}; metadata={}
            for identity,path,kind in (('v1',video,'video'),('i',picture,'image')):
                raw=path.read_bytes(); originals[identity]=raw
                descriptor=InputDescriptor(identity,'handle-'+identity,kind,hashlib.sha256(raw).hexdigest(),len(raw))
                values[identity]=descriptor; staged.save(descriptor,io.BytesIO(raw))
                metadata[identity]=staged.reference_metadata(descriptor,deployment_profile_id=self.profile)
            # Admission provenance (the host itself only knows actual bytes).
            metadata['v1']['source_duration']=56/24
            r,_=case(images=['i'],videos=['v1'],audios=['a'],video_audio={'v1':True})
            # A separate audio source forces the mixed-video PCM path, rather
            # than native K, and proves the upstream receives readable WAVs.
            audio=root/'source.wav'
            with wave.open(str(audio),'wb') as output:
                output.setnchannels(2); output.setsampwidth(2); output.setframerate(32000)
                output.writeframes(b'\0'*32000*2*2*2)
            raw=audio.read_bytes(); originals['a']=raw
            d=InputDescriptor('a','handle-a','audio',hashlib.sha256(raw).hexdigest(),len(raw)); values['a']=d
            staged.save(d,io.BytesIO(raw)); metadata['a']=staged.reference_metadata(d,deployment_profile_id=self.profile)
            prepared,binding=self.prepared(r,metadata)
            prepared=replace(prepared,inputs=tuple(values.values()))
            before=prepared.identity_digest
            settings=resolve_inputs(prepared,staged,binding)
            self.assertEqual(settings['audio_prompt_type'],'AB')
            for field in ('audio_guide','audio_guide2'):
                with wave.open(settings[field],'rb') as stream:
                    self.assertEqual((stream.getframerate(),stream.getnchannels(),stream.getsampwidth()),(32000,2,2))
            self.assertEqual(prepared.identity_digest,before)
            for identity,descriptor in values.items():
                self.assertEqual(staged.resolve(descriptor).read_bytes(),originals[identity])
            self.assertEqual(len(list((staged.root/'typed-video-audio').glob('*.wav'))),1)


if __name__=='__main__': unittest.main()

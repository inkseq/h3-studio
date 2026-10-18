"""Selected-profile integration boundaries on temporary SQLite; no paid calls."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from studio_platform.api import create_app
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies, input_envelope_blockers, validate_policy
from studio_platform.h3_profile_support import envelope
from studio_platform.inference import wangp_profile_dispatch
from studio_platform.quick_chat import default_next_settings, settings_check
from studio_platform.repository import Repository, Scope
from studio_platform.runtime_catalog import engine_manifest, get_profile, profile_output_delivery
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy


PROFILE = 'h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
LEGACY = 'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4'


def image_asset(identity):
    return {'metadata': {'kind':'image', 'model_ready':True, 'width':832, 'height':480},
        'model': {'key':'owners/superdan/assets/'+identity+'/model', 'sha256':'a'*64, 'size_bytes':4}}


class FullControlsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sixnine-full-controls-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Repository('sqlite:///'+(self.root/'test.sqlite').as_posix())
        self.repo.create_schema()
        self.addCleanup(self.repo.close)
        self.profiles = self.root/'profiles.json'
        self.policies = [self.bound_policy(mode) for mode in ('fl','ref')]
        self.profiles.write_text(json.dumps({'schema_version':1,'policies':self.policies}),encoding='utf8')
        self.settings = Settings(self.root,auth_mode='local-test',execution_backend='wangp-worker',
            generation_enabled=True,execution_profiles_file=self.profiles,default_deployment_profile_id=PROFILE)
        self.repo.configure_capacity(max_instances=2,max_physical_gpus=2)
        for identity,owner in (('test-tenant:sixnine',None),('test-owner:sixnine:superdan','superdan')):
            self.repo.configure_budget(identity,tenant_id='sixnine',owner_id=owner,limit_microusd=10_000_000)

    def bound_policy(self,mode):
        result = policy(self.repo.clock())
        result.update(deployment_profile_id=PROFILE,model_id=get_profile(PROFILE)['model_id'],
            backend='wangp-worker',pool='test-full-'+mode,configuration_id='test-full-'+mode,
            recipe_ids=['h3-base-'+('fl2va' if mode=='fl' else 'ref2va')+'-v1'],
            engine_manifest_digest=engine_manifest(PROFILE,mode).digest,
            output_delivery=profile_output_delivery(PROFILE))
        result['qualification'].update(status='runtime_required',profile=QUEUED_TASK_PROFILE)
        result['envelope']=envelope(PROFILE,mode)
        return validate_policy(result)

    def compile(self,mode='ref',inputs=None,controls=None):
        body=generation_request(deployment_profile_id=PROFILE,
            recipe_id='h3-base-'+('fl2va' if mode=='fl' else 'ref2va')+'-v1',
            inputs=inputs or {},controls={'duration':5,'resolution':'480P','seed':'42',**(controls or {})})
        return compile_request(body,lambda identity:image_asset(identity),backend='wangp-worker')

    def test_new_default_is_valid_saved_settings_and_selected_public_schema(self):
        selected=default_next_settings(self.settings)
        self.assertEqual(settings_check(selected),selected)
        self.assertEqual(selected['deployment_profile_id'],PROFILE)
        self.assertNotIn('guides',selected['controls'])
        self.assertNotIn('video_audio',selected['controls'])
        with TestClient(create_app(self.settings,repository=self.repo)) as client:
            client.post('/api/auth/login',json={'username':'superdan'}).raise_for_status()
            response=client.get('/v1/quick-chat/schema'); response.raise_for_status()
            schema=response.json()
            self.assertEqual(schema['default_next_settings'],selected)
            profile=next(item for item in schema['capabilities']['deployment_profiles'] if item['id']==PROFILE)
            self.assertTrue(profile['generation_support']['ref']['input_support']['first_frame'])
            controls=profile['generation_support']['ref']['controls']
            self.assertIn('er_sde',controls['sampler_name']['enum'])
            self.assertEqual(controls['image_refs_relative_size']['maximum'],400)
            self.assertIn('video_audio',controls)

    def test_ref_policy_counts_nine_references_not_eight_injections_or_two_anchors(self):
        images=['ref-'+str(i) for i in range(9)]
        guides=[{'media_id':'injection-'+str(i),'time_seconds':(i+1)/4} for i in range(8)]
        compiled,fingerprint=self.compile(inputs={'images':images,'first_frame':'first','last_frame':'last',
            'guides':guides},controls={'steps':73,'sampler_name':'er_sde','shift_video':8.75})
        self.assertEqual(len(compiled['assets']),19)
        value=self.policies[1]
        value['envelope']['max_reference_files']=9
        self.assertEqual(validate_policy(value),value)
        limits=value['envelope']['input_limits']
        self.assertEqual(input_envelope_blockers(compiled,limits),[])
        smaller=copy.deepcopy(limits); smaller['max_images']=9
        self.assertTrue(input_envelope_blockers(compiled,smaller))
        self.profiles.write_text(json.dumps({'schema_version':1,'policies':self.policies}),encoding='utf8')
        control=WorkerControl(self.repo)
        control.register(WorkerSpec('test-full-ref-worker',value['pool'],'test-provider','test-instance',
            ('test-gpu',),tuple(value['recipe_ids']),value['model_id'],value['configuration_id'],
            backend='wangp-worker',engine_manifest_digest=value['engine_manifest_digest'],
            output_delivery=value['output_delivery']))
        control.mark_ready('test-full-ref-worker',upstream_idle_confirmed=True)
        before=self.profiles.read_bytes()
        admission=ExecutionPolicies(self.settings,self.repo).evaluate(compiled,Scope('sixnine','superdan','story-one'),fingerprint)
        self.assertTrue(admission.execution['enabled'],admission.execution['blockers'])
        self.assertEqual(admission.execution['engine_manifest_digest'],engine_manifest(PROFILE,'ref').digest)
        self.assertEqual(self.profiles.read_bytes(),before)
        # Narrowing an actual resource policy remains effective; broad support
        # never bypasses the operator's explicit nine-row reservation envelope.
        value['envelope']['max_reference_files']=8
        self.profiles.write_text(json.dumps({'schema_version':1,'policies':self.policies}),encoding='utf8')
        blocked=ExecutionPolicies(self.settings,self.repo).evaluate(compiled,Scope('sixnine','superdan','story-one'),fingerprint)
        self.assertFalse(blocked.execution['enabled'])
        self.assertTrue(any('9' in item and '8' in item for item in blocked.execution['blockers']))

    def test_native_tail_guide_is_admitted_but_endpoint_and_anchor_collisions_are_not(self):
        for seconds,frame in ((5,123),(15,361)):
            with self.subTest(seconds=seconds):
                controls={'duration':seconds,'steps':37}
                inputs={'guides':[{'media_id':'tail','time_seconds':frame/24}]}
                compiled,_=self.compile('fl',inputs,controls)
                self.assertEqual(compiled['output_spec']['frames'],frame+1)
                self.assertEqual(input_envelope_blockers(compiled,envelope(PROFILE,'fl')['input_limits']),[])
                settings=wangp_profile_dispatch.compile_settings(compiled['request'],
                    {key:item['metadata'] for key,item in compiled['assets'].items()},compiled['output_spec'],
                    {'tail':'input-tail'},PROFILE)
                self.assertEqual(settings['frames_positions'],str(frame+1))
                inputs['last_frame']='tail'
                with self.assertRaisesRegex(ValueError,'conflicting_image_anchors'):
                    self.compile('fl',inputs,controls)
                del inputs['last_frame']; inputs['guides'][0]['time_seconds']=(frame+1)/24
                with self.assertRaisesRegex(ValueError,'guide_outside_output'):
                    self.compile('fl',inputs,controls)

    def test_legacy_profile_does_not_silently_acquire_new_native_roles(self):
        before=engine_manifest(LEGACY,'ref')
        self.assertNotEqual(before.digest,engine_manifest(PROFILE,'ref').digest)
        body=generation_request(deployment_profile_id=LEGACY,recipe_id='h3-base-ref2va-v1',
            controls={'duration':5,'resolution':'480P','steps':20,'seed':'42'},inputs={'images':['ref']})
        compiled,_=compile_request(body,lambda identity:image_asset(identity),backend='wangp-worker')
        settings=wangp_profile_dispatch.compile_settings(compiled['request'],
            {key:item['metadata'] for key,item in compiled['assets'].items()},compiled['output_spec'],{'ref':'input-ref'},LEGACY)
        self.assertEqual(settings['model_type'],get_profile(LEGACY)['models'][1]['model_type'])
        body['inputs']['first_frame']='anchor'
        with self.assertRaises(ValueError):
            compile_request(body,lambda identity:image_asset(identity),backend='wangp-worker')
        self.assertEqual(engine_manifest(LEGACY,'ref').document_json,before.document_json)

    def test_browser_and_agent_http_save_same_v2_controls_and_ref_anchors_without_job(self):
        app=create_app(self.settings,repository=self.repo)
        with TestClient(app) as browser:
            # One application lifespan, separate cookie jars/credentials.
            agent=TestClient(app)
            self.addCleanup(agent.close)
            browser.post('/api/auth/login',json={'username':'superdan'}).raise_for_status()
            result=browser.post('/v1/api-keys',json={'name':'Synthetic controls test',
                'scopes':['projects:create','projects:read','projects:write','assets:read','assets:write','jobs:read'],
                'all_projects':True,'expires_in_days':1})
            result.raise_for_status()
            agent.headers['Authorization']='Bearer '+result.json()['api_key']
            controls={'duration':5,'resolution':'480P','steps':31,'seed':'7','sampler_name':'ralston_2s',
                'video_conditioning':'reference','audio_conditioning':'reference',
                'image_refs_relative_size':125,'audio_refinement':'enabled','video_excerpts':[],'audio_excerpts':[]}
            revisions=[]
            for channel,client in (('browser',browser),('agent',agent)):
                session=client.post('/v1/quick-chat/sessions',json={'title':'Synthetic '+channel},
                    headers={'Idempotency-Key':channel+'-session'})
                session.raise_for_status(); sid=session.json()['session']['id']
                source=io.BytesIO(); Image.new('RGB',(832,480),'navy').save(source,'PNG')
                upload=client.post('/v1/quick-chat/sessions/'+sid+'/assets',
                    files={'file':('reference.png',source.getvalue(),'image/png')},data={'client_asset_id':channel+'-image'})
                upload.raise_for_status(); identity=upload.json()['asset_id']
                session_path='/v1/quick-chat/sessions/'+sid
                current=client.get(session_path).json()['session']
                changed=client.patch(session_path,json={'expected_version':current['version'],
                    'next_settings':{'deployment_profile_id':PROFILE,'recipe_id':'h3-base-ref2va-v1',
                        'controls':copy.deepcopy(controls),'copies':1}},
                    headers={'Idempotency-Key':channel+'-settings'})
                changed.raise_for_status()
                current=changed.json()['session']
                bindings=[{'binding_id':channel+'-first','version':0,'asset_id':identity,'kind':'image',
                    'slot':'first_frame','enabled':True}]
                selected=client.put(session_path+'/materials',json={'expected_version':current['version'],'bindings':bindings},
                    headers={'Idempotency-Key':channel+'-materials'})
                selected.raise_for_status()
                current=client.get(session_path).json()['session']
                self.assertEqual(current['input_refs']['first_frame'],{'asset_id':identity})
                body={'deployment_profile_id':PROFILE,'recipe_id':'h3-base-ref2va-v1','copies':1,
                    'prompt':'A blue paper bird unfolds over a desk.','controls':copy.deepcopy(controls),
                    'inputs':{'first_frame':{'asset_id':identity},'last_frame':{'asset_id':identity},
                        'images':[{'asset_id':identity}],
                        'guides':[{'media_id':identity,'time_seconds':1,'use_audio':False}]}}
                path='/v1/quick-chat/sessions/'+sid+'/cards'
                response=client.post(path,json=body,headers={'Idempotency-Key':channel+'-card'})
                self.assertEqual(response.status_code,201,response.text)
                revision=response.json()['revision']; revisions.append(revision)
                self.assertEqual(revision['deployment_profile_id'],PROFILE)
                self.assertEqual(revision['controls'],controls)
                for slot in ('first_frame','last_frame'):
                    self.assertEqual(revision['inputs'][slot],{'asset_id':identity})
                self.assertEqual(revision['inputs']['guides'][0]['media_id'],identity)
                # Authoring did not create a render or paid generation task.
                self.assertEqual(client.get('/v1/jobs').json()['jobs'],[])
            self.assertEqual(revisions[0]['controls'],revisions[1]['controls'])


if __name__=='__main__':
    unittest.main()

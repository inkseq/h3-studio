"""Exact profile selection through shared admission; temporary SQL, no GPU calls."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.execution_profiles import read_profiles
from studio_platform.repository import Repository, Scope
from studio_platform.runtime_catalog import get_profile, engine_manifest
from studio_platform.qualification_profiles import MULTIMODAL_INPUT_LIMITS, QUEUED_TASK_PROFILE
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy

PRUNED = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
INT8 = "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1"


class ExecutionProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = Repository("sqlite:///" + (self.root / "test.sqlite").as_posix())
        self.repo.create_schema()
        self.addCleanup(self.repo.close)
        self.path = self.root / "profiles.json"
        self.settings = Settings(self.root, auth_mode="local-test", execution_backend="wangp-worker",
            generation_enabled=True, execution_profiles_file=self.path)
        self.scope = Scope("sixnine", "superdan", "story-one")
        self.repo.configure_capacity(max_instances=4, max_physical_gpus=4)
        for key, owner in (("test-tenant:sixnine", None), ("test-owner:sixnine:superdan", "superdan")):
            self.repo.configure_budget(key, tenant_id="sixnine", owner_id=owner, limit_microusd=10_000_000)
        self.values = []
        for i, profile_id in enumerate((PRUNED, INT8)):
            value = policy(self.repo.clock())
            value.update(deployment_profile_id=profile_id, model_id=get_profile(profile_id)["model_id"],
                backend="wangp-worker", pool=f"profile-pool-{i}", configuration_id=f"profile-config-{i}",
                recipe_ids=["h3-base-fl2va-v1"], engine_manifest_digest=engine_manifest(profile_id, "fl").digest,
                output_delivery="native-frames-v1")
            value["qualification"].update(status="runtime_required", profile=QUEUED_TASK_PROFILE)
            value["envelope"].update(max_duration_seconds=124/24, max_reference_files=0, max_guides=0,
                input_limits=copy.deepcopy(MULTIMODAL_INPUT_LIMITS))
            value["envelope"]["input_limits"].update(guide_kinds=[], guide_recipe_ids=[])
            value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
                "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
            self.values.append(value)
        self.write()

    def write(self):
        self.path.write_text(json.dumps({"schema_version": 1, "policies": self.values}), encoding="utf-8")

    def test_explicit_default_seeds_only_new_sessions_with_exact_profile_controls(self):
        from studio_platform.quick_chat import default_next_settings, settings_check
        from studio_platform.inference.wangp_profile_compiler import control_schema
        settings = replace(self.settings, default_deployment_profile_id=PRUNED)
        expected = default_next_settings(settings)
        self.assertEqual(expected['deployment_profile_id'], PRUNED)
        self.assertEqual(expected['controls'], {k:v['default'] for k,v in control_schema(PRUNED,'fl').items() if 'default' in v})
        self.assertEqual(expected['controls']['steps'],20)
        self.assertEqual(expected['controls']['resolution'],'480P')
        settings_check(expected)
        with TestClient(create_app(settings, repository=self.repo)) as client:
            client.post('/api/auth/login',json={'username':'superdan'}).raise_for_status()
            schema = client.get('/v1/quick-chat/schema').json()
            self.assertEqual(schema['capabilities']['default_deployment_profile_id'],PRUNED)
            self.assertEqual(schema['default_next_settings'],expected)
            headers = {'Idempotency-Key':'new-profile-default'}
            response = client.post('/v1/quick-chat/sessions',json={'title':'Default profile'},headers=headers)
            response.raise_for_status()
            original=response.json()['session']
            self.assertEqual(original['next_settings'],expected)
            self.values[0]['enabled']=False
            self.write()
            schema = client.get('/v1/quick-chat/schema').json()
            self.assertIsNone(schema['capabilities']['default_deployment_profile_id'])
            self.assertNotIn('deployment_profile_id',schema['default_next_settings'])
            replay=client.post('/v1/quick-chat/sessions',json={'title':'Default profile'},headers=headers)
            replay.raise_for_status()
            self.assertEqual(replay.json()['session'],original)
            self.assertEqual(client.get('/v1/quick-chat/sessions/'+original['id']).json()['session'],original)

    def test_default_never_selects_first_available_profile_or_enables_generation(self):
        from studio_platform.quick_chat import default_next_settings, DEFAULT_SETTINGS
        from studio_platform.execution_profiles import default_profile_id
        choices = [self.settings, replace(self.settings,default_deployment_profile_id=PRUNED,generation_enabled=False),
            replace(self.settings,default_deployment_profile_id=PRUNED,execution_profiles_file=None)]
        for settings in choices:
            self.assertIsNone(default_profile_id(settings))
            self.assertEqual(default_next_settings(settings),DEFAULT_SETTINGS)
        self.values[0]['qualification']['expires_at']=self.repo.clock()-1
        self.write()
        settings=replace(self.settings,default_deployment_profile_id=PRUNED)
        self.assertIsNone(default_profile_id(settings))
        self.assertEqual(default_next_settings(settings),DEFAULT_SETTINGS)
        with self.assertRaises(ValueError):
            replace(self.settings,default_deployment_profile_id='invented-default')

    def test_public_direct_card_example_preserves_selected_profile_without_submission(self):
        settings = replace(self.settings, default_deployment_profile_id=PRUNED)
        with TestClient(create_app(settings, repository=self.repo)) as client:
            client.post('/api/auth/login', json={'username':'superdan'}).raise_for_status()
            example = client.get('/for-agents/guide.json').json()['quick_chat']['examples']['card']
            session = client.post('/v1/quick-chat/sessions', json={'title':'Documented profile'},
                headers={'Idempotency-Key':'docs-session'}).json()['session']
            selected = session['next_settings']
            replacements = {'{selected_deployment_profile_id}':selected['deployment_profile_id'],
                '{selected_recipe_id}':selected['recipe_id'], '{selected_controls_object}':selected['controls'],
                '{complete_prompt}':'A blue cup rotates on a desk.'}
            body = {key:replacements.get(value,value) if isinstance(value,str) else value
                for key,value in copy.deepcopy(example['body']).items()}
            response = client.request(example['method'], example['path'].format(session_id=session['id']),
                json=body, headers={'Idempotency-Key':'docs-card'})
            response.raise_for_status()
            revision = response.json()['revision']
            self.assertEqual(revision['deployment_profile_id'], PRUNED)
            self.assertEqual(revision['controls']['steps'], 20)
            self.assertEqual(revision['controls']['resolution'], '480P')
            self.assertEqual(client.get('/v1/jobs').json()['jobs'], [])

    def compiled(self, profile=PRUNED):
        return compile_request(generation_request(deployment_profile_id=profile,
            controls={"duration": 5, "resolution": "480P", "steps": 20, "seed": "42"}),
            lambda _: None, backend="wangp-worker")

    def register(self, i):
        p = self.values[i]
        control = WorkerControl(self.repo)
        spec = WorkerSpec(f"worker-{i}", p["pool"], "test-only", f"instance-{i}", (f"gpu-{i}",),
            tuple(p["recipe_ids"]), p["model_id"], p["configuration_id"], "wangp-worker", p["engine_manifest_digest"],
            output_delivery="native-frames-v1")
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        return spec

    def test_different_ready_profile_never_substitutes_and_exact_one_admits(self):
        self.register(1)
        compiled, fingerprint = self.compiled()
        self.assertEqual(compiled["request"]["model"], "MiniMax-H3-Pruned-Rank8-INT8")
        policies = ExecutionPolicies(self.settings, self.repo)
        self.assertFalse(policies.evaluate(compiled, self.scope, fingerprint).execution["enabled"])
        self.register(0)
        admitted = policies.evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(admitted.execution["enabled"], admitted.execution)
        self.assertEqual(admitted.execution["pool"], self.values[0]["pool"])
        job = {"request": compiled, "execution_plan": admitted.execution, "tenant_id": "sixnine"}
        self.assertTrue(policies.activation_allowed(job))
        changed = copy.deepcopy(job)
        changed["request"]["deployment_profile_id"] = INT8
        self.assertFalse(policies.activation_allowed(changed))
        self.values[0]["enabled"] = False
        self.write()
        self.assertFalse(policies.activation_allowed(job))

    def test_supported_unmeasured_combinations_admit_without_changing_protected_resource_policy(self):
        from studio_platform.h3_profile_support import envelope
        from studio_platform.execution_profiles import public_profiles
        self.values[0]['envelope']=envelope(PRUNED,'fl')
        self.values[0]['envelope'].update(max_pixels=832*480,max_steps=50,max_duration_seconds=124/24)
        self.write(); self.register(0)
        before=self.path.read_bytes()
        asset={'metadata':{'kind':'image','model_ready':True,'width':832,'height':480},
            'model':{'key':'owners/superdan/assets/image/model','sha256':'a'*64,'size_bytes':4}}
        for roles in ({'first_frame':'image'},{'last_frame':'image'}):
            body=generation_request(deployment_profile_id=PRUNED,
                controls={'duration':5,'resolution':'480P','steps':30,'seed':'42'},inputs=roles)
            compiled,fingerprint=compile_request(body,lambda _:copy.deepcopy(asset),backend='wangp-worker')
            admission=ExecutionPolicies(self.settings,self.repo).evaluate(compiled,self.scope,fingerprint)
            self.assertTrue(admission.execution['enabled'],admission.execution['blockers'])
            self.assertNotIn('timing_hint',admission.execution)
        body['controls']['steps']=51
        compiled,fingerprint=compile_request(body,lambda _:copy.deepcopy(asset),backend='wangp-worker')
        admission=ExecutionPolicies(self.settings,self.repo).evaluate(compiled,self.scope,fingerprint)
        self.assertFalse(admission.execution['enabled'])
        self.assertTrue(any('50' in blocker for blocker in admission.execution['blockers']))
        self.assertEqual(self.path.read_bytes(),before)
        public=next(p for p in public_profiles(self.settings) if p['id']==PRUNED)['generation_support']['fl']
        self.assertTrue(public['input_support']['first_frame'])
        self.assertTrue(public['input_support']['last_frame'])
        self.assertEqual(public['joint_cases_scope'],'historical_examples_not_admission_allowlist')
        self.assertEqual(public['execution_policy_constraints']['max_steps'],50)
        self.assertEqual(public['controls']['steps']['maximum'],100)

    def test_profile_config_absence_invalid_id_and_ambiguous_bindings_fail_closed(self):
        compiled, fingerprint = self.compiled()
        policies = ExecutionPolicies(replace(self.settings, execution_profiles_file=None), self.repo)
        self.assertFalse(policies.evaluate(compiled, self.scope, fingerprint).execution["enabled"])
        with self.assertRaises(ValueError):
            self.compiled("invented-profile")
        self.values.append(copy.deepcopy(self.values[0]))
        self.write()
        with self.assertRaises(ValueError):
            read_profiles(self.path)

    def test_profile_admission_and_native_outputs_cover_every_recorded_case(self):
        from studio_platform.runtime_catalog import PROFILE_IDS
        from studio_platform.execution_policy import validate_policy
        from studio_platform.inference.outputs import native_delivery_spec
        from test_platform_wangp_profiles import job_for
        from studio_platform.execution_profiles import tested_envelope
        from studio_platform.repository import request_hash
        self.repo.configure_capacity(max_instances=32,max_physical_gpus=32)
        ordinal = 0
        for profile_id in PROFILE_IDS:
            for case in get_profile(profile_id)['verified_cases']:
                with self.subTest(profile=profile_id, case=case['id']):
                    job, manifest = job_for(profile_id, case)
                    output = native_delivery_spec(job['request'])
                    self.assertEqual(output['frame_count'], 124)
                    self.assertEqual(output['duration_s'],124/24)
                    value = copy.deepcopy(self.values[0])
                    value.update(deployment_profile_id=profile_id, model_id=get_profile(profile_id)['model_id'],
                        recipe_ids=[manifest.document['generation_recipe_id']], engine_manifest_digest=manifest.digest)
                    value['envelope'] = tested_envelope(profile_id,case['mode'])
                    ordinal += 1
                    value.update(configuration_id=f'case-config-{ordinal}',pool=f'case-pool-{ordinal}')
                    validate_policy(value)
                    self.values=[value]
                    self.write()
                    spec=WorkerSpec(f'case-worker-{ordinal}',value['pool'],'test-only',f'instance-{ordinal}',
                        (f'gpu-{ordinal}',),tuple(value['recipe_ids']),value['model_id'],value['configuration_id'],
                        'wangp-worker',value['engine_manifest_digest'],output_delivery='native-frames-v1')
                    control=WorkerControl(self.repo)
                    control.register(spec)
                    control.mark_ready(spec.worker_id,upstream_idle_confirmed=True)
                    native=job['request']['request']
                    inputs=copy.deepcopy(native['inputs'])
                    inputs['videos']=[{'asset_id':identity,'include_audio':False} for identity in inputs['videos']]
                    body=generation_request(recipe_id=value['recipe_ids'][0],deployment_profile_id=profile_id,
                        controls={k:native[k] for k in ('duration','resolution','steps','seed')},inputs=inputs)
                    compiled,fingerprint=compile_request(body,job['request']['assets'].__getitem__,backend='wangp-worker')
                    admitted=ExecutionPolicies(self.settings,self.repo).evaluate(compiled,self.scope,fingerprint)
                    self.assertTrue(admitted.execution['enabled'],admitted.execution['blockers'])

    def test_explicit_pruned_api_defaults_compile_without_legacy_768p_fallback(self):
        compiled,_=compile_request(generation_request(deployment_profile_id=PRUNED,controls={}),
            lambda _:None,backend='wangp-worker')
        self.assertEqual(compiled['output_spec']['height'],480)

    def test_invalid_legacy_policy_does_not_hide_independent_profiles(self):
        legacy=self.root/'legacy.json'
        legacy.write_text('{invalid',encoding='utf-8')
        selected=read_profiles(self.path)[(PRUNED,'h3-base-fl2va-v1')]
        self.assertEqual(selected['deployment_profile_id'],PRUNED)
        self.register(0)
        policies=ExecutionPolicies(replace(self.settings,execution_policy_file=legacy),self.repo)
        compiled,fingerprint=self.compiled()
        self.assertTrue(policies.evaluate(compiled,self.scope,fingerprint).execution['enabled'])

    def test_operator_routes_use_cookie_owner_and_same_origin_boundary(self):
        settings=replace(self.settings,operator_capacity_owners=('superdan',))
        with TestClient(create_app(settings,repository=self.repo)) as client:
            client.post('/api/auth/login',json={'username':'supervan'}).raise_for_status()
            self.assertFalse(client.get('/api/auth/me').json()['operator_capacity']['view'])
            self.assertEqual(client.get('/v1/operator/capacity/state').status_code,403)
            client.post('/api/auth/login',json={'username':'superdan'}).raise_for_status()
            self.assertTrue(client.get('/api/auth/me').json()['operator_capacity']['view'])
            state=client.get('/v1/operator/capacity/state')
            self.assertEqual(state.status_code,200)
            self.assertEqual(client.post('/v1/operator/capacity/previews',json={},
                headers={'Origin':'https://unrelated.invalid'}).status_code,403)
            catalog=client.get('/v1/operator/capacity/catalog').json()
            self.assertEqual(len(catalog['profiles']),6)
            self.assertTrue(all('runtime' not in profile for profile in catalog['profiles']))

    def test_quick_chat_selection_survives_turn_projection_revision_and_preflight(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            prefix = "/v1/quick-chat/sessions"
            session = client.post(prefix, json={}, headers={"Idempotency-Key": "new-session"}).json()["session"]
            base = prefix + "/" + session["id"]
            settings = {"recipe_id": "h3-base-fl2va-v1", "controls": {"resolution": "480P", "duration": 5, "steps": 20},
                "copies": 1, "deployment_profile_id": PRUNED}
            saved = client.patch(base, json={"expected_version": session["version"], "next_settings": settings},
                headers={"Idempotency-Key": "select-profile"})
            saved.raise_for_status()
            current = client.get(base).json()["session"]
            turn = client.post(base + "/turns", json={"expected_version": current["version"],
                "text": "A fictional swordsman leaps across rooftops.", "model_id": current["model_id"],
                "assistant_mode": "none", "create_card": True}, headers={"Idempotency-Key": "turn-card"})
            turn.raise_for_status()
            card = client.get(base + "/cards/" + turn.json()["card_id"]).json()
            revision = client.get(base + "/revisions/" + card["current_revision_id"]).json()
            self.assertEqual(revision["deployment_profile_id"], PRUNED)
            pf = client.post(base + "/revisions/" + revision["id"] + "/preflights", json={
                "revision_hash": revision["input_hash"], "capabilities_version": "sixnine-h3-base-20261004-v1"},
                headers={"Idempotency-Key": "preflight-profile"})
            pf.raise_for_status()
            self.assertIn(PRUNED, pf.text)
            self.assertIn("MiniMax-H3-Pruned-Rank8-INT8", pf.text)


if __name__ == "__main__":
    unittest.main()

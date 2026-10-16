"""Original API/ledger safety for storyboard versions; no paid execution."""
import copy
import unittest
import uuid
from unittest.mock import patch
from sqlalchemy import select, func, insert, update

import test_platform_generation_draft as fixtures
from studio_platform.repository import jobs, plans, artifacts, attempts
from studio_platform.source_snapshot import source_snapshot


class StoryboardVersionTests(unittest.TestCase):
    setUp = fixtures.GenerationDraftTests.setUp
    login = fixtures.GenerationDraftTests.login
    setup_project = fixtures.GenerationDraftTests.setup_project
    key = fixtures.GenerationDraftTests.key
    edit = fixtures.GenerationDraftTests.edit
    source = fixtures.GenerationDraftTests.source
    configure = fixtures.GenerationDraftTests.configure

    def current(self):
        return self.client.get('/v1/projects/story-one').json()

    def prepare(self):
        self.setup_project()
        self.source('frame', 'image')
        self.configure(prompt='Original action', controls={'seed': '23', 'duration': 5},
            inputs={'first_frame': {'asset_id': 'frame'}}).raise_for_status()

    def preflight(self, key='preflight-one', copies=2, body=None):
        return self.client.post('/v1/projects/story-one/storyboard/preflights',
            headers={'Idempotency-Key': key}, json=body or {'expected_version': self.current()['version'],
                'shot_ids': ['shot-one'], 'copies': copies})

    def completed(self, plan_id, *, legacy=False):
        """Trusted synthetic completed worker receipt; never executes a job."""
        with self.app.state.repository.transaction() as conn:
            plan = conn.execute(select(plans).where(plans.c.id == plan_id)).mappings().one()
            request = copy.deepcopy(plan['request'])
            if legacy:
                request.pop('authoring_recipe', None)
                request.pop('authoring_context', None)
            ident, attempt, artifact = (str(uuid.uuid4()) for _ in range(3))
            conn.execute(insert(jobs).values(id=ident, tenant_id=plan['tenant_id'], owner_id=plan['owner_id'],
                project_id=plan['project_id'], actor_id='test-worker-receipt', idempotency_key=ident,
                request_hash=plan['request_hash'], plan_id=plan_id, request=request, execution_plan=plan['execution_plan'],
                status='succeeded', pool='synthetic', expected_runtime_s=1, estimated_cost_microusd=0,
                created_at=1, updated_at=1, not_before=0))
            conn.execute(insert(attempts).values(id=attempt, job_id=ident, number=1, status='succeeded',
                fence=1, worker_id='synthetic', created_at=1, updated_at=1))
            conn.execute(insert(artifacts).values(id=artifact, job_id=ident, attempt_id=attempt, created_at=1,
                metadata={'kind':'video','mime':'video/mp4','size_bytes':100,'sha256':'0'*64,'object_key':'synthetic'}))
        return ident, artifact

    def test_restore_uses_immutable_effective_recipe_after_profiles_and_defaults_change(self):
        self.prepare()
        project = self.current()['project']
        frame_id = next(e['id'] for e in project['entities'] if e['data'].get('cloudAssetId') == 'frame')
        self.edit([
            {'op':'entity.create','entity':{'id':'hero','type':'character','title':'Hero','description':'Old coat',
                'data':{'looks':[{'id':'day','name':'Day','description':'Old look','version':1,'gallery':{},'imageIds':[frame_id]}], 'defaultLookId':'day'}}},
            {'op':'entity.update','entity_id':'shot-one','patch':{'data':{'cast':[{'characterId':'hero','lookId':'day'}],
                'storyboard':{'version':1,'mode':'fl','firstFrameId':frame_id}}}},
            {'op':'journey.update','patch':{'storyboard':{'style':'Original style'}}}], version=self.current()['version']).raise_for_status()
        result = self.preflight(copies=1).json()
        self.assertEqual(result['status'], 'completed', result)
        job_id, artifact = self.completed(result['items'][0]['plan_id'])
        with self.app.state.repository.engine.connect() as conn:
            original_job = dict(conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one())
        version = self.client.get('/v1/projects/story-one/shots/shot-one/versions').json()['versions'][0]
        original_recipe = version['recipe']
        self.assertIn('Old coat', original_recipe['prompt'])
        self.edit([{'op':'entity.update','entity_id':'hero','patch':{'description':'New costume'}},
            {'op':'journey.update','patch':{'storyboard':{'style':'New style','defaults':{'controls':{'steps':24}}}}}],
            version=self.current()['version']).raise_for_status()
        self.edit([{'op':'storyboard.restore','shot_id':'shot-one','artifact_id':artifact}],
            version=self.current()['version']).raise_for_status()
        restored = self.client.get('/v1/projects/story-one/shots/shot-one/generation-draft').json()['draft']
        self.assertEqual(restored, original_recipe)
        next_plan = self.preflight('restored-plan', copies=1).json()['items'][0]['plan']
        self.assertEqual(next_plan['effective_request'], result['items'][0]['plan']['effective_request'])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(dict(conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()), original_job)
        source_before = source_snapshot(self.current()['project'], 'shot-one')
        self.edit([{'op':'storyboard.star','shot_id':'shot-one','artifact_id':artifact,'starred':True}],
            version=self.current()['version']).raise_for_status()
        self.assertEqual(source_snapshot(self.current()['project'], 'shot-one'), source_before)
        self.assertTrue(self.client.get('/v1/projects/story-one/shots/shot-one/versions').json()['versions'][0]['starred'])

    def test_legacy_recipe_unknown_never_inferred_from_current_shot(self):
        self.prepare()
        plan = self.preflight(copies=1).json()['items'][0]['plan_id']
        _, artifact = self.completed(plan, legacy=True)
        row = self.client.get('/v1/projects/story-one/shots/shot-one/versions').json()['versions'][0]
        self.assertEqual(row['recipe_status'], 'unknown')
        self.assertIsNone(row['recipe'])
        before = self.current()
        response = self.edit([{'op':'storyboard.restore','shot_id':'shot-one','artifact_id':artifact}], version=before['version'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.current(), before)

    def test_profile_fork_rewires_only_selected_shot_with_one_project_cas(self):
        self.prepare()
        self.edit([{'op':'entity.create','entity':{'id':'hero','type':'character','title':'Hero','data':{'looks':[]}}},
            {'op':'entity.update','entity_id':'scene-one','patch':{'data':{'cast':[{'characterId':'hero','lookId':''}]}}},
            {'op':'entity.create','entity':{'id':'shot-two','type':'shot','parentId':'scene-one','data':{'seconds':5,'prompt':'Second'}}}],
            version=self.current()['version']).raise_for_status()
        before = self.current()
        response = self.edit([{'op':'storyboard.fork_profile','profile_id':'hero','new_profile_id':'hero-local',
            'shot_ids':['shot-one'],'title':'Local Hero'}], version=before['version'])
        response.raise_for_status()
        after = response.json()
        self.assertEqual(after['version'], before['version'] + 1)
        old = {e['id']:e for e in before['project']['entities']}
        new = {e['id']:e for e in after['project']['entities']}
        for ident in ('hero','shot-two','scene-one'):
            self.assertEqual(new[ident], old[ident])
        self.assertEqual(new['shot-one']['data']['cast'], [{'characterId':'hero-local','lookId':''}])
        usage = self.client.get('/v1/projects/story-one/storyboard/profiles').json()
        self.assertEqual(usage['usage_scope'], 'project')
        self.assertEqual({p['id']:p['shot_count'] for p in usage['profiles']}, {'hero':1,'hero-local':1})

    def test_distinct_seeds_replay_and_partial_plan_commit_use_original_ids(self):
        self.prepare()
        body = {'expected_version': self.current()['version'], 'shot_ids': ['shot-one'], 'copies': 3}
        service = self.app.state.storyboard_versions
        original = service._put
        failed = False
        def interrupted(conn, scope, kind, ident, payload, *args, **kwargs):
            nonlocal failed
            if kind == 'storyboard_preflight' and any(i.get('plan') for i in payload.get('items', [])) and not failed:
                failed = True
                raise RuntimeError('synthetic projection interruption')
            return original(conn, scope, kind, ident, payload, *args, **kwargs)
        with patch('studio_platform.storyboard_versions.secrets.randbits', return_value=23), patch.object(service, '_put', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.preflight(body=body)
        with self.app.state.repository.engine.connect() as conn:
            before = list(conn.execute(select(plans.c.id, plans.c.request_hash)))
        self.assertEqual(len(before), 1)
        with patch('studio_platform.storyboard_versions.secrets.randbits', side_effect=AssertionError('redraw')):
            response = self.preflight(body=body)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value['status'], 'completed')
        self.assertEqual([v['seed'] for v in value['items']], ['23', '24', '25'])
        self.assertEqual(value['items'][0]['plan_id'], before[0][0])
        self.assertEqual(self.preflight(body=body).json(), value)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(plans)), 3)
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)
            self.assertEqual(conn.scalar(select(plans.c.request_hash).where(plans.c.id == before[0][0])), before[0][1])

    def test_shared_copies_preference_and_byte_bound_before_seed_materialization(self):
        self.prepare()
        self.assertEqual(self.preflight(copies=5).status_code, 422)
        response = self.client.patch('/v1/quick-chat/preferences', headers={'Idempotency-Key': 'raise-copies'},
            json={'expected_version': 0, 'copies_max': 2147483647})
        self.assertEqual(response.status_code, 200, response.text)
        with patch('studio_platform.storyboard_versions.secrets.randbits', side_effect=AssertionError('allocation')):
            self.assertEqual(self.preflight(copies=2147483647).status_code, 422)

    def test_defaults_precedence_source_fence_and_duration_seed_per_shot(self):
        self.prepare()
        response = self.client.patch('/v1/storyboard/preferences', headers={'Idempotency-Key': 'global-defaults'},
            json={'expected_version': 0, 'defaults': {'controls': {'steps': 18}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.edit([{'op': 'storyboard.defaults', 'value': {'controls': {'steps': 20}}}],
            version=self.current()['version']).raise_for_status()
        draft = self.client.get('/v1/projects/story-one/shots/shot-one/generation-draft').json()
        self.assertEqual(draft['draft']['controls']['steps'], 20)
        self.assertEqual(draft['defaults']['provenance']['controls.steps'], 'story')
        plan_id = self.preflight(copies=1).json()['items'][0]['plan_id']
        with self.app.state.repository.engine.connect() as conn:
            before = conn.execute(select(plans).where(plans.c.id == plan_id)).mappings().one()
        self.client.patch('/v1/storyboard/preferences', headers={'Idempotency-Key': 'global-new'},
            json={'expected_version': 1, 'defaults': {'controls': {'steps': 22}}}).raise_for_status()
        # Use original authenticated HTTP admission: it must reject stale source,
        # before execution readiness or financial reservations.
        result = self.client.post('/v1/jobs', headers={'Idempotency-Key': 'old-quote'}, json={'plan_id': plan_id})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn('storyboard_defaults_changed', result.text)
        with self.app.state.repository.engine.connect() as conn:
            after = conn.execute(select(plans).where(plans.c.id == plan_id)).mappings().one()
            self.assertEqual(before['request'], after['request'])
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)
        bad = self.client.patch('/v1/storyboard/preferences', headers={'Idempotency-Key': 'forbidden-duration'},
            json={'expected_version': 2, 'defaults': {'controls': {'duration': 10}}})
        self.assertEqual(bad.status_code, 422)

    def test_bulk_single_cas_atomic_invalid_member_and_closed_conflict(self):
        self.prepare()
        before = self.current()
        action = {'op': 'storyboard.bulk', 'shot_ids': ['shot-one', 'missing'], 'operation': 'final'}
        response = self.edit([action], version=before['version'])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.current(), before)
        body = {'expected_version': before['version'], 'actions': [{**action, 'shot_ids': ['shot-one']}]}
        first = self.client.post('/v1/projects/story-one/actions', json=body, headers={'Idempotency-Key': 'bulk'})
        first.raise_for_status()
        self.assertEqual(first.json()['version'], before['version'] + 1)
        self.assertEqual(self.client.post('/v1/projects/story-one/actions', json=body, headers={'Idempotency-Key': 'bulk'}).json(), first.json())
        stale = self.client.post('/v1/projects/story-one/actions', json=body, headers={'Idempotency-Key': 'new-bulk'})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.json()['detail']['no_mutation_committed'])

    def test_preferences_and_preflight_are_owner_scoped(self):
        self.prepare()
        value = self.preflight(copies=1).json()
        self.login('supervan')
        self.assertEqual(self.client.get('/v1/projects/story-one/storyboard/preflights/' + value['id']).status_code, 404)
        self.assertEqual(self.client.get('/v1/projects/story-one/shots/shot-one/versions').status_code, 404)
        self.assertEqual(self.client.get('/v1/storyboard/preferences').json()['version'], 0)


if __name__ == '__main__':
    unittest.main()

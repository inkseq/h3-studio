"""Original API/ledger safety for storyboard versions; no paid execution."""

import json
import copy
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from sqlalchemy import select, func, insert, update

import test_platform_generation_draft as fixtures
from studio_platform.repository import (
    jobs,
    plans,
    artifacts,
    attempts,
    budget_reservations,
    documents,
    Scope,
    Conflict,
    request_hash,
)
from studio_platform.auth import Principal
from studio_platform.source_snapshot import source_snapshot


class StoryboardVersionTests(unittest.TestCase):
    setUp = fixtures.GenerationDraftTests.setUp
    login = fixtures.GenerationDraftTests.login
    setup_project = fixtures.GenerationDraftTests.setup_project
    key = fixtures.GenerationDraftTests.key
    edit = fixtures.GenerationDraftTests.edit
    source = fixtures.GenerationDraftTests.source
    configure = fixtures.GenerationDraftTests.configure

    def test_shared_copies_stale_cas_has_exact_no_write_proof_and_original_replay(self):
        from studio_platform.repository import request_hash

        self.login()
        first = {"expected_version": 0, "copies_max": 6}
        accepted = self.client.patch(
            '/v1/quick-chat/preferences',
            headers={'Idempotency-Key': 'copies-first'},
            json=first,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        stale = {"expected_version": 0, "copies_max": 2}
        rejected = self.client.patch(
            '/v1/quick-chat/preferences',
            headers={'Idempotency-Key': 'copies-stale'},
            json=stale,
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()['resource'], 'quick_chat_preferences')
        self.assertTrue(rejected.json()['no_mutation_committed'])
        self.assertEqual(rejected.json()['request_hash'], request_hash(stale))
        self.assertEqual(
            self.client.get('/v1/quick-chat/preferences').json()['copies_max'], 6
        )
        replay = self.client.patch(
            '/v1/quick-chat/preferences',
            headers={'Idempotency-Key': 'copies-first'},
            json=first,
        )
        self.assertEqual(replay.json(), accepted.json())
        conflict = self.client.patch(
            '/v1/quick-chat/preferences',
            headers={'Idempotency-Key': 'copies-first'},
            json=stale,
        )
        self.assertNotIn('no_mutation_committed', conflict.json())

    def test_select_original_receipt_is_cas_idempotent_and_never_generates(self):
        self.prepare()
        plan_id = self.preflight(copies=1).json()['items'][0]['plan_id']
        job_id, artifact = self.completed(plan_id)
        body = {
            'expected_version': self.current()['version'],
            'actions': [
                {
                    'op': 'storyboard.select',
                    'shot_id': 'shot-one',
                    'artifact_id': artifact,
                }
            ],
        }
        headers = {'Idempotency-Key': 'select-original-result'}
        selected = self.client.post(
            '/v1/projects/story-one/actions', headers=headers, json=body
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        shot = next(
            e for e in selected.json()['project']['entities'] if e['id'] == 'shot-one'
        )
        self.assertEqual(shot['data']['selectedAssetId'], 'result-' + artifact)
        self.assertEqual(
            self.client.post(
                '/v1/projects/story-one/actions', headers=headers, json=body
            ).json(),
            selected.json(),
        )
        stale = self.client.post(
            '/v1/projects/story-one/actions',
            headers={'Idempotency-Key': 'select-stale'},
            json=body,
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertTrue(stale.json()['detail']['no_mutation_committed'])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(func.count()).select_from(jobs)).scalar_one(), 1
            )
            self.assertEqual(
                conn.execute(
                    select(func.count()).select_from(budget_reservations)
                ).scalar_one(),
                0,
            )

    def test_profile_usage_honors_explicit_storyboard_bindings_and_restored_mode(self):
        from types import SimpleNamespace
        from studio_platform.storyboard_versions import StoryboardVersions

        profiles = [
            {'id': i, 'type': kind, 'title': i, 'data': {}}
            for i, kind in [
                ('hero', 'character'),
                ('old-place', 'location'),
                ('new-place', 'location'),
            ]
        ]
        scene = {
            'id': 'scene',
            'type': 'scene',
            'data': {'cast': ['hero'], 'locationId': 'old-place'},
        }

        def shot(ident, data):
            return {'id': ident, 'type': 'shot', 'parentId': 'scene', 'data': data}

        project = {
            'entities': profiles
            + [
                scene,
                shot('cleared', {'cast': [], 'storyboard': {'place': None}}),
                shot(
                    'explicit',
                    {'cast': [], 'storyboard': {'place': {'locationId': 'new-place'}}},
                ),
                shot('inherited', {'storyboard': {}}),
                shot(
                    'restored',
                    {
                        'h3': {'explicitReferences': True},
                        'cast': ['hero'],
                        'locationId': 'old-place',
                    },
                ),
            ],
            'links': [],
        }
        service = object.__new__(StoryboardVersions)
        service.access = SimpleNamespace(
            project=lambda *args: {'payload': project, 'version': 1}
        )
        result = service.profiles(None, 'project')
        usage = {p['id']: p['shot_ids'] for p in result['profiles']}
        self.assertEqual(
            usage,
            {
                'hero': ['inherited'],
                'old-place': ['inherited'],
                'new-place': ['explicit'],
            },
        )

    def current(self):
        return self.client.get('/v1/projects/story-one').json()

    def prepare(self):
        self.setup_project()
        self.source('frame', 'image')
        self.configure(
            prompt='Original action',
            controls={'seed': '23', 'duration': 5},
            inputs={'first_frame': {'asset_id': 'frame'}},
        ).raise_for_status()

    def preflight(self, key='preflight-one', copies=2, body=None):
        return self.client.post(
            '/v1/projects/story-one/storyboard/preflights',
            headers={'Idempotency-Key': key},
            json=body
            or {
                'expected_version': self.current()['version'],
                'shot_ids': ['shot-one'],
                'copies': copies,
            },
        )

    def completed(self, plan_id, *, legacy=False):
        """Trusted synthetic completed worker receipt; never executes a job."""
        with self.app.state.repository.transaction() as conn:
            plan = (
                conn.execute(select(plans).where(plans.c.id == plan_id))
                .mappings()
                .one()
            )
            request = copy.deepcopy(plan['request'])
            if legacy:
                request.pop('authoring_recipe', None)
                request.pop('authoring_context', None)
            ident, attempt, artifact = (str(uuid.uuid4()) for _ in range(3))
            conn.execute(
                insert(jobs).values(
                    id=ident,
                    tenant_id=plan['tenant_id'],
                    owner_id=plan['owner_id'],
                    project_id=plan['project_id'],
                    actor_id='test-worker-receipt',
                    idempotency_key=ident,
                    request_hash=plan['request_hash'],
                    plan_id=plan_id,
                    request=request,
                    execution_plan=plan['execution_plan'],
                    status='succeeded',
                    pool='synthetic',
                    expected_runtime_s=1,
                    estimated_cost_microusd=0,
                    created_at=1,
                    updated_at=1,
                    not_before=0,
                )
            )
            conn.execute(
                insert(attempts).values(
                    id=attempt,
                    job_id=ident,
                    number=1,
                    status='succeeded',
                    fence=1,
                    worker_id='synthetic',
                    created_at=1,
                    updated_at=1,
                )
            )
            conn.execute(
                insert(artifacts).values(
                    id=artifact,
                    job_id=ident,
                    attempt_id=attempt,
                    created_at=1,
                    metadata={
                        'kind': 'video',
                        'mime': 'video/mp4',
                        'size_bytes': 100,
                        'sha256': '0' * 64,
                        'object_key': 'synthetic',
                    },
                )
            )
        return ident, artifact

    def migration_versions_fixture(self):
        """Seed two original succeeded shot jobs and one trusted adoption receipt."""
        self.prepare()
        current = self.current()
        created = self.edit(
            [
                {
                    "op": "entity.create",
                    "entity": {
                        "id": "shot-alias",
                        "type": "shot",
                        "parentId": "scene-one",
                        "title": "Earlier take",
                        "data": {"seconds": 5},
                    },
                },
                {
                    "op": "shot.configure_generation",
                    "shot_id": "shot-alias",
                    "prompt": "Earlier immutable action",
                    "controls": {"seed": "17", "duration": 5},
                    "inputs": {"first_frame": {"asset_id": "frame"}},
                },
            ],
            version=current['version'],
        )
        self.assertEqual(created.status_code, 200, created.text)
        response = self.client.post(
            '/v1/projects/story-one/storyboard/preflights',
            headers={'Idempotency-Key': 'alias-fixture-plans'},
            json={
                'expected_version': created.json()['version'],
                'shot_ids': ['shot-one', 'shot-alias'],
                'copies': 1,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        made = [self.completed(item['plan_id']) for item in response.json()['items']]
        current = self.current()
        project = current['project']
        sources = ['shot-one', 'shot-alias']
        mapping = {}
        for index, ident in enumerate(sources):
            shot = next(e for e in project['entities'] if e['id'] == ident)
            shot['data']['quickChatProjection'] = {
                'session_id': 'session-migrated',
                'revision_id': f'revision-{index}',
                'item_id': f'item-{index}',
            }
            mapping[f'item-{index}'] = {
                'card_id': 'card-original',
                'revision_id': f'revision-{index}',
                'shot_id': ident,
            }
        before_project = copy.deepcopy(project)
        archived = next(
            e for e in before_project['entities'] if e['id'] == 'shot-alias'
        )
        canonical = next(e for e in project['entities'] if e['id'] == 'shot-one')
        canonical['data']['migration'] = {
            'canonical': True,
            'source_shot_ids': sources,
            'source_session_id': 'session-migrated',
            'receipt_id': 'session-migrated',
        }
        project['entities'] = [
            e for e in project['entities'] if e['id'] != 'shot-alias'
        ]
        project['links'] = [
            e
            for e in project['links']
            if 'shot-alias' not in (e['source'], e['target'])
        ]
        project['layout']['positions'].pop('shot-alias', None)
        receipt = {
            'contract': 1,
            'mapping_version': 2,
            'status': 'completed',
            'tenant_id': self.settings.tenant_id,
            'owner_id': 'superdan',
            'project_id': 'story-one',
            'source_session_id': 'session-migrated',
            'receipt_id': 'session-migrated',
            'after_project_version': current['version'],
            'archive_strategy': 'receipt_archive',
            'before_project': before_project,
            'before_project_hash': request_hash(before_project),
            'archived_shots': {
                'shot-alias': {'entity': archived, 'projection_hash': 'a' * 64}
            },
            'mapping': mapping,
            'canonical_shots': {
                'shot-one': {
                    'card_id': 'card-original',
                    'source_shot_ids': sources,
                    'selected_asset_id': None,
                    'selection_reason': 'current_revision_first_item',
                }
            },
        }
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(documents)
                .where(
                    documents.c.kind == 'project',
                    documents.c.document_id == 'story-one',
                    documents.c.owner_id == 'superdan',
                )
                .values(payload=project)
            )
            conn.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    owner_id='superdan',
                    project_id='story-one',
                    kind='quick_chat_story_migration',
                    document_id='session-migrated',
                    version=1,
                    payload=receipt,
                    updated_at=1,
                )
            )
        return made, project, receipt

    def test_take_review_uses_verified_migration_alias_and_original_receipt(self):
        made, _, _ = self.migration_versions_fixture()
        repo = self.app.state.repository
        job, artifact = made[1]
        with repo.transaction() as conn:
            attempt = conn.execute(
                select(artifacts.c.attempt_id).where(artifacts.c.id == artifact)
            ).scalar_one()
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job)
                .values(current_attempt_id=attempt, result={"artifact_ids": [artifact]})
            )
        path = f'/v1/projects/story-one/shots/shot-one/takes/{artifact}/review'
        result = self.client.patch(
            path,
            json={"expected_revision": 0, "verdict": "keep"},
            headers={"Idempotency-Key": "alias-review"},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get(path).json()['verdict'], 'keep')
        summary_path = '/v1/projects/story-one/shots/shot-one/take-reviews'
        summaries = self.client.get(summary_path)
        self.assertEqual(summaries.status_code, 200, summaries.text)
        self.assertIn({'artifact_id': artifact, 'revision': 1, 'verdict': 'keep',
                       'mark_count': 0, 'actor_kind': 'human',
                       'updated_at': result.json()['updated_at']}, summaries.json())
        self.assertEqual(
            self.client.get('/v1/guided-schema').json()['take_reviews']['path'],
            self.client.get('/v1/agent-guide').json()['take_reviews']['path'],
        )
        self.login('supervan')
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get(summary_path).status_code, 404)

    def test_migration_alias_versions_select_restore_and_star_preserve_original_jobs(
        self,
    ):
        made, _, _ = self.migration_versions_fixture()
        with self.app.state.repository.engine.connect() as conn:
            before = conn.execute(select(jobs)).mappings().all()
        path = '/v1/projects/story-one/shots/shot-one/versions'
        first = self.client.get(path + '?limit=1').json()
        second = self.client.get(path + '?limit=1&offset=1').json()
        versions = first['versions'] + second['versions']
        self.assertEqual({v['id'] for v in versions}, {a for _, a in made})
        self.assertEqual(
            {v['source_shot_id'] for v in versions}, {'shot-one', 'shot-alias'}
        )
        self.assertEqual(sum(v['aliased'] for v in versions), 1)
        self.assertTrue(all(not v['selected'] for v in versions))
        alias = next(v for v in versions if v['aliased'])
        edited = self.edit(
            [
                {
                    'op': 'storyboard.select',
                    'shot_id': 'shot-one',
                    'artifact_id': alias['id'],
                },
                {
                    'op': 'storyboard.star',
                    'shot_id': 'shot-one',
                    'artifact_id': alias['id'],
                    'starred': True,
                },
                {
                    'op': 'storyboard.restore',
                    'shot_id': 'shot-one',
                    'artifact_id': alias['id'],
                },
            ],
            version=self.current()['version'],
            key='alias-select-restore',
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        values = self.client.get(path).json()['versions']
        picked = next(v for v in values if v['id'] == alias['id'])
        self.assertTrue(picked['selected'] and picked['starred'])
        shot = next(
            e for e in edited.json()['project']['entities'] if e['id'] == 'shot-one'
        )
        self.assertEqual(shot['data']['prompt'], 'Earlier immutable action')
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs)).mappings().all(), before)
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(budget_reservations)), 0
            )

    def test_migration_alias_rejects_forged_marker_or_wrong_receipt_scope(self):
        made, project, _ = self.migration_versions_fixture()
        original = copy.deepcopy(project)
        shot = next(e for e in project['entities'] if e['id'] == 'shot-one')
        shot['data']['migration']['source_shot_ids'] = ['shot-one', 'unrelated-shot']
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'project')
                .values(payload=project)
            )
        path = '/v1/projects/story-one/shots/shot-one/versions'
        self.assertEqual(self.client.get(path).status_code, 409)
        refused = self.edit(
            [
                {
                    'op': 'storyboard.select',
                    'shot_id': 'shot-one',
                    'artifact_id': made[1][1],
                }
            ],
            version=self.current()['version'],
            key='forged-migration-select',
        )
        self.assertEqual(refused.status_code, 409)
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'project')
                .values(payload=original)
            )
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'quick_chat_story_migration')
                .values(owner_id='supervan')
            )
        self.assertEqual(self.client.get(path).status_code, 409)

    def test_migration_alias_rejects_cross_card_mapping_and_projection_drift(self):
        _, project, receipt = self.migration_versions_fixture()
        path = '/v1/projects/story-one/shots/shot-one/versions'
        invalid = copy.deepcopy(receipt)
        invalid['mapping']['item-1']['card_id'] = 'another-card'
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'quick_chat_story_migration')
                .values(payload=invalid)
            )
        self.assertEqual(self.client.get(path).status_code, 409)
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'quick_chat_story_migration')
                .values(payload=receipt)
            )
            source = next(e for e in project['entities'] if e['id'] == 'shot-one')
            source['data']['quickChatProjection']['session_id'] = 'another-session'
            conn.execute(
                update(documents)
                .where(documents.c.kind == 'project')
                .values(payload=project)
            )
        self.assertEqual(self.client.get(path).status_code, 409)

    def test_stale_or_damaged_archive_receipt_cannot_authorize_version_actions(self):
        made, _, receipt = self.migration_versions_fixture()
        path = '/v1/projects/story-one/shots/shot-one/versions'
        variants = []
        rolled_back = copy.deepcopy(receipt)
        rolled_back['rollback'] = {'status': 'completed'}
        variants.append((1, rolled_back))
        variants.append((2, receipt))
        damaged = copy.deepcopy(receipt)
        damaged['archived_shots']['shot-alias']['entity']['data']['prompt'] = 'changed'
        variants.append((1, damaged))
        damaged = copy.deepcopy(receipt)
        damaged['before_project']['title'] = 'changed original'
        variants.append((1, damaged))
        before = self.current()
        for index, (version, value) in enumerate(variants):
            with self.subTest(index=index):
                with self.app.state.repository.transaction() as conn:
                    conn.execute(
                        update(documents)
                        .where(documents.c.kind == 'quick_chat_story_migration')
                        .values(version=version, payload=value)
                    )
                self.assertEqual(self.client.get(path).status_code, 409)
                response = self.edit(
                    [
                        {
                            'op': 'storyboard.star',
                            'shot_id': 'shot-one',
                            'artifact_id': made[1][1],
                            'starred': True,
                        }
                    ],
                    version=before['version'],
                    key=f'invalid-archive-{index}',
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.current(), before)

    def test_migration_archive_keeps_versions_and_owner_project_job_filters(self):
        made, _, _ = self.migration_versions_fixture()
        path = '/v1/projects/story-one/shots/shot-alias/versions'
        kept = self.client.get(path)
        self.assertEqual(kept.status_code, 404, kept.text)
        before = self.client.get('/v1/projects/story-one/shots/shot-one/versions')
        self.assertEqual(
            {v['id'] for v in before.json()['versions']}, {a for _, a in made}
        )
        with self.app.state.repository.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == made[1][0])
                .values(project_id='another-project')
            )
        canonical = self.client.get('/v1/projects/story-one/shots/shot-one/versions')
        self.assertEqual([v['id'] for v in canonical.json()['versions']], [made[0][1]])
        self.login('supervan')
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_restore_uses_immutable_effective_recipe_after_profiles_and_defaults_change(
        self,
    ):
        self.prepare()
        project = self.current()['project']
        frame_id = next(
            e['id']
            for e in project['entities']
            if e['data'].get('cloudAssetId') == 'frame'
        )
        self.edit(
            [
                {
                    'op': 'entity.create',
                    'entity': {
                        'id': 'hero',
                        'type': 'character',
                        'title': 'Hero',
                        'description': 'Old coat',
                        'data': {
                            'looks': [
                                {
                                    'id': 'day',
                                    'name': 'Day',
                                    'description': 'Old look',
                                    'version': 1,
                                    'gallery': {},
                                    'imageIds': [frame_id],
                                }
                            ],
                            'defaultLookId': 'day',
                        },
                    },
                },
                {
                    'op': 'entity.update',
                    'entity_id': 'shot-one',
                    'patch': {
                        'data': {
                            'cast': [{'characterId': 'hero', 'lookId': 'day'}],
                            'storyboard': {
                                'version': 1,
                                'mode': 'fl',
                                'firstFrameId': frame_id,
                            },
                        }
                    },
                },
                {
                    'op': 'journey.update',
                    'patch': {'storyboard': {'style': 'Original style'}},
                },
            ],
            version=self.current()['version'],
        ).raise_for_status()
        result = self.preflight(copies=1).json()
        self.assertEqual(result['status'], 'completed', result)
        job_id, artifact = self.completed(result['items'][0]['plan_id'])
        with self.app.state.repository.engine.connect() as conn:
            original_job = dict(
                conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
            )
        version = self.client.get(
            '/v1/projects/story-one/shots/shot-one/versions'
        ).json()['versions'][0]
        original_recipe = version['recipe']
        self.assertIn('Old coat', original_recipe['prompt'])
        self.edit(
            [
                {
                    'op': 'entity.update',
                    'entity_id': 'hero',
                    'patch': {'description': 'New costume'},
                },
                {
                    'op': 'journey.update',
                    'patch': {
                        'storyboard': {
                            'style': 'New style',
                            'defaults': {'controls': {'steps': 24}},
                        }
                    },
                },
            ],
            version=self.current()['version'],
        ).raise_for_status()
        self.edit(
            [
                {
                    'op': 'storyboard.restore',
                    'shot_id': 'shot-one',
                    'artifact_id': artifact,
                }
            ],
            version=self.current()['version'],
        ).raise_for_status()
        self.edit(
            [{'op': 'storyboard.bulk', 'shot_ids': ['shot-one'], 'operation': 'final'}],
            version=self.current()['version'],
        ).raise_for_status()
        restored_shot = next(
            e for e in self.current()['project']['entities'] if e['id'] == 'shot-one'
        )
        self.assertNotIn('storyboard', restored_shot['data'])
        self.assertEqual(restored_shot['data']['qualityIntent'], 'final')
        restored = self.client.get(
            '/v1/projects/story-one/shots/shot-one/generation-draft'
        ).json()['draft']
        self.assertEqual(restored, original_recipe)
        next_plan = self.preflight('restored-plan', copies=1).json()['items'][0]['plan']
        self.assertEqual(
            next_plan['effective_request'],
            result['items'][0]['plan']['effective_request'],
        )
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(
                dict(
                    conn.execute(select(jobs).where(jobs.c.id == job_id))
                    .mappings()
                    .one()
                ),
                original_job,
            )
        source_before = source_snapshot(self.current()['project'], 'shot-one')
        self.edit(
            [
                {
                    'op': 'storyboard.star',
                    'shot_id': 'shot-one',
                    'artifact_id': artifact,
                    'starred': True,
                }
            ],
            version=self.current()['version'],
        ).raise_for_status()
        self.assertEqual(
            source_snapshot(self.current()['project'], 'shot-one'), source_before
        )
        self.assertTrue(
            self.client.get('/v1/projects/story-one/shots/shot-one/versions').json()[
                'versions'
            ][0]['starred']
        )

    def test_legacy_recipe_unknown_never_inferred_from_current_shot(self):
        self.prepare()
        plan = self.preflight(copies=1).json()['items'][0]['plan_id']
        _, artifact = self.completed(plan, legacy=True)
        row = self.client.get('/v1/projects/story-one/shots/shot-one/versions').json()[
            'versions'
        ][0]
        self.assertEqual(row['recipe_status'], 'unknown')
        self.assertIsNone(row['recipe'])
        before = self.current()
        response = self.edit(
            [
                {
                    'op': 'storyboard.restore',
                    'shot_id': 'shot-one',
                    'artifact_id': artifact,
                }
            ],
            version=before['version'],
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.current(), before)

    def test_restore_retains_original_ranges_roles_video_sound_and_guide_settings(self):
        self.prepare()
        for ident, kind in (
            ('audio', 'audio'),
            ('clip', 'video'),
            ('audio-cut', 'audio'),
            ('clip-cut', 'video'),
        ):
            self.source(ident, kind)
        self.configure(
            recipe_id='h3-base-ref2va-v1',
            inputs={
                'first_frame': None,
                'images': [{'asset_id': 'frame', 'purpose': 'identity'}],
                'videos': [
                    {
                        'asset_id': 'clip',
                        'purpose': 'motion',
                        'source_range': {'start': 1, 'end': 4},
                        'include_audio': False,
                    }
                ],
                'audios': [
                    {
                        'asset_id': 'audio',
                        'purpose': 'audio',
                        'source_range': {'start': 2, 'end': 5},
                    }
                ],
            },
        ).raise_for_status()
        with patch.object(
            self.app.state.assets,
            'derive',
            side_effect=lambda owner, ident, start, end: {
                'status': 'ready',
                'asset_id': ident + '-cut',
            },
        ):
            first = self.preflight(copies=1).json()
        self.assertEqual(first['status'], 'completed', first)
        _, artifact = self.completed(first['items'][0]['plan_id'])
        recipe = self.client.get(
            '/v1/projects/story-one/shots/shot-one/versions'
        ).json()['versions'][0]['recipe']
        self.assertEqual(
            recipe['inputs']['videos'][0]['source_range'], {'start': 1, 'end': 4}
        )
        self.assertFalse(recipe['inputs']['videos'][0]['include_audio'])
        self.configure(
            inputs={'images': [], 'videos': [], 'audios': []}, controls={'seed': '999'}
        ).raise_for_status()
        self.edit(
            [
                {
                    'op': 'storyboard.restore',
                    'shot_id': 'shot-one',
                    'artifact_id': artifact,
                }
            ],
            version=self.current()['version'],
        ).raise_for_status()
        with patch.object(
            self.app.state.assets,
            'derive',
            side_effect=lambda owner, ident, start, end: {
                'status': 'ready',
                'asset_id': ident + '-cut',
            },
        ):
            restored = self.preflight('restored-ranges', copies=1).json()
        self.assertEqual(restored['status'], 'completed', restored)
        self.assertEqual(
            restored['items'][0]['plan']['effective_request'],
            first['items'][0]['plan']['effective_request'],
        )

    def test_profile_fork_rewires_only_selected_shot_with_one_project_cas(self):
        self.prepare()
        self.edit(
            [
                {
                    'op': 'entity.create',
                    'entity': {
                        'id': 'hero',
                        'type': 'character',
                        'title': 'Hero',
                        'data': {'looks': []},
                    },
                },
                {
                    'op': 'entity.update',
                    'entity_id': 'scene-one',
                    'patch': {
                        'data': {'cast': [{'characterId': 'hero', 'lookId': ''}]}
                    },
                },
                {
                    'op': 'entity.create',
                    'entity': {
                        'id': 'shot-two',
                        'type': 'shot',
                        'parentId': 'scene-one',
                        'data': {'seconds': 5, 'prompt': 'Second'},
                    },
                },
            ],
            version=self.current()['version'],
        ).raise_for_status()
        before = self.current()
        response = self.edit(
            [
                {
                    'op': 'storyboard.fork_profile',
                    'profile_id': 'hero',
                    'new_profile_id': 'hero-local',
                    'shot_ids': ['shot-one'],
                    'title': 'Local Hero',
                }
            ],
            version=before['version'],
        )
        response.raise_for_status()
        after = response.json()
        self.assertEqual(after['version'], before['version'] + 1)
        old = {e['id']: e for e in before['project']['entities']}
        new = {e['id']: e for e in after['project']['entities']}
        for ident in ('hero', 'shot-two', 'scene-one'):
            self.assertEqual(new[ident], old[ident])
        self.assertEqual(
            new['shot-one']['data']['cast'],
            [{'characterId': 'hero-local', 'lookId': ''}],
        )
        usage = self.client.get('/v1/projects/story-one/storyboard/profiles').json()
        self.assertEqual(usage['usage_scope'], 'project')
        self.assertEqual(
            {p['id']: p['shot_count'] for p in usage['profiles']},
            {'hero': 1, 'hero-local': 1},
        )

    def test_distinct_seeds_replay_and_partial_plan_commit_use_original_ids(self):
        self.prepare()
        body = {
            'expected_version': self.current()['version'],
            'shot_ids': ['shot-one'],
            'copies': 3,
        }
        service = self.app.state.storyboard_versions
        original = service._put
        failed = False

        def interrupted(conn, scope, kind, ident, payload, *args, **kwargs):
            nonlocal failed
            if (
                kind == 'storyboard_preflight'
                and any(i.get('plan') for i in payload.get('items', []))
                and not failed
            ):
                failed = True
                raise RuntimeError('synthetic projection interruption')
            return original(conn, scope, kind, ident, payload, *args, **kwargs)

        with (
            patch(
                'studio_platform.storyboard_versions.secrets.randbits', return_value=23
            ),
            patch.object(service, '_put', side_effect=interrupted),
        ):
            with self.assertRaises(RuntimeError):
                self.preflight(body=body)
        with self.app.state.repository.engine.connect() as conn:
            before = list(conn.execute(select(plans.c.id, plans.c.request_hash)))
        self.assertEqual(len(before), 1)
        with patch(
            'studio_platform.storyboard_versions.secrets.randbits',
            side_effect=AssertionError('redraw'),
        ):
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
            self.assertEqual(
                conn.scalar(
                    select(plans.c.request_hash).where(plans.c.id == before[0][0])
                ),
                before[0][1],
            )

    def test_shared_copies_preference_and_byte_bound_before_seed_materialization(self):
        self.prepare()
        self.assertEqual(self.preflight(copies=5).status_code, 422)
        response = self.client.patch(
            '/v1/quick-chat/preferences',
            headers={'Idempotency-Key': 'raise-copies'},
            json={'expected_version': 0, 'copies_max': 2147483647},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with patch(
            'studio_platform.storyboard_versions.secrets.randbits',
            side_effect=AssertionError('allocation'),
        ):
            self.assertEqual(self.preflight(copies=2147483647).status_code, 422)

    def test_defaults_precedence_source_fence_and_duration_seed_per_shot(self):
        self.prepare()
        response = self.client.patch(
            '/v1/storyboard/preferences',
            headers={'Idempotency-Key': 'global-defaults'},
            json={'expected_version': 0, 'defaults': {'controls': {'steps': 18}}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.edit(
            [{'op': 'storyboard.defaults', 'value': {'controls': {'steps': 20}}}],
            version=self.current()['version'],
        ).raise_for_status()
        draft = self.client.get(
            '/v1/projects/story-one/shots/shot-one/generation-draft'
        ).json()
        self.assertEqual(draft['draft']['controls']['steps'], 20)
        self.assertEqual(draft['defaults']['provenance']['controls.steps'], 'story')
        plan_id = self.preflight(copies=1).json()['items'][0]['plan_id']
        with self.app.state.repository.engine.connect() as conn:
            before = (
                conn.execute(select(plans).where(plans.c.id == plan_id))
                .mappings()
                .one()
            )
        self.client.patch(
            '/v1/storyboard/preferences',
            headers={'Idempotency-Key': 'global-new'},
            json={'expected_version': 1, 'defaults': {'controls': {'steps': 22}}},
        ).raise_for_status()
        # Use original authenticated HTTP admission: it must reject stale source,
        # before execution readiness or financial reservations.
        result = self.client.post(
            '/v1/jobs',
            headers={'Idempotency-Key': 'old-quote'},
            json={'plan_id': plan_id},
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn('storyboard_defaults_changed', result.text)
        with self.app.state.repository.engine.connect() as conn:
            after = (
                conn.execute(select(plans).where(plans.c.id == plan_id))
                .mappings()
                .one()
            )
            self.assertEqual(before['request'], after['request'])
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)
        bad = self.client.patch(
            '/v1/storyboard/preferences',
            headers={'Idempotency-Key': 'forbidden-duration'},
            json={'expected_version': 2, 'defaults': {'controls': {'duration': 10}}},
        )
        self.assertEqual(bad.status_code, 422)

    def test_bulk_single_cas_atomic_invalid_member_and_closed_conflict(self):
        self.prepare()
        before = self.current()
        action = {
            'op': 'storyboard.bulk',
            'shot_ids': ['shot-one', 'missing'],
            'operation': 'final',
        }
        response = self.edit([action], version=before['version'])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.current(), before)
        body = {
            'expected_version': before['version'],
            'actions': [{**action, 'shot_ids': ['shot-one']}],
        }
        first = self.client.post(
            '/v1/projects/story-one/actions',
            json=body,
            headers={'Idempotency-Key': 'bulk'},
        )
        first.raise_for_status()
        self.assertEqual(first.json()['version'], before['version'] + 1)
        self.assertEqual(
            self.client.post(
                '/v1/projects/story-one/actions',
                json=body,
                headers={'Idempotency-Key': 'bulk'},
            ).json(),
            first.json(),
        )
        stale = self.client.post(
            '/v1/projects/story-one/actions',
            json=body,
            headers={'Idempotency-Key': 'new-bulk'},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.json()['detail']['no_mutation_committed'])

    def test_quality_tier_sets_the_shot_resolution(self):
        self.prepare()
        shot = lambda: next(
            e for e in self.current()['project']['entities'] if e['id'] == 'shot-one'
        )
        self.edit(
            [
                {
                    'op': 'entity.update',
                    'entity_id': 'shot-one',
                    'patch': {'data': {'storyboard': {'version': 1, 'mode': 'fl'}}},
                }
            ],
            version=self.current()['version'],
        ).raise_for_status()
        for operation, resolution in (('final', '768P'), ('draft', '480P')):
            self.edit(
                [
                    {
                        'op': 'storyboard.bulk',
                        'shot_ids': ['shot-one'],
                        'operation': operation,
                    }
                ],
                version=self.current()['version'],
            ).raise_for_status()
            data = shot()['data']
            self.assertEqual(data['storyboard']['quality'], operation)
            self.assertEqual(data['h3']['controls']['resolution'], resolution)
            plan = self.preflight('tier-' + operation, copies=1).json()['items'][0][
                'plan'
            ]
            self.assertIn('"resolution": "' + resolution + '"', json.dumps(plan))

    def test_quality_tier_sets_resolution_on_a_shot_without_storyboard(self):
        self.prepare()
        for operation, resolution in (('final', '768P'), ('draft', '480P')):
            self.edit(
                [
                    {
                        'op': 'storyboard.bulk',
                        'shot_ids': ['shot-one'],
                        'operation': operation,
                    }
                ],
                version=self.current()['version'],
            ).raise_for_status()
            data = next(
                e
                for e in self.current()['project']['entities']
                if e['id'] == 'shot-one'
            )['data']
            self.assertNotIn('storyboard', data)
            self.assertEqual(data['qualityIntent'], operation)
            self.assertEqual(data['h3']['controls']['resolution'], resolution)

    def test_preferences_and_preflight_are_owner_scoped(self):
        self.prepare()
        value = self.preflight(copies=1).json()
        self.login('supervan')
        self.assertEqual(
            self.client.get(
                '/v1/projects/story-one/storyboard/preflights/' + value['id']
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                '/v1/projects/story-one/shots/shot-one/versions'
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get('/v1/storyboard/preferences').json()['version'], 0
        )

    def test_same_key_and_shot_ids_in_distinct_projects_never_share_plan_identity(self):
        self.prepare()
        first = self.preflight(copies=1).json()
        self.setup_project('story-two')
        self.edit(
            [
                {
                    'op': 'shot.configure_generation',
                    'shot_id': 'shot-one',
                    'prompt': 'Other project',
                }
            ],
            ident='story-two',
            version=1,
        ).raise_for_status()
        second = self.client.post(
            '/v1/projects/story-two/storyboard/preflights',
            headers={'Idempotency-Key': 'preflight-one'},
            json={'expected_version': 2, 'shot_ids': ['shot-one'], 'copies': 1},
        )
        second.raise_for_status()
        value = second.json()
        self.assertEqual(value['status'], 'completed', value)
        self.assertNotEqual(first['id'], value['id'])
        self.assertNotEqual(first['items'][0]['plan_id'], value['items'][0]['plan_id'])
        self.assertEqual(
            self.client.get(
                '/v1/projects/story-two/storyboard/preflights/' + first['id']
            ).status_code,
            404,
        )
        _, scoped = self.key(
            all_projects=False,
            project_ids=['story-one'],
            scopes=['projects:read', 'projects:write', 'jobs:read', 'jobs:write'],
        )
        self.assertEqual(
            self.client.get(
                '/v1/projects/story-two/storyboard/preflights/' + value['id'],
                headers=scoped,
            ).status_code,
            404,
        )

    def test_concurrent_same_preflight_recovers_one_set_of_plans(self):
        self.prepare()
        service = self.app.state.storyboard_versions
        p = Principal('superdan', 'browser:superdan')
        body = {
            'expected_version': self.current()['version'],
            'shot_ids': ['shot-one'],
            'copies': 2,
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _: service.preflight(p, 'story-one', body, 'concurrent'),
                    range(4),
                )
            )
        self.assertTrue(all(value == results[0] for value in results))
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(plans)), 2)
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)

    def test_last_transaction_fence_blocks_defaults_edit_between_outer_check_and_job_write(
        self,
    ):
        self.prepare()
        plan_id = self.preflight(copies=1).json()['items'][0]['plan_id']
        repo = self.app.state.repository
        original = repo.create_job

        def interleaved(*args, **kwargs):
            self.app.state.storyboard_versions.set_preferences(
                Principal('superdan', 'browser:superdan'),
                {'expected_version': 0, 'defaults': {'controls': {'steps': 19}}},
                'between-check-and-write',
            )
            return original(*args, **kwargs)

        with patch.object(repo, 'create_job', side_effect=interleaved):
            response = self.client.post(
                '/v1/jobs',
                headers={'Idempotency-Key': 'stale-create'},
                json={'plan_id': plan_id},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('storyboard_defaults_changed', response.text)
        with repo.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(budget_reservations)), 0
            )

    def test_enqueue_final_fence_and_original_accepted_replay(self):
        self.prepare()
        plan_id = self.preflight(copies=1).json()['items'][0]['plan_id']
        repo = self.app.state.repository
        scope = Scope(
            self.settings.tenant_id, 'superdan', 'story-one', 'browser:superdan'
        )
        # An inert job is not acceptance; it must pass the final fence when queued.
        inert = repo.create_job(scope, plan_id, 'inert', initial_status='planned')
        accepted = self.client.post(
            '/v1/jobs',
            headers={'Idempotency-Key': 'accepted'},
            json={'plan_id': plan_id},
        )
        accepted.raise_for_status()
        self.app.state.storyboard_versions.set_preferences(
            Principal('superdan', 'browser:superdan'),
            {'expected_version': 0, 'defaults': {'controls': {'steps': 19}}},
            'change-after-create',
        )
        with self.assertRaisesRegex(Conflict, 'storyboard_defaults_changed'):
            repo.enqueue(scope, inert['id'])
        replay = self.client.post(
            '/v1/jobs',
            headers={'Idempotency-Key': 'accepted'},
            json={'plan_id': plan_id},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()['id'], accepted.json()['id'])
        with repo.engine.connect() as conn:
            self.assertEqual(
                conn.scalar(select(jobs.c.status).where(jobs.c.id == inert['id'])),
                'planned',
            )
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(budget_reservations)), 0
            )

    def test_legacy_string_profile_binding_and_inherited_unselected_are_preserved(self):
        self.prepare()
        self.edit(
            [
                {
                    'op': 'entity.create',
                    'entity': {
                        'id': 'legacy-hero',
                        'type': 'character',
                        'data': {'looks': []},
                    },
                },
                {
                    'op': 'entity.update',
                    'entity_id': 'scene-one',
                    'patch': {'data': {'cast': ['legacy-hero']}},
                },
                {
                    'op': 'entity.create',
                    'entity': {
                        'id': 'other-shot',
                        'type': 'shot',
                        'parentId': 'scene-one',
                        'data': {'seconds': 5},
                    },
                },
            ],
            version=self.current()['version'],
        ).raise_for_status()
        before = self.current()
        result = self.edit(
            [
                {
                    'op': 'storyboard.fork_profile',
                    'profile_id': 'legacy-hero',
                    'new_profile_id': 'forked',
                    'shot_ids': ['shot-one'],
                }
            ],
            version=before['version'],
        )
        result.raise_for_status()
        data = {e['id']: e for e in result.json()['project']['entities']}
        self.assertEqual(data['shot-one']['data']['cast'], ['forked'])
        self.assertNotIn('cast', data['other-shot']['data'])
        self.assertEqual(data['scene-one']['data']['cast'], ['legacy-hero'])


if __name__ == '__main__':
    unittest.main()

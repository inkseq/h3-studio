"""Frozen owner-scoped film receipts, no provider/model calls."""

import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from studio_platform.auth import Principal
from studio_platform.film_runs import (
    FilmRuns,
    KIND,
    film_scope,
    frozen_story,
    selected_recipe_current,
    validate_film_source,
)
from studio_platform.generation_services import GenerationAccess
from studio_platform.render_plans import compile_film_export
from studio_platform.repository import (
    Repository,
    Scope,
    Conflict,
    NotFound,
    request_hash,
)
from test_platform_render_plans import story, source


class FilmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository('sqlite:///' + str(Path(self.temp.name) / 'film.db'))
        self.repo.create_schema()
        self.addCleanup(self.repo.close)
        self.principal = Principal('owner', 'browser')
        self.scope = film_scope('tenant', 'owner', 'story-one')
        self.project = story()
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'), 'project', 'story-one', self.project
        )
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                repository=self.repo,
                settings=SimpleNamespace(tenant_id='tenant', max_project_bytes=2**20),
                generation_access=GenerationAccess(self.repo, 'tenant'),
                resolve_render_source=lambda *args: source(args[-2], args[-1]),
                generation_admission=SimpleNamespace(
                    capabilities_provider=self.capabilities
                ),
            )
        )
        self.enqueue = Mock()
        self.service = FilmRuns(self.app, enqueue=self.enqueue)

    @staticmethod
    def capabilities():
        from studio_platform.capabilities import control_schema, RECIPES

        return {
            'capabilities_version': 'original-test-capabilities',
            'recipes': [
                {'id': ident, 'mode': mode, 'controls': control_schema()}
                for ident, mode in RECIPES.items()
            ],
        }

    def preview(self):
        return self.service.preflight(
            self.principal,
            'story-one',
            {'expected_version': 1, 'resolution': '480P'},
            'original-key',
        )

    def raw(self, run_id):
        return self.repo.get_document(self.scope, KIND, run_id)['payload']

    def test_preflight_reuses_selected_take_and_same_key_after_story_edit(self):
        self.project['entities'][2]['data']['selectedVideoRange'] = {
            'start': 1,
            'end': 4,
        }
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            self.project,
            expected_version=1,
        )
        body = {'expected_version': 2, 'resolution': '480P'}
        preview = self.service.preflight(self.principal, 'story-one', body, 'exact')
        self.assertEqual(
            preview['counts'],
            {
                'shots': 1,
                'reused': 1,
                'generate': 0,
                'generated_completed': 0,
                'chapters_completed': 0,
                'export_completed': 0,
            },
        )
        original = self.raw(preview['id'])
        self.assertEqual(
            frozen_story(original['project'], original['results']), self.project
        )
        changed = copy.deepcopy(self.project)
        changed['title'] = 'Editable next version'
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            changed,
            expected_version=2,
        )
        self.assertEqual(
            self.service.preflight(self.principal, 'story-one', body, 'exact'), preview
        )
        self.assertEqual(self.raw(preview['id'])['project'], self.project)
        self.assertNotIn('owners/', json.dumps(preview))

    def test_selected_recipe_survives_real_adoption_but_not_changed_generation_inputs(
        self,
    ):
        from studio_platform.generation_draft import plan_body, read_draft
        from studio_platform.source_snapshot import source_snapshot

        project = copy.deepcopy(self.project)
        shot = project['entities'][2]
        shot['data']['seconds'] = 5
        shot['data']['h3'] = {
            'recipeId': 'h3-base-fl2va-v1',
            'controls': {'resolution': '480P'},
        }
        frame = {
            **copy.deepcopy(project['entities'][3]),
            'id': 'frame',
            'type': 'image',
            'data': {'fileId': 'local-frame', 'cloudAssetId': 'original-frame'},
        }
        project['entities'].append(frame)
        project['links'].append(
            {
                'id': 'first',
                'source': 'frame',
                'target': 'shot-one',
                'role': 'firstFrame',
            }
        )
        caps = self.capabilities()
        draft, issues = read_draft(project, 'shot-one', capabilities=caps)
        self.assertEqual(issues, [])
        planned = plan_body(
            project, 'shot-one', caps, lambda *args: self.fail('no derivation')
        )
        recipe = {
            key: copy.deepcopy(planned[key])
            for key in ('recipe_id', 'prompt', 'controls')
        }
        recipe['inputs'] = draft['inputs']
        recipe['controls']['seed'] = '8235'
        original = {
            'authoring_recipe': recipe,
            'server_source_hash': source_snapshot(project, 'shot-one'),
        }
        shot['version'] += 1
        shot['data']['selectedAssetId'] = 'clip'
        self.assertNotEqual(
            original['server_source_hash'], source_snapshot(project, 'shot-one')
        )
        self.assertTrue(
            selected_recipe_current(project, 'shot-one', original, {}, caps)
        )
        for mutate in (
            lambda p: p['entities'][2]['data'].update(prompt='A changed prompt'),
            lambda p: p['entities'][2]['data']['h3'].update(
                recipeId='h3-base-ref2va-v1'
            ),
            lambda p: p['entities'][4]['data'].update(cloudAssetId='replacement-frame'),
            lambda p: p['entities'][2]['data']['h3']['controls'].update(seed='8236'),
        ):
            changed = copy.deepcopy(project)
            mutate(changed)
            self.assertFalse(
                selected_recipe_current(changed, 'shot-one', original, {}, caps)
            )
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )
        self.app.state.resolve_render_source = lambda *args: {
            **source(args[-2], args[-1]),
            'source_job_id': 'original-job',
        }
        with patch.object(
            self.service.access, 'job', return_value={'request': original}
        ):
            preview = self.service.preflight(
                self.principal,
                'story-one',
                {'expected_version': 2, 'resolution': '480P'},
                'adopted',
            )
        self.assertEqual(preview['counts']['reused'], 1)
        self.assertEqual(preview['counts']['generate'], 0)
        self.assertEqual(preview['warnings'], [])

    def test_historical_authorized_selected_take_is_reused_with_unknown_recipe_warning(
        self,
    ):
        self.app.state.resolve_render_source = lambda *args: {
            **source(args[-2], args[-1]),
            'source_job_id': 'historical-job',
        }
        with patch.object(
            self.service.access,
            'job',
            return_value={'request': {'server_source_hash': 'historical'}},
        ):
            preview = self.preview()
        self.assertEqual(preview['counts']['generate'], 0)
        self.assertEqual(preview['warnings'], ['film_selected_recipe_unknown'])

    def test_blocked_original_batch_is_rechecked_without_changing_job_identity(self):
        preview = self.preview()
        value = self._with_generation(preview['id'], 'original-job')
        submit = Mock(
            return_value={
                'items': [{'plan_id': 'original-plan', 'job_id': 'original-job'}]
            }
        )
        self.app.state.submit_generation_batch = submit
        original_version = self.repo.get_document(self.scope, KIND, preview['id'])[
            'version'
        ]
        for status in ('planned', 'blocked', 'running', 'failed'):
            with patch.object(
                self.service.access, 'job', return_value={'status': status}
            ):
                value = self.service._admit(
                    self.principal, value, value['generation'], 'shots'
                )
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(
            all(
                call.args[2] == preview['id'] + ':shots'
                for call in submit.call_args_list
            )
        )
        self.assertEqual(value['generation'][0]['job_id'], 'original-job')
        self.assertEqual(
            self.repo.get_document(self.scope, KIND, preview['id'])['version'],
            original_version,
        )

    def _with_generation(self, run_id, job_id):
        return self.service._mutate(
            self.principal,
            'story-one',
            run_id,
            lambda value: value['generation'].append(
                {
                    'shot_id': 'shot-one',
                    'plan_id': 'original-plan',
                    'job_id': job_id,
                }
            ),
        )

    def test_owner_project_actor_and_revision_fences(self):
        preview = self.preview()
        with self.assertRaises(NotFound):
            self.service.get(
                Principal('foreign', 'browser'), 'story-one', preview['id']
            )
        with self.assertRaises(NotFound):
            self.service.get(
                Principal('owner', 'scoped', True, ('other',), ('jobs:read',)),
                'story-one',
                preview['id'],
            )
        with self.assertRaises(Conflict):
            self.service.preflight(
                self.principal, 'story-one', {'expected_version': 9}, 'new'
            )
        with self.assertRaises(Conflict):
            self.service.preflight(
                self.principal,
                'story-one',
                {'expected_version': 1, 'resolution': '720P'},
                'original-key',
            )
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            side_effect=AssertionError('foreign actor must not inspect grants'),
        ):
            with self.assertRaisesRegex(Conflict, 'film_actor_mismatch'):
                self.service.submit(
                    Principal('owner', 'key:other'),
                    'story-one',
                    {'preflight_id': preview['id'], 'confirm': True},
                    'foreign-actor',
                )

    def test_submit_is_atomic_with_producer_and_confirm_is_not_a_spending_grant(self):
        preview = self.preview()
        body = {'preflight_id': preview['id'], 'confirm': True}
        self.enqueue.side_effect = RuntimeError('synthetic transport failure')
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            return_value=SimpleNamespace(version=1),
        ):
            with self.assertRaises(RuntimeError):
                self.service.submit(self.principal, 'story-one', body, 'submit')
        self.assertEqual(self.raw(preview['id'])['phase'], 'preflight')
        self.enqueue.side_effect = None
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            side_effect=Conflict('spending_confirmation_required'),
        ):
            with self.assertRaises(Conflict):
                self.service.submit(self.principal, 'story-one', body, 'submit')
        self.assertEqual(self.raw(preview['id'])['phase'], 'preflight')
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            return_value=SimpleNamespace(version=1),
        ):
            first = self.service.submit(self.principal, 'story-one', body, 'submit')
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            side_effect=AssertionError('must not repurchase'),
        ):
            self.assertEqual(
                self.service.submit(self.principal, 'story-one', body, 'submit'), first
            )
        self.assertEqual(first['phase'], 'generating')

    def test_frozen_authority_requires_registered_exact_plan_and_accepted_original(
        self,
    ):
        preview = self.preview()
        payload = {
            'client_ref': {'project_id': 'story-one'},
            'film_source_ref': {
                'run_id': preview['id'],
                'snapshot_hash': preview['snapshot_hash'],
            },
            'request': {'seed': '1'},
        }
        row = self.repo.get_document(self.scope, KIND, preview['id'])
        value = row['payload']
        value['plan_hashes'] = {'original-plan': request_hash(payload)}
        self.repo.put_document(
            self.scope, KIND, preview['id'], value, expected_version=row['version']
        )
        with self.repo.engine.connect() as conn, self.assertRaises(Conflict):
            validate_film_source(conn, self.repo, self.scope, payload)
        value['phase'] = 'generating'
        self.repo.put_document(
            self.scope, KIND, preview['id'], value, expected_version=2
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                validate_film_source(conn, self.repo, self.scope, payload)['id'],
                preview['id'],
            )
            changed = copy.deepcopy(payload)
            changed['request']['seed'] = '2'
            with self.assertRaises(Conflict):
                validate_film_source(conn, self.repo, self.scope, changed)
            with self.assertRaises(Conflict):
                validate_film_source(
                    conn, self.repo, Scope('other', 'owner', 'story-one'), payload
                )

    def test_cancel_preserves_header_and_finishes_only_domain_terminal(self):
        preview = self.preview()
        self.service.cancel(self.principal, 'story-one', preview['id'])
        self.assertEqual(
            self.service.turn(self.principal, 'story-one', preview['id']),
            {'terminal': True, 'phase': 'cancelled'},
        )
        self.assertEqual(
            self.raw(preview['id'])['snapshot_hash'], preview['snapshot_hash']
        )
        with self.assertRaises(Conflict):
            self.service.resume(self.principal, 'story-one', preview['id'])

    def test_export_uses_all_frozen_chapters_audio_offsets_and_same_media_bounds(self):
        video = source({'id': 'v'}, 'video')
        video['simulation'] = False
        audio = {
            **source({'id': 'a'}, 'audio'),
            'object': {
                **video['object'],
                'key': 'owners/owner/assets/a/file.flac',
                'content_type': 'audio/flac',
            },
        }
        chapter = {
            'chapter_id': 'chapter-one',
            'video': video,
            'audio': audio,
            'output_spec': {'width': 480, 'height': 480, 'fps': 24, 'frame_count': 48},
        }
        compiled = compile_film_export(
            'story-one', 'film-one', [chapter, {**chapter, 'chapter_id': 'chapter-two'}]
        )
        self.assertEqual(compiled['output_spec']['frame_count'], 96)
        self.assertEqual(
            [
                item['timeline_start']
                for item in compiled['request']['render']['audio_tracks']
            ],
            [0, 2],
        )
        wrong = copy.deepcopy(chapter)
        wrong['output_spec']['width'] = 720
        with self.assertRaises(ValueError):
            compile_film_export('story-one', 'film-one', [chapter, wrong])

    def test_missing_shot_keeps_original_preflight_seed_and_clone_identity(self):
        project = copy.deepcopy(self.project)
        project['entities'][2]['data'].pop('selectedAssetId')
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )

        def preflight(principal, project_id, body, key):
            plan = self.repo.create_plan(
                self.scope,
                {
                    'recipe_id': 'original-model',
                    'client_ref': {'project_id': project_id, 'shot_id': 'shot-one'},
                    'request': {'seed': 123},
                    'sources': {'original': {'sha256': 'a' * 64}},
                },
                {'enabled': True, 'quote_known': True},
                estimated_cost_microusd=10,
                expires_at=self.repo.clock() + 60,
            )
            self.repo.put_document(
                self.scope,
                'storyboard_preflight',
                'original-preview',
                {
                    'items': [
                        {'plan_id': plan['id'], 'shot_id': 'shot-one', 'seed': '123'}
                    ]
                },
            )
            return {'id': 'original-preview'}

        original = Mock(side_effect=preflight)
        self.app.state.storyboard_versions = SimpleNamespace(preflight=original)
        body = {'expected_version': 2, 'resolution': '480P'}
        preview = self.service.preflight(self.principal, 'story-one', body, 'missing')
        self.assertEqual(preview['generation'][0]['seed'], '123')
        self.assertNotEqual(
            preview['generation'][0]['plan_id'],
            preview['generation'][0]['source_plan_id'],
        )
        self.assertEqual(
            self.service.preflight(self.principal, 'story-one', body, 'missing'),
            preview,
        )
        original.assert_called_once()
        plan = self.repo.get_plan(self.scope, preview['generation'][0]['plan_id'])
        self.assertEqual(plan['request']['request']['seed'], 123)
        self.assertEqual(plan['request']['sources'], {'original': {'sha256': 'a' * 64}})

    def test_replayed_film_reuses_chapter_and_export_original_ids(self):
        preview = self.preview()
        with patch(
            'studio_platform.spending_limits.check_submission_confirmation',
            return_value=SimpleNamespace(version=1),
        ):
            self.service.submit(
                self.principal,
                'story-one',
                {'preflight_id': preview['id'], 'confirm': True},
                'submit',
            )
        admitted, jobs, artifact_map = [], {}, {}

        def submit(principal, body, key):
            admitted.append(key)
            items = []
            for plan_id in body['plan_ids']:
                plan = self.repo.get_plan(self.scope, plan_id)
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + plan_id))
                jobs[job_id] = {
                    'id': job_id,
                    'status': 'running',
                    'request': plan['request'],
                }
                artifact_map[job_id] = [
                    {
                        'id': str(uuid.uuid5(uuid.NAMESPACE_URL, job_id)),
                        'job_id': job_id,
                        'metadata': {
                            'kind': 'video',
                            'mime': 'video/mp4',
                            'object_key': 'owners/owner/artifacts/'
                            + job_id
                            + '/video.mp4',
                            'sha256': 'a' * 64,
                            'size_bytes': 100,
                        },
                    }
                ]
                items.append({'plan_id': plan_id, 'job_id': job_id})
            return {'items': items}

        self.app.state.submit_generation_batch = submit
        self.app.state.generation_planning = SimpleNamespace(
            policies=SimpleNamespace(
                evaluate=lambda *args: SimpleNamespace(
                    execution={'enabled': True}, expires_at=self.repo.clock() + 60
                )
            )
        )
        with (
            patch.object(
                self.service.access,
                'job',
                side_effect=lambda principal, ident: jobs[ident],
            ),
            patch.object(
                self.service.access,
                'artifact',
                side_effect=lambda principal, ident: next(
                    row
                    for rows in artifact_map.values()
                    for row in rows
                    if row['id'] == ident
                ),
            ),
            patch.object(
                self.repo,
                'list_artifacts',
                side_effect=lambda scope, ident: artifact_map[ident],
            ),
        ):
            first = self.service.turn(self.principal, 'story-one', preview['id'])
            self.assertEqual(first['phase'], 'chapters')
            version = self.repo.get_document(self.scope, KIND, preview['id'])['version']
            self.service.turn(self.principal, 'story-one', preview['id'])
            self.assertEqual(
                self.repo.get_document(self.scope, KIND, preview['id'])['version'],
                version,
            )
            jobs[next(iter(jobs))]['status'] = 'succeeded'
            self.assertEqual(
                self.service.turn(self.principal, 'story-one', preview['id'])['phase'],
                'exporting',
            )
            waiting = self.service.get(self.principal, 'story-one', preview['id'])
            self.assertEqual(waiting['counts']['chapters_completed'], 1)
            self.assertIsNotNone(waiting['chapters'][0]['artifact_id'])
            version = self.repo.get_document(self.scope, KIND, preview['id'])['version']
            self.service.turn(self.principal, 'story-one', preview['id'])
            self.assertEqual(
                self.repo.get_document(self.scope, KIND, preview['id'])['version'],
                version,
            )
            for job in jobs.values():
                job['status'] = 'succeeded'
            first = self.service.turn(self.principal, 'story-one', preview['id'])
            self.assertEqual(first, {'terminal': True, 'phase': 'completed'})
            final = self.raw(preview['id'])
            again = self.service.turn(self.principal, 'story-one', preview['id'])
        self.assertEqual(again, first)
        self.assertEqual(len(admitted), 2)
        self.assertEqual(len(jobs), 2)
        self.assertIsNotNone(final['export']['artifact_id'])
        self.assertEqual(
            self.repo.get_document(
                Scope('tenant', 'owner', '__projects'), 'project', 'story-one'
            )['payload'],
            self.project,
        )

    def test_workflow_uses_id_only_steps_and_durable_recv(self):
        from studio_platform.film_workflows import register_film_workflows

        waits, registration = [], []

        class Runtime:
            @staticmethod
            def step(**kwargs):
                registration.append(kwargs)
                return lambda function: function

            @staticmethod
            def workflow(**kwargs):
                registration.append(kwargs)
                return lambda function: function

            @staticmethod
            async def recv_async(**kwargs):
                waits.append(kwargs)

        service = SimpleNamespace(
            tenant='tenant',
            turn=Mock(
                side_effect=[
                    {'terminal': False, 'phase': 'generating'},
                    {'terminal': True, 'phase': 'completed'},
                ]
            ),
        )
        workflow = register_film_workflows(
            Runtime, service, lambda *args: self.principal
        )
        result = asyncio.run(workflow('tenant', 'owner', 'story-one', 'film-one'))
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(waits, [{'topic': 'film_changed', 'timeout_seconds': 10}])
        self.assertFalse(registration[0]['retries_allowed'])
        self.assertEqual(service.turn.call_args.args[1:], ('story-one', 'film-one'))


if __name__ == '__main__':
    unittest.main()

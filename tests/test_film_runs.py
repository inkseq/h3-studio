"""Frozen owner-scoped film receipts, no provider/model calls."""

import copy
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch
from sqlalchemy import select, update

from studio_platform.auth import AuthenticationError, Principal
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
    jobs,
    plans,
    documents,
    budget_reservations,
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

    def test_film_cut_excludes_missing_shot_before_generation_quote_or_hold(self):
        project = copy.deepcopy(self.project)
        missing = copy.deepcopy(project['entities'][2])
        missing.update(id='excluded-missing', order=-1)
        missing['data'].pop('selectedAssetId')
        project['entities'].append(missing)
        project['journey']['filmCuts'] = {'chapter-one': {'shotIds': ['shot-one']}}
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )
        planning = Mock(side_effect=AssertionError('Excluded shot must not be planned'))
        self.app.state.storyboard_versions = SimpleNamespace(preflight=planning)
        resolver = Mock(wraps=self.app.state.resolve_render_source)
        self.app.state.resolve_render_source = resolver
        with patch.object(
            self.repo, 'create_plan', side_effect=AssertionError('No quote or plan')
        ):
            preview = self.service.preflight(
                self.principal, 'story-one', {'expected_version': 2}, 'cut'
            )
        self.assertEqual(preview['counts']['shots'], 1)
        self.assertEqual(preview['counts']['generate'], 0)
        self.assertEqual(
            self.raw(preview['id'])['chapters'][0]['shot_ids'], ['shot-one']
        )
        self.assertEqual(self.raw(preview['id'])['project'], project)
        self.assertEqual(resolver.call_count, 1)
        planning.assert_not_called()
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(plans)).all(), [])
            self.assertEqual(connection.execute(select(jobs)).all(), [])
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
        project['entities'][2]['data'].pop('selectedAssetId')
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=2,
        )
        planning.side_effect = StopIteration('Inspect included generation only')
        with self.assertRaises(StopIteration):
            self.service.preflight(
                self.principal, 'story-one', {'expected_version': 3}, 'included-missing'
            )
        self.assertEqual(planning.call_args.args[2]['shot_ids'], ['shot-one'])

    def test_effectively_empty_cut_refuses_before_plans_or_receipt(self):
        self.project['entities'][2]['data'].pop('selectedAssetId')
        self.project['journey']['filmCuts'] = {'chapter-one': {'shotIds': ['unknown']}}
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            self.project,
            expected_version=1,
        )
        self.app.state.storyboard_versions = SimpleNamespace(
            preflight=Mock(side_effect=AssertionError('No generation'))
        )
        with patch.object(
            self.repo, 'create_plan', side_effect=AssertionError('No plan')
        ):
            with self.assertRaisesRegex(ValueError, '^film_cut_empty$'):
                self.service.preflight(
                    self.principal, 'story-one', {'expected_version': 2}, 'empty-cut'
                )
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(plans)).all(), [])
            self.assertEqual(
                connection.execute(
                    select(documents).where(documents.c.kind == KIND)
                ).all(),
                [],
            )

    def test_old_accepted_cut_keeps_original_shots_trim_and_caption_basis(self):
        from studio_platform.caption_server import caption_signature
        from studio_platform.render_plans import ordered_chapter, render_source_hash

        project = copy.deepcopy(self.project)
        second = copy.deepcopy(project['entities'][2])
        second.update(id='second', order=1)
        project['entities'].append(second)
        first = project['entities'][2]
        first['data']['selectedVideoRange'] = {
            'assetId': 'clip',
            'fileId': 'local-clip',
            'cloudAssetId': 'asset-one',
            'cloudArtifactId': None,
            'start': 1,
            'end': 3,
        }
        audio = copy.deepcopy(project['entities'][3])
        audio.update(id='voice', type='audio', data={'fileId': 'local-voice'})
        project['entities'].append(audio)
        project['journey']['sound']['mode'] = 'mixed'
        project['journey']['soundTracks']['chapter-one'] = [
            {
                'assetId': 'voice',
                'fileId': 'local-voice',
                'shotId': 'shot-one',
                'start': 0,
                'end': 1,
                'offset': 0,
                'gain': 0.7,
            }
        ]
        cues = [{'id': 'original', 'start': 0, 'end': 1, 'text': 'Original accepted'}]
        project['journey']['captionTracks'] = {'chapter-one': {'cues': cues}}
        project['journey']['captionTracks']['chapter-one']['confirmedSnapshot'] = (
            caption_signature(
                project, 'chapter-one', ordered_chapter(project, 'chapter-one')[2]
            )
        )
        old_ordered = copy.deepcopy(project)
        project['journey']['filmCuts'] = {'chapter-one': {'shotIds': ['second']}}
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )

        def legacy_selector(value, chapter_id):
            legacy = copy.deepcopy(value)
            legacy['journey'].pop('filmCuts', None)
            return ordered_chapter(legacy, chapter_id)

        with (
            patch(
                'studio_platform.film_runs.ordered_chapter', side_effect=legacy_selector
            ),
            patch(
                'studio_platform.render_plans.chapter_tracks',
                side_effect=lambda value, chapter_id: value['journey'][
                    'soundTracks'
                ].get(chapter_id, []),
            ),
        ):
            preview = self.service.preflight(
                self.principal,
                'story-one',
                {'expected_version': 2, 'burn_subtitles': True},
                'historical-cut',
            )
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(accepted_at=self.repo.clock(), phase='chapters'),
        )
        original = self.raw(preview['id'])
        captured = []

        def inspect_plan(_principal, _value, _label, compiled, blockers):
            self.assertEqual(blockers, [])
            captured.append(compiled)
            raise StopIteration('Stop before planning')

        with (
            patch.object(self.service, '_render_plan', side_effect=inspect_plan),
            patch.object(
                self.repo, 'create_plan', side_effect=AssertionError('No new quote')
            ),
        ):
            with self.assertRaises(StopIteration):
                self.service.turn(self.principal, 'story-one', preview['id'])
        self.assertEqual(self.raw(preview['id']), original)
        shots = captured[0]['request']['render']['shots']
        self.assertEqual([row['shot_id'] for row in shots], ['shot-one', 'second'])
        self.assertEqual([row['source_start_frame'] for row in shots], [24, 0])
        self.assertEqual(captured[0]['output_spec']['frame_count'], 96)
        self.assertEqual(
            captured[0]['request']['render']['subtitles']['cues'][0]['text'],
            'Original accepted',
        )
        self.assertEqual(
            captured[0]['request']['render']['audio_tracks'][0]['source_id'], 'voice'
        )
        self.assertEqual(
            captured[0]['server_source_hash'],
            render_source_hash(old_ordered, 'chapter-one', burn_subtitles=True),
        )

    def test_missing_sound_plan_refuses_before_generation_or_receipt_writes(self):
        planning = Mock(side_effect=AssertionError('No generation planning allowed'))
        self.app.state.storyboard_versions = SimpleNamespace(preflight=planning)
        for index, sound in enumerate(
            (None, {}, {'mode': None}, {'mode': 'unknown'}, {'mode': []})
        ):
            with self.subTest(sound=sound):
                project = copy.deepcopy(self.project)
                project['entities'][2]['data'].pop('selectedAssetId')
                if sound is None:
                    project['journey'].pop('sound')
                else:
                    project['journey']['sound'] = sound
                self.repo.put_document(
                    Scope('tenant', 'owner', '__projects'),
                    'project',
                    'story-one',
                    project,
                    expected_version=index + 1,
                )
                tables = (jobs, plans, documents, budget_reservations)
                with self.repo.engine.connect() as conn:
                    before = [
                        list(conn.execute(select(table)).mappings()) for table in tables
                    ]
                with (
                    patch.object(
                        self.repo, 'create_plan', wraps=self.repo.create_plan
                    ) as create_plan,
                    patch.object(
                        self.repo, 'put_document', wraps=self.repo.put_document
                    ) as put_document,
                ):
                    with self.assertRaisesRegex(Conflict, '^film_sound_plan_required$'):
                        self.service.preflight(
                            self.principal,
                            'story-one',
                            {'expected_version': index + 2, 'resolution': '480P'},
                            'missing-sound-' + str(index),
                        )
                    create_plan.assert_not_called()
                    put_document.assert_not_called()
                with self.repo.engine.connect() as conn:
                    self.assertEqual(
                        [
                            list(conn.execute(select(table)).mappings())
                            for table in tables
                        ],
                        before,
                    )
        planning.assert_not_called()
        self.enqueue.assert_not_called()

    def test_existing_sound_modes_remain_explicit_and_unchanged(self):
        for index, mode in enumerate(('silent', 'dialogue', 'music', 'mixed')):
            with self.subTest(mode=mode):
                project = copy.deepcopy(self.project)
                project['journey']['sound']['mode'] = mode
                self.repo.put_document(
                    Scope('tenant', 'owner', '__projects'),
                    'project',
                    'story-one',
                    project,
                    expected_version=index + 1,
                )
                preview = self.service.preflight(
                    self.principal,
                    'story-one',
                    {'expected_version': index + 2, 'resolution': '480P'},
                    'explicit-mode-' + mode,
                )
                self.assertEqual(self.raw(preview['id'])['project'], project)
                self.assertEqual(preview['counts']['generate'], 0)

    def test_failed_transport_is_visible_without_writing_or_restarting_work(self):
        preview = self.preview()
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(accepted_at=self.repo.clock(), phase='chapters'),
        )
        original = self.raw(preview['id'])
        transport = SimpleNamespace(film_status=Mock(return_value='ERROR'))
        self.repo.generation_transport = transport

        current = self.service.get(self.principal, 'story-one', preview['id'])
        self.assertEqual(current['phase'], 'blocked')
        self.assertEqual(current['error_code'], 'film_workflow_failed')
        self.assertEqual(current['failed_phase'], 'chapters')
        self.assertEqual(self.raw(preview['id']), original)
        self.enqueue.assert_not_called()
        self.assertEqual(transport.film_status.call_count, 1)
        self.assertEqual(
            transport.film_status.call_args.args[1:],
            ('tenant', 'owner', 'story-one', preview['id']),
        )
        with self.assertRaises(NotFound):
            self.service.get(
                Principal('foreign', 'browser'), 'story-one', preview['id']
            )
        self.assertEqual(transport.film_status.call_count, 1)

        transport.film_status.return_value = 'ENQUEUED'
        resumed = self.service.get(self.principal, 'story-one', preview['id'])
        self.assertEqual(resumed['phase'], 'chapters')
        self.assertNotIn('failed_phase', resumed)
        self.assertEqual(self.raw(preview['id']), original)
        self.enqueue.assert_not_called()

    def test_transport_projection_preserves_cancellation_and_known_refusals(self):
        preview = self.preview()
        transport = SimpleNamespace(film_status=Mock(return_value='ERROR'))
        self.repo.generation_transport = transport
        for phase, cancel, error in (
            ('preflight', False, None),
            ('chapters', True, None),
            ('completed', False, None),
            ('cancelled', False, None),
            ('blocked', False, 'film_render_input_blocked'),
            ('authorization_required', False, 'film_actor_grant_unavailable'),
        ):
            with self.subTest(phase=phase, cancel=cancel):
                self.service._mutate(
                    self.principal,
                    'story-one',
                    preview['id'],
                    lambda value: value.update(
                        accepted_at=None if phase == 'preflight' else self.repo.clock(),
                        phase=phase,
                        cancel_requested=cancel,
                        error_code=error,
                    ),
                )
                current = self.service.get(self.principal, 'story-one', preview['id'])
                self.assertEqual(current['phase'], phase)
                self.assertEqual(current['error_code'], error)
        transport.film_status.assert_not_called()
        self.enqueue.assert_not_called()

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
        changed['journey'].pop('sound')
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

    def confirmed_captions(self, project, text='Boat begins\nits quiet journey.'):
        from studio_platform.guided import Guided

        guided = Guided(self.app)
        guided.apply(
            project,
            {
                'op': 'captions.set',
                'chapter_id': 'chapter-one',
                'cues': [{'id': 'cue', 'start': 0, 'end': 1.9, 'text': text}],
            },
        )
        guided.apply(
            project,
            {
                'op': 'captions.confirm',
                'chapter_id': 'chapter-one',
                'reviewed': True,
            },
        )
        return project

    def test_caption_preflight_preserves_confirmation_and_reports_invalid_burn_content(
        self,
    ):
        from studio_platform.guided import Guided

        original = self.confirmed_captions(copy.deepcopy(self.project))
        version = 1
        for index, (change, reason) in enumerate(
            [
                (lambda p: None, None),
                (
                    lambda p: p['journey']['captionTracks']['chapter-one']['cues'][
                        0
                    ].update(text='Edited words'),
                    'film_captions_require_confirmation',
                ),
                (
                    lambda p: p['entities'][2]['data'].update(seconds=3),
                    'film_captions_require_confirmation',
                ),
                (
                    lambda p: p['journey']['sound'].update(mode='music'),
                    'film_captions_require_confirmation',
                ),
                (
                    lambda p: self.confirmed_captions(
                        p, 'A small boat begins its journey.'
                    ),
                    'film_captions_invalid',
                ),
            ]
        ):
            with self.subTest(reason=reason, index=index):
                project = copy.deepcopy(original)
                change(project)
                version += 1
                self.repo.put_document(
                    Scope('tenant', 'owner', '__projects'),
                    'project',
                    'story-one',
                    project,
                    expected_version=version - 1,
                )
                arguments = (
                    self.principal,
                    'story-one',
                    {
                        'expected_version': version,
                        'resolution': '480P',
                        'burn_subtitles': True,
                    },
                    'captions-' + str(index),
                )
                if reason:
                    with self.assertRaisesRegex(Conflict, reason):
                        self.service.preflight(*arguments)
                else:
                    preview = self.service.preflight(*arguments)
                    self.assertEqual(self.raw(preview['id'])['project'], original)
        # A millisecond-valid/SRT-confirmable cue can still contain less than
        # one whole24fps frame. Human confirmation does not waive burn bounds.
        project = copy.deepcopy(original)
        project['journey']['captionTracks']['chapter-one']['cues'][0].update(
            start=0.001, end=0.002
        )
        Guided(self.app).apply(
            project,
            {'op': 'captions.confirm', 'chapter_id': 'chapter-one', 'reviewed': True},
        )
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=version,
        )
        with self.assertRaisesRegex(Conflict, 'film_captions_invalid'):
            self.service.preflight(
                self.principal,
                'story-one',
                {
                    'expected_version': version + 1,
                    'resolution': '480P',
                    'burn_subtitles': True,
                },
                'subframe',
            )

    def test_caption_preflight_uses_same_half_frame_rounding_as_confirm_and_render(
        self,
    ):
        from studio_platform.guided import Guided

        project = copy.deepcopy(self.project)
        project['entities'][2]['data']['seconds'] = 12.5 / 24
        guided = Guided(self.app)
        guided.apply(
            project,
            {
                'op': 'captions.set',
                'chapter_id': 'chapter-one',
                'cues': [{'id': 'cue', 'start': 0, 'end': 13 / 24, 'text': 'Boat'}],
            },
        )
        guided.apply(
            project,
            {'op': 'captions.confirm', 'chapter_id': 'chapter-one', 'reviewed': True},
        )
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )
        preview = self.service.preflight(
            self.principal,
            'story-one',
            {'expected_version': 2, 'resolution': '480P', 'burn_subtitles': True},
            'half-frame',
        )
        self.assertEqual(preview['counts']['reused'], 1)

    def test_confirmed_frozen_captions_follow_original_generated_artifact_through_export(
        self,
    ):
        """Original queue/artifact receipts; synthetic media, no decode/inference."""
        from studio_platform.artifact_writer import ArtifactWriter
        from studio_platform.caption_server import caption_signature
        from studio_platform.queue import TaskQueue
        from studio_platform.render_plans import ordered_chapter

        project = copy.deepcopy(self.project)
        project['entities'][2]['data'].pop('selectedAssetId')
        self.confirmed_captions(project)
        confirmed = copy.deepcopy(project['journey']['captionTracks'])
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            project,
            expected_version=1,
        )
        execution = {'pool': 'film-test', 'backend': 'mock', 'quote_known': True}

        def preflight(principal, project_id, body, key):
            plan = self.repo.create_plan(
                self.scope,
                {
                    'recipe_id': 'test-shot',
                    'client_ref': {'project_id': project_id, 'shot_id': 'shot-one'},
                    'request': {'seed': 123},
                },
                execution,
                expires_at=self.repo.clock() + 60,
                estimated_cost_microusd=10,
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

        self.app.state.storyboard_versions = SimpleNamespace(preflight=preflight)
        preview = self.service.preflight(
            self.principal,
            'story-one',
            {'expected_version': 2, 'resolution': '480P', 'burn_subtitles': True},
            'captions-missing',
        )
        edited = copy.deepcopy(project)
        edited['journey']['captionTracks']['chapter-one']['cues'][0]['text'] = (
            'Later editable text'
        )
        self.repo.put_document(
            Scope('tenant', 'owner', '__projects'),
            'project',
            'story-one',
            edited,
            expected_version=2,
        )
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
        admitted = []
        self.repo.configure_budget(
            'film-caption-test',
            tenant_id='tenant',
            owner_id='owner',
            limit_microusd=1_000_000,
        )

        def submit(principal, body, key):
            admitted.append(key)
            return {
                'items': [
                    {
                        'plan_id': ident,
                        'job_id': self.repo.create_job(
                            self.service.access.scope(principal, 'story-one'),
                            ident,
                            key + ':' + ident,
                            budget_account_ids=('film-caption-test',),
                        )['id'],
                    }
                    for ident in body['plan_ids']
                ]
            }

        self.app.state.submit_generation_batch = submit
        self.app.state.generation_planning = SimpleNamespace(
            policies=SimpleNamespace(
                evaluate=lambda *args: SimpleNamespace(
                    execution=execution, expires_at=self.repo.clock() + 60
                )
            )
        )
        queue = TaskQueue(self.repo)

        def finish(job_id):
            claim = queue.claim('synthetic-worker', 'film-test', job_ids=[job_id])
            self.assertEqual(claim.job['id'], job_id)
            queue.begin_submission(claim.lease)
            queue.record_submitted(claim.lease, 'synthetic-' + job_id)
            queue.begin_collection(claim.lease)
            specs = ArtifactWriter._specs(
                {
                    'roles': {
                        'video': {
                            'key': 'owners/owner/artifacts/' + job_id + '/video.mp4',
                            'size_bytes': 100,
                            'sha256': 'a' * 64,
                            'content_type': 'video/mp4',
                            'evidence': {},
                        }
                    }
                }
            )
            queue.complete(claim.lease, specs, actual_cost_microusd=None)

        self.assertEqual(
            self.service.turn(self.principal, 'story-one', preview['id'])['phase'],
            'generating',
        )
        generated = self.raw(preview['id'])['generation'][0]['job_id']
        finish(generated)
        self.assertEqual(
            self.service.turn(self.principal, 'story-one', preview['id'])['phase'],
            'chapters',
        )
        value = self.raw(preview['id'])
        chapter = self.repo.get_plan(self.scope, value['chapters'][0]['plan_id'])
        self.assertEqual(
            chapter['request']['request']['render']['subtitles']['cues'][0]['text'],
            'Boat begins\nits quiet journey.',
        )
        self.assertEqual(value['project']['journey']['captionTracks'], confirmed)
        rendered = frozen_story(value['project'], value['results'])
        self.assertEqual(rendered['journey']['captionTracks'], confirmed)
        self.assertNotEqual(
            caption_signature(
                rendered, 'chapter-one', ordered_chapter(rendered, 'chapter-one')[2]
            ),
            confirmed['chapter-one']['confirmedSnapshot'],
        )
        self.assertEqual(value['snapshot_hash'], request_hash(value['project']))
        finish(value['chapters'][0]['job_id'])
        self.assertEqual(
            self.service.turn(self.principal, 'story-one', preview['id'])['phase'],
            'exporting',
        )
        value = self.raw(preview['id'])
        export = self.repo.get_plan(self.scope, value['export']['plan_id'])
        self.assertEqual(
            next(iter(export['request']['sources'].values()))['object']['content_type'],
            'video/mp4',
        )
        finish(value['export']['job_id'])
        self.assertEqual(
            self.service.turn(self.principal, 'story-one', preview['id']),
            {'terminal': True, 'phase': 'completed'},
        )
        self.service.turn(self.principal, 'story-one', preview['id'])
        self.assertEqual(len(admitted), 3)
        self.assertEqual(
            self.repo.get_document(
                Scope('tenant', 'owner', '__projects'), 'project', 'story-one'
            )['payload'],
            edited,
        )

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

    def test_revoked_original_grant_waits_visibly_and_same_actor_resume_keeps_ids(self):
        from studio_platform.film_workflows import register_film_workflows

        preview = self.preview()
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(
                phase='generating',
                accepted_at=self.repo.clock(),
            ),
        )
        self._with_generation(preview['id'], 'original-job')
        original = copy.deepcopy(self.raw(preview['id']))
        resolver = Mock(
            side_effect=[
                AuthenticationError('private failure text'),
                AuthenticationError(),
                self.principal,
            ]
        )
        waits, blocked_versions = [], []
        renewed = replace(self.principal, session_hash='b' * 64)

        class Runtime:
            step = staticmethod(lambda **kwargs: lambda function: function)
            workflow = staticmethod(lambda **kwargs: lambda function: function)

            @staticmethod
            async def recv_async(**kwargs):
                waits.append(kwargs)
                current = self.service.get(self.principal, 'story-one', preview['id'])
                self.assertEqual(current['phase'], 'authorization_required')
                self.assertEqual(current['error_code'], 'film_actor_grant_unavailable')
                blocked_versions.append(
                    self.repo.get_document(self.scope, KIND, preview['id'])['version']
                )
                if len(waits) == 2:
                    self.assertEqual(blocked_versions[0], blocked_versions[1])
                    resumed = self.service.resume(renewed, 'story-one', preview['id'])
                    self.assertEqual(resumed['phase'], 'generating')

        with patch.object(
            self.service, 'turn', return_value={'terminal': True, 'phase': 'completed'}
        ) as turn:
            workflow = register_film_workflows(Runtime, self.service, resolver)
            asyncio.run(workflow('tenant', 'owner', 'story-one', preview['id']))
        self.assertEqual(len(waits), 2)
        self.assertTrue(
            all(
                item == {'topic': 'film_changed', 'timeout_seconds': 10}
                for item in waits
            )
        )
        turn.assert_called_once_with(self.principal, 'story-one', preview['id'])
        final = self.raw(preview['id'])
        self.assertEqual(final['generation'], original['generation'])
        self.assertEqual(final['snapshot_hash'], original['snapshot_hash'])
        self.assertEqual(final['actor_grant']['session_hash'], renewed.session_hash)
        self.enqueue.assert_called_once()
        with self.assertRaisesRegex(Conflict, 'film_actor_mismatch'):
            self.service.resume(
                Principal('owner', 'another-actor'), 'story-one', preview['id']
            )

    def test_revoked_cancel_only_observes_own_terminal_original_jobs(self):
        preview = self.preview()
        plan = self.repo.create_plan(
            self.scope, {'original': 'accepted'}, {}, expires_at=self.repo.clock() + 60
        )
        original_job = self.repo.create_job(
            self.scope, plan['id'], 'original-job', initial_status='blocked'
        )
        self._with_generation(preview['id'], original_job['id'])
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(
                accepted_at=self.repo.clock(),
                cancel_requested=True,
                phase='cancel_requested',
            ),
        )
        arguments = (
            'tenant',
            'owner',
            'story-one',
            preview['id'],
            'film_actor_grant_unavailable',
        )
        before = self.repo.get_job(self.scope, original_job['id'])
        waiting = self.service.record_execution_block(*arguments)
        self.assertEqual(waiting['phase'], 'cancel_requested')
        self.assertFalse(waiting['terminal'])
        self.assertEqual(self.repo.get_job(self.scope, original_job['id']), before)
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == original_job['id'])
                .values(status='succeeded')
            )
        terminal = self.repo.get_job(self.scope, original_job['id'])
        self.assertEqual(
            self.service.record_execution_block(*arguments)['phase'], 'cancelled'
        )
        self.assertEqual(self.repo.get_job(self.scope, original_job['id']), terminal)
        self.enqueue.assert_not_called()

    def test_controller_block_retains_scope_trash_and_finite_metadata_guards(self):
        preview = self.preview()
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(
                accepted_at=self.repo.clock(), phase='generating'
            ),
        )
        original = self.raw(preview['id'])
        for tenant, owner, project_id in [
            ('foreign', 'owner', 'story-one'),
            ('tenant', 'foreign', 'story-one'),
            ('tenant', 'owner', 'foreign'),
        ]:
            with self.assertRaises((NotFound, Conflict)):
                self.service.record_execution_block(
                    tenant,
                    owner,
                    project_id,
                    preview['id'],
                    'film_actor_grant_unavailable',
                )
        with self.assertRaisesRegex(ValueError, 'film_execution_block_invalid'):
            self.service.record_execution_block(
                'tenant',
                'owner',
                'story-one',
                preview['id'],
                'arbitrary private message',
            )
        with patch.object(
            self.repo, '_content_write_guard', side_effect=Conflict('project_in_trash')
        ):
            with self.assertRaisesRegex(Conflict, 'project_in_trash'):
                self.service.record_execution_block(
                    'tenant',
                    'owner',
                    'story-one',
                    preview['id'],
                    'film_actor_grant_unavailable',
                )
        self.assertEqual(self.raw(preview['id']), original)

    def test_non_authentication_error_is_not_masked_as_grant_wait(self):
        from studio_platform.film_workflows import register_film_workflows

        class Runtime:
            step = staticmethod(lambda **kwargs: lambda function: function)
            workflow = staticmethod(lambda **kwargs: lambda function: function)

        workflow = register_film_workflows(
            Runtime, self.service, Mock(side_effect=RuntimeError('original failure'))
        )
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            asyncio.run(workflow('tenant', 'owner', 'story-one', 'original-run'))

    def test_known_business_refusal_is_visible_and_requires_same_run_resume(self):
        from studio_platform.film_workflows import register_film_workflows

        preview = self.preview()
        self.service._mutate(
            self.principal,
            'story-one',
            preview['id'],
            lambda value: value.update(accepted_at=self.repo.clock(), phase='chapters'),
        )
        original = self.raw(preview['id'])
        waits = []

        class Runtime:
            step = staticmethod(lambda **kwargs: lambda function: function)
            workflow = staticmethod(lambda **kwargs: lambda function: function)

            @staticmethod
            async def recv_async(**kwargs):
                waits.append(kwargs)
                blocked = self.service.get(self.principal, 'story-one', preview['id'])
                self.assertEqual(blocked['phase'], 'blocked')
                self.assertEqual(blocked['error_code'], 'film_render_input_blocked')
                self.assertEqual(
                    self.raw(preview['id'])['generation'], original['generation']
                )
                self.service.resume(self.principal, 'story-one', preview['id'])

        with patch.object(
            self.service,
            'turn',
            side_effect=[
                Conflict('film_render_input_blocked'),
                {'terminal': True, 'phase': 'completed'},
            ],
        ):
            workflow = register_film_workflows(
                Runtime, self.service, lambda *args: self.principal
            )
            asyncio.run(workflow('tenant', 'owner', 'story-one', preview['id']))
        self.assertEqual(waits, [{'topic': 'film_changed', 'timeout_seconds': 10}])
        self.assertEqual(self.raw(preview['id'])['phase'], 'generating')
        self.assertIsNone(self.raw(preview['id'])['error_code'])
        self.enqueue.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'film_execution_block_invalid'):
            self.service.record_business_block(
                self.principal, 'story-one', preview['id'], 'private driver text'
            )


if __name__ == '__main__':
    unittest.main()

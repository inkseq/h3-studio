"""Film-to-storyboard integration through real validation; no paid execution."""

from dataclasses import replace
import copy
import os
from pathlib import Path
import shutil
import unittest

from sqlalchemy import select, func

import test_storyboard_versions as fixtures
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.film_runs import film_scope
from studio_platform.repository import jobs, plans, NotFound, artifacts, attempts


class FilmStoryboardIntegrationTests(unittest.TestCase):
    setUp = fixtures.StoryboardVersionTests.setUp
    login = fixtures.StoryboardVersionTests.login
    setup_project = fixtures.StoryboardVersionTests.setup_project
    key = fixtures.StoryboardVersionTests.key
    edit = fixtures.StoryboardVersionTests.edit
    source = fixtures.StoryboardVersionTests.source
    configure = fixtures.StoryboardVersionTests.configure
    current = fixtures.StoryboardVersionTests.current
    prepare = fixtures.StoryboardVersionTests.prepare

    def test_missing_shot_uses_valid_original_storyboard_key_and_retains_seed(self):
        self.prepare()
        app = create_app(
            replace(self.settings, generation_enabled=True, execution_backend='mock'),
            repository=self.app.state.repository,
            storage=self.app.state.storage,
        )
        service = app.state.film_runs
        principal = Principal('superdan', 'browser')
        body = {'expected_version': self.current()['version'], 'resolution': '480P'}
        preview = service.preflight(principal, 'story-one', body, 'film-real-validator')
        self.assertEqual(
            preview['counts'],
            {
                'shots': 1,
                'reused': 0,
                'generate': 1,
                'generated_completed': 0,
                'chapters_completed': 0,
                'export_completed': 0,
            },
        )
        self.assertEqual(preview['generation'][0]['seed'], '23')
        self.assertEqual(
            service.preflight(principal, 'story-one', body, 'film-real-validator'),
            preview,
        )
        repo = app.state.repository
        scope = film_scope(self.settings.tenant_id, 'superdan', 'story-one')
        frozen = repo.get_plan(scope, preview['generation'][0]['plan_id'])
        original = repo.get_plan(scope, preview['generation'][0]['source_plan_id'])
        self.assertEqual(frozen['request']['request']['seed'], '23')
        self.assertEqual(
            frozen['request']['authoring_recipe'],
            original['request']['authoring_recipe'],
        )
        with repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(func.count()).select_from(jobs)).scalar_one(), 0
            )
            self.assertEqual(
                conn.execute(select(func.count()).select_from(plans)).scalar_one(), 2
            )

    @unittest.skipUnless(
        shutil.which('ffmpeg') and shutil.which('ffprobe'),
        'CPU ffmpeg/ffprobe required',
    )
    def test_real_app_generated_artifacts_reach_chapters_export_and_same_run_replay(
        self,
    ):
        """Real local media/receipts and services; no H3, provider or SDK execution."""
        from studio_platform.control import WorkerControl, WorkerSpec
        from studio_platform.render_backend import CPURenderBackend
        from studio_platform.render_plans import CONFIGURATIONS, MODEL, POOL, RECIPE
        from studio_platform.worker import MockBackend, WorkerRunner

        self.setup_project()
        self.configure(
            prompt='Synthetic CPU integration only',
            controls={
                'duration': 4,
                'resolution': '480P',
                'seed': '23',
                'generate_audio': False,
            },
            inputs={},
        ).raise_for_status()
        record = self.current()
        project = copy.deepcopy(record['project'])
        chapter, scene, shot = project['entities']
        shot['data']['seconds'] = 4
        second_chapter = {**copy.deepcopy(chapter), 'id': 'chapter-two', 'order': 1}
        second_scene = {
            **copy.deepcopy(scene),
            'id': 'scene-two',
            'parentId': 'chapter-two',
        }
        for ident, parent, order, seed in (
            ('shot-two', 'scene-one', 1, '24'),
            ('shot-three', 'scene-two', 0, '25'),
        ):
            extra = {
                **copy.deepcopy(shot),
                'id': ident,
                'parentId': parent,
                'order': order,
            }
            extra['data']['h3']['controls']['seed'] = seed
            project['entities'].append(extra)
        project['entities'].extend([second_chapter, second_scene])
        self.client.put(
            '/v1/projects/story-one',
            json={'project': project, 'expected_version': record['version']},
        ).raise_for_status()
        cues = []
        for ident in ('chapter-one', 'chapter-two'):
            cues.extend(
                [
                    {
                        'op': 'sound.set',
                        'chapter_id': ident,
                        'tracks': [],
                        'mode': 'silent',
                    },
                    {
                        'op': 'captions.set',
                        'chapter_id': ident,
                        'cues': [
                            {
                                'id': 'cue',
                                'start': 0,
                                'end': 1,
                                'text': 'Synthetic test',
                            }
                        ],
                    },
                    {'op': 'captions.confirm', 'chapter_id': ident, 'reviewed': True},
                ]
            )
        self.edit(cues, version=self.current()['version']).raise_for_status()
        frozen = self.current()
        app = create_app(
            replace(
                self.settings,
                generation_enabled=True,
                execution_backend='mock',
                render_enabled=True,
            ),
            repository=self.app.state.repository,
            storage=self.app.state.storage,
        )
        repo, store = app.state.repository, app.state.storage
        control = WorkerControl(repo)
        for configuration in sorted(set(CONFIGURATIONS.values())):
            spec = WorkerSpec(
                'integration-' + configuration,
                POOL,
                'local-cpu',
                'integration-' + configuration,
                (),
                (RECIPE,),
                MODEL,
                configuration,
                backend='cpu-render',
            )
            control.register(spec)
            control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        service = app.state.film_runs
        # Only SDK transport is substituted. Domain admission, plans, budgets,
        # ownership, media resolver and artifact collection are the real app.
        wakes = []
        service.enqueue = lambda conn, *identity: wakes.append(identity)
        principal = Principal('superdan', 'browser')
        preview = service.preflight(
            principal,
            'story-one',
            {
                'expected_version': frozen['version'],
                'resolution': '480P',
                'aspect': '16:9',
                'burn_subtitles': True,
            },
            'real-chain-preflight',
        )
        self.assertEqual(preview['counts']['generate'], 3)
        self.assertEqual(
            [item['seed'] for item in preview['generation']], ['23', '24', '25']
        )
        service.submit(
            principal,
            'story-one',
            {'preflight_id': preview['id'], 'confirm': True},
            'real-chain-confirm',
        )
        self.assertEqual(
            service.turn(principal, 'story-one', preview['id'])['phase'], 'generating'
        )
        scope = film_scope(self.settings.tenant_id, 'superdan', 'story-one')

        def value():
            return repo.get_document(scope, 'film_run', preview['id'])['payload']

        root = Path(self.tmp.name) / 'real-film-chain'
        generator = WorkerRunner(
            repo,
            store,
            root / 'generation',
            backend=MockBackend(root / 'synthetic-clips', enabled=True),
        )
        renderer = WorkerRunner(
            repo,
            store,
            root / 'collection',
            backend=CPURenderBackend(
                root / 'renders',
                enabled=True,
                subtitle_font_profile='windows-yahei'
                if os.name == 'nt'
                else 'noto-cjk',
            ),
            submission_guard=app.state.generation_planning.policies.submission_allowed,
        )

        def finish(runner, job_id, pool):
            runner.selected_job_id = job_id
            result = runner.run_once('cpu-integration', pool)
            with repo.engine.connect() as conn:
                attempt = dict(
                    conn.execute(select(attempts).where(attempts.c.job_id == job_id))
                    .mappings()
                    .one()
                )
            self.assertEqual(result['state'], 'succeeded', (result, attempt))
            self.assertEqual(repo.get_job(scope, job_id)['status'], 'succeeded')
            return repo.list_artifacts(scope, job_id)

        for item in value()['generation']:
            outputs = finish(generator, item['job_id'], 'mock')
            self.assertEqual(len(outputs), 1)
            self.assertTrue(outputs[0]['metadata']['validated'])
        # This reached State.resolve_render_source through real compile_render
        # before the wiring correction, rather than an injected fake resolver.
        self.assertEqual(
            service.turn(principal, 'story-one', preview['id'])['phase'], 'chapters'
        )
        chapter_ids = [item['job_id'] for item in value()['chapters']]
        self.assertEqual(len(chapter_ids), 2)
        self.assertTrue(all(chapter_ids))
        first = value()['generation'][0]
        source = value()['results'][first['shot_id']]['entity']
        self.assertEqual(
            app.state.resolve_render_source(principal, 'story-one', source, 'video')[
                'kind'
            ],
            'video',
        )
        with self.assertRaises(NotFound):
            app.state.resolve_render_source(
                Principal('supervan', 'browser'), 'story-one', source, 'video'
            )
        with self.assertRaises(NotFound):
            app.state.resolve_render_source(principal, 'another-story', source, 'video')
        for job_id in chapter_ids:
            finish(renderer, job_id, POOL)
        self.assertEqual(
            service.turn(principal, 'story-one', preview['id'])['phase'], 'exporting'
        )
        export_id = value()['export']['job_id']
        self.assertIsNotNone(export_id)
        finish(renderer, export_id, POOL)
        self.assertEqual(
            service.turn(principal, 'story-one', preview['id']),
            {'terminal': True, 'phase': 'completed'},
        )
        completed = value()
        with repo.engine.connect() as conn:
            counts = [
                conn.scalar(select(func.count()).select_from(table))
                for table in (jobs, attempts, artifacts)
            ]
        self.assertEqual(counts[0:2], [6, 6])
        self.assertEqual(
            service.turn(principal, 'story-one', preview['id']),
            {'terminal': True, 'phase': 'completed'},
        )
        self.assertEqual(value(), completed)
        with repo.engine.connect() as conn:
            self.assertEqual(
                [
                    conn.scalar(select(func.count()).select_from(table))
                    for table in (jobs, attempts, artifacts)
                ],
                counts,
            )
        self.assertEqual(self.current(), frozen)
        self.assertEqual(completed['project'], frozen['project'])
        self.assertEqual(len(wakes), 1)


if __name__ == '__main__':
    unittest.main()

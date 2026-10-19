"""Film-to-storyboard integration through real validation; no paid execution."""

from dataclasses import replace
import unittest

from sqlalchemy import select, func

import test_storyboard_versions as fixtures
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.film_runs import film_scope
from studio_platform.repository import jobs, plans


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


if __name__ == '__main__':
    unittest.main()

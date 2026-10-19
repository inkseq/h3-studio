"""Policy binding after final film and render compilation, with no model calls."""

import copy
import unittest
from unittest.mock import patch

from sqlalchemy import select

import test_film_storyboard_integration as film_fixtures
import test_platform_render_plans as render_fixtures
from studio_platform.content_policy import verify_input
from studio_platform.repository import plans


POLICY = {
    "revision": "creator-policy-v1",
    "version": "creator-policy-test-v1",
    "status": "draft",
    "moderation": {
        "enabled": True,
        "keywords": ["synthetic-forbidden-sentinel"],
        "output_classifier": None,
    },
}


class FilmPolicyCreatorTests(film_fixtures.FilmStoryboardIntegrationTests):
    def test_final_film_source_is_bound_to_its_own_valid_receipt(self):
        with patch(
            "studio_platform.content_policy.load_policy",
            side_effect=lambda _connection, _tenant: copy.deepcopy(POLICY),
        ):
            super().test_missing_shot_uses_valid_original_storyboard_key_and_retains_seed()
            repo = self.app.state.repository
            with repo.engine.connect() as connection:
                rows = list(connection.execute(select(plans)).mappings())
                original = next(
                    r for r in rows if "film_source_ref" not in r["request"]
                )
                frozen = next(r for r in rows if "film_source_ref" in r["request"])
                self.assertNotEqual(
                    frozen["request"]["content_moderation"]["input_hash"],
                    original["request"]["content_moderation"]["input_hash"],
                )
                for row in rows:
                    self.assertEqual(
                        verify_input(
                            connection,
                            row["tenant_id"],
                            row["owner_id"],
                            row["request"],
                            row["request"]["content_moderation"],
                        )["status"],
                        "allowed",
                    )


class RenderPolicyCreatorTests(render_fixtures.RenderApiTests):
    def test_real_render_api_can_admit_and_replay_with_prompt_screening_on(self):
        with patch(
            "studio_platform.content_policy.load_policy",
            side_effect=lambda _connection, _tenant: copy.deepcopy(POLICY),
        ):
            # Reuse actual media upload/plan/admission assertions, under the
            # newly enabled policy rather than rerunning unchanged OFF behavior.
            super().test_local_source_real_decode_and_idempotent_consent()


def load_tests(_loader, _tests, _pattern):
    return unittest.TestSuite(
        cls(name)
        for cls in (FilmPolicyCreatorTests, RenderPolicyCreatorTests)
        for name in cls.__dict__
        if name.startswith("test_")
    )

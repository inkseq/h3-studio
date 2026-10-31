"""POST /v1/enhance-plans then /v1/jobs (#795); the source take is patched in."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_platform_api as api_fixtures
from studio_platform.api import create_app
from studio_platform.settings import Settings

TAKE = {
    "id": "take",
    "type": "video",
    "title": "Take",
    "version": 1,
    "order": 0,
    "parentId": None,
    "description": "",
    "status": "draft",
    "data": {"fileId": "cloud_artifact_a", "cloudArtifactId": "artifact-one"},
}


def source_job(recipe="h3-default", width=1376, height=768):
    return {
        "request": {
            "recipe_id": recipe,
            "client_ref": {
                "project_id": "story-one",
                "shot_id": "s1",
                "shot_version": 2,
            },
            "output_spec": {"width": width, "height": height},
            "request": {"duration": 124 / 24},
        }
    }


class EnhanceRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            Settings(Path(tmp.name), auth_mode="local-test", enhance_enabled=True)
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"})
        project = api_fixtures.project()
        project["entities"].append(dict(TAKE))
        self.client.post("/v1/projects", json={"project": project}).raise_for_status()
        self.app.state.resolve_render_source = lambda *args: {
            "kind": "video",
            "duration": 124 / 24,
            "source_job_id": "job-one",
            "artifact_id": "artifact-one",
            "object": {"key": "k", "size_bytes": 1000, "sha256": "a" * 64},
            "simulation": False,
        }
        self.job = source_job()
        repository = self.app.state.repository
        original = repository.get_job
        patcher = patch.object(
            repository,
            "get_job",
            side_effect=lambda scope, job_id: (
                self.job if job_id == "job-one" else original(scope, job_id)
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        capacity = patch(
            "studio_platform.control.WorkerControl.pool_status",
            return_value={"ready": 1, "busy": 0},
        )
        capacity.start()
        self.addCleanup(capacity.stop)

    def plan(self, tier="2k"):
        return self.client.post(
            "/v1/enhance-plans",
            json={
                "client_ref": {"project_id": "story-one", "entity_id": "take"},
                "tier": tier,
            },
        )

    def test_plan_then_job_joins_the_source_shot(self):
        response = self.plan()
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        self.assertEqual(plan["status"], "ready", plan)
        self.assertEqual(plan["output_spec"]["width"], 2560)
        self.assertEqual(plan["enhance"]["tier"], "2k")
        self.assertNotIn("credits", plan["enhance"])
        # The provider behind enhance is internal (#898).
        self.assertNotIn("backend", plan["execution"])
        job = self.client.post(
            "/v1/jobs",
            json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "enhance-one"},
        )
        self.assertEqual(job.status_code, 202, job.text)
        self.assertEqual(job.json()["recipe_id"], "enhance-v1")
        self.assertNotIn("h3", job.text.lower())
        # Users see the tier and source take, never steps, sigma or weights.
        self.assertEqual(
            job.json()["effective_request"],
            {
                "duration": 124 / 24,
                "enhance": {"source_artifact_id": "artifact-one"},
                "tier": "2k",
            },
        )
        self.assertEqual(job.json()["client_ref"]["shot_id"], "s1")

    def test_capabilities_offer_enhance_only_when_enabled(self):
        enhance = self.client.get("/v1/capabilities").json()["enhance"]
        self.assertEqual(
            enhance,
            {"enabled": True, "tiers": ["1080p", "2k"], "plan": "/v1/enhance-plans"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            off = create_app(Settings(Path(tmp), auth_mode="local-test"))
            with TestClient(off) as client:
                client.post("/api/auth/login", json={"username": "superdan"})
                self.assertFalse(
                    client.get("/v1/capabilities").json()["enhance"]["enabled"]
                )

    def test_reviewed_terms_are_frozen_into_the_plan_before_the_job(self):
        # A reviewed policy with accepted terms: the plan must freeze the same
        # acceptance /v1/jobs recomputes, or the job is refused with 409.
        accepted = {"version": "terms-v1", "accepted_at": 1.0, "policy_revision": "r"}
        with patch(
            "studio_platform.content_policy.require_generation_terms",
            return_value=accepted,
        ):
            plan = self.plan("1080p").json()
            job = self.client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "enhance-terms"},
            )
        self.assertEqual(job.status_code, 202, job.text)
        from sqlalchemy import select

        from studio_platform.repository import plans

        with self.app.state.repository.engine.connect() as connection:
            execution = connection.execute(
                select(plans.c.execution_plan).where(plans.c.id == plan["plan_id"])
            ).scalar_one()
        self.assertEqual(execution["terms_acceptance"], accepted)

    def test_enhanced_or_oversized_sources_are_refused(self):
        for job in (source_job("enhance-v1"), source_job(width=1920, height=1088)):
            self.job = job
            with self.subTest(job=job["request"]["recipe_id"]):
                self.assertEqual(self.plan().status_code, 422)


if __name__ == "__main__":
    unittest.main()

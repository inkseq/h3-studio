"""HTTP cold-start integration using synthetic approval, fake provider and local DB."""
from dataclasses import replace

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.repository import Scope
from test_platform_api import generation_request, project
import test_platform_capacity as capacity_tests


# Inherit the fixture methods only, not the entire contract-test suite.
class CapacityApiTests(capacity_tests.CapacityTests):
    __unittest_skip__ = False

    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, tenant_id=self.scope.tenant_id, auth_mode="local-test")
        self.scope = Scope(self.scope.tenant_id, self.scope.owner_id, "story-one")
        self.app = create_app(self.settings, repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "superdan"}).status_code, 200)
        self.assertEqual(self.client.post("/v1/projects", json={"project": project()}).status_code, 201)

    def test_http_wait_retry_activate_same_job_get_and_owner_isolation(self):
        self.approve()
        response = self.client.post("/v1/generation-plans", json=generation_request())
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["execution"]["admission_state"], "waiting_capacity")
        self.assertNotIn("capacity_approval_id", str(plan))
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "http-wait"})
        self.assertEqual(job.status_code, 202, job.text)
        original = job.json()
        self.assertEqual(original["status"], "waiting_capacity")
        retry = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "http-wait"})
        self.assertEqual(retry.json()["id"], original["id"])
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)
        instance = self.start()
        self.worker(instance)
        self.assertEqual(self.tick()["activated"], 1)
        visible = self.client.get("/v1/jobs/"+original["id"])
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(visible.json()["id"], original["id"])
        self.assertEqual(visible.json()["status"], "queued")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "supervan"})
        self.assertEqual(self.client.get("/v1/jobs/"+original["id"]).status_code, 404)

    def test_http_cancel_waiting_releases_task_only_and_never_creates_instance(self):
        self.approve()
        plan = self.client.post("/v1/generation-plans", json=generation_request()).json()
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "http-cancel"}).json()
        result = self.client.post("/v1/jobs/"+job["id"]+"/cancel")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "cancelled")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        self.assertEqual(self.tick()["state"], "no_waiters")
        self.assertEqual(self.provider.creates, [])

    def test_http_edit_source_while_waiting_requires_new_precheck_and_keeps_instance_accounting(self):
        self.approve()
        plan = self.client.post("/v1/generation-plans", json=generation_request()).json()
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "http-stale"}).json()
        instance = self.start()
        record = self.client.get("/v1/projects/story-one").json()
        project_value = record["project"]
        # Parent changes are included even without a manually bumped shot version.
        project_value["entities"][1]["description"] = "The scene was edited while the GPU was starting"
        saved = self.client.put("/v1/projects/story-one", json={"project": project_value, "expected_version": record["version"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.worker(instance)
        self.assertEqual(self.tick()["failed"], 1)
        visible = self.client.get("/v1/jobs/"+job["id"]).json()
        self.assertEqual(visible["status"], "failed")
        self.assertEqual(visible["error_code"], "capacity_source_changed_before_activation")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.assertEqual(len(self.provider.creates), 1)

    def test_http_source_change_during_unknown_creation_releases_task_not_unknown_instance(self):
        from studio_platform.scaler import ProviderFact
        self.approve()
        plan = self.client.post("/v1/generation-plans", json=generation_request()).json()
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "http-stale-unknown"}).json()
        self.provider.create_uncertain = True
        instance = self.start()
        self.provider.facts[instance["id"]] = ProviderFact("unknown")
        record = self.client.get("/v1/projects/story-one").json()
        record["project"]["entities"][1]["description"] = "Changed while provider creation was uncertain"
        self.assertEqual(self.client.put("/v1/projects/story-one", json={"project": record["project"],
            "expected_version": record["version"]}).status_code, 200)
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.client.get("/v1/jobs/"+job["id"]).json()["error_code"], "capacity_source_changed_before_activation")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creation_unknown")
        self.assertEqual(len(self.provider.creates), 1)


# Do not duplicate inherited unit tests: this module runs only the HTTP cases.
for _name in dir(capacity_tests.CapacityTests):
    if _name.startswith("test_") and _name not in CapacityApiTests.__dict__:
        setattr(CapacityApiTests, _name, None)

if __name__ == "__main__":
    import unittest
    unittest.main()

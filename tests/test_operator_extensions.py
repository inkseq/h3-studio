"""Isolated ledger extensions; provider effects deliberately unavailable."""
import copy

from sqlalchemy import select, update

from studio_platform.auth import Principal
from studio_platform.operator_capacity import OperatorError, operator_nodes, node_version, operator_heartbeats
from studio_platform.repository import instance_intents, budget_reservations
from test_operator_capacity import OperatorCase


class ExtensionTests(OperatorCase):

    def prepare(self, *, headroom=True):
        self.create(chosen={**self.chosen, "ttl_seconds": 900})
        self.controller.tick()
        node = self.service.state(self.actor)["nodes"][0]
        self.node_id = node["id"]
        with self.repo.transaction() as conn:
            row = conn.execute(select(operator_nodes).where(operator_nodes.c.intent_id == self.node_id)).mappings().one()
            intent = conn.execute(select(instance_intents).where(instance_intents.c.id == self.node_id)).mappings().one()
            proof = {"state": "verified", "instance_id": intent["provider_instance_id"], "observed_at": self.now,
                "safe_deadline": intent["hard_deadline"] + (900 if headroom else 0)}
            conn.execute(update(operator_nodes).where(operator_nodes.c.intent_id == self.node_id).values(
                payload={**row["payload"], "lifetime": proof}))
        return self.current()

    def current(self):
        return next(n for n in self.service.state(self.actor)["nodes"] if n["id"] == self.node_id)

    def preview(self, seconds=300):
        return self.service.extension_preview(self.actor, self.node_id,
            {"expected_version": self.current()["version"], "additional_seconds": seconds})

    def test_explicit_extension_preserves_identity_reservations_and_replays(self):
        original = self.prepare()
        with self.repo.engine.connect() as conn:
            reservations = [dict(r) for r in conn.execute(select(budget_reservations)).mappings()]
            intent = dict(conn.execute(select(instance_intents)).mappings().one())
            node = copy.deepcopy(dict(conn.execute(select(operator_nodes)).mappings().one()))
        budget = self.repo.get_budget("owner-budget")
        preview = self.preview()
        self.assertTrue(preview["can_extend"], preview["blockers"])
        self.assertEqual(preview["incremental_reservation_microusd"], 0)
        self.assertEqual(preview["estimated_incremental_cost_microusd"], 30000)
        body = {"preview_id": preview["preview_id"]}
        result = self.service.extend(self.actor, self.node_id, body, "extension-1")
        self.assertEqual(result, self.service.extend(self.actor, self.node_id, body, "extension-1"))
        self.assertEqual(result["operation"]["state"], "completed")
        updated = self.current()
        self.assertEqual(updated["hard_deadline"], original["hard_deadline"] + 300)
        self.assertNotEqual(updated["version"], original["version"])
        self.assertEqual(updated["provider_instance_id"], original["provider_instance_id"])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        with self.repo.engine.connect() as conn:
            self.assertEqual(reservations, [dict(r) for r in conn.execute(select(budget_reservations)).mappings()])
            current = dict(conn.execute(select(instance_intents)).mappings().one())
            for k in ("id", "intent_key", "request_hash", "created_at", "reserved_cost_microusd", "provider_instance_id"):
                self.assertEqual(current[k], intent[k])
            current_node = dict(conn.execute(select(operator_nodes)).mappings().one())
            self.assertEqual(current_node["payload"]["selection"], node["payload"]["selection"])
        self.assertEqual(len(self.provider.creates), 1)

    def test_provider_hard_stop_is_specific_unsupported_not_fake_success(self):
        self.prepare(headroom=False)
        preview = self.preview()
        self.assertFalse(preview["can_extend"])
        self.assertIn({"code": "operator_provider_extension_unsupported"}, preview["blockers"])
        with self.assertRaisesRegex(OperatorError, "operator_extension_preview_blocked"):
            self.service.extend(self.actor, self.node_id, {"preview_id": preview["preview_id"]}, "blocked")
        self.assertEqual(len(self.provider.creates), 1)

    def test_stop_wins_stale_preview_and_newer_deadline_cannot_be_overwritten(self):
        node = self.prepare()
        preview = self.preview()
        self.service.node_command(self.actor, self.node_id, {"expected_version": node["version"]}, "stop", "stop")
        with self.assertRaisesRegex(OperatorError, "operator_node_version_conflict"):
            self.service.extend(self.actor, self.node_id, {"preview_id": preview["preview_id"]}, "after-stop")
        self.assertEqual(self.current()["hard_deadline"], node["hard_deadline"])
        self.assertFalse(self.preview()["can_extend"])

    def test_proof_expiry_budget_coverage_and_policy_rechecked_at_confirm(self):
        self.prepare()
        preview = self.preview()
        self.now += 31
        with self.repo.transaction() as conn:
            conn.execute(update(operator_heartbeats).values(observed_at=self.now))
        with self.assertRaisesRegex(OperatorError, "operator_provider_lifetime_unverified"):
            self.service.extend(self.actor, self.node_id, {"preview_id": preview["preview_id"]}, "stale-proof")
        self.assertFalse(self.preview()["can_extend"])
        with self.repo.transaction() as conn:
            row = conn.execute(select(operator_nodes)).mappings().one()
            conn.execute(update(operator_nodes).values(payload={**row["payload"], "lifetime": {**row["payload"]["lifetime"], "observed_at": self.now}}))
            conn.execute(update(budget_reservations).values(amount_microusd=1))
        self.assertIn({"code": "operator_extension_reservation_insufficient"}, self.preview()["blockers"])

    def test_invalid_inputs_and_machine_keys_do_not_mutate(self):
        node = self.prepare()
        for seconds in (False, 0, -1, 59, 14401, "300"):
            with self.assertRaisesRegex(OperatorError, "operator_extension_invalid"):
                self.preview(seconds)
        machine = Principal("superdan", "agent", machine=True)
        with self.assertRaisesRegex(OperatorError, "operator_forbidden"):
            self.service.extension_preview(machine, self.node_id, {"expected_version": node["version"], "additional_seconds": 300})
        self.assertEqual(self.current()["hard_deadline"], node["hard_deadline"])

    def test_http_preview_confirmation_readback_and_key_scope(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from studio_platform.operator_routes import register_routes
        node = self.prepare()
        app = FastAPI()
        actor = [self.actor]
        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.principal = actor[0]
            return await call_next(request)
        register_routes(app, service=self.service)
        prefix = f'/v1/operator/capacity/nodes/{node["id"]}'
        with TestClient(app) as client:
            response = client.post(prefix+'/extension-previews', json={"expected_version": node["version"], "additional_seconds": 300})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            body = {"preview_id": response.json()["preview_id"]}
            response = client.post(prefix+'/extensions', json=body, headers={"Idempotency-Key": "http-extension"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["operation"]["extension"]["new_deadline"], self.current()["hard_deadline"])
            self.assertEqual(response.json(), client.post(prefix+'/extensions', json=body,
                headers={"Idempotency-Key": "http-extension"}).json())
            self.assertEqual(client.post(prefix+'/extensions', json=body).status_code, 422)
            actor[0] = Principal("superdan", "agent", machine=True)
            self.assertEqual(client.post(prefix+'/extension-previews', json={"expected_version": node["version"], "additional_seconds": 300}).status_code, 403)

    def test_preview_is_actor_scoped_and_cannot_be_consumed_twice(self):
        self.prepare()
        self.settings.operator_capacity_owners = ("superdan", "supervan")
        preview = self.preview()
        with self.assertRaisesRegex(OperatorError, "operator_extension_preview_not_found"):
            self.service.extend(Principal("supervan", "browser"), self.node_id, {"preview_id": preview["preview_id"]}, "other")
        self.service.extend(self.actor, self.node_id, {"preview_id": preview["preview_id"]}, "first")
        with self.assertRaisesRegex(OperatorError, "operator_preview_already_confirmed"):
            self.service.extend(self.actor, self.node_id, {"preview_id": preview["preview_id"]}, "second")

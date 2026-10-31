"""Real stored reads, existing admin boundary and no inventory-driven writes."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from studio_platform import settings_inventory as inventory
from studio_platform.repository import documents, outbox, spending_settings
import test_capacity_settings as fixtures

PATH = "/v1/admin/settings-inventory"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CapacityRouteTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def get(self):
        response = self.f.client.get(PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        result = response.json()
        self.assertEqual(len({x["id"] for x in result["items"]}), len(result["items"]))
        return result, {x["id"]: x for x in result["items"]}

    def counts(self):
        with self.f.app.state.repository.engine.connect() as connection:
            return tuple(
                connection.scalar(select(func.count()).select_from(t))
                for t in (documents, outbox, spending_settings)
            )

    def test_defaults_are_truthful_and_reads_never_adopt(self):
        before = self.counts()
        result, rows = self.get()
        cap = rows["capacity.fleet_hourly_limit_microusd"]
        self.assertEqual(cap["revision"], 0)
        self.assertFalse(cap["adopted"])
        self.assertTrue(cap["source"].startswith("runtime_default:"))
        self.assertFalse(rows["modal_defaults.max_containers"]["effective"])
        self.assertIn(
            "modal_runtime:controller_readback_required", result["unavailable"]
        )
        self.assertIn("pending:first_video_max_seconds", result["unavailable"])
        self.assertNotIn("capacity.first_video_max_seconds", rows)
        self.assertEqual(self.counts(), before)

    def test_read_through_after_audited_change_and_no_provider_calls(self):
        preview = self.f.preview({"max_machines": 3})
        committed = self.f.commit(preview, {"max_machines": 3})
        self.assertEqual(committed.status_code, 200, committed.text)
        before = self.counts()
        _, rows = self.get()
        value = rows["capacity.max_machines"]
        self.assertEqual(value["value"], 3)
        self.assertEqual(value["revision"], committed.json()["version"])
        self.assertEqual(value["latest_change"]["reason"], "#456 approved settings")
        self.assertEqual(value["source"], "database:spending_settings")
        self.assertEqual(self.counts(), before)

    def test_nonadmin_and_machine_remain_forbidden(self):
        self.f.login("supervan")
        self.assertEqual(self.f.client.get(PATH).status_code, 403)
        self.f.login("superdan")
        auth = self.f.app.state.auth
        token = auth.create_key(
            "superdan",
            authenticated_session=auth.session(
                self.f.client.cookies.get("sixnine_session")
            ),
            name="inventory-fixture",
            scopes=["jobs:read"],
            project_ids=[],
            all_projects=True,
            expires_in_days=1,
        )
        with TestClient(
            self.f.app, headers={"Authorization": "Bearer " + token["api_key"]}
        ) as client:
            self.assertEqual(client.get(PATH).status_code, 403)

    def test_other_tenant_and_unknown_fields_are_not_exposed(self):
        with self.f.app.state.repository.transaction() as connection:
            connection.execute(
                insert(spending_settings).values(
                    tenant_id="other",
                    owner_id="__admin__",
                    version=1,
                    value={"max_machines": 987, "api_key": "synthetic-secret"},
                    actor_id="other-actor",
                    updated_at=1,
                )
            )
        result, _ = self.get()
        self.assertNotIn("synthetic-secret", str(result))
        self.assertNotIn("other-actor", str(result))
        stored = {
            "version": 1,
            "value": {"max_machines": 2, "api_key": "synthetic-secret"},
        }
        self.assertEqual(
            [x["id"] for x in inventory.capacity_items(stored, {})],
            ["capacity.max_machines"],
        )

    def test_model_resource_and_reference_counts_keep_their_distinct_meanings(self):
        import test_platform_full_controls as model_fixture

        f = model_fixture.FullControlsIntegrationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        rows, unavailable = inventory.model_items(f.settings, f.repo.clock())
        self.assertEqual(unavailable, [])
        ref = [x for x in rows if x["mode"] == "ref"]
        envelope = next(x for x in ref if x["id"].endswith("resource_envelope"))
        limits = next(x for x in ref if x["id"].endswith("reference_limits"))
        self.assertEqual(envelope["value"]["input_limits"]["max_images"], 17)
        self.assertEqual(limits["value"]["max_images"], 9)
        self.assertEqual(len(envelope["file_sha256"]), 64)
        self.assertFalse(envelope["editable"])
        self.assertNotIn(str(f.profiles), str(rows))

    def test_start_rules_read_the_original_revision_without_provider_calls(self):
        import test_capacity_start_rules as rules_fixture

        f = rules_fixture.StartRulesTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.addCleanup(f.tearDown)
        body = f.confirmed(f.body(boot_seconds=751))
        original = f.rules.write(f.owner, body, key="inventory-rules")
        before = f.counts()
        app = SimpleNamespace(state=SimpleNamespace(dstack_operator=f.service))
        with patch.object(
            f.capacity, "plan", side_effect=AssertionError("no provider")
        ):
            with f.repo.engine.connect() as connection:
                rows, missing = inventory.start_items(app, connection)
        self.assertEqual(missing, [])
        row = next(x for x in rows if x["id"] == "start_rules.5090-fl.boot_seconds")
        self.assertEqual(row["value"], 751)
        self.assertEqual(row["revision"], original["revision"])
        self.assertFalse(row["effective"])
        self.assertEqual(row["latest_change"]["reason"], body["reason"])
        self.assertEqual(f.counts(), before)

    def test_credit_price_inventory_uses_adopted_values_and_tenant_scope(self):
        import test_credits_routes as credit_fixture

        f = credit_fixture.CreditsRouteTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        revision = f.enable()
        with f.repo.engine.connect() as connection:
            rows, missing = inventory.credit_items(connection, "sixnine")
            foreign, _ = inventory.credit_items(connection, "other")
        rates = next(x for x in rows if x["id"] == "credits.rates")
        self.assertEqual(rates["revision"], revision)
        self.assertTrue(rates["effective"])
        self.assertEqual(rates["value"]["standard"], 4)
        self.assertFalse(any(x["revision"] == revision for x in foreign))
        self.assertNotIn("credits:credit_prices_unavailable", missing)


if __name__ == "__main__":
    unittest.main()

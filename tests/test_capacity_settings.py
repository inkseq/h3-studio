"""Admin-only capacity policy adoption/CAS; no cloud or provider clients."""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, func

from studio_platform import capacity_settings as capacity
from studio_platform.api import create_app
from studio_platform.repository import (
    outbox,
    spending_settings,
    budget_accounts,
    budget_reservations,
)
from studio_platform.settings import Settings
from studio_platform.spending_limits import ADMIN_DEFAULTS, ADMIN_OWNER

PATH = "/v1/operator/admin/spending-settings"


class CapacityPolicyTests(unittest.TestCase):
    def legacy(self, **changes):
        return {"version": 7, "value": {**ADMIN_DEFAULTS, **changes}}

    def test_legacy_read_and_snapshot_never_adopt_or_mutate(self):
        old = self.legacy(max_runtime_seconds=86400)
        before = copy.deepcopy(old)
        metadata = capacity.metadata(old)
        self.assertFalse(metadata["adopted"])
        self.assertEqual(metadata["defaults"]["max_hourly_price_microusd"], 3_000_000)
        self.assertIsNone(capacity.admission_snapshot(old))
        self.assertEqual(old, before)
        preview = capacity.preview_change(
            old, {"fleet_hourly_limit_microusd": 3_000_000}, {}
        )
        self.assertEqual(preview["value"]["max_runtime_seconds"], 86400)
        self.assertEqual(preview["value"]["max_hourly_price_microusd"], 1_000_000)
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(old, before)

    def test_recommended_machine_ceiling_matches_ref_without_adopting(self):
        old = {"version": 0, "value": {}}
        preview = capacity.preview_change(old, {}, {})
        self.assertEqual(preview["value"]["max_hourly_price_microusd"], 3_000_000)
        self.assertFalse(capacity.metadata(old)["adopted"])
        self.assertEqual(old, {"version": 0, "value": {}})
        self.assertNotIn(
            "mode_ceiling_above_machine_ceiling",
            {item["code"] for item in preview["consequences"]},
        )
        self.assertEqual(preview["value"]["fleet_hourly_limit_microusd"], 15_000_000)
        self.assertEqual(preview["value"]["max_machines"], 10)
        self.assertEqual(preview["value"]["tenant_daily_limit_microusd"], 100_000_000)
        self.assertEqual(
            preview["value"]["mode_hourly_ceiling_microusd"]["ref"], 3_000_000
        )
        metadata = capacity.metadata(old)
        self.assertEqual(
            metadata["field_bounds"]["max_machines"], {"minimum": 1, "maximum": 10}
        )
        self.assertEqual(
            metadata["supplier_admission"],
            {
                "basis": "confirmed_supplier_exposure",
                "unresolved_references": "warning",
            },
        )

    def test_original_two_machine_snapshot_survives_new_recommended_defaults(self):
        value = {
            **copy.deepcopy(capacity.DEFAULTS),
            "max_machines": 2,
            "fleet_hourly_limit_microusd": 3_000_000,
            "max_hourly_price_microusd": 1_500_000,
        }
        value["mode_hourly_ceiling_microusd"]["ref"] = 1_500_000
        snapshot = capacity.admission_snapshot({"version": 1, "value": value})
        before = copy.deepcopy(snapshot)
        self.assertEqual(capacity.validate_snapshot(snapshot), value)
        self.assertEqual(snapshot, before)

    def test_lower_machine_ceiling_warns_once_per_mode_and_still_constrains(self):
        profiles = {
            name: {"mode": "ref", "price_ceiling_microusd": 2_000_000}
            for name in ("ref-a", "ref-b")
        }
        preview = capacity.preview_change(self.legacy(), {}, profiles)
        self.assertEqual(
            [
                item
                for item in preview["consequences"]
                if item["code"] == "mode_ceiling_above_machine_ceiling"
            ],
            [
                {
                    "code": "mode_ceiling_above_machine_ceiling",
                    "mode": "ref",
                    "mode_hourly_microusd": 3_000_000,
                    "machine_hourly_microusd": 1_000_000,
                }
            ],
        )
        combination = next(
            item
            for item in preview["consequences"]
            if item["code"] == "fleet_limits_combination"
        )
        self.assertEqual(combination["required_hourly_microusd"], 10_000_000)
        self.assertEqual(preview["value"]["max_hourly_price_microusd"], 1_000_000)

    def test_profile_ceiling_still_constrains_higher_recommended_machine_cap(self):
        profiles = {"ref": {"mode": "ref", "price_ceiling_microusd": 850_000}}
        preview = capacity.preview_change({"version": 0, "value": {}}, {}, profiles)
        combination = next(
            item
            for item in preview["consequences"]
            if item["code"] == "fleet_allows_combination"
        )
        self.assertEqual(combination["hourly_microusd"], 8_500_000)
        self.assertEqual(preview["profile_snapshots"], profiles)

    def test_invalid_changes_have_finite_field_diagnostics(self):
        for field, value in (
            ("max_machines", True),
            ("max_machines", 11),
            ("tenant_daily_warning_fraction", float("nan")),
            ("tenant_daily_warning_fraction", float("inf")),
            ("tenant_daily_warning_fraction", 0.49),
            ("tenant_daily_limit_microusd", 0),
            ("tenant_daily_limit_microusd", 10**1000),
            ("max_runtime_seconds", 60),
            ("test_task_limit_microusd", 50_000_001),
            ("capacity_policy_version", True),
            ("unknown", 1),
        ):
            with self.subTest(field=field, value_type=type(value).__name__):
                with self.assertRaises(capacity.CapacitySettingsError) as caught:
                    capacity.preview_change(self.legacy(), {field: value}, {})
                self.assertEqual(caught.exception.code, "spending_settings_invalid")
                self.assertEqual(caught.exception.details[0]["field"], field)

    def test_cross_field_constraints_and_pending_metadata(self):
        for changes in (
            {"mode_hourly_ceiling_microusd": {"other": 1_000_000}},
            {"idle_quiet_seconds": 1600},
            {"max_machines": 1, "per_user_minimum_machine_share": 2},
            {"batch_guaranteed_share_slots": {"numerator": 5, "denominator": 4}},
            {"per_user_machine_share_policy": "arbitrary-code"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(capacity.CapacitySettingsError),
            ):
                capacity.preview_change(self.legacy(), changes, {})
        preview = capacity.preview_change(self.legacy(), {}, {})
        codes = {item["code"] for item in preview["consequences"]}
        self.assertEqual(
            codes,
            {
                "tenant_daily_supplier_limit",
                "admitted_work_may_exceed_daily_limit",
                "fleet_limits_combination",
                "mode_price_ceiling",
                "mode_ceiling_above_machine_ceiling",
                "setting_next_admission_only",
                "control_pending_implementation",
            },
        )
        self.assertTrue(
            capacity.metadata(self.legacy())["controls"]["idle_active_seconds"][
                "pending_implementation"
            ]
        )
        self.assertFalse(
            capacity.metadata(self.legacy())["controls"]["max_machines"][
                "pending_implementation"
            ]
        )

    def test_lower_fleet_is_confirmable_not_silently_raised(self):
        old = self.legacy(max_hourly_price_microusd=2_000_000)
        preview = capacity.preview_change(
            old, {"fleet_hourly_limit_microusd": 1_000_000}, {}
        )
        effect = next(
            x
            for x in preview["consequences"]
            if x["code"] == "fleet_limits_combination"
        )
        self.assertEqual(effect["required_hourly_microusd"], 20_000_000)
        self.assertEqual(preview["value"]["fleet_hourly_limit_microusd"], 1_000_000)

    def test_snapshot_and_digest_bind_admin_profile_and_consequences(self):
        old = self.legacy()
        profiles = {
            "ref": {
                "snapshot": "a" * 64,
                "mode": "ref",
                "max_price_microusd": 850000,
                "price_ceiling_microusd": 850000,
            }
        }
        preview = capacity.preview_change(old, {}, profiles)
        different = copy.deepcopy(profiles)
        different["ref"]["snapshot"] = "b" * 64
        self.assertNotEqual(
            preview["confirmation_digest"],
            capacity.preview_change(old, {}, different)["confirmation_digest"],
        )
        adopted = {"version": 8, "value": preview["value"]}
        snapshot = capacity.admission_snapshot(adopted)
        adopted["value"]["max_machines"] = 8
        self.assertEqual(snapshot["value"]["max_machines"], 10)
        self.assertEqual(snapshot["settings_version"], 8)

    def test_snapshot_validation_preserves_legacy_ranges_but_rejects_bad_authority(
        self,
    ):
        old = self.legacy(max_runtime_seconds=86400)
        value = capacity.preview_change(old, {}, {})["value"]
        snapshot = capacity.admission_snapshot({"version": 8, "value": value})
        self.assertEqual(
            capacity.validate_snapshot(snapshot)["max_runtime_seconds"], 86400
        )
        for mutate in (
            lambda s: s.update(settings_version=True),
            lambda s: s.update(capacity_policy_version=2),
            lambda s: s.update(extra="authority"),
            lambda s: s["value"].pop("tenant_daily_limit_microusd"),
            lambda s: s["value"].update(max_runtime_seconds=True),
            lambda s: s["value"].update(max_machines=11),
            lambda s: s["value"]["mode_hourly_ceiling_microusd"].update(
                other=5_000_000
            ),
        ):
            broken = copy.deepcopy(snapshot)
            mutate(broken)
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                capacity.validate_snapshot(broken)


class CapacityRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(
            Settings(
                Path(self.temp.name),
                auth_mode="local-test",
                operator_capacity_owners=("superdan", "supervan"),
                operator_admin_owners=("superdan",),
            )
        )
        self.client = TestClient(self.app)
        self.app.state.repository.configure_capacity(
            max_instances=2, max_physical_gpus=2
        )
        self.login("superdan")

    def tearDown(self):
        self.client.close()
        self.app.state.repository.close()
        self.temp.cleanup()

    def login(self, username):
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": username, "password": ""}
            ).status_code,
            200,
        )

    def preview(self, changes=None):
        old = self.client.get(PATH).json()
        response = self.client.post(
            PATH + "/preview",
            json={"expected_version": old["version"], "changes": changes or {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def commit(self, preview, changes=None, **extra):
        return self.client.put(
            PATH,
            json={
                "expected_version": preview["expected_version"],
                "changes": changes or {},
                "reason": "#456 approved settings",
                "confirmation_digest": preview["confirmation_digest"],
                "profile_snapshots": preview["profile_snapshots"],
                **extra,
            },
        )

    def money_counts(self):
        with self.app.state.repository.engine.connect() as c:
            return tuple(
                c.scalar(select(func.count()).select_from(t))
                for t in (budget_accounts, budget_reservations)
            )

    def test_explicit_test_cap_increase_requires_confirmation_and_preserves_snapshot(
        self,
    ):
        self.assertEqual(capacity.DEFAULTS["test_task_limit_microusd"], 20_000_000)
        adopted = self.commit(self.preview())
        self.assertEqual(adopted.status_code, 200, adopted.text)
        snapshot = capacity.admission_snapshot(adopted.json())
        original = copy.deepcopy(snapshot)
        counts = self.money_counts()
        changes = {"test_task_limit_microusd": 50_000_000}
        preview = self.preview(changes)
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(
            self.commit(preview, changes, confirmation_digest="").status_code, 409
        )
        self.assertEqual(
            self.client.get(PATH).json()["value"]["test_task_limit_microusd"],
            20_000_000,
        )
        response = self.commit(preview, changes)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["value"]["test_task_limit_microusd"], 50_000_000
        )
        self.assertEqual(self.commit(preview, changes).status_code, 409)
        self.assertEqual(capacity.validate_snapshot(snapshot), original["value"])
        self.assertEqual(snapshot, original)
        self.assertEqual(self.money_counts(), counts)

    def test_get_preview_are_read_only_and_explicit_commit_adopts_once(self):
        before = self.money_counts()
        old = self.client.get(PATH).json()
        self.assertFalse(old["capacity_policy"]["adopted"])
        self.assertEqual(
            old["capacity_policy"]["defaults"]["max_hourly_price_microusd"],
            3_000_000,
        )
        self.assertEqual(old["value"]["max_hourly_price_microusd"], 1_000_000)
        preview = self.preview()
        after_preview = self.client.get(PATH).json()
        self.assertFalse(after_preview["capacity_policy"]["adopted"])
        self.assertEqual(after_preview["version"], old["version"])
        self.assertEqual(after_preview["value"], old["value"])
        with self.app.state.repository.engine.connect() as c:
            self.assertEqual(
                c.scalar(select(func.count()).select_from(spending_settings)), 0
            )
        result = self.commit(preview)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["capacity_policy"]["adopted"])
        self.assertEqual(result.json()["value"]["max_machines"], 10)
        self.assertEqual(
            result.json()["value"]["tenant_daily_limit_microusd"], 100_000_000
        )
        self.assertEqual(self.commit(preview).status_code, 409)
        self.assertEqual(before, self.money_counts())
        with self.app.state.repository.engine.connect() as c:
            audit = (
                c.execute(
                    select(outbox).where(
                        outbox.c.event_type == "spending.settings_updated"
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(audit["payload"]["reason"], "#456 approved settings")
            self.assertEqual(
                audit["payload"]["confirmation_digest"], preview["confirmation_digest"]
            )
        current = self.client.get(PATH).json()
        self.assertEqual(
            current["capacity_policy"]["latest_changes"]["max_machines"]["reason"],
            "#456 approved settings",
        )

    def test_confirmation_required_changed_body_and_reason_refusals(self):
        changes = {"max_machines": 3}
        preview = self.preview(changes)
        missing = self.client.put(
            PATH, json={"expected_version": 0, "changes": changes, "reason": "approve"}
        )
        self.assertEqual(missing.json()["code"], "spending_confirmation_required")
        altered = self.commit(preview, {"max_machines": 4})
        self.assertEqual(altered.json()["code"], "spending_confirmation_changed")
        for reason in (None, "", " ", "x" * 501, True):
            result = self.commit(preview, changes, reason=reason)
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()["details"][0]["field"], "reason")
        self.assertFalse(self.client.get(PATH).json()["capacity_policy"]["adopted"])

    def test_old_alias_and_out_of_range_values_preserved_without_erasing_new_fields(
        self,
    ):
        legacy = {
            **ADMIN_DEFAULTS,
            "max_runtime_seconds": 86400,
            "test_daily_limit_microusd": 17_000_000,
        }
        del legacy["test_task_limit_microusd"]
        with self.app.state.repository.transaction() as c:
            c.execute(
                insert(spending_settings).values(
                    tenant_id="sixnine",
                    owner_id=ADMIN_OWNER,
                    version=1,
                    value=legacy,
                    actor_id="old-admin",
                    updated_at=1.0,
                )
            )
        old = self.client.get(PATH).json()
        echo = self.client.put(PATH, json={"expected_version": 1, "value": legacy})
        self.assertEqual(echo.status_code, 200)
        self.assertFalse(echo.json()["changed"])
        preview = self.preview({"fleet_hourly_limit_microusd": 3_000_000})
        result = self.commit(preview, {"fleet_hourly_limit_microusd": 3_000_000})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["value"]["max_runtime_seconds"], 86400)
        updated = self.client.get(PATH).json()
        old_client = {key: updated["value"][key] for key in ADMIN_DEFAULTS}
        old_client["idle_shutdown_seconds"] = 700
        result = self.client.put(
            PATH,
            json={
                "expected_version": updated["version"],
                "value": old_client,
                "reason": "legacy idle preference",
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            result.json()["value"]["tenant_daily_limit_microusd"], 100_000_000
        )
        self.assertEqual(
            old["value"]["test_task_limit_microusd"],
            result.json()["value"]["test_task_limit_microusd"],
        )

    def test_profile_change_invalidates_admin_preview(self):
        self.app.state.dstack_operator = SimpleNamespace(
            policy={},
            profiles={
                "ref": {
                    "id": "ref",
                    "owner_id": "superdan",
                    "mode": "ref",
                    "max_price_microusd": 850000,
                }
            },
        )
        preview = self.preview()
        self.app.state.dstack_operator.profiles["ref"]["max_price_microusd"] = 900000
        result = self.commit(preview)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["code"], "dstack_price_policy_conflict")

    def test_nonadmin_and_machine_cannot_preview_or_write(self):
        self.login("supervan")
        for path in (PATH, PATH + "/preview"):
            request = self.client.put if path == PATH else self.client.post
            self.assertEqual(
                request(path, json={"expected_version": 0, "changes": {}}).status_code,
                403,
            )
        auth = self.app.state.auth
        token = auth.create_key(
            "supervan",
            authenticated_session=auth.session(
                self.client.cookies.get("sixnine_session")
            ),
            name="limited",
            scopes=["jobs:read"],
            project_ids=[],
            all_projects=True,
            expires_in_days=1,
        )
        with TestClient(
            self.app, headers={"Authorization": "Bearer " + token["api_key"]}
        ) as machine:
            self.assertEqual(
                machine.post(
                    PATH + "/preview", json={"expected_version": 0, "changes": {}}
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()

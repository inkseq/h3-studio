"""Optional wait targets preserve old authority; audited fair toggle controls order."""

import copy
import unittest

from sqlalchemy import func, select, update

from studio_platform import capacity_settings as capacity
from studio_platform.repository import attempts, request_hash, spending_settings
import test_capacity_settings as fixtures
import test_queue_fairness as queue_fixtures


class WaitPolicyTests(unittest.TestCase):
    def old(self):
        return {
            "version": 4,
            "value": {
                key: copy.deepcopy(value)
                for key, value in capacity.DEFAULTS.items()
                if key not in capacity.OPTIONAL_DEFAULTS
            },
        }

    def test_old_snapshot_and_unrelated_save_keep_original_bytes(self):
        old = self.old()
        before = copy.deepcopy(old)
        snapshot = capacity.admission_snapshot(old)
        digest = request_hash(snapshot)
        for changes in ({}, {"idle_shutdown_seconds": 601}):
            preview = capacity.preview_change(old, changes, {})
            self.assertTrue(
                set(capacity.OPTIONAL_DEFAULTS).isdisjoint(preview["value"])
            )
            self.assertEqual(
                preview["value"]["fleet_hourly_limit_microusd"], 15_000_000
            )
        self.assertEqual(capacity.validate_snapshot(snapshot), old["value"])
        self.assertEqual(request_hash(snapshot), digest)
        self.assertEqual(old, before)

    def test_partial_and_full_opt_in_validate_without_injecting_missing_fields(self):
        for changes in (
            {"fair_share": False},
            {"modal_trigger_seconds": 180},
            capacity.OPTIONAL_DEFAULTS,
        ):
            with self.subTest(changes=changes):
                preview = capacity.preview_change(self.old(), changes, {})
                self.assertTrue(preview["confirmation_required"])
                self.assertEqual(set(preview["changed_fields"]), set(changes))
                snapshot = capacity.admission_snapshot(
                    {"version": 5, "value": preview["value"]}
                )
                self.assertEqual(capacity.validate_snapshot(snapshot), preview["value"])
                self.assertEqual(
                    set(snapshot["value"]) & capacity.OPTIONAL_DEFAULTS.keys(),
                    set(changes),
                )

    def test_invalid_targets_boolean_and_cross_field_are_refused(self):
        for changes in (
            {"fair_share": 1},
            {"fair_share": "true"},
            {"fair_share": None},
            {"first_video_max_seconds": True},
            {"first_video_max_seconds": 29},
            {"next_video_interval_seconds": 3601},
            {"modal_trigger_seconds": 0},
            {"first_video_max_seconds": 120, "modal_trigger_seconds": 180},
            {"unknown_wait": 300},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(capacity.CapacitySettingsError),
            ):
                capacity.preview_change(self.old(), changes, {})
        adopted = self.old()
        adopted["value"].update(capacity.OPTIONAL_DEFAULTS)
        with self.assertRaises(capacity.CapacitySettingsError):
            capacity.preview_change(adopted, {"first_video_max_seconds": 120}, {})

    def test_still_requires_all_base_authority_fields(self):
        snapshot = capacity.admission_snapshot(self.old())
        for change in (lambda v: v.pop("max_machines"), lambda v: v.update(other=1)):
            invalid = copy.deepcopy(snapshot)
            change(invalid["value"])
            with self.assertRaises(ValueError):
                capacity.validate_snapshot(invalid)


class WaitRouteTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CapacityRouteTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_explicit_opt_in_requires_reason_confirmation_and_cas_with_history(self):
        f = self.f
        original = f.commit(f.preview()).json()
        snapshot = capacity.admission_snapshot(original)
        frozen = request_hash(snapshot)
        before_money = f.money_counts()
        changes = dict(capacity.OPTIONAL_DEFAULTS)
        preview = f.preview(changes)
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(
            f.commit(preview, changes, confirmation_digest="").status_code, 409
        )
        self.assertEqual(f.commit(preview, changes, reason="").status_code, 422)
        self.assertEqual(
            f.commit(preview, {**changes, "fair_share": False}).status_code, 409
        )
        self.assertEqual(f.client.get(fixtures.PATH).json()["value"], original["value"])
        saved = f.commit(preview, changes)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(f.commit(preview, changes).status_code, 409)
        stored = f.client.get(fixtures.PATH).json()
        for key, value in changes.items():
            self.assertEqual(stored["value"][key], value)
            self.assertEqual(
                stored["capacity_policy"]["latest_changes"][key]["reason"],
                "#456 approved settings",
            )
        self.assertEqual(request_hash(snapshot), frozen)
        self.assertEqual(capacity.validate_snapshot(snapshot), original["value"])
        self.assertEqual(f.money_counts(), before_money)
        inventory = f.client.get("/v1/admin/settings-inventory").json()
        rows = {row["id"]: row for row in inventory["items"]}
        for key in changes:
            row = rows["capacity." + key]
            self.assertEqual(row["revision"], stored["version"])
            self.assertEqual(row["source"], "database:spending_settings")
            self.assertEqual(row["effective"], key == "fair_share")
            self.assertNotIn("pending:" + key, inventory["unavailable"])


class FairToggleTests(unittest.TestCase):
    def test_toggle_restores_legacy_order_and_read_never_claims(self):
        f = queue_fixtures.FairQueueTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        batch = []
        for _ in range(8):
            batch.append(f.job(cost=0)["id"])
            f.now += 1
        f.now += 901
        newcomer = f.job(scope=f.other, cost=0)["id"]
        for value in (False, True, None):
            with self.subTest(value=value):
                policy = dict(capacity.DEFAULTS)
                if value is None:
                    policy.pop("fair_share")
                else:
                    policy["fair_share"] = value
                with f.repo.transaction() as connection:
                    connection.execute(update(spending_settings).values(value=policy))
                order = f.ordered()
                if value is False:
                    self.assertEqual([j["id"] for j in order], batch + [newcomer])
                else:
                    self.assertIn(newcomer, [j["id"] for j in order[:2]])
                self.assertIsNone(f.cursor())
                with f.repo.engine.connect() as connection:
                    self.assertEqual(
                        connection.scalar(select(func.count()).select_from(attempts)), 0
                    )


if __name__ == "__main__":
    unittest.main()

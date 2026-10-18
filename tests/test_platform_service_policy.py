"""Pure service-policy checks; no provider, database or process execution."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from studio_platform.service_policy import (MAX_CYCLE_SEQUENCE, ServicePolicyError,
    cycle_sequence_allowed, validate_service_config, validate_service_policy)


def service_policy(now=1000, *, end=None, ceiling=6_000_000, owners=None, idle=600, cycles=None):
    return {"version": 1, "mode": "continuing-single-slot", "authorization_id": "test-service-authority",
        "tenant_id": "sixnine", "owner_ids": owners or ["superdan", "supervan"],
        "starts_at": now, "expires_at": end if end is not None else now+30*86400,
        "budget_ceiling_microusd": ceiling, "idle_shutdown_seconds": idle, "max_cycles": cycles}


def service_config(value):
    return SimpleNamespace(service_policy=value, tenant=value["tenant_id"], owner=value["owner_ids"][0],
        allowed_owners=list(value["owner_ids"]), created_at=value["starts_at"], hard_deadline=value["expires_at"],
        authorization_extension_s=0, max_cycles=value["max_cycles"], cycle_id="service",
        capacity_approval_id="approval", scale_policy={"hard_deadline": value["expires_at"],
            "approved_remaining_microusd": value["budget_ceiling_microusd"],
            "idle_before_drain_s": value["idle_shutdown_seconds"], "max_instances": 1,
            "max_physical_gpus": 1, "new_instance_slots": 1, "new_instance_physical_gpus": 1})


class ServicePolicyTests(unittest.TestCase):
    def test_explicit_service_can_outlive_test_window_and_eight_cycles(self):
        value = service_policy(owners=["creator-one", "creator-two"], idle=300)
        config = service_config(value)
        self.assertEqual(validate_service_config(config), value)
        for sequence in (1, 9, 1000, MAX_CYCLE_SEQUENCE):
            self.assertTrue(cycle_sequence_allowed(config, sequence))
        self.assertFalse(cycle_sequence_allowed(config, MAX_CYCLE_SEQUENCE+1))
        self.assertEqual(value["expires_at"], config.hard_deadline)

    def test_explicit_finite_cycle_limit_still_applies(self):
        config = service_config(service_policy(cycles=16))
        self.assertTrue(cycle_sequence_allowed(config, 16))
        self.assertFalse(cycle_sequence_allowed(config, 17))
        for invalid in (0, -1, True, 1.5):
            self.assertFalse(cycle_sequence_allowed(config, invalid))
        config.max_cycles = None
        with self.assertRaises(ServicePolicyError):
            cycle_sequence_allowed(config, 17)

    def test_missing_policy_never_enables_unbounded_legacy_config(self):
        config = SimpleNamespace(max_cycles=8)
        self.assertIsNone(validate_service_config(config))
        self.assertTrue(cycle_sequence_allowed(config, 8))
        self.assertFalse(cycle_sequence_allowed(config, 9))
        config.max_cycles = None
        self.assertFalse(cycle_sequence_allowed(config, 9))

    def test_invalid_or_ambiguous_authority_fails_without_echoing_input(self):
        cases = [{"extra": "untrusted-operator-content"}, {"version": True}, {"mode": "unlimited"},
            {"authorization_id": "untrusted-operator-content/invalid"}, {"owner_ids": []},
            {"owner_ids": ["same", "same"]}, {"starts_at": False}, {"starts_at": float("nan")},
            {"expires_at": float("inf")}, {"expires_at": 1000}, {"budget_ceiling_microusd": -1},
            {"budget_ceiling_microusd": float("inf")}, {"budget_ceiling_microusd": True},
            {"budget_ceiling_microusd": 2**63}, {"max_cycles": 0}, {"max_cycles": False},
            {"idle_shutdown_seconds": 0}, {"idle_shutdown_seconds": float("nan")}]
        for changes in cases:
            with self.subTest(fields=list(changes)):
                with self.assertRaises(ServicePolicyError) as raised:
                    validate_service_policy({**service_policy(), **changes})
                self.assertNotIn("untrusted-operator-content", str(raised.exception))

    def test_policy_never_overrides_conflicting_controller_identity_or_bounds(self):
        for field, replacement in (("tenant", "other"), ("owner", "another"),
                ("allowed_owners", ["superdan"]), ("created_at", 2000),
                ("hard_deadline", 99_999_999), ("authorization_extension_s", 18000), ("max_cycles", 8)):
            with self.subTest(field=field):
                config = service_config(service_policy())
                setattr(config, field, replacement)
                with self.assertRaises(ServicePolicyError):
                    validate_service_config(config)
        for field, replacement in (("approved_remaining_microusd", 8_000_000),
                ("hard_deadline", 99_999_999), ("idle_before_drain_s", 1), ("max_instances", 2),
                ("new_instance_slots", 2), ("max_physical_gpus", True)):
            with self.subTest(scale_field=field):
                config = service_config(service_policy())
                config.scale_policy[field] = replacement
                with self.assertRaises(ServicePolicyError):
                    validate_service_config(config)

    def test_validation_returns_copy_and_does_not_renew_absolute_window(self):
        value = service_policy()
        before = deepcopy(value)
        result = validate_service_policy(value)
        result["owner_ids"].append("later-owner")
        self.assertEqual(value, before)
        self.assertEqual(validate_service_policy(value)["expires_at"], before["expires_at"])


if __name__ == "__main__":
    unittest.main()

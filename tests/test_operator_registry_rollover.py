"""Pure selection checks: no provider, database, budgets, or credentials."""
from dataclasses import asdict,replace
import unittest

from studio_platform.operator_capacity import DeploymentBinding, OperatorError, OperatorRegistry, selection
from studio_platform.repository import Scope,request_hash
from studio_platform.scaler import LaunchSpec


class RegistryRolloverTests(unittest.TestCase):
    def setUp(self):
        self.old = DeploymentBinding(binding_id="old", runtime_profile_id="profile", gpu_type="RTX 5090",
            gpu_count=1, pool="old-pool", configuration_id="old-config", model_id="model",
            recipe_ids=("h3-base-fl2va-v1",), engine_manifest_digest="a"*64,
            launch=LaunchSpec("lium", "old-config", "model"), scope=Scope("tenant", "owner", "project"),
            budget_account_ids=("existing-budget",), hourly_cost_microusd=850000,
            reservation_per_node_microusd=2550000, expires_at=2000000000, enabled=False)
        self.new = replace(self.old, binding_id="new", pool="new-pool", configuration_id="new-config",
            launch=LaunchSpec("lium", "new-config", "model"), enabled=True)
        self.chosen = selection({"runtime_profile_id":"profile", "gpu_type":"RTX 5090", "gpu_count":1,
            "node_count":1, "mode":"fl", "ttl_seconds":3600})

    def test_unique_enabled_successor_wins_but_historical_identity_stays_exact(self):
        fingerprint = self.old.fingerprint
        for bindings in ([self.old, self.new], [self.new, self.old]):
            registry = OperatorRegistry(bindings)
            self.assertIs(registry.resolve(self.chosen), self.new)
            self.assertIs(registry.get("old"), self.old)
            self.assertEqual(registry.get("old").fingerprint, fingerprint)

    def test_multiple_enabled_matches_never_choose_first(self):
        registry = OperatorRegistry([self.old, self.new, replace(self.new, binding_id="another")])
        with self.assertRaisesRegex(OperatorError, "operator_deployment_not_configured"):
            registry.resolve(self.chosen)

    def test_single_disabled_match_retained_for_disabled_preview_reason(self):
        self.assertIs(OperatorRegistry([self.old]).resolve(self.chosen), self.old)
        with self.assertRaisesRegex(OperatorError, "operator_deployment_not_configured"):
            OperatorRegistry([self.old, replace(self.old, binding_id="other-disabled")]).resolve(self.chosen)

    def test_enabled_other_mode_does_not_replace_requested_disabled_recipe(self):
        other = replace(self.new, recipe_ids=("h3-base-ref2va-v1",))
        self.assertIs(OperatorRegistry([other, self.old]).resolve(self.chosen), self.old)

    def test_targon_requires_explicit_registry_qualification_and_exact_provider(self):
        targon=replace(self.new,binding_id="targon",launch=replace(self.new.launch,provider="targon"))
        chosen={**self.chosen,"provider":"targon"}
        with self.assertRaisesRegex(OperatorError,"provider_start_unqualified"):
            OperatorRegistry([targon]).resolve(chosen)
        registry=OperatorRegistry([self.new,targon],qualified_providers=("lium","targon"))
        self.assertIs(registry.resolve(chosen),targon)
        self.assertIs(registry.resolve(self.chosen),self.new)
        legacy=asdict(self.old);legacy.pop("enabled")
        self.assertEqual(self.old.fingerprint,request_hash(legacy))
        self.assertNotIn("provider",asdict(self.old))
        self.assertNotIn("provider",self.chosen)


if __name__ == "__main__":
    unittest.main()

"""Real native offer selectors, immutable mapping and original ledger guards."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from sqlalchemy import select, update

import test_platform_repository as ledger
import test_dstack_operator as fixtures
from studio_platform.dstack_capacity import DstackClient, DstackError, _owns
from studio_platform.host_scorecard import OperatorHosts
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import budget_accounts


class HostSelectionTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.hosts = OperatorHosts(self.repo, self.settings)

    def preview(self):
        return self.service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})

    def start(self, preview, key="original-start"):
        return self.service.start(self.owner, {"preview_id": preview["preview_id"]}, key)

    def deny(self):
        row = self.hosts.list(self.owner, admin=True, machine_id="19493")["hosts"][0]["policy"]
        self.hosts.policy(self.owner, {"provider": "vastai", "machine_id": "19493", "policy": "deny",
            "reason_code": "operator_host_denied", "expected_revision": row["revision"],
            "expected_snapshot": row["snapshot"]}, "deny-bad")

    def test_native_selector_is_frozen_reconstructed_and_replay_first(self):
        preview = self.preview()
        self.assertEqual(preview["offers"][0]["offer_id"], "12345")
        self.assertEqual(preview["offers"][0]["host_history"][0]["machine_id"], "19493")
        result = self.start(preview)
        binding = self.store.load(result["node_id"])
        self.assertEqual(binding["run_spec"]["profile"], {"instance_types": ["12345"]})
        self.assertEqual(binding["host_selection"]["offers"], [{"offer_id": "12345", "machine_id": "19493"}])
        self.deny()
        self.now += 121
        self.assertEqual(self.start(preview), result)
        self.assertEqual(len(self.client.applied), 1)
        run = self.client.runs[binding["run_name"]]
        run["run_spec"]["configuration"]["instance_types"] = ["bad"]
        with self.assertRaises(DstackError):
            _owns(run, binding, self.client.project)

    def test_manual_deny_or_mapping_drift_refuses_before_reservation(self):
        preview = self.preview()
        self.deny()
        with self.assertRaises(ValueError):
            self.start(preview)
        self.assertEqual(self.client.applied, [])
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(budget_accounts.c.reserved_microusd)).scalar_one(), 0)

    def test_fresh_mapping_and_unknown_reuse_are_both_required(self):
        preview = self.preview()
        self.client.vast_offer_mapping = lambda *a, **kw: {"observed_at": self.now,
            "offers": [{"offer_id": "12345", "machine_id": "other"}]}
        with self.assertRaises(ValueError):
            self.start(preview)
        self.assertEqual(self.client.applied, [])
        self.client.vast_offer_mapping = lambda *a, **kw: {"observed_at": self.now,
            "offers": [{"offer_id": "12345", "machine_id": "19493"}]}
        self.client.reusable_hosts_confirmed = Mock(side_effect=DstackError("dstack_host_reuse_identity_unknown"))
        with self.assertRaises(DstackError):
            self.start(preview)
        self.assertEqual(self.client.applied, [])

    def test_positive_allocation_is_saved_before_running_and_unknown_never_bootstraps(self):
        result = self.start(self.preview())
        binding = self.store.load(result["node_id"])
        run = self.client.runs[binding["run_name"]]
        run.update(status="provisioning", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "1234567", "instance_type": {"resources": {
                "gpus": [{"name": "RTX5090"}]}}}})
        value = self.capacity.observe(result["node_id"])
        self.assertFalse(value["ready"])
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(operator_nodes).where(operator_nodes.c.intent_id == result["node_id"])).mappings().one()
        self.assertEqual(row["payload"]["host_observation"]["allocated_ram_mib"], 96*1024)
        self.assertEqual(row["payload"]["host_observation"]["machine_id"], "19493")
        # Once bound, a different type/physical allocation is not bootstrap
        # permission; original provider/budget history remains untouched.
        run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["name"] = "99999"
        run["status"] = "running"
        self.capacity.readiness = Mock()
        self.capacity.observe(result["node_id"])
        self.capacity.readiness.assert_not_called()

    def test_prefer_changes_native_selector_not_only_display_order(self):
        native_plan = self.client.plan
        def two(spec):
            value = native_plan(spec)
            second = copy.deepcopy(value["job_plans"][0]["offers"][0])
            second["instance"]["name"] = "54321"
            value["job_plans"][0]["offers"].append(second)
            return value
        self.client.plan = two
        self.client.vast_offer_mapping = lambda *a, **kw: {"observed_at": self.now, "offers": [
            {"offer_id": "12345", "machine_id": "19493"}, {"offer_id": "54321", "machine_id": "19494"}]}
        policy = self.hosts.list(self.owner, admin=True, machine_id="19494")["hosts"][0]["policy"]
        self.hosts.policy(self.owner, {"provider": "vastai", "machine_id": "19494", "policy": "prefer",
            "reason_code": "operator_host_preferred", "expected_revision": policy["revision"],
            "expected_snapshot": policy["snapshot"]}, "prefer-host")
        result = self.start(self.preview())
        self.assertEqual(self.store.load(result["node_id"])["run_spec"]["profile"]["instance_types"], ["54321"])

    def test_policy_changed_before_controller_apply_preserves_accepted_intent(self):
        with patch.object(self.capacity, "start", return_value={"state": "starting"}):
            result = self.start(self.preview())
        self.deny()
        from studio_platform.dstack_operator import _request
        with self.repo.engine.connect() as conn:
            payload = conn.execute(select(operator_nodes.c.payload).where(operator_nodes.c.intent_id == result["node_id"])).scalar_one()
        with self.assertRaises(ValueError):
            self.capacity.start(_request(payload["request"]), fixtures.PUB)
        self.assertEqual(self.client.applied, [])
        with self.repo.engine.connect() as conn:
            self.assertGreater(conn.execute(select(budget_accounts.c.reserved_microusd)).scalar_one(), 0)
        self.assertFalse(self.store.load(result["node_id"]).get("apply_started", False))

    def test_blocking_reuse_read_cannot_outlive_selection_before_reserve_or_apply(self):
        preview = self.preview()
        def delayed(_):
            self.now += 121
            return True
        self.client.reusable_hosts_confirmed = delayed
        with self.assertRaises(ValueError):
            self.start(preview)
        self.assertEqual(self.client.applied, [])
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(budget_accounts.c.reserved_microusd)).scalar_one(), 0)
        # A separately accepted original intent also keeps its reservation
        # when the controller's final read crosses the immutable expiry.
        self.client.reusable_hosts_confirmed = lambda _: True
        with patch.object(self.capacity, "start", return_value={"state": "starting"}):
            result = self.start(self.preview(), "second-original")
        self.client.reusable_hosts_confirmed = delayed
        from studio_platform.dstack_operator import _request
        with self.repo.engine.connect() as conn:
            payload = conn.execute(select(operator_nodes.c.payload).where(operator_nodes.c.intent_id == result["node_id"])).scalar_one()
        with self.assertRaises(ValueError):
            self.capacity.start(_request(payload["request"]), fixtures.PUB)
        self.assertEqual(self.client.applied, [])
        self.assertFalse(self.store.load(result["node_id"]).get("apply_started", False))


class NativeHostTransportTests(unittest.TestCase):
    def client(self, post):
        value = object.__new__(DstackClient)
        value.project = "sixnine"
        value._post = post
        return value

    def test_complete_imported_fleet_unknown_type_refuses(self):
        post = Mock(return_value=[{"instances": [{"status": "provisioning", "backend": "vastai", "instance_type": None}]}])
        value = self.client(post)
        with self.assertRaises(DstackError):
            value.reusable_hosts_confirmed({"12345": "19493"})
        self.assertEqual(post.call_args.args, ("/api/project/sixnine/fleets/list", {"include_imported": True}))
        post.return_value = [{"instances": [{"status": "idle", "backend": "vastai", "instance_type": {"name": "12345"}},
            {"status": "terminated", "backend": None, "instance_type": None}]}]
        self.assertTrue(value.reusable_hosts_confirmed({"12345": "19493"}))

    def test_raw_search_preserves_pinned_body_and_unknown_identity(self):
        value = self.client(Mock())
        value.timeout = 25
        value._vast_key = lambda: "synthetic-never-sent"
        value._provider_document = Mock(return_value={"offers": [{"id": 12345, "machine_id": 19493, "verified": "verified"},
            {"id": 9999, "machine_id": None}, {"id": 7777, "machine_id": 19492, "verified": False}]})
        import test_dstack_capacity as cap
        mapping = value.vast_offer_mapping(cap.request(), clock=lambda: 1000)
        self.assertEqual(mapping["offers"], [{"offer_id": "12345", "machine_id": "19493"}])
        call = value._provider_document.call_args
        self.assertNotIn("q", call.kwargs["body"])
        self.assertEqual(call.kwargs["body"]["verified"], {"eq": True})

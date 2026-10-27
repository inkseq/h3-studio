"""Original reservation settlement from complete native non-allocation proof."""

import copy
import json
from unittest.mock import patch

from sqlalchemy import select

from studio_platform.dstack_allocation_proof import (
    PREFIX,
    NO_FLEET_MESSAGE,
    NO_FLEET_SOURCE,
    from_terminal_run,
    validate_proof,
)
from studio_platform.dstack_capacity import terminal_no_allocation
from studio_platform.repository import (
    budget_reservations,
    instance_intents,
    scaler_receipts,
)
from test_platform_repository import LedgerCase
import test_dstack_capacity as native_fixture
import test_dstack_operator as fixture


def marker(
    *, code="rate_limited", status=429, outcome="rejected", contract=None, ask="12345"
):
    return PREFIX + json.dumps(
        {
            "version": 1,
            "outcome": "no_allocation" if outcome == "rejected" else "unknown",
            "attempted_count": 1,
            "attempts": [
                {
                    "ask_id": ask,
                    "http_status": status,
                    "code": code,
                    "outcome": outcome,
                    "contract_id": contract,
                }
            ],
        }
    )


class AllocationProofTests(LedgerCase):
    preview = fixture.DstackOperatorTests.preview
    start = fixture.DstackOperatorTests.start

    def setUp(self):
        super().setUp()
        fixture.DstackOperatorTests._setup_dstack(self)

    def failed(self, message, *, key="proof-start"):
        node = self.start(key=key)["node_id"]
        bound = self.store.load(node)
        run = native_fixture.no_capacity_run(self.client.runs[bound["run_name"]])
        run["latest_job_submission"]["termination_reason_message"] = message
        run["jobs"][0]["job_submissions"][0]["termination_reason_message"] = message
        self.client.runs[bound["run_name"]] = run
        return node, bound, run

    def reservation(self, node):
        with self.repo.engine.connect() as c:
            return dict(
                c.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == "instance",
                        budget_reservations.c.reference_id == node,
                    )
                )
                .mappings()
                .one()
            )

    def test_complete_typed_rejection_releases_only_original_reservation_once(self):
        unrelated = self.job("unrelated-job")
        node, _, _ = self.failed(marker())
        original = self.reservation(node)
        version = self.service.state(self.owner)["nodes"][0]["version"]
        result = self.capacity.observe(node)
        self.assertEqual(result["reason_code"], "dstack_never_allocated")
        self.assertEqual(
            self.service.state(self.owner)["nodes"][0]["version"], version + 1
        )
        settled = self.reservation(node)
        self.assertEqual(
            (settled["id"], settled["state"], settled["actual_cost_microusd"]),
            (original["id"], "released", 0),
        )
        self.assertEqual(settled["amount_microusd"], original["amount_microusd"])
        with self.repo.engine.connect() as c:
            self.assertEqual(
                c.execute(
                    select(instance_intents.c.state).where(
                        instance_intents.c.id == node
                    )
                ).scalar_one(),
                "destroyed",
            )
            self.assertEqual(
                c.execute(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_id == unrelated["id"]
                    )
                ).scalar_one(),
                "reserved",
            )
            receipts = list(
                c.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.intent_id == node,
                        scaler_receipts.c.operation == "no_allocation",
                    )
                ).mappings()
            )
            self.assertEqual(len(receipts), 1)
        before = self.repo.get_budget("owner-budget")
        self.capacity.observe(node)
        self.assertEqual(self.reservation(node), settled)
        self.assertEqual(
            self.service.state(self.owner)["nodes"][0]["version"], version + 1
        )
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.repo.engine.connect() as c:
            self.assertEqual(
                len(
                    list(
                        c.execute(
                            select(scaler_receipts).where(
                                scaler_receipts.c.intent_id == node,
                                scaler_receipts.c.operation == "no_allocation",
                            )
                        )
                    )
                ),
                1,
            )
        self.assertEqual(len(self.client.applied), 1)

    def test_unknown_or_incomplete_marker_keeps_money_and_capacity_obligation(self):
        for index, message in enumerate(
            (
                marker(outcome="unknown", code="mutation_unknown", status=None),
                marker(
                    outcome="unknown",
                    code="contract_returned_with_error",
                    status=500,
                    contract="778899",
                ),
                marker()[:-1],
                marker(ask="999999"),
            )
        ):
            with self.subTest(index=index):
                # Isolate each original obligation; it must keep occupying a slot.
                self.service.policy.update(
                    max_instances=10,
                    max_physical_gpus=10,
                    max_hourly_cost_microusd=10_000_000,
                )
                self.repo.configure_pool(
                    "dstack-test", max_instances=10, max_physical_gpus=10
                )
                node, bound, run = self.failed(message, key="bad-proof-" + str(index))
                before = self.reservation(node)
                self.assertFalse(terminal_no_allocation(run, bound))
                result = self.capacity.observe(node)
                self.assertEqual(result["state"], "creation_unknown")
                self.assertEqual(
                    result["reason_code"], "dstack_allocation_requires_reconciliation"
                )
                self.assertEqual(self.reservation(node), before)

    def test_legacy_summary_still_never_auto_refunds(self):
        node, bound, run = self.failed("tried 2 of 2 offers, all failed: 404 / 429")
        before = self.reservation(node)
        self.assertTrue(terminal_no_allocation(run, bound))
        self.assertIsNone(from_terminal_run(run, bound))
        self.capacity.observe(node)
        self.assertEqual(self.reservation(node), before)

    def test_exact_preallocation_no_fleet_releases_original_once_and_unblocks_replacement(
        self,
    ):
        from studio_platform.dstack_demand_start import _ended_for_replacement
        from studio_platform.operator_capacity import operator_nodes

        unrelated = self.job("keep-job")
        node, bound, run = self.failed(NO_FLEET_MESSAGE)
        original = self.reservation(node)
        proof = from_terminal_run(run, bound)
        self.assertEqual(proof["source"], NO_FLEET_SOURCE)
        self.assertEqual((proof["attempted_count"], proof["attempts"]), (0, []))
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node)
                )
                .mappings()
                .one()
            )
            self.assertFalse(_ended_for_replacement(conn, row))
        self.capacity.observe(node)
        settled = self.reservation(node)
        self.assertEqual(
            (settled["id"], settled["amount_microusd"]),
            (original["id"], original["amount_microusd"]),
        )
        self.assertEqual(
            (settled["state"], settled["actual_cost_microusd"]), ("released", 0)
        )
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node)
                )
                .mappings()
                .one()
            )
            self.assertTrue(_ended_for_replacement(conn, row))
            self.assertEqual(
                conn.scalar(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_id == unrelated["id"]
                    )
                ),
                "reserved",
            )
        self.capacity.observe(node)
        self.assertEqual(self.reservation(node), settled)
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as conn:
            receipts = list(
                conn.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.intent_id == node,
                        scaler_receipts.c.operation == "no_allocation",
                    )
                )
            )
        self.assertEqual(len(receipts), 1)

    def test_no_fleet_requires_exact_message_and_complete_original_history(self):
        _, bound, run = self.failed(NO_FLEET_MESSAGE)
        for message in (
            "No matching fleet found",
            NO_FLEET_MESSAGE + " ",
            "provider error: " + NO_FLEET_MESSAGE,
            "Failed to use specified fleets",
            "No offers available",
        ):
            changed = copy.deepcopy(run)
            changed["latest_job_submission"]["termination_reason_message"] = message
            self.assertIsNone(from_terminal_run(changed, bound))
        for change in (
            "retry",
            "missing",
            "provisioned",
            "runtime",
            "not_terminal",
            "different_latest",
        ):
            changed = copy.deepcopy(run)
            if change == "retry":
                changed["jobs"][0]["job_submissions"].append(
                    copy.deepcopy(changed["latest_job_submission"])
                )
            elif change == "missing":
                del changed["latest_job_submission"]["job_provisioning_data"]
            elif change == "provisioned":
                changed["latest_job_submission"]["job_provisioning_data"] = {
                    "instance_id": "123"
                }
            elif change == "runtime":
                changed["latest_job_submission"]["job_runtime_data"] = {}
            elif change == "not_terminal":
                changed["status"] = "running"
            else:
                changed["jobs"][0]["job_submissions"] = []
            with self.subTest(change=change):
                self.assertIsNone(from_terminal_run(changed, bound))
        for field, value in (
            ("run_id", "foreign"),
            ("provider_instance_id", "123"),
            ("bootstrap_started", True),
            ("runtime_incarnation", "x"),
            ("first_runtime_ready_at", 1),
            ("backend", "runpod"),
        ):
            self.assertIsNone(from_terminal_run(run, {**bound, field: value}))
        proof = from_terminal_run(run, bound)
        for field, value in (
            ("attempted_count", False),
            ("attempted_count", 1),
            ("attempts", [{}]),
            ("evidence_sha256", "0" * 64),
        ):
            self.assertIsNone(validate_proof({**proof, field: value}, bound))

    def test_no_fleet_settlement_failure_rolls_back_and_reconciles_original(self):
        node, _, _ = self.failed(NO_FLEET_MESSAGE)
        before = self.reservation(node)
        original = self.repo._settle

        def lost_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("fixture_interrupted_commit")

        with patch.object(self.repo, "_settle", side_effect=lost_commit):
            with self.assertRaisesRegex(RuntimeError, "fixture_interrupted_commit"):
                self.capacity.observe(node)
        self.assertEqual(self.reservation(node), before)
        self.capacity.observe(node)
        self.assertEqual(self.reservation(node)["state"], "released")

    def test_documented_rejections_release_only_the_original_hold(self):
        for status, code in ((400, "invalid_args"), (410, "no_such_ask")):
            with self.subTest(status=status):
                node, bound, run = self.failed(
                    marker(code=code, status=status), key="documented-" + str(status)
                )
                original = self.reservation(node)
                self.assertTrue(terminal_no_allocation(run, bound))
                self.capacity.observe(node)
                result = self.reservation(node)
                self.assertEqual(
                    (result["id"], result["amount_microusd"]),
                    (original["id"], original["amount_microusd"]),
                )
                self.assertEqual(
                    (result["state"], result["actual_cost_microusd"]), ("released", 0)
                )

    def test_changed_history_foreign_offer_and_extra_fields_never_prove_zero(self):
        _, bound, run = self.failed(marker())
        self.assertIsNotNone(from_terminal_run(run, bound))
        for field, value in (
            ("provider_instance_id", "778899"),
            ("runtime_incarnation", "a" * 32),
            ("first_runtime_ready_at", 999.0),
            ("bootstrap_started", True),
            ("run_id", "not-original"),
        ):
            with self.subTest(field=field):
                changed = {**bound, field: value}
                self.assertIsNone(from_terminal_run(run, changed))
        for change in ("count", "extra", "duplicate"):
            modified = copy.deepcopy(run)
            doc = json.loads(marker()[len(PREFIX) :])
            if change == "count":
                doc["attempted_count"] = 2
            elif change == "extra":
                doc["private"] = "must not be accepted"
            else:
                doc["attempts"].append(copy.deepcopy(doc["attempts"][0]))
                doc["attempted_count"] = 2
            modified["latest_job_submission"]["termination_reason_message"] = (
                PREFIX + json.dumps(doc)
            )
            self.assertIsNone(from_terminal_run(modified, bound))

    def test_budget_write_failure_rolls_back_original_observation_and_settlement(self):
        node, _, _ = self.failed(marker(code="no_such_ask", status=404))
        before, balance, binding = (
            self.reservation(node),
            self.repo.get_budget("owner-budget"),
            self.store.load(node),
        )
        original = self.repo._settle

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("test-interrupted-commit")

        with (
            patch.object(self.repo, "_settle", side_effect=interrupted),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
        ):
            self.capacity.observe(node)
        self.assertEqual(self.reservation(node), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), balance)
        self.assertEqual(self.store.load(node), binding)
        self.capacity.observe(node)
        self.assertEqual(self.reservation(node)["state"], "released")

    def test_explicit_owner_review_settles_old_start_after_fresh_complete_account_check(
        self,
    ):
        node, _, _ = self.failed(
            "tried 2 of 2 offers, all failed: explicit rejected starts"
        )
        self.capacity.observe(node)
        self.client.provider_inventory = lambda backend: {
            "backend": backend,
            "complete": True,
            "observed_at": self.now,
            "instance_ids": [],
        }
        version = self.service.state(self.owner)["nodes"][0]["version"]
        reviewed = self.capacity.review_unallocated_start(
            node,
            reviewed_by="owner",
            source="https://github.com/inkseq/h3-studio/issues/193",
        )
        self.assertFalse(reviewed["already_reviewed"])
        self.assertEqual(self.reservation(node)["state"], "released")
        balance = self.repo.get_budget("owner-budget")
        self.assertTrue(
            self.capacity.review_unallocated_start(
                node,
                reviewed_by="owner",
                source="https://github.com/inkseq/h3-studio/issues/193",
            )["already_reviewed"]
        )
        self.assertEqual(self.repo.get_budget("owner-budget"), balance)
        self.capacity.observe(node)
        self.assertEqual(self.store.load(node)["billing_state"], "no_charge_confirmed")
        self.assertEqual(
            self.service.state(self.owner)["nodes"][0]["version"], version + 1
        )

    def test_owner_review_refuses_live_partial_stale_or_unknown_account_results(self):
        node, _, _ = self.failed("legacy explicit owner-confirmed no-allocation review")
        self.capacity.observe(node)
        for inventory in (
            {
                "backend": "vastai",
                "complete": True,
                "observed_at": self.now,
                "instance_ids": ["778899"],
            },
            {
                "backend": "vastai",
                "complete": False,
                "observed_at": self.now,
                "instance_ids": [],
            },
            {
                "backend": "vastai",
                "complete": True,
                "observed_at": self.now - 31,
                "instance_ids": [],
            },
            {
                "backend": "runpod",
                "complete": True,
                "observed_at": self.now,
                "instance_ids": [],
            },
        ):
            with self.subTest(inventory=inventory):
                self.client.provider_inventory = lambda backend: inventory
                before = self.reservation(node)
                with self.assertRaisesRegex(
                    Exception, "dstack_no_allocation_review_unconfirmed"
                ):
                    self.capacity.review_unallocated_start(
                        node,
                        reviewed_by="owner",
                        source="https://github.com/inkseq/h3-studio/issues/193",
                    )
                self.assertEqual(self.reservation(node), before)

    def test_settled_no_allocation_history_refuses_contradictory_identity_or_unknown_outcome(
        self,
    ):
        node, _, _ = self.failed(marker())
        self.capacity.observe(node)
        binding, original = self.store.load(node), self.reservation(node)
        with self.repo.engine.connect() as c:
            intent = dict(
                c.execute(select(instance_intents).where(instance_intents.c.id == node))
                .mappings()
                .one()
            )
        for extra in (
            {"provider_instance_id": "778899"},
            {"runtime_incarnation": "a" * 32},
            {
                "state": "creation_unknown",
                "reason_code": "dstack_allocation_requires_reconciliation",
            },
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(
                    Exception, "dstack_no_allocation_history_conflict"
                ),
            ):
                self.store.record(
                    node,
                    {
                        "run_id": binding["run_id"],
                        "state": "stopped",
                        "dstack_status": "failed",
                        "ready": False,
                        "billing_state": "unsettled",
                        "observed_at": self.now,
                        **extra,
                    },
                )
            self.assertEqual(self.store.load(node), binding)
            self.assertEqual(self.reservation(node), original)
            with self.repo.engine.connect() as c:
                self.assertEqual(
                    dict(
                        c.execute(
                            select(instance_intents).where(
                                instance_intents.c.id == node
                            )
                        )
                        .mappings()
                        .one()
                    ),
                    intent,
                )

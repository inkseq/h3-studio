"""Opt-in lookahead uses original evidence and rechecks before reservation."""

import copy
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.capacity_lookahead_runtime import (
    enabled,
    enforce,
    evaluate,
    record_decision,
)
from studio_platform.capacity_start_rules import StartRules
from studio_platform.dstack_workspace import DstackWorkspace
from studio_platform.operator_capacity import OperatorError
from studio_platform.repository import outbox, registered_workers
from test_platform_repository import LedgerCase
import test_dstack_operator as fixtures


class LookaheadRuntimeTests(LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.service.policy["demand_start"] = {
            "profile_ids": ["5090-fl"],
            "lookahead_version": 1,
        }
        self.profile = self.service.profiles["5090-fl"]
        self.job = {
            "id": "job-1",
            "pool": self.profile["pool"],
            "tenant_id": self.scope.tenant_id,
            "owner_id": self.owner.owner,
            "execution_plan": {
                "enabled": True,
                "backend": "wangp-worker",
                "dispatch_backend": "hatchet-v1",
                "pool": self.profile["pool"],
                "configuration_id": self.profile["configuration_id"],
                "model_id": self.profile["model_id"],
                "engine_manifest_digest": self.profile["manifest_digest"],
                "output_delivery": "video",
            },
            "request": {
                "deployment_profile_id": self.profile["runtime_profile_id"],
                "request": {"model": self.profile["model_id"], "mode": "fl"},
            },
        }
        self.node = {
            "intent_id": "00000000-0000-4000-8000-000000000001",
            "pool": self.profile["pool"],
            "provider_instance_id": "12345",
            "desired_state": "running",
            "payload": {
                "owner_id": self.owner.owner,
                "tenant_id": self.scope.tenant_id,
                "dstack": {
                    "profile_id": self.profile["runtime_profile_id"],
                    "mode": "fl",
                    "model_id": self.profile["model_id"],
                    "configuration_id": self.profile["configuration_id"],
                    "manifest_digest": self.profile["manifest_digest"],
                    "backend": "vastai",
                },
            },
        }
        self.worker_id = "dstack-" + self.node["intent_id"].replace("-", "")
        self.usage = {
            "observations": [
                {
                    "intent_id": self.node["intent_id"],
                    "counted": True,
                    "reason_code": "dstack_capacity_active",
                }
            ]
        }

    def worker(self, **overrides):
        value = {
            "id": self.worker_id,
            "pool": self.profile["pool"],
            "provider": "vast",
            "instance_id": "12345",
            "spec": {
                "pool": self.profile["pool"],
                "backend": "wangp-worker",
                "dispatch_backend": "hatchet-v1",
                "model_id": self.profile["model_id"],
                "configuration_id": self.profile["configuration_id"],
                "engine_manifest_digest": self.profile["manifest_digest"],
                "output_delivery": "video",
                "recipe_ids": [],
            },
            "spec_hash": "a" * 64,
            "state": "ready",
            "current_job_id": None,
            "drain_requested": 0,
            "fence": 1,
            "expires_at": self.now + 60,
            "updated_at": self.now,
            **overrides,
        }
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(**value))

    def work(self, count):
        return [
            ({**self.job, "id": "job-" + str(i + 1)}, self.profile)
            for i in range(count)
        ]

    def calculate(self, count, nodes=None, usage=None):
        with (
            self.repo.engine.connect() as connection,
            patch("studio_platform.dstack_demand._nodes", return_value=nodes or []),
        ):
            return evaluate(
                self.service,
                connection,
                self.job,
                self.profile,
                self.work(count),
                usage or self.usage,
                self.now,
            )

    def test_floor_and_owner_examples(self):
        self.assertEqual(self.calculate(1)["decision"]["start_count"], 1)
        self.worker()
        first = self.calculate(30, [self.node])
        self.assertEqual(first["decision"]["left_at_ready"], 10)
        self.assertEqual(first["decision"]["start_count"], 0)
        second = self.calculate(45, [self.node])
        self.assertEqual(second["decision"]["left_at_ready"], 25)
        self.assertEqual(second["decision"]["start_count"], 1)

    def test_unregistered_or_stale_slots_do_not_supply_ready_throughput_or_duplicate_floor(
        self,
    ):
        first = self.calculate(1, [self.node])
        self.assertEqual(first["decision"]["start_count"], 0)
        self.assertEqual(first["machines"][0]["state"], "starting")
        self.worker(expires_at=self.now - 1)
        stale = self.calculate(30, [self.node])
        self.assertIsNone(stale["machines"][0]["ready_in_seconds"])
        self.assertEqual(stale["decision"]["left_at_ready"], 30)

    def test_other_owner_pool_configuration_and_provider_never_supply_capacity(self):
        self.worker(provider="another-provider")
        self.assertEqual(
            self.calculate(30, [self.node])["machines"][0]["state"], "starting"
        )
        for change in ("owner", "pool", "configuration"):
            node = copy.deepcopy(self.node)
            if change == "owner":
                node["payload"]["owner_id"] = "other"
            elif change == "pool":
                node["pool"] = "other"
            else:
                node["payload"]["dstack"]["configuration_id"] = "other"
            self.assertEqual(self.calculate(1, [node])["decision"]["reason"], "floor")

    def test_unknown_duplicate_or_stopping_capacity_cannot_authorize_starts(self):
        unknown = {
            "observations": [
                {
                    **self.usage["observations"][0],
                    "reason_code": "dstack_capacity_unconfirmed",
                }
            ]
        }
        self.assertEqual(
            self.calculate(45, [self.node], unknown)["decision"]["start_count"], 0
        )
        duplicate = {**self.node, "intent_id": "00000000-0000-4000-8000-000000000002"}
        usage = {
            "observations": self.usage["observations"]
            + [{**self.usage["observations"][0], "intent_id": duplicate["intent_id"]}]
        }
        self.assertEqual(
            self.calculate(45, [self.node, duplicate], usage)["decision"][
                "start_count"
            ],
            0,
        )
        self.assertEqual(
            self.calculate(45, [{**self.node, "desired_state": "stopped"}])["decision"][
                "start_count"
            ],
            0,
        )

    def test_final_guard_rechecks_coverage_rules_and_idle_snapshot(self):
        value = {
            "demand_start": {"job_id": self.job["id"], "start_rules_revision": None},
            "lifecycle": {"idle_shutdown_seconds": 600},
        }
        self.worker()
        with (
            self.repo.transaction() as connection,
            patch("studio_platform.dstack_demand._nodes", return_value=[self.node]),
            patch(
                "studio_platform.dstack_demand.eligible_jobs",
                return_value=self.work(30),
            ),
        ):
            with self.assertRaisesRegex(OperatorError, "dstack_lookahead_covered"):
                enforce(
                    self.service, connection, value, self.profile, self.usage, self.now
                )
        with (
            self.repo.transaction() as connection,
            patch("studio_platform.dstack_demand._nodes", return_value=[]),
            patch(
                "studio_platform.dstack_demand.eligible_jobs", return_value=self.work(1)
            ),
        ):
            with self.assertRaisesRegex(OperatorError, "dstack_demand_changed"):
                enforce(
                    self.service,
                    connection,
                    {**value, "lifecycle": {"idle_shutdown_seconds": 1}},
                    self.profile,
                    self.usage,
                    self.now,
                )
            enforce(self.service, connection, value, self.profile, self.usage, self.now)
        with self.repo.engine.connect() as connection:
            audits = (
                connection.execute(
                    select(outbox.c.payload).where(
                        outbox.c.event_type == "capacity.lookahead_decision"
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["phase"], "admission")
        settings = StartRules(DstackWorkspace(self.service))
        body = {
            "profile_id": "5090-fl",
            "expected_revision": None,
            "value": {"job_seconds": 40, "boot_seconds": 600, "minimum_jobs": 20},
        }
        preview = settings.preview(self.owner, body)
        settings.write(
            self.owner,
            {
                **body,
                "confirmation_digest": preview["confirmation_digest"],
                "reason": "staging",
            },
            "new-policy",
        )
        with (
            self.repo.transaction() as connection,
            patch("studio_platform.dstack_demand._nodes", return_value=[]),
            patch(
                "studio_platform.dstack_demand.eligible_jobs", return_value=self.work(1)
            ),
        ):
            with self.assertRaisesRegex(OperatorError, "dstack_demand_changed"):
                enforce(
                    self.service, connection, value, self.profile, self.usage, self.now
                )

    def test_rollout_is_explicit_and_manual_path_unchanged(self):
        for version in (None, False, True, 0, "1"):
            self.service.policy["demand_start"]["lookahead_version"] = version
            self.assertFalse(enabled(self.service))
            with (
                self.repo.engine.connect() as connection,
                patch(
                    "studio_platform.capacity_lookahead_runtime.evaluate",
                    side_effect=AssertionError("not active"),
                ),
            ):
                enforce(
                    self.service,
                    connection,
                    {"demand_start": {"job_id": "missing"}},
                    self.profile,
                    {},
                    self.now,
                )
        self.service.policy["demand_start"]["lookahead_version"] = 1
        with (
            self.repo.engine.connect() as connection,
            patch(
                "studio_platform.capacity_lookahead_runtime.evaluate",
                side_effect=AssertionError("manual"),
            ),
        ):
            enforce(self.service, connection, {}, self.profile, {}, self.now)

    def test_identical_planning_snapshot_does_not_duplicate_audit(self):
        snapshot = self.calculate(1)
        with self.repo.transaction() as connection:
            record_decision(
                connection, self.scope.tenant_id, self.job["id"], snapshot, self.now
            )
            record_decision(
                connection,
                self.scope.tenant_id,
                self.job["id"],
                snapshot,
                self.now + 1,
                prior=snapshot,
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                len(
                    connection.execute(
                        select(outbox).where(
                            outbox.c.event_type == "capacity.lookahead_decision"
                        )
                    ).all()
                ),
                1,
            )

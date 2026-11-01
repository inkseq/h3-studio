"""Hosted offload decision and rebind (#931 step 3b); no vendor or GPU calls."""

import copy
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.providers.hosted_models import (
    BACKEND,
    MODELS,
    POOL,
    PROVIDER,
    RECIPES,
)
from studio_platform.repository import jobs
from studio_platform.scheduling.decide import hosted_offload
from studio_platform.scheduling.rebind import hosted_plan, offload, rebind
from test_platform_api import generation_request
from test_platform_repository import LedgerCase

TURBO = MODELS["h3-max-turbo"]


def job(identity, created=1000, owner="a", tier="standard"):
    return (
        {"id": identity, "created_at": created, "owner_id": owner, "tier": tier},
        {},
    )


def tier_of(row):
    return row["tier"]


class OffloadDecisionTests(unittest.TestCase):
    def ids(self, queued, tiers=("standard", "fast", "refine"), used=None, now=1100):
        chosen = hosted_offload(queued, tier_of, set(tiers), used or {}, now)
        return [row["id"] for row, _ in chosen]

    def test_only_jobs_past_their_tier_wait_limit_go_hosted(self):
        queued = [
            job("fresh", created=1090),
            job("standard-old", created=1030),
            job("fast-old", created=1060, tier="fast"),
            job("refine-young", created=1030, tier="refine"),
        ]
        self.assertEqual(self.ids(queued), ["standard-old", "fast-old"])

    def test_a_tier_that_is_not_switched_on_stays_on_our_machines(self):
        self.assertEqual(self.ids([job("old", created=1)], tiers=("fast",)), [])
        self.assertEqual(self.ids([job("old", created=1)], tiers=()), [])

    def test_vendor_window_and_owner_share_bound_the_result(self):
        queued = [job(str(i), created=1) for i in range(3)]
        self.assertEqual(self.ids(queued, used={"b": 24}), ["0"])
        self.assertEqual(self.ids(queued, used={"a": 9}), ["0"])
        self.assertEqual(self.ids(queued, used={"b": 25}), [])
        mixed = [job("a1", created=1), job("b1", created=2, owner="b")]
        self.assertEqual(self.ids(mixed, used={"a": 10}), ["b1"])


class HostedPlanTests(unittest.TestCase):
    def test_route_fields_go_and_money_fields_stay(self):
        execution = {
            "pool": "own",
            "backend": "wangp-worker",
            "configuration_id": "v9",
            "dispatch_backend": "hatchet-v1",
            "output_delivery": "native-frames-v1",
            "engine_manifest_digest": "d",
            "credit_quote": {"credits": 20},
            "budget_account_ids": ["acct"],
            "capacity_policy_snapshot": {"v": 1},
            "fingerprint": "f",
        }
        plan = hosted_plan(execution, "h3-max-turbo", 5)
        for gone in ("dispatch_backend", "output_delivery", "engine_manifest_digest"):
            self.assertNotIn(gone, plan)
        self.assertEqual(plan["credit_quote"], {"credits": 20})
        self.assertEqual(plan["budget_account_ids"], ["acct"])
        self.assertEqual(
            (plan["pool"], plan["backend"], plan["configuration_id"]),
            (POOL, BACKEND, TURBO.configuration_id),
        )
        self.assertEqual(plan["primary_plan"], execution)


class RebindLedgerTests(LedgerCase):
    def queued_job(self):
        value, _ = compile_request(generation_request(), lambda _: None)
        plan = self.repo.create_plan(
            self.scope,
            copy.deepcopy(value),
            {
                "pool": "own",
                "backend": "wangp-worker",
                "enabled": True,
                "expected_runtime_s": 60,
                "configuration_id": "v9",
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            self.scope, plan["id"], "rebind-key", budget_account_ids=["owner-budget"]
        )

    def row(self, identity):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(select(jobs).where(jobs.c.id == identity))
                .mappings()
                .one()
            )

    def hosted_control(self):
        control = WorkerControl(self.repo)
        worker = "hosted-turbo"
        control.register(
            WorkerSpec(
                worker,
                POOL,
                PROVIDER,
                worker,
                (),
                RECIPES,
                TURBO.id,
                TURBO.configuration_id,
                BACKEND,
            )
        )
        control.mark_ready(worker, upstream_idle_confirmed=True)
        return control, worker

    def service(self, *, tiers=frozenset({"standard"}), enabled=True):
        return SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(
                tenant_id=self.scope.tenant_id,
                everyframe_enabled=enabled,
                everyframe_tiers=tiers,
            ),
        )

    def test_rebound_job_is_claimed_by_the_hosted_slot_with_its_hold_intact(self):
        created = self.queued_job()
        before = self.row(created["id"])
        with self.repo.transaction() as connection:
            self.assertTrue(
                rebind(self.repo, connection, before, "h3-max-turbo", self.now)
            )
        after = self.row(created["id"])
        self.assertEqual(after["pool"], POOL)
        self.assertEqual(after["fence"], before["fence"] + 1)
        self.assertEqual(after["estimated_cost_microusd"], 100_000)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        control, worker = self.hosted_control()
        claim = control.claim(worker, POOL, lease_seconds=30)
        self.assertEqual(claim.job["id"], created["id"])

    def test_a_stale_or_leased_job_is_not_moved(self):
        created = self.queued_job()
        stale = self.row(created["id"])
        stale["fence"] += 1
        with self.repo.transaction() as connection:
            self.assertFalse(
                rebind(self.repo, connection, stale, "h3-max-turbo", self.now)
            )
        self.assertEqual(self.row(created["id"])["pool"], "own")

    def test_offload_does_nothing_until_credential_and_tier_are_on(self):
        created = self.queued_job()
        old = self.row(created["id"])
        queued = [(old, {})]
        now = old["created_at"] + 3600
        self.hosted_control()
        with self.repo.transaction() as connection:
            for service in (
                self.service(tiers=frozenset()),
                self.service(enabled=False),
            ):
                self.assertEqual(offload(service, connection, queued, now), queued)
        self.assertEqual(self.row(created["id"])["pool"], "own")

    def test_no_live_hosted_slot_leaves_the_job_for_our_machines(self):
        created = self.queued_job()
        old = self.row(created["id"])
        now = old["created_at"] + 3600
        with self.repo.transaction() as connection:
            self.assertEqual(
                offload(self.service(), connection, [(old, {})], now), [(old, {})]
            )


if __name__ == "__main__":
    unittest.main()

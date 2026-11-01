"""Hosted offload decision and rebind (#931 step 3b); no vendor or GPU calls."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from sqlalchemy import select, update

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.credits_pricing import get_tier
from studio_platform.inference.protocol import Outcome, SubmissionRejected
from studio_platform.providers.hosted_models import (
    BACKEND,
    MODELS,
    POOL,
    PROVIDER,
    RECIPES,
)
from studio_platform.repository import attempts, jobs
from studio_platform.scheduling.decide import (
    hosted_has_first_claim,
    hosted_offload,
)
from studio_platform.scheduling import rebind as rebind_module
from studio_platform.scheduling.rebind import (
    hold_for_hosted,
    hosted_plan,
    next_plan,
    offload,
    rebind,
    refresh_balance,
    routing,
    used_in_window,
)
from studio_platform.worker import WorkerRunner
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
        chosen = hosted_offload(
            queued, tier_of, set(tiers), used or {}, now, per_window=25
        )
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


class FirstClaimTests(unittest.TestCase):
    def test_machines_wait_only_while_the_hosted_api_can_still_take_the_job(self):
        job = {"created_at": 1000, "execution_plan": {}}
        tiers = {"standard"}
        for now, expected in ((1001, True), (1089, True), (1090, False)):
            with self.subTest(now=now):
                self.assertEqual(
                    hosted_has_first_claim(job, "standard", tiers, now), expected
                )
        self.assertFalse(hosted_has_first_claim(job, "fast", tiers, 1001))
        self.assertFalse(hosted_has_first_claim(job, "unknown", {"unknown"}, 1001))
        back = {"created_at": 1000, "execution_plan": {"hosted_tried": ["x"]}}
        self.assertFalse(hosted_has_first_claim(back, "standard", tiers, 1001))


class RoutingFileTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "runtime-secrets"
            folder.mkdir()
            if text is not None:
                (folder / "everyframe-routing.json").write_text(text, encoding="utf-8")
            return routing(root)

    def test_absent_or_malformed_means_off(self):
        off = (frozenset(), 0)
        for text in (None, "not json", "{}", '{"tiers": "fast", "per_window": 99}'):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), off)
        self.assertEqual(self.read('{"tiers": ["fast"], "per_window": 41}'), off)

    def test_a_valid_file_sets_tiers_and_this_environment_window(self):
        self.assertEqual(
            self.read('{"tiers": ["fast"], "per_window": 5}'), (frozenset({"fast"}), 5)
        )


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


class NextPlanTests(unittest.TestCase):
    def compiled(self):
        value, _ = compile_request(generation_request(), lambda _: None)
        return value

    def test_chain_moves_on_never_repeats_and_ends_on_our_machines(self):
        primary = {"pool": "own", "backend": "wangp-worker", "expected_runtime_s": 60}
        job = {
            "request": self.compiled(),
            "execution_plan": hosted_plan(primary, "h3-max-turbo", 1),
        }
        seen = []
        for _ in range(3):
            job["execution_plan"] = next_plan(job, 2)
            seen.append(job["execution_plan"]["backend"])
            if job["execution_plan"]["backend"] == BACKEND:
                seen[-1] = job["execution_plan"]["configuration_id"]
        self.assertEqual(
            seen,
            [
                MODELS["pika-h3-flex"].configuration_id,
                MODELS["h3-max-fast"].configuration_id,
                "wangp-worker",
            ],
        )
        self.assertEqual(job["execution_plan"]["pool"], "own")
        self.assertEqual(
            job["execution_plan"]["hosted_tried"],
            ["h3-max-turbo", "pika-h3-flex", "h3-max-fast"],
        )

    def test_a_plan_without_an_original_route_just_fails(self):
        job = {"request": self.compiled(), "execution_plan": {"pool": POOL}}
        self.assertIsNone(next_plan(job, 1))


class BalanceTests(unittest.TestCase):
    def setUp(self):
        rebind_module._balance.update(usd=None, read_at=None)
        self.addCleanup(rebind_module._balance.update, usd=None, read_at=None)
        self.settings = SimpleNamespace(everyframe_enabled=True)

    def test_read_at_most_every_five_minutes_and_low_balance_closes_hosted(self):
        reads = []

        def reader(_):
            reads.append(1)
            return 0.5

        self.assertEqual(refresh_balance(self.settings, 1000, reader=reader), 0.5)
        self.assertFalse(rebind_module.balance_allows_hosted())
        refresh_balance(self.settings, 1200, reader=reader)
        self.assertEqual(len(reads), 1)
        refresh_balance(self.settings, 1301, reader=reader)
        self.assertEqual(len(reads), 2)

    def test_an_unreadable_balance_leaves_hosted_open(self):
        def broken(_):
            raise OSError("no network")

        self.assertIsNone(refresh_balance(self.settings, 1000, reader=broken))
        self.assertTrue(rebind_module.balance_allows_hosted())


class RebindLedgerTests(LedgerCase):
    def setUp(self):
        super().setUp()
        rebind_module._balance.update(usd=None, read_at=None)
        self.addCleanup(rebind_module._balance.update, usd=None, read_at=None)

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
        worker = "dbos-everyframe-test-0"
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

    def service(self, *, tiers=("standard",), enabled=True):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        folder = Path(root.name) / "runtime-secrets"
        folder.mkdir()
        (folder / "everyframe-routing.json").write_text(
            json.dumps({"tiers": list(tiers), "per_window": 25}), encoding="utf-8"
        )
        return SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(
                tenant_id=self.scope.tenant_id,
                everyframe_enabled=enabled,
                data_dir=root.name,
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

    def test_refused_attempt_requeues_on_the_next_model_keeping_the_hold(self):
        created = self.queued_job()
        with self.repo.transaction() as connection:
            rebind(
                self.repo, connection, self.row(created["id"]), "h3-max-turbo", self.now
            )
        control, worker = self.hosted_control()
        claim = control.claim(worker, POOL, lease_seconds=30)
        control.queue.begin_submission(claim.lease)
        plan = next_plan(self.row(created["id"]), self.now)
        moved = control.queue.reroute(claim.lease, plan, "submission_rejected")
        self.assertEqual(moved["status"], "queued")
        self.assertIsNone(moved["current_attempt_id"])
        self.assertEqual(
            moved["execution_plan"]["configuration_id"],
            MODELS["pika-h3-flex"].configuration_id,
        )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def run_hosted(self, submit, poll=None):
        """One real WorkerRunner turn on a hosted slot with a fake vendor."""
        created = self.queued_job()
        with self.repo.transaction() as connection:
            rebind(
                self.repo, connection, self.row(created["id"]), "h3-max-turbo", self.now
            )
        control, worker = self.hosted_control()
        backend = SimpleNamespace(
            kind=BACKEND,
            enabled=True,
            slot_key="test-slot",
            prepare=lambda *args: object(),
            submit=submit,
            poll=poll or (lambda tag, task: Outcome("failed", task, 0)),
            reconcile=lambda tag, task=None: Outcome("unknown"),
            close=lambda: None,
        )
        with tempfile.TemporaryDirectory() as work:
            WorkerRunner(
                self.repo,
                None,
                Path(work),
                backend=backend,
                control=control,
                submission_guard=lambda _: True,
            ).run_once(worker, POOL)
        return self.row(created["id"])

    def test_vendor_refusal_moves_a_real_worker_turn_to_the_next_model(self):
        def refuse(prepared, tag):
            raise SubmissionRejected("everyframe_render_refused")

        row = self.run_hosted(refuse)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(
            row["execution_plan"]["configuration_id"],
            MODELS["pika-h3-flex"].configuration_id,
        )

    def test_refunded_vendor_failure_moves_on_instead_of_failing_the_job(self):
        row = self.run_hosted(lambda prepared, tag: "rnd_1")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["execution_plan"]["hosted_tried"], ["h3-max-turbo"])
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def submitted_ago(self, job_id, seconds):
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.job_id == job_id)
                .values(submission_started_at=self.now - seconds)
            )

    def test_a_render_past_its_deadline_moves_on_and_keeps_its_charge(self):
        running = lambda tag, task: Outcome(
            "running", task, actual_cost_microusd=38_000
        )

        created = self.queued_job()
        with self.repo.transaction() as connection:
            rebind(
                self.repo, connection, self.row(created["id"]), "h3-max-turbo", self.now
            )
        control, worker = self.hosted_control()
        posts = []
        backend = SimpleNamespace(
            kind=BACKEND,
            enabled=True,
            slot_key="test-slot",
            prepare=lambda *args: object(),
            submit=lambda prepared, tag: posts.append(tag) or "rnd_1",
            poll=running,
            reconcile=running,
            close=lambda: None,
        )
        with tempfile.TemporaryDirectory() as work:
            runner = WorkerRunner(
                self.repo,
                None,
                Path(work),
                backend=backend,
                control=control,
                retry_after_s=0,
                submission_guard=lambda _: True,
            )
            runner.run_once(worker, POOL)
            early = self.row(created["id"])
            self.assertEqual(early["pool"], POOL)
            self.assertNotIn("hosted_tried", early["execution_plan"])
            # Still running just before the deadline.
            self.submitted_ago(created["id"], TURBO.deadline_s - 1)
            runner.run_once(worker, POOL)
            self.assertNotIn("hosted_tried", self.row(created["id"])["execution_plan"])
            self.submitted_ago(created["id"], TURBO.deadline_s + 1)
            runner.run_once(worker, POOL)
        moved = self.row(created["id"])
        self.assertEqual(moved["status"], "queued")
        self.assertEqual(moved["error_code"], "hosted_render_deadline")
        self.assertEqual(moved["execution_plan"]["hosted_tried"], ["h3-max-turbo"])
        self.assertEqual(len(posts), 1)
        with self.repo.engine.connect() as connection:
            charged = connection.execute(
                select(attempts.c.actual_cost_microusd).where(
                    attempts.c.job_id == created["id"]
                )
            ).scalar_one()
        self.assertEqual(charged, 38_000)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_the_window_counts_every_render_a_later_model_replaced(self):
        row = self.run_hosted(lambda prepared, tag: "rnd_1")
        self.assertEqual(row["status"], "queued")  # Failed once; now on the next model.
        with self.repo.engine.connect() as connection:
            used = used_in_window(connection, self.scope.tenant_id, self.now)
        # The refunded render still used the vendor's limit, plus the queued retry.
        self.assertEqual(used, {self.scope.owner_id: 2})

    def test_sustained_hosted_use_stops_holding_machines_back(self):
        created = self.queued_job()
        job = self.row(created["id"])
        service = self.service(tiers=("standard", "fast", "refine"))
        with self.repo.engine.connect() as connection:
            self.assertTrue(
                hold_for_hosted(
                    service.settings, connection, job, job["created_at"] + 1
                )
            )
        with (
            mock.patch.object(
                rebind_module,
                "used_in_window",
                return_value={"someone": rebind_module.HOSTED_WARMS_MACHINE_AT},
            ),
            self.repo.engine.connect() as connection,
        ):
            self.assertFalse(
                hold_for_hosted(
                    service.settings, connection, job, job["created_at"] + 1
                )
            )

    def test_low_balance_sends_jobs_to_our_machines(self):
        created = self.queued_job()
        job = self.row(created["id"])
        service = self.service(tiers=("standard", "fast", "refine"))
        rebind_module._balance.update(usd=0.5, read_at=self.now)
        self.hosted_control()
        with self.repo.transaction() as connection:
            self.assertFalse(
                hold_for_hosted(
                    service.settings, connection, job, job["created_at"] + 1
                )
            )
            self.assertEqual(
                offload(service, connection, [(job, {})], job["created_at"] + 121),
                [(job, {})],
            )
        self.assertEqual(self.row(created["id"])["pool"], "own")

    def test_a_job_back_from_the_chain_is_not_offloaded_again(self):
        created = self.queued_job()
        old = self.row(created["id"])
        old["execution_plan"] = {**old["execution_plan"], "hosted_tried": ["x"]}
        self.hosted_control()
        with self.repo.transaction() as connection:
            self.assertEqual(
                offload(
                    self.service(), connection, [(old, {})], old["created_at"] + 3600
                ),
                [(old, {})],
            )
        self.assertEqual(self.row(created["id"])["pool"], "own")

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
                self.service(tiers=()),
                self.service(enabled=False),
            ):
                self.assertEqual(offload(service, connection, queued, now), queued)
        self.assertEqual(self.row(created["id"])["pool"], "own")

    def test_an_overdue_job_moves_when_everything_is_switched_on(self):
        created = self.queued_job()
        old = self.row(created["id"])
        self.now = old["created_at"] + 121  # Past every tier's limit.
        now = self.now
        self.hosted_control()  # A slot that is live at that moment.
        tier = get_tier(old["request"], execution=old["execution_plan"])
        with self.repo.transaction() as connection:
            left = offload(self.service(tiers=(tier,)), connection, [(old, {})], now)
        self.assertEqual(left, [])
        self.assertEqual(self.row(created["id"])["pool"], POOL)

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

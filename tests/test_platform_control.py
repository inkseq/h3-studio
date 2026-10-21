import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.repository import BudgetExceeded, Conflict, LeaseLost
from studio_platform.worker import MockBackend, WorkerRunner, main
from studio_platform.storage import LocalObjectStore
from test_platform_repository import LedgerCase


class ControlTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)

    def spec(
        self,
        worker="worker",
        *,
        pool="control-test",
        instance="fake-instance",
        gpu_ids=("fake-gpu-0",),
    ):
        return WorkerSpec(
            worker,
            pool,
            "test-only",
            instance,
            gpu_ids,
            ("test-recipe",),
            "test-model",
            "manifest-test",
        )

    def controlled_job(
        self,
        *,
        pool="control-test",
        model="test-model",
        config="manifest-test",
        recipe="test-recipe",
    ):
        plan = self.repo.create_plan(
            self.scope,
            {"recipe_id": recipe, "request": {"model": model}},
            {
                "pool": pool,
                "backend": "comfy-worker",
                "enabled": True,
                "configuration_id": config,
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        import uuid

        return self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex)

    def ready(self, spec):
        self.control.register(spec)
        self.control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)

    def test_known_instance_deadline_skips_long_jobs_but_admits_a_short_one(self):
        self.repo.configure_pool("control-test", max_instances=1, max_physical_gpus=1)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "control-test",
            "ttl-test",
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=100_000,
            hard_deadline=self.now + 600,
            budget_account_ids=["owner-budget"],
            dry_run=False,
            provider="test-only",
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(
            intent["id"], "starting", provider_instance_id="fake-instance"
        )
        self.ready(self.spec())
        long_job = self.controlled_job()
        self.assertIsNone(self.control.claim("worker", "control-test"))
        plan = self.repo.create_plan(
            self.scope,
            {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
            {
                "pool": "control-test",
                "backend": "comfy-worker",
                "enabled": True,
                "configuration_id": "manifest-test",
                "expected_runtime_s": 60,
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        short_job = self.repo.create_job(self.scope, plan["id"], "short-ttl-job")
        claim = self.control.claim("worker", "control-test")
        self.assertEqual(claim.job["id"], short_job["id"])
        self.assertTrue(self.control.submission_allowed(claim.job))
        from sqlalchemy import update
        from studio_platform.repository import instance_intents

        with self.repo.transaction() as connection:
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(hard_deadline=self.now + 90)
            )
        self.assertFalse(self.control.submission_allowed(claim.job))
        self.assertEqual(
            self.repo.get_job(self.scope, long_job["id"])["status"], "queued"
        )

    def test_cpu_render_slot_has_no_gpu_budget_and_fixed_instance_ownership(self):
        from sqlalchemy import select
        from studio_platform.repository import registered_devices

        self.repo.configure_capacity()
        spec = WorkerSpec(
            "chapter-cpu",
            "chapter",
            "local-cpu",
            "local-cpu-instance",
            (),
            ("chapter-roughcut-v1",),
            "sixnine-chapter-roughcut-v1",
            "cpu-render-v1",
            "cpu-render",
        )
        self.ready(spec)
        self.assertEqual(self.control.capacity(), {"instances": 0, "physical_gpus": 0})
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(registered_devices))), [])
        status = self.control.pool_status(
            "chapter",
            model_id=spec.model_id,
            configuration_id=spec.configuration_id,
            recipe_id="chapter-roughcut-v1",
            backend="cpu-render",
        )
        self.assertEqual(status["ready"], 1)
        from dataclasses import replace

        with self.assertRaisesRegex(Conflict, "cpu_instance_already_owned"):
            self.control.register(replace(spec, worker_id="second-cpu"))
        self.control.retire(spec.worker_id, upstream_idle_confirmed=True)
        self.assertEqual(
            self.control.register(replace(spec, worker_id="second-cpu"))["state"],
            "registered",
        )

    def test_cpu_render_identity_and_recipe_are_exact(self):
        spec = WorkerSpec(
            "chapter-cpu",
            "chapter",
            "local-cpu",
            "local-cpu-instance",
            (),
            ("chapter-roughcut-v1",),
            "sixnine-chapter-roughcut-v1",
            "cpu-render-v1",
            "cpu-render",
        )
        from dataclasses import replace

        for fields in (
            {"physical_gpu_ids": ("fake",)},
            {"provider": "test-only"},
            {"model_id": "other"},
            {"recipe_ids": ("h3-base-fl2va-v1",)},
            {"configuration_id": "other"},
        ):
            with self.assertRaises(ValueError):
                replace(spec, **fields)
        row = self.control.register(spec)
        good = {
            "pool": "chapter",
            "execution_plan": {
                "backend": "cpu-render",
                "enabled": True,
                "configuration_id": "cpu-render-v1",
            },
            "request": {
                "recipe_id": "chapter-roughcut-v1",
                "request": {"model": "sixnine-chapter-roughcut-v1"},
            },
        }
        self.assertTrue(self.control.matches(row, good))
        good["request"]["request"]["model"] = "other"
        self.assertFalse(self.control.matches(row, good))

    def test_cpu_render_wire_versions_route_to_exact_separate_configurations(self):
        self.repo.configure_capacity()
        specs, rows = {}, {}
        for version in (1, 2, 3, 5):
            specs[version] = WorkerSpec(
                "cpu-v" + str(version),
                "cpu-render",
                "local-cpu",
                "cpu-instance-v" + str(version),
                (),
                ("chapter-roughcut-v1",),
                "sixnine-chapter-roughcut-v1",
                "cpu-render-v" + str(version),
                "cpu-render",
            )
            self.ready(specs[version])
            plan = self.repo.create_plan(
                self.scope,
                {
                    "recipe_id": "chapter-roughcut-v1",
                    "request": {
                        "model": "sixnine-chapter-roughcut-v1",
                        "render": {"version": version},
                    },
                },
                {
                    "pool": "cpu-render",
                    "backend": "cpu-render",
                    "enabled": True,
                    "configuration_id": "cpu-render-v" + str(version),
                },
                expires_at=self.now + 1000,
                estimated_cost_microusd=0,
            )
            rows[version] = self.repo.create_job(
                self.scope, plan["id"], "cpu-version-" + str(version)
            )
        for version in (1, 2, 3, 5):
            for other in (1, 2, 3, 5):
                self.assertEqual(
                    self.control.matches(
                        self.control.get("cpu-v" + str(version)), rows[other]
                    ),
                    version == other,
                )
        for version in (1, 2, 3, 5):
            claimed = self.control.claim(specs[version].worker_id, "cpu-render")
            self.assertEqual(claimed.job["id"], rows[version]["id"])
        self.assertEqual(self.control.capacity(), {"instances": 0, "physical_gpus": 0})

    def test_cpu_render_configuration_allows_only_explicit_validated_versions(
        self,
    ):
        for configuration in (
            "cpu-render-v0",
            "cpu-render-v4",
            "CPU-RENDER-V2",
            "cpu-render-v2 ",
            "arbitrary",
        ):
            with (
                self.subTest(configuration=configuration),
                self.assertRaisesRegex(ValueError, "cpu_render_identity_required"),
            ):
                WorkerSpec(
                    "cpu",
                    "cpu-render",
                    "local-cpu",
                    "cpu-instance",
                    (),
                    ("chapter-roughcut-v1",),
                    "sixnine-chapter-roughcut-v1",
                    configuration,
                    "cpu-render",
                )

    def test_control_filters_dispatch_identity_in_sql_without_loading_other_snapshots(
        self,
    ):
        self.ready(self.spec())
        for i in range(24):
            self.controlled_job(model="other-model-" + str(i))
        good = self.controlled_job()
        from sqlalchemy import event

        reads = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            sql = context.compiled.statement if context.compiled is not None else None
            if (
                sql is not None
                and getattr(sql, "is_select", False)
                and {"request", "pool"} <= set(sql.selected_columns.keys())
            ):
                reads.append(statement)

        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            claim = self.control.claim("worker", "control-test")
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(claim.job["id"], good["id"])
        self.assertEqual(len(reads), 2)
        self.assertTrue(all("platform_jobs.id =" in sql for sql in reads))

    def test_nested_request_model_precedence_is_preserved_by_sql_filter(self):
        self.ready(self.spec())
        plan = self.repo.create_plan(
            self.scope,
            {"recipe_id": "test-recipe", "model": "test-model", "request": {}},
            {
                "pool": "control-test",
                "backend": "comfy-worker",
                "enabled": True,
                "configuration_id": "manifest-test",
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        self.repo.create_job(self.scope, plan["id"], "not-a-nested-model")
        good = self.controlled_job()
        self.assertEqual(
            self.control.claim("worker", "control-test").job["id"], good["id"]
        )

    def test_invalid_enabled_value_does_not_fail_or_authorize_pool(self):
        self.ready(self.spec())
        plan = self.repo.create_plan(
            self.scope,
            {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
            {
                "pool": "control-test",
                "backend": "comfy-worker",
                "enabled": "not-a-boolean",
                "configuration_id": "manifest-test",
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        self.repo.create_job(self.scope, plan["id"], "not-authorized")
        good = self.controlled_job()
        self.assertEqual(
            self.control.claim("worker", "control-test").job["id"], good["id"]
        )

    def test_global_default_zero_denies_real_gpu_registration(self):
        self.repo.configure_capacity()
        with self.assertRaises(BudgetExceeded):
            self.control.register(self.spec())
        self.assertEqual(self.control.capacity(), {"instances": 0, "physical_gpus": 0})

    def test_same_physical_gpu_cannot_be_registered_by_another_worker(self):
        first = self.control.register(self.spec())
        self.assertEqual(self.control.register(self.spec())["id"], first["id"])
        with self.assertRaises(Conflict):
            self.control.register(self.spec("second"))
        with self.assertRaises(Conflict):
            self.control.register(self.spec(pool="other"))

    def test_tp2_owns_two_devices_one_execution_slot(self):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=2)
        self.ready(self.spec(gpu_ids=("fake-gpu-0", "fake-gpu-1")))
        one, two = self.controlled_job(), self.controlled_job()
        claim = self.control.claim("worker", "control-test")
        self.assertIn(claim.job["id"], (one["id"], two["id"]))
        self.assertIsNone(self.control.claim("worker", "control-test"))
        self.assertEqual(self.control.capacity(), {"instances": 1, "physical_gpus": 2})
        with self.assertRaises(Conflict):
            self.control.register(self.spec("second", gpu_ids=("fake-gpu-1",)))

    def test_multiple_pools_share_global_physical_gate(self):
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=1)
        self.control.register(self.spec(pool="fl"))
        with self.assertRaises(BudgetExceeded):
            self.control.register(
                self.spec("second", pool="ref", instance="fake-other")
            )

    def test_global_instance_intents_count_against_registration_without_double_counting(
        self,
    ):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=2)
        self.repo.configure_pool("control-test", max_instances=1, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "control-test",
            "fake-create",
            provider="test-only",
            physical_gpus=2,
            hard_deadline=2000,
            reserved_cost_microusd=100_000,
            budget_account_ids=["owner-budget"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(
            intent["id"], "starting", provider_instance_id="fake-instance"
        )
        self.control.register(self.spec(gpu_ids=("fake-gpu-0", "fake-gpu-1")))
        self.assertEqual(self.control.capacity(), {"instances": 1, "physical_gpus": 2})

    def test_create_intents_across_pools_share_global_gate(self):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        for pool in ("fl", "ref"):
            self.repo.configure_pool(pool, max_instances=2, max_physical_gpus=2)
        self.repo.reserve_instance_intent(
            self.scope,
            "fl",
            "one",
            hard_deadline=2000,
            reserved_cost_microusd=100_000,
            budget_account_ids=["owner-budget"],
            dry_run=False,
        )
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_instance_intent(
                self.scope,
                "ref",
                "two",
                hard_deadline=2000,
                reserved_cost_microusd=100_000,
                budget_account_ids=["owner-budget"],
                dry_run=False,
            )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_expired_running_remains_owned_and_must_reconcile(self):
        self.ready(self.spec())
        job = self.controlled_job()
        claim = self.control.claim("worker", "control-test", lease_seconds=10)
        self.control.queue.begin_submission(claim.lease)
        self.control.queue.record_submitted(claim.lease, "known-task")
        self.control.queue.release(claim.lease, retry_after_s=0)
        self.control.observe("worker", job["id"])
        self.now += 121
        self.control.recover_expired()
        self.assertEqual(self.control.get("worker")["state"], "unknown")
        with self.assertRaises(Conflict):
            self.control.mark_ready("worker", upstream_idle_confirmed=True)
        with self.assertRaises(Conflict):
            self.control.register(self.spec("replacement"))
        self.assertIsNone(self.control.claim("worker", "control-test"))
        recovered = self.control.claim("worker", "control-test", purpose="reconcile")
        self.assertEqual(recovered.job["id"], job["id"])
        self.assertEqual(recovered.job["attempt_no"], 1)

    def test_crashed_unsubmitted_slot_needs_idle_proof_before_safe_requeue(self):
        self.ready(self.spec())
        job = self.controlled_job()
        old = self.control.claim("worker", "control-test", lease_seconds=10)
        self.now += 11
        self.control.recover_expired()
        self.control.queue.recover_expired()
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        self.assertIsNone(self.control.claim("worker", "control-test"))
        with self.assertRaises(Conflict):
            self.control.mark_ready("worker")
        self.control.mark_ready("worker", upstream_idle_confirmed=True)
        resumed = self.control.claim("worker", "control-test")
        self.assertEqual(resumed.job["id"], job["id"])
        self.assertEqual(resumed.job["attempt_no"], 2)
        with self.assertRaises(LeaseLost):
            self.control.queue.begin_submission(old.lease)

    def test_requeued_uncertain_attempt_never_clears_or_retires_physical_slot(self):
        from sqlalchemy import update
        from studio_platform.repository import jobs

        for fault in ("submitted", "missing-evidence"):
            with self.subTest(fault=fault):
                spec = self.spec(
                    "worker-" + fault,
                    pool="pool-" + fault,
                    instance="instance-" + fault,
                )
                self.ready(spec)
                plan = self.repo.create_plan(
                    self.scope,
                    {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
                    {
                        "pool": spec.pool,
                        "backend": "comfy-worker",
                        "enabled": True,
                        "configuration_id": "manifest-test",
                    },
                    expires_at=self.now + 1000,
                    estimated_cost_microusd=100_000,
                )
                job = self.repo.create_job(
                    self.scope,
                    plan["id"],
                    "job-" + fault,
                    budget_account_ids=("owner-budget",),
                )
                claim = self.control.claim(spec.worker_id, spec.pool)
                if fault == "submitted":
                    self.control.queue.begin_submission(claim.lease)
                    self.control.queue.record_submitted(claim.lease, "original-task")
                with self.repo.transaction() as connection:
                    fields = dict(
                        status="queued", lease_worker_id=None, lease_expires_at=None
                    )
                    if fault == "missing-evidence":
                        fields["current_attempt_id"] = "missing-attempt-proof"
                    connection.execute(
                        update(jobs).where(jobs.c.id == job["id"]).values(**fields)
                    )
                before = self.repo.get_budget("owner-budget")
                observed = self.control.observe(spec.worker_id, job["id"])
                self.assertEqual(
                    (observed["state"], observed["current_job_id"]),
                    ("unknown", job["id"]),
                )
                with self.assertRaisesRegex(
                    Conflict, "current_attempt_still_unresolved"
                ):
                    self.control.retire(spec.worker_id, upstream_idle_confirmed=True)
                with self.assertRaisesRegex(
                    Conflict, "current_attempt_still_unresolved"
                ):
                    self.control.mark_ready(
                        spec.worker_id, upstream_idle_confirmed=True
                    )
                with self.assertRaisesRegex(Conflict, "physical_gpu_already_owned"):
                    self.control.register(
                        self.spec(
                            "replacement-" + fault,
                            pool=spec.pool,
                            instance=spec.instance_id,
                        )
                    )
                self.assertIsNone(self.control.claim(spec.worker_id, spec.pool))
                self.assertEqual(self.repo.get_budget("owner-budget"), before)
                status = self.control.pool_status(
                    spec.pool,
                    model_id=spec.model_id,
                    configuration_id=spec.configuration_id,
                )
                self.assertEqual((status["unknown"], status["ready"]), (1, 0))

    def test_proven_unsubmitted_defer_can_observe_ready_or_retire_and_reassign(self):
        spec = self.spec()
        self.ready(spec)
        job = self.controlled_job()
        first = self.control.claim(spec.worker_id, spec.pool)
        self.control.queue.defer_unsubmitted(first.lease, retry_after_s=0)
        observed = self.control.observe(spec.worker_id, job["id"])
        self.assertEqual(
            (observed["state"], observed["current_job_id"]), ("ready", None)
        )
        second = self.control.claim(spec.worker_id, spec.pool)
        self.control.queue.defer_unsubmitted(second.lease, retry_after_s=0)
        self.assertEqual(
            self.control.retire(spec.worker_id, upstream_idle_confirmed=True)["state"],
            "retired",
        )
        replacement = self.spec("replacement")
        self.ready(replacement)
        resumed = self.control.claim(replacement.worker_id, replacement.pool)
        self.assertEqual((resumed.job["id"], resumed.job["attempt_no"]), (job["id"], 3))

    def test_source_recipe_model_configuration_and_pool_must_all_match(self):
        self.ready(self.spec())
        self.controlled_job(model="different-model")
        self.controlled_job(config="other-gpu-config")
        self.controlled_job(recipe="other-recipe")
        self.controlled_job(pool="other-pool")
        expected = self.controlled_job()
        claim = self.control.claim("worker", "control-test")
        self.assertEqual(claim.job["id"], expected["id"])

    def test_pool_status_is_exact_read_only_and_expired_slots_are_not_ready(self):
        self.ready(self.spec())
        query = dict(
            model_id="test-model",
            configuration_id="manifest-test",
            recipe_id="test-recipe",
        )
        self.assertEqual(self.control.pool_status("control-test", **query)["ready"], 1)
        self.assertEqual(
            self.control.pool_status("control-test", **{**query, "model_id": "wrong"})[
                "matched_slots"
            ],
            0,
        )
        self.assertEqual(
            self.control.pool_status(
                "control-test", **{**query, "configuration_id": "wrong"}
            )["matched_slots"],
            0,
        )
        self.assertEqual(
            self.control.pool_status("control-test", **{**query, "recipe_id": "wrong"})[
                "matched_slots"
            ],
            0,
        )
        self.now += 121
        status = self.control.pool_status("control-test", **query)
        self.assertEqual((status["ready"], status["unknown"]), (0, 1))
        self.assertEqual(self.control.get("worker")["state"], "ready")
        self.assertEqual(self.control.capacity()["physical_gpus"], 1)

    def test_pool_status_counts_claimed_and_drain_slots_without_inventing_readiness(
        self,
    ):
        self.ready(self.spec())
        query = dict(
            model_id="test-model",
            configuration_id="manifest-test",
            recipe_id="test-recipe",
        )
        self.controlled_job()
        self.control.claim("worker", "control-test")
        self.assertEqual(self.control.pool_status("control-test", **query)["busy"], 1)
        self.control.drain("worker")
        self.assertEqual(
            self.control.pool_status("control-test", **query)["draining"], 1
        )
        self.assertEqual(self.control.pool_status("control-test", **query)["ready"], 0)

    def test_pool_status_snapshot_expires_and_next_request_reads_current_stop(self):
        self.ready(self.spec())
        query = dict(
            model_id="test-model",
            configuration_id="manifest-test",
            recipe_id="test-recipe",
        )
        ordinary = self.control.pool_status("control-test", **query)
        with self.control.pool_status_snapshot(pools=("control-test",)) as snapshot:
            self.assertEqual(
                self.control.pool_status("control-test", **query, _snapshot=snapshot),
                ordinary,
            )
            with self.assertRaisesRegex(ValueError, "pool_status_snapshot_unavailable"):
                self.control.pool_status("other-pool", **query, _snapshot=snapshot)
        with self.assertRaisesRegex(ValueError, "pool_status_snapshot_unavailable"):
            self.control.pool_status("control-test", **query, _snapshot=snapshot)
        self.control.drain("worker")
        with self.control.pool_status_snapshot(pools=("control-test",)) as fresh:
            state = self.control.pool_status("control-test", **query, _snapshot=fresh)
            self.assertEqual((state["ready"], state["draining"]), (0, 1))

    def test_worker_recipe_binding_must_be_an_explicit_unique_tuple(self):
        base = self.spec()
        from dataclasses import replace

        for recipes in ("test-recipe", ["test-recipe"], ("a", "a")):
            with self.assertRaises(ValueError):
                replace(base, recipe_ids=recipes)

    def test_drain_allows_current_reconciliation_but_not_new_generation(self):
        self.ready(self.spec())
        job = self.controlled_job()
        claim = self.control.claim("worker", "control-test")
        self.control.queue.begin_submission(claim.lease)
        self.control.queue.record_submitted(claim.lease, "task")
        self.control.queue.release(claim.lease, retry_after_s=0)
        self.control.drain("worker")
        self.assertIsNone(self.control.claim("worker", "control-test"))
        reconciler = self.control.claim("worker", "control-test", purpose="reconcile")
        self.assertEqual(reconciler.job["id"], job["id"])
        with self.assertRaises(Conflict):
            self.control.retire("worker", upstream_idle_confirmed=True)

    def managed_instance(self):
        self.repo.configure_pool("control-test", max_instances=1, max_physical_gpus=1)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "control-test",
            "managed-test-only",
            provider="test-only",
            physical_gpus=1,
            slots=1,
            hard_deadline=self.now + 3600,
            reserved_cost_microusd=100_000,
            budget_account_ids=["owner-budget"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(
            intent["id"], "starting", provider_instance_id="fake-instance"
        )
        self.ready(self.spec())
        self.repo.update_instance(intent["id"], "ready")
        return intent

    def test_managed_draining_host_cannot_resume_idle_worker_or_take_regular_queued_job(
        self,
    ):
        intent = self.managed_instance()
        job = self.controlled_job()
        self.control.drain("worker")
        self.repo.update_instance(intent["id"], "draining")
        with self.assertRaisesRegex(Conflict, "worker_instance_not_admitting"):
            self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertIsNone(self.control.claim("worker", "control-test"))
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_managed_instance_drain_overrides_stale_worker_ready_for_admission_and_claim(
        self,
    ):
        intent = self.managed_instance()
        job = self.controlled_job()
        # Emulate an operator/older control path that wrote only instance state.
        self.repo.update_instance(intent["id"], "draining")
        self.assertEqual(self.control.get("worker")["state"], "ready")
        status = self.control.pool_status(
            "control-test",
            model_id="test-model",
            configuration_id="manifest-test",
            recipe_id="test-recipe",
        )
        self.assertEqual((status["ready"], status["draining"]), (0, 1))
        with self.assertRaisesRegex(Conflict, "worker_instance_not_admitting"):
            self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.assertIsNone(self.control.claim("worker", "control-test"))
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_managed_draining_host_keeps_original_attempt_reconciliation_and_collection(
        self,
    ):
        intent = self.managed_instance()
        job = self.controlled_job()
        first = self.control.claim("worker", "control-test")
        self.control.queue.begin_submission(first.lease)
        self.control.queue.record_submitted(first.lease, "fake-existing-prompt")
        self.control.queue.release(first.lease, retry_after_s=0)
        later = self.controlled_job()  # Already admitted before shutdown began.
        self.control.drain("worker")
        self.repo.update_instance(intent["id"], "draining")
        with self.assertRaisesRegex(Conflict, "capacity_drain_repreflight"):
            self.controlled_job()  # A stale ready plan must not add new demand.
        with self.assertRaisesRegex(Conflict, "worker_instance_not_admitting"):
            self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.assertIsNone(self.control.claim("worker", "control-test"))
        reconcile = self.control.claim("worker", "control-test", purpose="reconcile")
        self.assertEqual(reconcile.job["id"], job["id"])
        self.assertEqual(reconcile.lease.attempt_id, first.lease.attempt_id)
        self.control.queue.begin_collection(reconcile.lease)
        self.control.queue.release(reconcile.lease, retry_after_s=0)
        collecting = self.control.claim("worker", "control-test", purpose="collect")
        self.assertEqual(collecting.lease.attempt_id, first.lease.attempt_id)
        self.control.queue.complete(
            collecting.lease,
            [
                {
                    "kind": "video",
                    "object_key": "owners/superdan/result.mp4",
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                    "validated": True,
                }
            ],
            actual_cost_microusd=0,
        )
        self.control.observe("worker", job["id"])
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["status"], "succeeded"
        )
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(self.control.get("worker")["state"], "draining")
        self.assertIsNone(self.control.claim("worker", "control-test"))
        self.assertEqual(self.repo.get_job(self.scope, later["id"])["status"], "queued")
        with self.assertRaisesRegex(Conflict, "worker_instance_not_admitting"):
            self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_drain_survives_expiry_and_current_task_completion_until_explicit_resume(
        self,
    ):
        self.ready(self.spec())
        current = self.controlled_job()
        claim = self.control.claim("worker", "control-test")
        self.control.queue.begin_submission(claim.lease)
        self.control.queue.record_submitted(claim.lease, "task")
        self.control.queue.release(claim.lease, retry_after_s=0)
        self.control.drain("worker")
        self.now += 901
        self.control.recover_expired()
        self.assertEqual(self.control.get("worker")["state"], "unknown")
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        recovered = self.control.claim("worker", "control-test", purpose="reconcile")
        self.control.queue.fail(
            recovered.lease,
            "test-only-finished",
            actual_cost_microusd=0,
            upstream_stopped=True,
        )
        self.control.observe("worker", current["id"])
        self.assertEqual(self.control.get("worker")["state"], "draining")
        later = self.controlled_job()
        self.assertIsNone(self.control.claim("worker", "control-test"))
        self.control.mark_ready("worker", upstream_idle_confirmed=True)
        self.assertEqual(
            self.control.claim("worker", "control-test").job["id"], later["id"]
        )

    def test_additive_drain_migration_preserves_existing_slot_and_draining_intent(self):
        self.control.register(self.spec())
        self.control.drain("worker")
        with self.repo.transaction() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE platform_registered_workers DROP COLUMN drain_requested"
            )
        self.repo.create_schema()
        self.repo.create_schema()
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertEqual(self.control.get("worker")["state"], "draining")
        self.assertEqual(self.control.capacity()["physical_gpus"], 1)

    def test_retirement_needs_idle_proof_then_explicit_reassignment(self):
        self.control.register(self.spec())
        with self.assertRaises(Conflict):
            self.control.retire("worker")
        self.control.retire("worker", upstream_idle_confirmed=True)
        self.control.register(self.spec("replacement"))
        self.assertEqual(self.control.capacity(), {"instances": 1, "physical_gpus": 1})

    def test_concurrent_register_two_names_same_device_only_one_wins(self):
        def register(i):
            try:
                return self.control.register(self.spec("worker-" + str(i)))
            except Conflict:
                return None

        results = self.parallel(register)
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_mock_registered_runner_works_under_zero_real_cloud_capacity(self):
        self.repo.configure_capacity()
        spec = WorkerSpec(
            "mock-worker",
            "mock",
            "mock",
            "local-only",
            ("cpu",),
            (),
            "SIMULATION",
            "simulation-v1",
            backend="mock",
        )
        self.ready(spec)
        self.assertEqual(self.control.capacity(), {"instances": 0, "physical_gpus": 0})
        root = Path(self.temp.name)
        runner = WorkerRunner(
            self.repo,
            LocalObjectStore(root / "objects"),
            root / "work",
            backend=MockBackend(root / "mock", enabled=True),
            control=self.control,
        )
        self.assertEqual(runner.run_once("mock-worker", "mock")["state"], "idle")

    def test_cli_disabled_is_noop_and_mock_once_only_initializes_local_test_data(self):
        root = Path(self.temp.name)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "--worker-id",
                        "test",
                        "--pool",
                        "mock",
                        "--work-dir",
                        str(root / "unused"),
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["state"], "disabled")
        self.assertFalse((root / "unused").exists())
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            result = main(
                [
                    "--backend",
                    "mock",
                    "--worker-id",
                    "mock-test",
                    "--pool",
                    "mock",
                    "--once",
                    "--work-dir",
                    str(root / "cli-work"),
                    "--data-dir",
                    str(root / "cli-data"),
                ]
            )
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["state"], "idle")

    def test_cli_data_dir_keeps_explicit_database_file_setting(self):
        root = Path(self.temp.name)
        configured_db = root / "explicit-local-test.sqlite3"
        config_file = root / "local-test-dsn.txt"
        config_file.write_text(
            "sqlite:///" + configured_db.as_posix(), encoding="utf-8"
        )
        config_file.chmod(0o600)  # Match the protected-file contract on POSIX too.
        data_dir = root / "separate-data"
        output = io.StringIO()
        with (
            patch.dict(
                os.environ, {"SIXNINE_DATABASE_URL_FILE": str(config_file)}, clear=True
            ),
            contextlib.redirect_stdout(output),
        ):
            result = main(
                [
                    "--backend",
                    "mock",
                    "--worker-id",
                    "mock-file-test",
                    "--pool",
                    "mock",
                    "--once",
                    "--work-dir",
                    str(root / "cli-file-work"),
                    "--data-dir",
                    str(data_dir),
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(configured_db.exists())
        self.assertFalse((data_dir / "platform.sqlite3").exists())
        self.assertNotIn("sqlite", output.getvalue())
        self.assertNotIn(str(config_file), output.getvalue())

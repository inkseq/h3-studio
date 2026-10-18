"""Fresh worker rows cannot override managed provider or execution windows."""

from dataclasses import replace
from sqlalchemy import delete, select, update

from studio_platform.control import WorkerControl
from studio_platform.generation_availability import _mode_result
from studio_platform.operator_capacity import operator_nodes, operator_heartbeats
from studio_platform.repository import (
    instance_intents,
    registered_workers,
    jobs,
    budget_reservations,
    request_hash,
)
from test_operator_capacity import OperatorCase


class ManagedAdmissionTests(OperatorCase):
    def prepare(self, *, duration=120, profile=None):
        self.create()
        self.controller.tick()
        self.control = WorkerControl(self.repo)
        with self.repo.engine.connect() as conn:
            self.worker = dict(
                conn.execute(select(registered_workers)).mappings().one()
            )
            self.intent = dict(conn.execute(select(instance_intents)).mappings().one())
        self.profile = profile or self.binding.runtime_profile_id
        plan = self.repo.create_plan(
            self.scope,
            {
                "recipe_id": self.binding.recipe_ids[0],
                "deployment_profile_id": self.profile,
                "request": {"model": self.binding.model_id},
            },
            {
                "pool": self.binding.pool,
                "backend": "wangp-worker",
                "enabled": True,
                "configuration_id": self.binding.configuration_id,
                "engine_manifest_digest": self.binding.engine_manifest_digest,
                "expected_runtime_s": duration,
            },
            expires_at=self.now + 9000,
            estimated_cost_microusd=100,
        )
        self.job = self.repo.create_job(
            self.scope,
            plan["id"],
            "only-original-job",
            budget_account_ids=["owner-budget"],
        )
        self.duration = duration

    def counts(self):
        return self.control.pool_status(
            self.binding.pool,
            model_id=self.binding.model_id,
            configuration_id=self.binding.configuration_id,
            recipe_id=self.binding.recipe_ids[0],
            backend="wangp-worker",
            engine_manifest_digest=self.binding.engine_manifest_digest,
            expected_runtime_s=self.duration,
            deployment_profile_id=self.profile,
            output_delivery=getattr(self, "output_delivery", ""),
            dispatch_backend=getattr(self, "dispatch", "legacy"),
        )

    def availability(self):
        from types import SimpleNamespace

        policy = {
            "deployment_profile_id": self.profile,
            "recipe_ids": list(self.binding.recipe_ids),
            "pool": self.binding.pool,
            "model_id": self.binding.model_id,
            "configuration_id": self.binding.configuration_id,
            "engine_manifest_digest": self.binding.engine_manifest_digest,
            "backend": "wangp-worker",
            "enabled": True,
            "qualification": {
                "status": "accepted",
                "verified_at": 0,
                "expires_at": 10000,
            },
            "reservation": {"expected_runtime_s": self.duration, "expires_at": 10000},
            "output_delivery": getattr(self, "output_delivery", ""),
            "dispatch_backend": getattr(self, "dispatch", "legacy"),
        }
        with self.control.pool_status_snapshot(now=self.now) as pool:
            return _mode_result(
                SimpleNamespace(
                    generation_enabled=True, execution_backend="wangp-worker"
                ),
                self.repo,
                self.registry,
                policy,
                True,
                "fl",
                self.binding.recipe_ids[0],
                self.now,
                control=self.control,
                snapshot={"pool": pool, "preparing": None},
            )

    def assert_blocked(self, reason):
        before = self.repo.get_job(self.scope, self.job["id"])
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(self.counts()["ready"], 0)
        self.assertIn(reason, self.counts()["reason_counts"])
        self.assertEqual(self.availability()["reason_code"], reason)
        self.assertFalse(self.availability()["available"])
        self.assertIsNone(self.control.claim(self.worker["id"], self.binding.pool))
        self.assertEqual(self.control.queued_diagnostic(before)["reason_code"], reason)
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"]), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(len(self.provider.creates), 1)

    def test_stale_provider_proof_is_not_ready_despite_fresh_worker(self):
        self.prepare()
        self.now += 31
        self.assert_blocked("managed_provider_lifetime_unverified")

    def test_stop_new_margin_matches_readiness_and_claim(self):
        self.prepare(duration=60)
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(hard_deadline=self.now + 299))
        self.assert_blocked("worker_window_closing")

    def test_long_job_does_not_fit_a_ready_machine(self):
        self.prepare(duration=3500)
        self.assert_blocked("job_exceeds_worker_window")

    def test_profile_identity_cannot_use_another_bound_mode(self):
        self.prepare(profile="different-profile")
        self.assert_blocked("matching_profile_unavailable")

    def test_managed_node_cannot_advertise_legacy_unspecified_profile(self):
        self.prepare()
        self.profile = None
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.job["id"])
                .values(
                    request={
                        k: v
                        for k, v in self.job["request"].items()
                        if k != "deployment_profile_id"
                    }
                )
            )
        self.assert_blocked("matching_profile_unavailable")

    def test_provider_execution_unconfirmed_does_not_use_old_ready_slot(self):
        self.prepare()
        with self.repo.transaction() as conn:
            conn.execute(
                update(operator_nodes).values(
                    runtime_state="provider_execution_unverified"
                )
            )
        self.assert_blocked("managed_runtime_unavailable")

    def test_operator_summary_counts_claimable_slots_and_history_preserves_records(
        self,
    ):
        self.prepare()
        self.assertEqual(self.service.state(self.actor)["summary"]["slots_ready"], 1)
        node = self.service.state(self.actor)["nodes"][0]
        self.assertEqual(node["record_group"], "current")
        self.service.node_command(
            self.actor,
            node["id"],
            {"expected_version": node["version"]},
            "stop-summary",
            "stop",
        )
        stopped = self.service.state(self.actor)
        self.assertEqual(stopped["summary"]["slots_ready"], 0)
        self.assertEqual(stopped["nodes"][0]["record_group"], "current")
        self.assertEqual(
            self.repo.get_job(self.scope, self.job["id"])["status"], "queued"
        )
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(state="destroyed"))
        archived = self.service.state(self.actor)
        self.assertEqual(archived["nodes"][0]["record_group"], "history")
        self.assertEqual(archived["summary"]["slots_ready"], 0)
        self.assertEqual(
            self.repo.get_job(self.scope, self.job["id"])["status"], "queued"
        )
        with self.repo.engine.connect() as conn:
            self.assertTrue(list(conn.execute(select(budget_reservations)).mappings()))

    def test_late_guard_rechecks_proof_and_preserves_collection(self):
        self.prepare()
        self.assertEqual(self.availability()["state"], "ready")
        self.assertEqual(
            self.control.queued_diagnostic(self.job)["reason_code"],
            "awaiting_worker_claim",
        )
        claim = self.control.claim(self.worker["id"], self.binding.pool)
        self.assertIsNotNone(claim)
        self.assertTrue(self.control.submission_allowed(claim.job))
        self.now += 31
        self.assertFalse(self.control.submission_allowed(claim.job))
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.job["id"])
                .values(
                    status="collecting", lease_worker_id=None, lease_expires_at=None
                )
            )
        collection = self.control.claim(
            self.worker["id"], self.binding.pool, purpose="collect"
        )
        self.assertIsNotNone(collection)
        self.assertEqual(collection.job["id"], self.job["id"])
        self.assertEqual(len(self.provider.creates), 1)

    def test_consumer_shutdown_withdraws_only_exact_fence_incarnation_observation(self):
        import copy
        from types import SimpleNamespace
        from studio_platform.postgres_dispatch import withdraw_consumer

        self.prepare_dstack_lifetime()

        def read():
            with self.repo.engine.connect() as c:
                return dict(c.execute(select(operator_nodes)).mappings().one())

        node = read()
        proof = copy.deepcopy(node["payload"]["dstack"]["consumer_observation"])
        backend = SimpleNamespace(kind="wangp-worker", slot_key=node["intent_id"])
        # A newer consumer proof must not be withdrawn by the departing process.
        payload = copy.deepcopy(node["payload"])
        payload["dstack"]["consumer_observation"]["observed_at"] += 1
        with self.repo.transaction() as c:
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node["intent_id"])
                .values(payload=payload)
            )
        withdraw_consumer(self.repo, self.control, backend, self.worker["id"], proof)
        self.assertTrue(read()["payload"]["dstack"]["consumer_observation"]["ready"])
        fresh = copy.deepcopy(read()["payload"]["dstack"]["consumer_observation"])
        before = self.control.get(self.worker["id"])
        withdraw_consumer(self.repo, self.control, backend, self.worker["id"], fresh)
        self.assertFalse(read()["payload"]["dstack"]["consumer_observation"]["ready"])
        self.assertEqual(self.control.get(self.worker["id"]), before)

    def prepare_dstack_lifetime(self, *, keep_on="idle", hold_until=0):
        # New-path delivery is an explicit catalog-profile capability. Keep
        # this synthetic lifecycle fixture on the retained CPU-delivery profile.
        self.binding = replace(
            self.binding,
            runtime_profile_id="h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4",
        )
        self.registry.bindings[self.binding.binding_id] = self.binding
        self.chosen["runtime_profile_id"] = self.binding.runtime_profile_id
        self.prepare(duration=1800)
        with self.repo.transaction() as conn:
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            payload = {
                **node["payload"],
                "capacity_backend": "dstack-v1",
                "hard_deadline": None,
                "tenant_id": self.scope.tenant_id,
                "owner_id": self.scope.owner_id,
                "project_id": self.scope.project_id,
                "hourly_cost_microusd": 360000,
                "extra_reservation_microusd": 0,
                "observation_fresh_seconds": 30,
                "lifecycle": {
                    "version": 2,
                    "idle_shutdown_seconds": 600,
                    "keep_on": keep_on,
                    "hold_until": hold_until,
                    "reservation_seconds": 3600,
                    "budget_account_ids": ["owner-budget"],
                },
                "request": {
                    "profile_id": self.profile,
                    "mode": "fl",
                    "lifecycle_version": 2,
                    "model_id": self.binding.model_id,
                    "configuration_id": self.binding.configuration_id,
                    "manifest_digest": self.binding.engine_manifest_digest,
                },
                "dstack": {
                    "run_id": "original-run",
                    "runtime_incarnation": "c" * 32,
                    "lifecycle_version": 2,
                    "profile_id": self.profile,
                    "mode": "fl",
                    "manifest_digest": self.binding.engine_manifest_digest,
                    "configuration_id": self.binding.configuration_id,
                    "model_id": self.binding.model_id,
                    "first_runtime_ready_at": self.now,
                    "consumer_observation": {
                        "ready": True,
                        "worker_id": self.worker["id"],
                        "observed_at": self.now,
                        "worker_fence": self.worker["fence"],
                        "instance_id": self.worker["instance_id"],
                        "runtime_incarnation": "c" * 32,
                    },
                },
                "dstack_observation": {
                    "run_id": "original-run",
                    "status": "running",
                    "instance_id": self.intent["provider_instance_id"],
                    "observed_at": self.now,
                },
            }
            conn.execute(update(operator_nodes).values(payload=payload))
            conn.execute(update(instance_intents).values(hard_deadline=None))
            spec = {
                **self.worker["spec"],
                "dispatch_backend": "hatchet-v1",
                "output_delivery": "native-frames-v1",
            }
            conn.execute(
                update(registered_workers).values(
                    spec=spec, spec_hash=request_hash(spec)
                )
            )
            plan = {
                **self.job["execution_plan"],
                "dispatch_backend": "hatchet-v1",
                "output_delivery": "native-frames-v1",
                "deployment_profile_id": self.profile,
            }
            stored = {
                **self.job["request"],
                "request": {**self.job["request"]["request"], "mode": "fl"},
            }
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.job["id"])
                .values(execution_plan=plan, request=stored)
            )
            self.job = dict(
                conn.execute(select(jobs).where(jobs.c.id == self.job["id"]))
                .mappings()
                .one()
            )
        self.dispatch = "hatchet-v1"
        self.output_delivery = "native-frames-v1"

    def refresh_dstack(self):
        with self.repo.transaction() as conn:
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            payload = node["payload"]
            payload["dstack_observation"]["observed_at"] = self.now
            payload["dstack"]["consumer_observation"].update(
                observed_at=self.now,
                worker_fence=self.control.get(self.worker["id"])["fence"],
            )
            conn.execute(update(operator_nodes).values(payload=payload))
            conn.execute(
                update(registered_workers).values(
                    updated_at=self.now, expires_at=self.now + 90
                )
            )

    def test_idle_policy_allows_long_job_and_late_guard_while_original_claim_is_active(
        self,
    ):
        self.prepare_dstack_lifetime()
        self.assertEqual(self.counts()["ready"], 1)
        self.assertTrue(self.availability()["available"])
        claim = self.control.claim(self.worker["id"], self.binding.pool)
        self.assertIsNotNone(claim)
        self.now += 650
        self.refresh_dstack()
        self.assertTrue(self.control.submission_allowed(claim.job))
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"])["attempt_no"], 1)

    def test_idle_expiry_and_finite_hold_use_same_advisory_and_claim_gate(self):
        for keep_on, hold in (("idle", 0), ("until", self.now + 100)):
            with self.subTest(keep_on=keep_on):
                # Each assertion uses an isolated fixture created by unittest.
                if keep_on == "idle":
                    self.prepare_dstack_lifetime()
                    with self.repo.transaction() as conn:
                        conn.execute(
                            update(jobs)
                            .where(jobs.c.id == self.job["id"])
                            .values(status="cancelled")
                        )
                    self.now += 600
                else:
                    with self.repo.transaction() as conn:
                        node = dict(
                            conn.execute(select(operator_nodes)).mappings().one()
                        )
                        payload = node["payload"]
                        payload["lifecycle"].update(
                            keep_on=keep_on, hold_until=self.now + 100
                        )
                        conn.execute(update(operator_nodes).values(payload=payload))
                    self.now += 699
                    self.refresh_dstack()
                    self.assertTrue(self.availability()["available"])
                    self.now += 1
                self.refresh_dstack()
                self.assertEqual(self.counts()["ready"], 0)
                self.assertIn("worker_idle_expired", self.counts()["reason_counts"])
                self.assertEqual(
                    self.availability()["reason_code"], "worker_idle_expired"
                )
                self.assertIsNone(
                    self.control.claim(self.worker["id"], self.binding.pool)
                )

    def test_matching_accepted_queue_pauses_idle_before_first_claim(self):
        self.prepare_dstack_lifetime()
        self.now += 650
        self.refresh_dstack()
        from studio_platform.worker_admission import matching_dstack_queued_jobs

        for status in ("queued", "waiting_capacity"):
            with self.repo.transaction() as conn:
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == self.job["id"])
                    .values(status=status)
                )
                intent = dict(conn.execute(select(instance_intents)).mappings().one())
                node = dict(conn.execute(select(operator_nodes)).mappings().one())
                self.assertEqual(
                    [
                        job["id"]
                        for job in matching_dstack_queued_jobs(conn, intent, node)
                    ],
                    [self.job["id"]],
                )
            self.assertEqual(self.counts()["ready"], 1)
            self.assertTrue(self.availability()["available"])
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == self.job["id"]).values(status="queued")
            )
        self.assertIsNotNone(self.control.claim(self.worker["id"], self.binding.pool))

    def test_unrelated_or_unaccepted_queue_does_not_keep_owned_run_on(self):
        self.prepare_dstack_lifetime()
        self.now += 650
        self.refresh_dstack()
        from studio_platform.worker_admission import (
            matching_dstack_queued_jobs,
            _dstack_idle_reason,
        )

        original = self.job
        changes = (
            {"tenant_id": "another-tenant"},
            {"owner_id": "another-owner"},
            {"project_id": "another-project"},
            {"pool": "another-pool"},
            {"status": "planned"},
            {"attempt_no": 1},
            {"lease_expires_at": self.now + 30},
            {
                "request": {
                    **original["request"],
                    "deployment_profile_id": "another-profile",
                }
            },
            {"request": {**original["request"], "recipe_id": "h3-base-ref2va-v1"}},
            {
                "request": {
                    **original["request"],
                    "request": {
                        **original["request"]["request"],
                        "model": "another-model",
                    },
                }
            },
            {
                "request": {
                    **original["request"],
                    "request": {**original["request"]["request"], "mode": "ref"},
                }
            },
            *(
                {"execution_plan": {**original["execution_plan"], field: value}}
                for field, value in (
                    ("configuration_id", "another-config"),
                    ("engine_manifest_digest", "f" * 64),
                    ("dispatch_backend", "legacy"),
                    ("deployment_profile_id", "another-profile"),
                    ("enabled", False),
                    ("output_delivery", ""),
                )
            ),
        )
        for change in changes:
            with self.subTest(change=change), self.repo.transaction() as conn:
                saved = {field: original[field] for field in change}
                conn.execute(
                    update(jobs).where(jobs.c.id == original["id"]).values(**change)
                )
                intent = dict(conn.execute(select(instance_intents)).mappings().one())
                node = dict(conn.execute(select(operator_nodes)).mappings().one())
                self.assertEqual(matching_dstack_queued_jobs(conn, intent, node), ())
                self.assertEqual(
                    _dstack_idle_reason(conn, intent, node, self.now),
                    "worker_idle_expired",
                )
                conn.execute(
                    update(jobs).where(jobs.c.id == original["id"]).values(**saved)
                )
        with self.repo.engine.connect() as conn:
            intent = dict(conn.execute(select(instance_intents)).mappings().one())
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            self.assertEqual(
                matching_dstack_queued_jobs(
                    conn, intent, {**node, "desired_state": "stopped"}
                ),
                (),
            )

    def test_until_manual_stop_is_limited_by_funds_not_elapsed_fixed_window(self):
        self.prepare_dstack_lifetime(keep_on="until_stopped")
        self.now += 5000
        self.refresh_dstack()
        self.assertTrue(self.availability()["available"])
        self.assertIsNotNone(self.control.claim(self.worker["id"], self.binding.pool))
        self.now += 3000
        self.refresh_dstack()
        with self.repo.engine.connect() as conn:
            from studio_platform.worker_admission import worker_window_reason

            worker = dict(conn.execute(select(registered_workers)).mappings().one())
            self.assertEqual(
                worker_window_reason(
                    conn,
                    worker,
                    self.now,
                    expected_runtime_s=1800,
                    deployment_profile_id=self.profile,
                ),
                "job_exceeds_spend_runway",
            )

    def test_missing_v2_or_mismatched_reservation_fails_closed_without_new_attempt(
        self,
    ):
        self.prepare_dstack_lifetime()
        with self.repo.transaction() as conn:
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            original = node["payload"]
            payload = {**original, "lifecycle": {**original["lifecycle"], "version": 1}}
            conn.execute(update(operator_nodes).values(payload=payload))
        self.assert_blocked("managed_dstack_lifecycle_unconfirmed")
        with self.repo.transaction() as conn:
            conn.execute(update(operator_nodes).values(payload=original))
            conn.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_type == "instance")
                .values(amount_microusd=self.intent["reserved_cost_microusd"] - 1)
            )
        self.assert_blocked("managed_dstack_budget_unconfirmed")

    def test_new_dstack_preparation_is_visible_without_a_legacy_registry_binding(self):
        self.prepare_dstack_lifetime()
        from studio_platform.operator_capacity import OperatorRegistry

        self.registry = OperatorRegistry()
        with self.repo.transaction() as conn:
            conn.execute(delete(operator_heartbeats))
            conn.execute(update(registered_workers).values(state="retired"))
            conn.execute(update(operator_nodes).values(runtime_state="preparing"))
        self.assertEqual(
            self.availability()["reason_code"], "matching_capacity_starting"
        )
        self.now += 31
        self.assertEqual(
            self.availability()["reason_code"], "capacity_readiness_unconfirmed"
        )
        self.now -= 31
        with self.repo.transaction() as conn:
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            payload = node["payload"]
            payload["dstack"]["manifest_digest"] = "f" * 64
            conn.execute(update(operator_nodes).values(payload=payload))
        self.assertEqual(
            self.availability()["reason_code"], "capacity_readiness_unconfirmed"
        )

    def test_malformed_new_lifecycle_has_safe_static_diagnosis(self):
        self.prepare_dstack_lifetime()
        with self.repo.transaction() as conn:
            node = dict(conn.execute(select(operator_nodes)).mappings().one())
            payload = node["payload"]
            payload["lifecycle"]["keep_on"] = ["until_stopped"]
            conn.execute(update(operator_nodes).values(payload=payload))
        self.assert_blocked("managed_dstack_lifecycle_unconfirmed")

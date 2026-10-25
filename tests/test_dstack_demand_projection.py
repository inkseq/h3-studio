"""Existing native telemetry and original start fences; no provider requests."""

import copy
import uuid
from unittest.mock import patch

from sqlalchemy import func, insert, select, update

from studio_platform.dstack_demand import (
    node_stage,
    job_wait_stage,
    eligible_jobs,
    summary,
)
from studio_platform.dstack_operator import DstackOperator, OperatorError
from studio_platform.dstack_workspace import DstackWorkspace
from studio_platform.repository import (
    Scope,
    BudgetExceeded,
    attempts,
    jobs,
    instance_intents,
    request_hash,
)
from test_platform_repository import LedgerCase
import test_dstack_operator as original


class DemandProjectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        original.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.config["project"] = "sixnine"
        self.config["policy"].update(
            lifecycle_version=2,
            reservation_seconds=3600,
            expires_at=None,
            demand_start={"profile_ids": ["5090-fl"]},
        )
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        profile = self.service.profiles["5090-fl"]
        self.policy = {
            "enabled": True,
            "backend": "wangp-worker",
            "dispatch_backend": "hatchet-v1",
            "pool": profile["pool"],
            "configuration_id": profile["configuration_id"],
            "model_id": profile["model_id"],
            "engine_manifest_digest": profile["manifest_digest"],
            "qualification": {
                "status": "accepted",
                "verified_at": 1.0,
                "expires_at": None,
            },
            "reservation": {"expected_runtime_s": 1800, "expires_at": None},
            "envelope": {"max_duration_seconds": 4.0},
        }
        selected = patch(
            "studio_platform.execution_profiles.selected_policy",
            return_value=self.policy,
        )
        selected.start()
        self.addCleanup(selected.stop)

    def preview(self):
        return self.service.preview(self.owner, {"profile_id": "5090-fl"})

    def native_job(self, mode, *, status='queued', scope=None):
        scope = scope or self.scope
        profile = self.service.profiles['5090-fl']
        plan = self.repo.create_plan(
            scope,
            {'request': {'mode': mode, 'model': profile['model_id']}},
            dict(
                backend='wangp-worker',
                dispatch_backend='hatchet-v1',
                pool=profile['pool'],
                configuration_id=profile['configuration_id'],
                policy_hash=request_hash(self.policy),
                expected_runtime_s=120,
            ),
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        created = self.repo.create_job(scope, plan['id'], uuid.uuid4().hex)
        if status != 'queued':
            # Exercise retained blocked/terminal history in a local fixture;
            # counting it grants no legacy capacity approval or new submission.
            with self.repo.transaction() as connection:
                connection.execute(
                    update(jobs).where(jobs.c.id == created['id']).values(status=status)
                )
        return self.repo.get_job(scope, created['id'])

    def test_eligible_mirrors_current_attempt_claim_guard_without_mutating_history(
        self,
    ):
        job = self.native_job('fl')
        profile = self.service.profiles['5090-fl']
        previous = str(uuid.uuid4())
        with (
            patch(
                'studio_platform.execution_policy.ExecutionPolicies.submission_allowed',
                return_value=True,
            ),
            patch('studio_platform.dstack_demand._profile_for', return_value=profile),
        ):
            with self.repo.engine.connect() as connection:
                self.assertEqual(
                    len(eligible_jobs(self.service, connection, self.now)), 1
                )
            with self.repo.transaction() as connection:
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job['id'])
                    .values(current_attempt_id=previous)
                )
            with self.repo.engine.connect() as connection:
                self.assertEqual(eligible_jobs(self.service, connection, self.now), [])
            with self.repo.transaction() as connection:
                connection.execute(
                    insert(attempts).values(
                        id=previous,
                        job_id=job['id'],
                        number=1,
                        status='failed',
                        fence=1,
                        worker_id='old-worker',
                        created_at=self.now,
                        updated_at=self.now,
                        upstream_stopped=1,
                    )
                )
            for submitted, upstream in (
                (self.now, None),
                (None, 'old-task'),
                (None, None),
            ):
                with self.subTest(submitted=submitted, upstream=upstream):
                    with self.repo.transaction() as connection:
                        connection.execute(
                            update(attempts)
                            .where(attempts.c.id == previous)
                            .values(
                                submission_started_at=submitted,
                                upstream_task_id=upstream,
                            )
                        )
                    before = self.repo.get_job(self.scope, job['id'])
                    with self.repo.engine.connect() as connection:
                        found = eligible_jobs(self.service, connection, self.now)
                    self.assertEqual(
                        len(found), int(submitted is None and upstream is None)
                    )
                    self.assertEqual(self.repo.get_job(self.scope, job['id']), before)

    def test_fenced_lost_attempt_allows_same_job_demand_without_fake_stop_proof(self):
        from studio_platform.queue import TaskQueue

        job = self.native_job('fl')
        queue = TaskQueue(self.repo)
        claim = queue.claim(
            'original-worker', job['pool'], dispatch_backend='hatchet-v1'
        )
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, 'original-upstream')
        queue.reclaim_capacity(
            self.scope,
            job['id'],
            expected_attempt_id=claim.lease.attempt_id,
            expected_fence=claim.lease.fence,
            reason='capacity_owner_recovery',
            evidence_id='owner-proof',
        )
        profile = self.service.profiles['5090-fl']
        with (
            patch(
                'studio_platform.execution_policy.ExecutionPolicies.submission_allowed',
                return_value=True,
            ),
            patch('studio_platform.dstack_demand._profile_for', return_value=profile),
        ):
            with self.repo.engine.connect() as connection:
                result = eligible_jobs(self.service, connection, self.now)
                old = (
                    connection.execute(
                        select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual([row['id'] for row, _ in result], [job['id']])
            self.assertEqual(old['upstream_stopped'], 0)
            self.assertIsNone(old['actual_cost_microusd'])
            # A label without its matching original authority must never earn a rental.
            with self.repo.transaction() as connection:
                connection.execute(
                    update(jobs).where(jobs.c.id == job['id']).values(result={})
                )
            with self.repo.engine.connect() as connection:
                self.assertEqual(eligible_jobs(self.service, connection, self.now), [])

    def test_admin_counts_all_native_waiting_work_even_when_not_qualified(self):
        self.native_job('fl')
        self.native_job('ref', status='waiting_capacity', scope=self.other)
        self.native_job('fl', status='cancelled')
        self.native_job('ref', scope=Scope('other-tenant', 'other', 'project'))
        self.job()  # A CPU/non-native job is not a GPU queue count.
        with (
            patch(
                'studio_platform.dstack_demand_policy.read_runtime',
                return_value={'enabled': True, 'revision': 'test'},
            ),
            patch('studio_platform.dstack_demand.eligible_jobs', return_value=[]),
            self.repo.engine.connect() as connection,
        ):
            value = summary(self.service, connection, self.now)
        self.assertEqual(value['queued_generation_jobs'], 2)
        self.assertEqual(value['queued_by_mode'], {'fl': 1, 'ref': 1})
        self.assertEqual(value['blocked_reason'], 'no_qualified_profile')

    def test_admin_empty_native_queue_is_distinct_from_blocked_work(self):
        self.job()
        with (
            patch(
                'studio_platform.dstack_demand_policy.read_runtime',
                return_value={'enabled': True, 'revision': 'test'},
            ),
            patch('studio_platform.dstack_demand.eligible_jobs', return_value=[]),
            self.repo.engine.connect() as connection,
        ):
            value = summary(self.service, connection, self.now)
        self.assertEqual(value['queued_generation_jobs'], 0)
        self.assertEqual(value['blocked_reason'], 'no_eligible_work')

    def test_null_authority_is_not_an_infinite_preview_or_machine_deadline(self):
        value = self.preview()
        self.assertEqual(value["expires_at"], self.now + 120)
        self.assertIsNone(value["hard_deadline"])
        self.now += 121
        with self.assertRaisesRegex(OperatorError, "dstack_preview_expired"):
            self.service.start(
                self.owner, {"preview_id": value["preview_id"]}, "expired"
            )
        self.assertEqual(self.client.applied, [])

    def test_manual_parallel_starts_also_obey_hard_two_when_configured_limit_is_three(
        self,
    ):
        previews = [self.preview() for _ in range(6)]

        def start(index):
            try:
                return self.service.start(
                    self.owner,
                    {"preview_id": previews[index]["preview_id"]},
                    "manual-" + str(index),
                )
            except BudgetExceeded as error:
                self.assertEqual(str(error), "global_capacity_exceeded")
                return None

        values = self.parallel(start, count=6)
        self.assertEqual(sum(value is not None for value in values), 2)
        self.assertEqual(len(self.client.applied), 2)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(instance_intents)), 2
            )

    def test_cancel_after_auto_preview_refuses_start_without_reserving_machine(self):
        work = self.job()
        work["execution_plan"]["policy_hash"] = request_hash(self.policy)
        switch = DstackWorkspace(self.service)
        switch.set_demand_start(
            self.owner,
            {"expected_revision": None, "enabled": True, "reason": "test demand"},
            "on",
        )
        profile = self.service.profiles["5090-fl"]
        with patch(
            "studio_platform.dstack_demand.eligible_jobs",
            return_value=[(work, profile)],
        ):
            preview = self.service._preview_for_owner(
                "superdan", {"profile_id": "5090-fl"}, demand_job_id=work["id"]
            )
            self.repo.request_cancel(self.scope, work["id"])
            with self.assertRaisesRegex(OperatorError, "dstack_demand_changed"):
                self.service._start_for_owner(
                    "superdan",
                    "service:dstack-demand",
                    {"preview_id": preview["preview_id"]},
                    "cancelled-demand",
                    demand_job_id=work["id"],
                )
        self.assertEqual(self.client.applied, [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(instance_intents)), 0
            )
        self.assertEqual(
            self.repo.get_job(self.scope, work["id"])["status"], "cancelled"
        )

    def test_real_bootstrap_telemetry_maps_neutral_download_and_separate_model_load(
        self,
    ):
        node = {
            "runtime_state": "starting",
            "desired_state": "running",
            "payload": {"dstack": {"observed_at": self.now, "preparation": {}}},
        }
        for phase in (
            "model_download",
            "download_file",
            "model_download_waiting_for_shared_cache",
        ):
            node["payload"]["dstack"]["preparation"] = {"phase": phase, "model": None}
            self.assertEqual(node_stage(node, self.now), "downloading")
        node["payload"]["dstack"]["preparation"] = {
            "phase": "provisioning",
            "model": None,
        }
        self.assertEqual(node_stage(node, self.now), "machine_starting")
        for preparation in (
            {"phase": "model_load"},
            {"phase": "runtime_start", "model": {"state": "loading"}},
        ):
            node["payload"]["dstack"]["preparation"] = preparation
            self.assertEqual(node_stage(node, self.now), "loading")
        node["payload"]["dstack"]["observed_at"] = self.now - 121
        self.assertEqual(node_stage(node, self.now), "unknown")

    def test_wait_hint_does_not_borrow_other_mode_or_other_configuration(self):
        job = self.job()
        job["request"] = {
            "deployment_profile_id": original.PROFILE,
            "request": {"mode": "ref", "model": "same-model"},
        }
        job["execution_plan"].update(
            pool="native-pool",
            model_id="same-model",
            configuration_id="right-config",
            engine_manifest_digest="a" * 64,
        )
        node = {
            "pool": "native-pool",
            "runtime_state": "starting",
            "desired_state": "running",
            "payload": {
                "dstack": {
                    "profile_id": original.PROFILE,
                    "mode": "fl",
                    "model_id": "same-model",
                    "configuration_id": "right-config",
                    "manifest_digest": "a" * 64,
                    "preparation": {"phase": "model_load"},
                }
            },
        }
        before = copy.deepcopy(job)
        with (
            self.repo.engine.connect() as connection,
            patch("studio_platform.dstack_demand._nodes", return_value=[node]),
        ):
            self.assertEqual(
                job_wait_stage(connection, job, self.now), "queued_no_machine"
            )
            node["payload"]["dstack"]["mode"] = "ref"
            self.assertEqual(job_wait_stage(connection, job, self.now), "loading")
            node["payload"]["dstack"]["configuration_id"] = "wrong-config"
            self.assertEqual(
                job_wait_stage(connection, job, self.now), "queued_no_machine"
            )
        self.assertEqual(job, before)

    def test_admin_counts_exclude_only_canonical_terminal_history_without_mutating_it(
        self,
    ):
        nodes = [
            {
                "intent_id": str(uuid.uuid4()),
                "runtime_state": "creation_unknown",
                "desired_state": "stopped",
                "payload": {"dstack": {"billing_state": "unsettled"}},
            }
            for _ in range(3)
        ]
        before = copy.deepcopy(nodes)
        witness = {
            "observations": [
                {"intent_id": n["intent_id"], "counted": i == 2}
                for i, n in enumerate(nodes)
            ]
        }
        with (
            patch("studio_platform.dstack_demand._nodes", return_value=nodes),
            patch.object(self.service, "_live_capacity_usage", return_value=witness),
            self.repo.engine.connect() as connection,
        ):
            value = summary(self.service, connection, self.now)
        self.assertEqual(value["uncertain_machines"], 1)
        self.assertEqual(value["running_machines"], 0)
        self.assertEqual(value["starting_machines"], 0)
        self.assertEqual(nodes, before)

    def test_admin_capacity_read_failure_does_not_trust_a_stopped_marker(self):
        node = {
            "intent_id": str(uuid.uuid4()),
            "runtime_state": "stopped",
            "desired_state": "stopped",
            "payload": {"dstack": {"billing_state": "unsettled"}},
        }
        with (
            patch("studio_platform.dstack_demand._nodes", return_value=[node]),
            patch.object(self.service, "_live_capacity_usage", side_effect=OSError),
            self.repo.engine.connect() as connection,
        ):
            value = summary(self.service, connection, self.now)
        self.assertEqual(value["uncertain_machines"], 1)
        self.assertEqual(node["runtime_state"], "stopped")

    def test_admin_fresh_ready_hint_cannot_mask_live_uncertainty(self):
        node = {
            "intent_id": str(uuid.uuid4()),
            "runtime_state": "ready",
            "desired_state": "running",
            "payload": {"dstack": {"observed_at": self.now}},
        }
        before = copy.deepcopy(node)
        for reason in (
            "dstack_capacity_unconfirmed",
            "dstack_terminal_provider_unconfirmed",
            None,
        ):
            with self.subTest(reason=reason):
                witness = {
                    "observations": [
                        {
                            "intent_id": node["intent_id"],
                            "counted": True,
                            "reason_code": reason,
                        }
                    ]
                    if reason is not None
                    else []
                }
                with (
                    patch("studio_platform.dstack_demand._nodes", return_value=[node]),
                    patch.object(
                        self.service, "_live_capacity_usage", return_value=witness
                    ),
                    self.repo.engine.connect() as connection,
                ):
                    value = summary(self.service, connection, self.now)
                self.assertEqual(value["uncertain_machines"], 1)
                self.assertEqual(value["running_machines"], 0)
                self.assertEqual(node, before)

"""Capacity lifecycle failures across real adapters, fake providers and temp ledgers."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

from studio_platform.dstack_capacity import (
    DstackCapacity,
    DstackError,
    request_from_document,
)
from studio_platform.dstack_operator import DstackOperator
from studio_platform.modal_dialout import ModalDialoutClient
from studio_platform.repository import (
    BudgetExceeded,
    budget_accounts,
    budget_reservations,
    instance_intents,
)
from studio_platform.operator_capacity import operator_nodes
from studio_platform.runpod_serverless import RoutedClient, RunPodServerlessClient
import test_dstack_operator as operator_cases
from test_modal_dialout import FakeModal
from test_platform_repository import LedgerCase
from test_runpod_serverless import FakeRunPod, RUN, spec


class DialoutCapacityFailures(LedgerCase):
    def setUp(self):
        super().setUp()
        operator_cases.DstackOperatorTests._setup_dstack(self)
        self.native = self.client
        self.modal = FakeModal()
        self.runpod = FakeRunPod()
        self.http = httpx.Client(transport=httpx.MockTransport(self.runpod))
        self.addCleanup(self.http.close)
        common = {
            "gpu_name": "RTX5090",
            "gpu_memory_gib": 32,
            "cpus": 12,
            "memory_gib": 96,
            "disk_gib": 200,
            "hourly_microusd": 750000,
        }
        self.rp = RunPodServerlessClient(
            {
                **common,
                "api_key": "k" * 32,
                "endpoint_id": "abc123",
                "state_dir": str(Path(self.temp.name) / "runpod"),
            },
            "sixnine",
            http=self.http,
            clock=self.repo.clock,
        )
        self.md = ModalDialoutClient(
            {
                **common,
                "token_id": "ak-" + "a" * 20,
                "token_secret": "as-" + "b" * 20,
                "app_name": "test-dialout",
                "image": "example/native@sha256:" + "a" * 64,
                "volume_id": "vo-abc123",
                "gpu": "RTX-PRO-6000",
                "state_dir": str(Path(self.temp.name) / "modal"),
            },
            "sixnine",
            sdk=self.modal,
            clock=self.repo.clock,
        )
        self.routed = RoutedClient(self.native, self.rp, self.md)

    def configure(self, backend):
        self.config["profiles"][0].update(backend=backend, transport="dialout")
        self.settings.public_origin = "https://staging.inkseq.com"
        self.capacity = DstackCapacity(self.routed, self.store, clock=self.repo.clock)
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        return self.md if backend == self.md.backend else self.rp

    def start(self):
        preview = self.service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        return self.service.start(
            self.owner, {"preview_id": preview["preview_id"]}, "start-1"
        )

    def assert_reserved(self, state="reserved"):
        with self.repo.engine.connect() as connection:
            reservation = (
                connection.execute(select(budget_reservations)).mappings().one()
            )
            account = connection.execute(select(budget_accounts)).mappings().one()
        self.assertEqual(reservation["state"], state)
        self.assertGreater(reservation["amount_microusd"], 0)
        self.assertIsNone(reservation["actual_cost_microusd"])
        self.assertEqual(
            account["reserved_microusd"],
            reservation["amount_microusd"] if state == "reserved" else 0,
        )
        self.assertEqual(account["spent_microusd"], 0)

    def assert_unknown_original(self, node, lane):
        original = self.store.load(node)
        self.assertEqual(original["backend"], lane.backend)
        with self.repo.engine.connect() as connection:
            payload = connection.execute(select(operator_nodes.c.payload)).scalar_one()
        request = request_from_document(payload["request"])
        for _ in range(2):
            value = self.capacity.start(request, self.config["ssh_public_key"])
            self.assertEqual(value["state"], "observation_unknown")
            self.assertEqual(
                value["reason_code"], "dstack_apply_requires_reconciliation"
            )
        record = lane._read(original["run_name"])
        self.assertIsNone(record["job_id"])
        self.assertEqual(self.native.applied, [])
        self.assert_reserved()

    def test_runpod_accepted_create_lost_reply_keeps_original_reservation(self):
        lane = self.configure(self.rp.backend)

        def lost(request):
            reply = self.runpod(request)
            if request.url.path.endswith("/run"):
                raise httpx.ReadTimeout("synthetic private response", request=request)
            return reply

        lane.http = httpx.Client(transport=httpx.MockTransport(lost))
        self.addCleanup(lane.http.close)
        node = self.start()["node_id"]
        self.assert_unknown_original(node, lane)
        self.assertEqual(len(self.runpod.jobs), 1)
        self.assertEqual(sum(path.endswith("/run") for _, path in self.runpod.calls), 1)
        self.assertEqual(self.modal.created, [])

    def test_modal_accepted_create_lost_reply_is_rejected_but_obligation_stays(self):
        lane = self.configure(self.md.backend)
        create = self.modal.Sandbox.create

        def lost(*args, **kwargs):
            create(*args, **kwargs)
            raise TimeoutError("synthetic private response")

        launch = lane._launch
        errors = []

        def capture(*args):
            try:
                return launch(*args)
            except DstackError as error:
                errors.append(str(error))
                raise

        with (
            patch.object(self.modal.Sandbox, "create", side_effect=lost),
            patch.object(lane, "_launch", side_effect=capture),
        ):
            node = self.start()["node_id"]
        self.assertEqual(errors, ["dstack_api_rejected"])
        self.assert_unknown_original(node, lane)
        self.assertEqual(len(self.modal.created), 1)
        self.assertEqual(self.runpod.calls, [])

    def test_a_process_without_the_modal_sdk_never_offers_the_modal_lane(self):
        # Staging, 2026-11-20: a manual start from the app, whose image has no
        # Modal SDK, was offered the Modal lane and held a capacity slot for
        # three hours as an unknown outcome (#1326).
        lane = self.configure(self.md.backend)
        app = ModalDialoutClient(lane.config, lane.project, clock=self.repo.clock)
        with patch("importlib.util.find_spec", return_value=None):
            self.assertFalse(app.launchable())
            plan = app.plan(spec(backends=[app.backend]))
        self.assertEqual(plan["job_plans"], [{"offers": []}])
        # Where the SDK is there (the controller), the lane offers as before.
        self.assertTrue(lane.launchable())
        self.assertEqual(
            len(lane.plan(spec(backends=[lane.backend]))["job_plans"][0]["offers"]),
            1,
        )

    def test_a_modal_start_that_fails_before_any_sandbox_call_ends_at_once(self):
        lane = self.configure(self.md.backend)
        with patch.object(
            self.modal.Client,
            "from_credentials",
            side_effect=ModuleNotFoundError("synthetic"),
        ):
            node = self.start()["node_id"]
        run_name = self.store.load(node)["run_name"]
        self.assertEqual(lane._read(run_name)["final"], {"status": "REFUSED"})
        observed = self.capacity.observe(node)
        self.assertEqual(
            (observed["dstack_status"], observed["lane_refused"]), ("failed", True)
        )
        self.assertEqual(self.modal.created, [])
        # Ended and its slot freed by the lane's own live list (#1328).
        self.now += 60
        self.capacity.observe_terminal(node)
        with self.repo.engine.connect() as connection:
            state = connection.execute(select(instance_intents.c.state)).scalar_one()
        self.assertEqual(state, "destroyed")

    def check_failed_journal_write(self, backend):
        lane = self.configure(backend)
        write = lane._write

        def failed(run_name, value, *, create):
            if not create:
                raise OSError("synthetic disk full")
            return write(run_name, value, create=create)

        with patch.object(lane, "_write", side_effect=failed):
            with self.assertRaisesRegex(OSError, "synthetic disk full"):
                self.start()
        with self.repo.engine.connect() as connection:
            node = connection.execute(
                select(budget_reservations.c.reference_id)
            ).scalar_one()
        self.assert_unknown_original(node, lane)
        self.assertEqual(
            sum(path.endswith("/run") for _, path in self.runpod.calls),
            int(lane is self.rp),
        )
        self.assertEqual(len(self.modal.created), int(lane is self.md))

    def test_runpod_after_create_journal_failure_never_launches_again(self):
        self.check_failed_journal_write(self.rp.backend)

    def test_modal_after_create_journal_failure_never_launches_again(self):
        self.check_failed_journal_write(self.md.backend)

    def test_exact_reserved_budget_cap_can_start(self):
        self.configure(self.rp.backend)
        preview = self.service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        amount = preview["reservation_microusd"]
        self.repo.configure_budget(
            "owner-budget",
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=amount,
        )
        self.service.start(self.owner, {"preview_id": preview["preview_id"]}, "start-1")
        self.assert_reserved()
        self.assertEqual(sum(path.endswith("/run") for _, path in self.runpod.calls), 1)

    def test_reserved_budget_one_micro_over_limit_never_starts(self):
        self.configure(self.rp.backend)
        preview = self.service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.repo.configure_budget(
            "owner-budget",
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=preview["reservation_microusd"] - 1,
        )
        with self.assertRaises(BudgetExceeded):
            self.service.start(
                self.owner, {"preview_id": preview["preview_id"]}, "start-1"
            )
        self.assertEqual(self.runpod.jobs, {})
        self.assertEqual(self.modal.created, [])
        self.assertEqual(self.native.applied, [])

    def test_both_provider_price_caps_include_equality_and_exclude_one_micro(self):
        for lane in (self.rp, self.md):
            with self.subTest(backend=lane.backend):
                for maximum, count in ((750000, 1), (749999, 0)):
                    offered = lane.plan(
                        spec(backends=[lane.backend], max_price=maximum / 1_000_000)
                    )
                    self.assertEqual(len(offered["job_plans"][0]["offers"]), count)

    def test_both_journals_preserve_exact_record_bytes_and_final_status_restart(self):
        for lane in (self.rp, self.md):
            with self.subTest(backend=lane.backend):
                value = spec(backends=[lane.backend])
                with patch("uuid.uuid4", return_value="fixed-run-id"):
                    lane.apply(value)
                job_id = "job-1" if lane is self.rp else "sb-1"
                expected = {
                    "run_name": RUN,
                    "run_id": "fixed-run-id",
                    "job_id": job_id,
                    "spec": value,
                    "created_at": self.now,
                }
                self.assertEqual(
                    lane._path(RUN).read_bytes(),
                    json.dumps(expected, sort_keys=True).encode(),
                )
                if lane is self.rp:
                    self.runpod.jobs[job_id].update(
                        status="COMPLETED", executionTime=1000
                    )
                else:
                    self.modal.running[job_id] = 0
                final = lane.get(run_name=RUN)
                self.assertEqual(final["status"], "done")
                # Reconstructing a process reads the original cached terminal value.
                kwargs = {"http": self.http} if lane is self.rp else {"sdk": self.modal}
                again = type(lane)(
                    lane.config, lane.project, clock=self.repo.clock, **kwargs
                )
                with patch.object(
                    again,
                    "_call" if lane is self.rp else "_modal",
                    side_effect=AssertionError("network"),
                ):
                    self.assertEqual(again.get(run_name=RUN), final)

    def check_lost_stop(self, backend, terminal):
        lane = self.configure(backend)
        node = self.start()["node_id"]
        if lane is self.rp:
            self.runpod.jobs["job-1"].update(status="IN_PROGRESS", workerId="w123")
        self.capacity.observe(node)
        self.service.node_command(self.owner, node, {}, "stop-1")
        cancel = lane._cancel
        calls = []

        def lost(job_id):
            calls.append(job_id)
            if terminal:
                cancel(job_id)
            raise DstackError("dstack_api_unavailable")

        with patch.object(lane, "_cancel", side_effect=lost):
            result = self.capacity.stop(node)
            self.assertEqual(result["state"], "removal_unknown")
            self.capacity.stop(node)
        self.assertEqual(len(calls), 1)
        if lane is self.md:
            self.assertTrue(lane._read(self.store.load(node)["run_name"])["cancelled"])
        self.assertEqual(self.native.stopped, [])
        self.assert_reserved("invoice_pending" if terminal else "reserved")

    def test_runpod_lost_stop_while_running_keeps_reservation(self):
        self.check_lost_stop(self.rp.backend, False)

    def test_modal_lost_stop_while_running_keeps_cancel_marker_and_reservation(self):
        self.check_lost_stop(self.md.backend, False)

    def test_runpod_lost_stop_after_exit_is_not_replayed_or_zero_billed(self):
        self.check_lost_stop(self.rp.backend, True)

    def test_modal_lost_stop_after_exit_is_not_replayed_or_zero_billed(self):
        self.check_lost_stop(self.md.backend, True)

    def ended_without_running(self, end):
        """A start whose job ended before any running read (#1328)."""
        lane = self.configure(self.rp.backend)
        node = self.start()["node_id"]
        self.capacity.observe(node)
        # Queued: no worker, so no instance id yet.
        self.assertIsNone(self.store.load(node).get("provider_instance_id"))
        end(lane, node)
        for _ in range(2):
            self.now += 60
            self.capacity.observe_terminal(node)
        binding = self.store.load(node)
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
        # Removal proven by the provider's own complete live list, as for a
        # job that ran: the reservation leaves "reserved" and the slot frees.
        self.assertEqual(intent["state"], "destroyed")
        self.assertEqual(
            binding["provider_removal_proof"]["provider_instance_id"],
            intent["provider_instance_id"],
        )
        # Not claimed free: the bill stays to come, as for any ended run.
        self.assert_reserved("invoice_pending")
        self.assertEqual(sum(path.endswith("/run") for _, path in self.runpod.calls), 1)
        return intent["provider_instance_id"]

    def test_a_job_that_never_got_a_worker_ends_and_releases_its_reservation(self):
        # Staging, 11-17 to 11-20: five such starts stayed "destroying" with
        # their reservations held, because no instance id was ever recorded.
        def no_worker(lane, node):
            self.runpod.workers = {
                "initializing": 0,
                "idle": 0,
                "ready": 0,
                "running": 0,
                "throttled": 1,
                "unhealthy": 0,
            }
            self.now += lane.NO_WORKER_SECONDS + 1
            self.assertTrue(self.capacity.observe(node)["lane_refused"])
            self.service.node_command(self.owner, node, {}, "stop-1")
            self.capacity.stop(node)
            self.now += 1
            self.capacity.observe_terminal(node)

        self.assertEqual(self.ended_without_running(no_worker), "job-job-1")

    def test_a_job_cancelled_from_the_queue_or_failed_ends_the_same_way(self):
        for status in ("CANCELLED", "FAILED"):
            with self.subTest(status=status):
                self.setUp()

                def ended(lane, node, status=status):
                    self.runpod.jobs["job-1"]["status"] = status
                    self.capacity.observe_terminal(node)

                self.assertEqual(self.ended_without_running(ended), "job-job-1")
                self.tearDown()

    def test_a_lost_submit_ends_once_no_job_of_it_can_exist(self):
        def lost(lane, node):
            # The create's answer never came: no job id in the journal.
            run_name = self.store.load(node)["run_name"]
            record = lane._read(run_name)
            record["job_id"] = None
            lane._write(run_name, record, create=False)
            self.runpod.jobs.clear()
            self.now += lane.UNBOUND_EXPIRY_SECONDS
            self.capacity.observe_terminal(node)

        identity = self.ended_without_running(lost)
        self.assertTrue(identity.startswith("unbound-"), identity)

    def test_unbound_first_lane_journal_does_not_fall_through_to_other_original(self):
        self.rp._write(
            RUN, {"run_name": RUN, "job_id": None, "run_id": "old"}, create=True
        )
        self.md.apply(spec(backends=[self.md.backend]))
        with self.assertRaisesRegex(
            DstackError, "dstack_apply_requires_reconciliation"
        ):
            self.routed.get(run_name=RUN)
        self.assertEqual(len(self.modal.created), 1)
        self.assertEqual(self.native.applied, [])

    def test_common_contract_reconcile_preserves_original_and_terminal_path(self):
        from studio_platform.dialout_capacity import DialoutCapacityClient

        lane = self.configure(self.rp.backend)
        self.assertIsInstance(self.md, DialoutCapacityClient)
        self.assertNotIsInstance(self.md, RunPodServerlessClient)
        node = self.start()["node_id"]
        before = self.capacity.observe(node)
        self.assertEqual(self.capacity.reconcile(node), before)
        self.assertIsNone(self.capacity.observe_terminal(node))
        self.assertEqual(sum(path.endswith("/run") for _, path in self.runpod.calls), 1)
        self.assertEqual(
            lane._read(self.store.load(node)["run_name"])["job_id"], "job-1"
        )
        self.assert_reserved()

    def test_provider_error_logs_only_safe_operation_category(self):
        self.configure(self.md.backend)
        with patch.object(
            self.modal.Sandbox,
            "create",
            side_effect=TimeoutError("PRIVATE-CREDENTIAL-CONTENT"),
        ):
            with self.assertLogs(
                "studio_platform.dialout_capacity", level="WARNING"
            ) as logs:
                node = self.start()["node_id"]
        text = "\n".join(logs.output)
        self.assertIn("operation=create", text)
        self.assertIn("code=dstack_api_rejected", text)
        self.assertIn("error_type=TimeoutError", text)
        self.assertNotIn("PRIVATE-CREDENTIAL-CONTENT", text)
        self.assert_unknown_original(node, self.md)

    def test_missing_provider_status_never_falls_through_to_another_lane(self):
        lane = self.configure(self.rp.backend)
        node = self.start()["node_id"]
        with patch.object(
            lane, "_status", side_effect=DstackError("dstack_api_unavailable")
        ):
            result = self.capacity.observe(node)
            self.assertEqual(result["state"], "observation_unknown")
            with self.assertRaisesRegex(DstackError, "api_unavailable"):
                self.routed.provider_inventory(lane.backend)
        self.assertEqual(self.native.applied, [])
        self.assertEqual(self.modal.created, [])
        self.assert_reserved()


if __name__ == "__main__":
    unittest.main()


class LanePriceTests(unittest.TestCase):
    def test_routed_client_reports_only_its_lanes_rates(self):
        from studio_platform.dialout_capacity import RoutedClient

        lane = SimpleNamespace(
            backend="modal-dialout", config={"hourly_microusd": 4_800_000}
        )
        routed = RoutedClient(SimpleNamespace(), lane)
        self.assertEqual(routed.lane_price("modal-dialout"), 4_800_000)
        self.assertIsNone(routed.lane_price("runpod"))
        self.assertIsNone(routed.lane_price("lium"))

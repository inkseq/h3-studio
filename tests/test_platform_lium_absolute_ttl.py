"""Absolute TTL contracts: fake HTTP and private temporary journals only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import httpx
import test_platform_lium_provider as provider_contracts

from studio_platform.lium_provider import LiumError
from studio_platform.rent_journal import RentJournal
from test_platform_lium_provider import TAG, POD, OTHER, EXECUTOR, launch, stamp


class AbsoluteTTLTests(unittest.TestCase):
    provider = provider_contracts.LiumProviderTests.provider
    tearDown = provider_contracts.LiumProviderTests.tearDown

    def setUp(self):
        provider_contracts.LiumProviderTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/"rents"
        self.detail = {"id": POD, "name": "sixnine-"+TAG, "status": "RUNNING", "created_at": stamp(900),
            "termination_hours": 2, "removal_scheduled_at": None,
            "executor": {"executor_ip_address": "8.8.8.8"}, "ports_mapping": {"22": 2022}}
        self.schedule = None
        self.before_schedule = None
        self.api.on_rent = self.rented
        self.api.hook = self.route

    def rented(self, request):
        self.api.pods = [self.detail]

    def route(self, request):
        if request.method == "GET" and request.url.path == "/api/pods/"+POD:
            return httpx.Response(200, json=self.detail)
        if request.method == "POST" and request.url.path.endswith("/schedule-removal"):
            if self.before_schedule:
                self.before_schedule()
            if self.schedule:
                return self.schedule(request)
            self.detail["removal_scheduled_at"] = json.loads(request.content)["removal_scheduled_at"]
            # The API need not return a success field. Only exact GET proves it.
            return httpx.Response(200, json={"message": "scheduled"})

    def start(self, **kwargs):
        provider = self.provider(journal_dir=self.directory)
        fact = provider.create_for_intent(TAG, launch(), hard_deadline=kwargs.get("hard_deadline", 8300),
            intent_created_at=kwargs.get("created_at", 900))
        self.assertEqual((fact.state, fact.instance_id), ("starting", POD))
        return provider

    def marker(self):
        return RentJournal(self.directory).read(TAG)

    def posts(self):
        return [v for v in self.api.calls if v[0] == "POST" and v[1].endswith("/schedule-removal")]

    def running(self, removed):
        self.detail.update(status="RUNNING", termination_hours=None, removal_scheduled_at=stamp(removed))

    def historical_pending_attempt(self, *, acknowledged, confirmed):
        # Pre-upgrade on-disk evidence, never a new request from current code.
        journal = RentJournal(self.directory)
        ttl = self.marker()["absolute_ttl"]
        ttl["attempts"] = [{"target": 8100, "started_at": self.now,
            "status": "PENDING", "acknowledged": acknowledged, "confirmed": confirmed}]
        with journal.ttl_lock(TAG):
            journal.update_ttl(TAG, ttl)

    def test_pending_is_unverified_until_first_running_schedule_after_restart(self):
        self.detail["status"] = "PENDING"
        provider = self.start()
        for _ in range(3):
            self.assertEqual(provider.reconcile(TAG, POD).state, "starting")
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                provider.lifetime(TAG, POD, local_created_at=900)
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                provider.ssh_connection(TAG, POD)
            self.now += 30
        ttl = self.marker()["absolute_ttl"]
        self.assertEqual(self.posts(), [])
        self.assertEqual(ttl["attempts"], [])
        self.assertEqual((ttl["instance_id"], ttl["provider_created_at"], ttl["deadline"]), (POD, 900, 8100))
        self.now += 1200
        self.running(9400)
        provider = self.provider(journal_dir=self.directory)
        self.assertEqual(provider.reconcile(TAG, POD).state, "running")
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["safe_deadline"], 7500)
        self.assertEqual(provider.ssh_connection(TAG, POD)["instance_id"], POD)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0][2], {"removal_scheduled_at": stamp(8100)})
        attempt = self.marker()["absolute_ttl"]["attempts"][0]
        self.assertEqual(attempt["status"], "RUNNING")
        self.assertTrue(attempt["acknowledged"] and attempt["confirmed"])
        self.assertEqual(sum(v[0] == "POST" and v[1].endswith("/rent") for v in self.api.calls), 1)

    def test_historical_unknown_pending_schedule_is_never_replayed(self):
        self.detail["status"] = "PENDING"
        self.start()
        self.historical_pending_attempt(acknowledged=False, confirmed=False)
        before = self.marker()["absolute_ttl"]
        provider = self.provider(journal_dir=self.directory)
        self.assertEqual(provider.reconcile(TAG, POD).state, "starting")
        self.running(9400)
        for _ in range(2):
            provider.reconcile(TAG, POD)
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(self.marker()["absolute_ttl"], before)
        self.assertEqual(self.posts(), [])
        # An exact GET can resolve the original unknown request; no new POST.
        self.running(8100)
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["safe_deadline"], 7500)
        attempt = self.marker()["absolute_ttl"]["attempts"][0]
        self.assertTrue(attempt["confirmed"])
        self.assertFalse(attempt["acknowledged"])
        self.assertEqual(self.posts(), [])

    def test_original_bound_is_durable_before_rent_and_schedule_post(self):
        def rented(request):
            ttl = self.marker()["absolute_ttl"]
            self.assertEqual((ttl["created_at"], ttl["deadline"], ttl["requested_hours"]), (900, 8100, 2))
            self.assertEqual(ttl["attempts"], [])
            self.api.pods = [self.detail]
        self.api.on_rent = rented
        def scheduling():
            attempt = self.marker()["absolute_ttl"]["attempts"][-1]
            self.assertFalse(attempt["acknowledged"])
            self.assertFalse(attempt["confirmed"])
            self.assertEqual(attempt["target"], 8100)
        self.before_schedule = scheduling
        self.start()
        ttl = self.marker()["absolute_ttl"]
        self.assertTrue(ttl["attempts"][0]["confirmed"])
        self.assertTrue(ttl["attempts"][0]["acknowledged"])
        self.assertEqual(self.posts()[0][2], {"removal_scheduled_at": stamp(8100)})

    def test_historical_acknowledged_but_unconfirmed_pending_schedule_stays_held(self):
        self.detail["status"] = "PENDING"
        self.start()
        self.historical_pending_attempt(acknowledged=True, confirmed=False)
        before = self.marker()["absolute_ttl"]
        self.running(9400)
        provider = self.provider(journal_dir=self.directory)
        for _ in range(2):
            provider.reconcile(TAG, POD)
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                provider.ssh_connection(TAG, POD)
        self.assertEqual(self.marker()["absolute_ttl"], before)
        self.assertEqual(self.posts(), [])

    def test_pending_past_original_deadline_never_schedules_or_extends(self):
        self.detail["status"] = "PENDING"
        provider = self.start()
        before = self.marker()["absolute_ttl"]
        self.now = 8101
        provider.reconcile(TAG, POD)
        self.running(16000)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(self.marker()["absolute_ttl"], before)
        self.assertEqual(self.posts(), [])

    def test_one_historical_confirmed_pending_to_running_correction_then_hold(self):
        self.detail.update(status="PENDING", removal_scheduled_at=stamp(8100))
        provider = self.start()
        self.historical_pending_attempt(acknowledged=True, confirmed=True)
        self.now += 1200
        self.running(9400)
        provider = self.provider(journal_dir=self.directory)
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["safe_deadline"], 7500)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.marker()["absolute_ttl"]["attempts"]), 2)
        self.assertEqual(self.marker()["absolute_ttl"]["deadline"], 8100)
        self.running(9500)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.ssh_connection(TAG, POD)
        self.assertEqual(len(self.posts()), 1)

    def test_earlier_observed_deadline_is_preserved_across_ready_overwrite(self):
        self.detail.update(status="PENDING", removal_scheduled_at=stamp(7000))
        provider = self.start()
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.marker()["absolute_ttl"]["effective_deadline"], 7000)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.running(9500)
        provider.ssh_connection(TAG, POD)
        self.assertEqual(self.posts()[0][2]["removal_scheduled_at"], stamp(7000))
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900)["safe_deadline"], 6400)

    def test_lost_schedule_response_that_was_applied_is_get_only_after_restart(self):
        def timeout(request):
            self.detail["removal_scheduled_at"] = json.loads(request.content)["removal_scheduled_at"]
            raise httpx.ReadTimeout("private upstream text", request=request)
        self.schedule = timeout
        self.start()
        provider = self.provider(journal_dir=self.directory)
        provider.reconcile(TAG, POD)
        self.assertTrue(self.marker()["absolute_ttl"]["attempts"][0]["confirmed"])
        self.assertFalse(self.marker()["absolute_ttl"]["attempts"][0]["acknowledged"])
        self.running(9500)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(len(self.posts()), 1)

    def test_lost_unapplied_response_and_http_failure_never_repeat_post(self):
        self.schedule = lambda req: httpx.Response(503, json={"message": "private upstream text"})
        self.start()
        provider = self.provider(journal_dir=self.directory)
        for _ in range(3):
            provider.reconcile(TAG, POD)
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed") as caught:
                provider.lifetime(TAG, POD, local_created_at=900)
            self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(self.posts()), 1)

    def test_confirmed_create_and_ttl_survive_lost_rent_response(self):
        def rent(request):
            self.api.pods = [self.detail]
            raise httpx.ReadTimeout("lost rental response", request=request)
        self.api.on_rent = rent
        provider = self.provider(journal_dir=self.directory)
        with self.assertRaises(LiumError):
            provider.create_for_intent(TAG, launch(), hard_deadline=8300, intent_created_at=900)
        self.assertEqual(self.marker()["phase"], "post_started")
        provider = self.provider(journal_dir=self.directory)
        self.assertEqual(provider.reconcile(TAG).instance_id, POD)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(sum(v[0] == "POST" and v[1].endswith("/rent") for v in self.api.calls), 1)
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            provider.create_for_intent(TAG, launch(), hard_deadline=8300, intent_created_at=900)

    def test_post_intent_survives_process_crash_and_no_new_post_is_dispatched(self):
        class Crash(BaseException): pass
        def crash(): raise Crash()
        self.before_schedule = crash
        with self.assertRaises(Crash): self.start()
        self.before_schedule = None
        provider = self.provider(journal_dir=self.directory)
        provider.reconcile(TAG, POD)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(len(self.posts()), 1)

    def test_naive_utc_fractional_seconds_and_consistent_offsets(self):
        self.detail["created_at"] = stamp(900.25).removesuffix("+00:00")
        def schedule(request):
            self.detail["removal_scheduled_at"] = json.loads(request.content)["removal_scheduled_at"].removesuffix("+00:00")
            return httpx.Response(200, json={})
        self.schedule = schedule
        provider = self.start(created_at=900.25)
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=900.25)["safe_deadline"], 7500.25)
        self.assertEqual(self.marker()["absolute_ttl"]["deadline"], 8100.25)

    def test_submicrosecond_database_clock_never_rounds_schedule_up(self):
        # datetime would normally round this target upward to .000001.
        created = 900.0000006
        self.detail["created_at"] = stamp(created)
        provider = self.start(created_at=created)
        ttl = self.marker()["absolute_ttl"]
        self.assertEqual(ttl["deadline"], created+7200)
        self.assertEqual(ttl["effective_deadline"], 8100)
        self.assertTrue(ttl["attempts"][0]["confirmed"])
        self.assertEqual(self.posts()[0][2]["removal_scheduled_at"], stamp(8100))
        self.assertEqual(provider.lifetime(TAG, POD, local_created_at=created)["safe_deadline"], 7500)

    def test_mixed_timezone_and_changed_creation_identity_refuse_schedule(self):
        self.detail.update(created_at=stamp(900).removesuffix("+00:00"), removal_scheduled_at=stamp(9000))
        provider = self.start()
        self.assertEqual(self.posts(), [])
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.detail.update(created_at=stamp(1500), removal_scheduled_at=None)
        provider.reconcile(TAG, POD)
        self.assertEqual(self.posts(), [])

    def test_invalid_hours_and_pod_identity_cannot_schedule(self):
        self.detail["termination_hours"] = True
        provider = self.start()
        self.assertEqual(self.posts(), [])
        for changes in ({"termination_hours": 3}, {"termination_hours": 2, "name": "sixnine-"+OTHER},
                        {"name": "sixnine-"+TAG, "id": OTHER}):
            self.detail.update(changes)
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(self.posts(), [])

    def test_legacy_journal_is_not_adopted_and_cannot_change_deadline(self):
        provider = self.provider(journal_dir=self.directory)
        provider.create(TAG, launch(), hard_deadline=8300)
        before = (self.directory/(TAG+".json")).read_bytes()
        self.running(900+4*3600+301)
        with self.assertRaisesRegex(LiumError, "lifetime_not_confirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(before, (self.directory/(TAG+".json")).read_bytes())
        self.assertEqual(self.posts(), [])

    def test_original_context_and_journal_bound_cannot_be_extended(self):
        provider = self.start()
        for wrong in (True, 901, 1000, float("nan")):
            with self.assertRaises(LiumError):
                provider.lifetime(TAG, POD, local_created_at=wrong)
        journal = RentJournal(self.directory)
        for field, value in (("created_at", 1000), ("deadline", 9000), ("effective_deadline", 9000),
                             ("hard_deadline", 10000), ("requested_hours", 4)):
            ttl = deepcopy(self.marker()["absolute_ttl"])
            ttl[field] = value
            with journal.ttl_lock(TAG), self.assertRaises(ValueError):
                journal.update_ttl(TAG, ttl)
        self.assertEqual(self.marker()["absolute_ttl"]["deadline"], 8100)

    def test_cross_provider_lock_prevents_concurrent_dispatch(self):
        other = self.provider(journal_dir=self.directory)
        def during_request():
            with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
                other._ensure_absolute_ttl(TAG, POD)
        self.before_schedule = during_request
        self.start()
        self.assertEqual(len(self.posts()), 1)

    def test_delayed_rent_acknowledgement_cannot_overwrite_ttl_intent(self):
        def rent(request):
            self.api.pods = [self.detail]
            raise httpx.ReadTimeout("lost rental response", request=request)
        self.api.on_rent = rent
        provider = self.provider(journal_dir=self.directory)
        with self.assertRaises(LiumError):
            provider.create_for_intent(TAG, launch(), hard_deadline=8300, intent_created_at=900)
        def schedule(request):
            with self.assertRaises(OSError):
                RentJournal(self.directory).save(TAG, "confirmed", executor_id=EXECUTOR, instance_id=POD)
            self.detail["removal_scheduled_at"] = json.loads(request.content)["removal_scheduled_at"]
            return httpx.Response(200, json={})
        self.schedule = schedule
        provider.reconcile(TAG, POD)
        ttl = self.marker()["absolute_ttl"]
        RentJournal(self.directory).save(TAG, "confirmed", executor_id=EXECUTOR, instance_id=POD)
        self.assertEqual(self.marker()["absolute_ttl"], ttl)
        self.assertEqual(len(self.posts()), 1)

    def test_initial_running_schedule_has_no_second_correction(self):
        self.running(9500)
        provider = self.start()
        self.assertEqual(len(self.posts()), 1)
        self.running(9600)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.ssh_connection(TAG, POD)
        self.assertEqual(len(self.posts()), 1)

    def test_bound_is_never_extended_when_original_time_has_expired(self):
        provider = self.start()
        self.now = 8101
        self.running(16000)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            provider.lifetime(TAG, POD, local_created_at=900)
        self.assertEqual(len(self.posts()), 1)

    def test_invalid_original_timestamp_refuses_before_rental(self):
        for value in (True, -1, 1001, float("nan"), float("inf"), "900"):
            with self.subTest(value=value), self.assertRaisesRegex(LiumError, "intent_creation_time"):
                self.provider(journal_dir=self.directory).create_for_intent(TAG, launch(),
                    hard_deadline=8300, intent_created_at=value)
        self.assertEqual(self.api.calls, [])

    def test_quarantined_or_wrong_local_instance_never_schedules(self):
        self.start()
        before = len(self.posts())
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            self.provider(journal_dir=self.directory)._ensure_absolute_ttl(TAG, OTHER)
        self.assertEqual(len(self.posts()), before)


class TTLCoordinatorTests(unittest.TestCase):
    def test_coordinator_passes_original_database_intent_time(self):
        from test_platform_scaler import ScalerTests
        fixture = ScalerTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        calls = []
        def bound(tag, launch, *, hard_deadline, intent_created_at):
            calls.append((tag, hard_deadline, intent_created_at))
            return fixture.provider.create(tag, launch, hard_deadline=hard_deadline)
        fixture.provider.create_for_intent = bound
        row = fixture.create()
        self.assertEqual(calls, [(row["id"], row["hard_deadline"], row["created_at"])])
        fixture.now += 121
        fixture.tick(demands=[])
        self.assertEqual(len(calls), 1)

    def test_ttl_hold_is_safe_visible_reason_and_does_not_construct_worker(self):
        from test_platform_production_scaler import FiniteTests
        fixture = FiniteTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        class HeldBoot:
            def __init__(self, *args, **kwargs): pass
            def tick(self, *args, **kwargs):
                raise LiumError("lium_absolute_ttl_unconfirmed")
            def children_done(self): return True
        fixture.controller.boot_factory = HeldBoot
        job = fixture.waiting()
        for _ in range(3):
            status = fixture.controller.tick()
            fixture.now += 16
        current = fixture.repo.get_job(fixture.scope, job["id"])
        self.assertEqual((current["status"], current["error_code"]),
            ("waiting_capacity", "capacity_provider_ttl_unconfirmed"))
        self.assertEqual(status["reason"], "provider_ttl_unconfirmed")
        self.assertEqual(len(fixture.provider.creates), 1)
        self.assertEqual(fixture.provider.destroys, [])
        self.assertGreater(fixture.repo.get_budget("finite-budget")["reserved_microusd"], 0)


if __name__ == "__main__":
    unittest.main()

"""Rent crash recovery uses only private temporary files and fake HTTP."""
import os
from pathlib import Path
import tempfile
import unittest

import httpx

from studio_platform.rent_journal import RentJournal
from studio_platform.lium_provider import LiumError, LiumNotSubmitted
import test_platform_lium_provider as contracts
from test_platform_lium_provider import TAG, POD, EXECUTOR, OTHER, TEMPLATE, manifest, launch


class JournalProviderTests(unittest.TestCase):
    provider = contracts.LiumProviderTests.provider
    tearDown = contracts.LiumProviderTests.tearDown

    def setUp(self):
        contracts.LiumProviderTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/"rents"

    def test_prepost_failure_survives_process_restart_without_second_post(self):
        self.api.executors = []
        provider = self.provider(journal_dir=self.directory)
        with self.assertRaises(LiumNotSubmitted):
            provider.create(TAG, launch(), hard_deadline=8300)
        restarted = self.provider(journal_dir=self.directory)
        self.api.calls.clear()
        fact = restarted.reconcile(TAG)
        self.assertEqual((fact.state, fact.actual_cost_microusd, fact.absence_confirmed), ("not_created", 0, True))
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            restarted.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual(self.api.calls, [])

    def test_post_timeout_is_unknown_after_restart_and_never_reposted(self):
        def timeout(request):
            raise httpx.ReadTimeout("fake private request body", request=request)
        self.api.on_rent = timeout
        provider = self.provider(journal_dir=self.directory)
        with self.assertRaisesRegex(LiumError, "request_unconfirmed"):
            provider.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "post_started")
        restarted = self.provider(journal_dir=self.directory)
        self.assertEqual(restarted.reconcile(TAG).state, "unknown")
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            restarted.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual(self.api.count("POST"), 1)

    def test_confirmed_identity_recovered_without_network_when_get_unavailable(self):
        self.provider(journal_dir=self.directory).create(TAG, launch(), hard_deadline=8300)
        restarted = self.provider(journal_dir=self.directory)
        self.api.calls.clear()
        self.api.hook = lambda request: httpx.Response(503)
        fact = restarted.reconcile(TAG)
        self.assertEqual((fact.state, fact.instance_id), ("starting", POD))
        self.assertEqual(self.api.calls, [])
        with self.assertRaisesRegex(LiumError, "identity_conflict"):
            restarted.reconcile(TAG, OTHER)

    def test_crash_during_check_is_not_absence_proof(self):
        RentJournal(self.directory).save(TAG, "checking")
        self.assertEqual(self.provider(journal_dir=self.directory).reconcile(TAG).state, "unknown")

    def test_legacy_intent_without_marker_stays_unknown(self):
        self.assertEqual(self.provider(journal_dir=self.directory).reconcile(TAG).state, "unknown")

    def test_existing_resource_bad_status_never_settles_as_not_created(self):
        self.api.pods = [{"id": POD, "name": "sixnine-"+TAG}]
        with self.assertRaises(LiumError) as caught:
            self.provider(journal_dir=self.directory).create(TAG, launch(), hard_deadline=8300)
        self.assertNotIsInstance(caught.exception, LiumNotSubmitted)
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "checking")
        self.assertEqual(self.api.count("POST"), 0)

    def test_malformed_marker_fails_closed_and_does_not_call_provider(self):
        journal = RentJournal(self.directory)
        journal.save(TAG, "checking")
        path = self.directory/(TAG+".json")
        path.write_text('{"version":true,"tag":"'+TAG+'","phase":"not_submitted"}')
        with self.assertRaisesRegex(LiumError, "journal_unconfirmed"):
            self.provider(journal_dir=self.directory).reconcile(TAG)
        self.assertEqual(self.api.calls, [])

    def server_provider(self):
        gpu = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
        return self.provider(journal_dir=self.directory, manifests=(manifest(executor_id="",
            compatible_gpu_names=(gpu,), minimum_vram_mib=95000,
            server_side_selection=True, minimum_ram_gib=64, minimum_disk_gib=100,
            require_docker_in_docker=True),))

    def spec_response(self, request):
        if request.url.path != "/api/executors/rent-by-spec":
            return None
        import json
        payload = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "dry_run": payload["dry_run"],
            "template_id": TEMPLATE, "pod_id": None if payload["dry_run"] else POD,
            "price_per_hour": 1.5, "selected_executor": {"id": OTHER}})

    def test_atomic_selection_preserves_price_count_ttl_and_single_paid_post(self):
        self.api.hook = self.spec_response
        provider = self.server_provider()
        fact = provider.create(TAG, launch(offer_id=""), hard_deadline=8300)
        self.assertEqual((fact.state, fact.instance_id), ("starting", POD))
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)  # One dry run and one allocation.
        paid = posts[-1][2]
        self.assertIs(paid["dry_run"], False)
        self.assertEqual((paid["gpu_count"], paid["termination_hours"], paid["max_price_per_gpu_hour"]), (2, 2, 1.0))
        self.assertEqual((paid["min_vram_gb"], paid["min_ram_gb"], paid["min_disk_gb"]), (95000/1024, 64, 100))
        self.assertIs(paid["docker_in_docker"], True)
        marker = RentJournal(self.directory).read(TAG)
        self.assertEqual(marker["instance_id"], POD)
        self.assertNotIn("executor_id", marker)  # Actual selection happens on server.
        with self.assertRaisesRegex(LiumError, "already_submitted"):
            self.server_provider().create(TAG, launch(offer_id=""), hard_deadline=8300)
        self.assertEqual(self.api.count("POST"), 2)

    def test_atomic_no_match_preflight_reserves_no_rental(self):
        self.api.hook = lambda req: httpx.Response(409, json={"success": False,
            "code": "no_executor_matches_spec", "message": "do not log private response"}) if req.method == "POST" else None
        provider = self.server_provider()
        self.assertEqual(provider.preflight_availability(launch(offer_id="")), "provider_inventory_unavailable")
        self.assertTrue(all(call[2]["dry_run"] for call in self.api.calls if call[0] == "POST"))
        self.assertFalse(self.directory.exists())  # Preflight makes no rent marker.

    def test_authoritative_rent_rejection_is_durable_no_allocation(self):
        def respond(request):
            if request.method == "POST":
                import json
                if not json.loads(request.content).get("dry_run", False):
                    return httpx.Response(409, json={"success": False, "code": "no_executor_matches_spec"})
            return self.spec_response(request)
        self.api.hook = respond
        fact = self.server_provider().create(TAG, launch(offer_id=""), hard_deadline=8300)
        self.assertEqual((fact.state, fact.absence_confirmed, fact.actual_cost_microusd), ("not_created", True, 0))
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "rejected")
        self.assertEqual(self.server_provider().reconcile(TAG).state, "not_created")

    def test_same_error_code_in_5xx_never_proves_no_allocation(self):
        def respond(request):
            if request.method == "POST":
                import json
                if not json.loads(request.content).get("dry_run", False):
                    return httpx.Response(503, json={"success": False, "code": "no_executor_matches_spec"})
            return self.spec_response(request)
        self.api.hook = respond
        with self.assertRaises(LiumError):
            self.server_provider().create(TAG, launch(offer_id=""), hard_deadline=8300)
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "post_started")
        self.assertEqual(self.server_provider().reconcile(TAG).state, "unknown")

    def test_rejection_with_contradictory_pod_id_retains_unknown_rental(self):
        def respond(request):
            if request.method == "POST":
                import json
                if not json.loads(request.content).get("dry_run", False):
                    return httpx.Response(409, json={"success": False,
                        "code": "no_executor_matches_spec", "pod_id": POD})
            return self.spec_response(request)
        self.api.hook = respond
        with self.assertRaises(LiumError):
            self.server_provider().create(TAG, launch(offer_id=""), hard_deadline=8300)
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "post_started")
        self.assertEqual(self.server_provider().reconcile(TAG).state, "unknown")

    def test_expired_manifest_does_not_override_preexisting_exact_resource(self):
        self.api.pods = [{"id": POD, "name": "sixnine-"+TAG, "status": "RUNNING"}]
        provider = self.provider(journal_dir=self.directory, manifests=(manifest(approved_until=999),))
        with self.assertRaises(LiumError) as caught:
            provider.create(TAG, launch(), hard_deadline=8300)
        self.assertNotIsInstance(caught.exception, LiumNotSubmitted)
        self.assertFalse(self.directory.exists())
        self.assertEqual((provider.reconcile(TAG).state, provider.reconcile(TAG).instance_id), ("running", POD))

    def test_post_success_bad_price_retains_paid_identity_for_reconciliation(self):
        def respond(request):
            result = self.spec_response(request)
            if result is not None:
                value = result.json()
                if value["dry_run"] is False:
                    value["price_per_hour"] = 2.01
                return httpx.Response(200, json=value)
        self.api.hook = respond
        with self.assertRaisesRegex(LiumError, "outside_approved_limits"):
            self.server_provider().create(TAG, launch(offer_id=""), hard_deadline=8300)
        restarted = self.server_provider()
        self.assertEqual(restarted.reconcile(TAG).instance_id, POD)
        self.assertEqual(RentJournal(self.directory).read(TAG)["phase"], "quarantined")
        self.assertIs(restarted.execution_allowed(TAG, POD), False)
        self.api.calls.clear()
        with self.assertRaisesRegex(LiumError, "contract_requires_reconciliation"):
            restarted.ssh_connection(TAG, POD)
        self.assertEqual(self.api.calls, [])


class RentJournalTests(unittest.TestCase):
    def test_terminal_marker_cannot_be_rewritten_to_no_charge(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RentJournal(Path(root)/"rents")
            journal.save(TAG, "checking")
            journal.save(TAG, "post_started", executor_id=EXECUTOR)
            with self.assertRaisesRegex(ValueError, "transition_refused"):
                journal.save(TAG, "not_submitted")
            journal.save(TAG, "confirmed", executor_id=EXECUTOR, instance_id=POD)
            with self.assertRaisesRegex(ValueError, "transition_refused"):
                journal.save(TAG, "checking")

    def test_invalid_fields_and_executor_change_cannot_corrupt_marker(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RentJournal(Path(root)/"rents")
            journal.save(TAG, "checking")
            with self.assertRaises(ValueError):
                journal.save(TAG, "not_submitted", instance_id=POD)
            journal.save(TAG, "post_started", executor_id=EXECUTOR)
            with self.assertRaisesRegex(ValueError, "executor_conflict"):
                journal.save(TAG, "confirmed", executor_id=OTHER, instance_id=POD)
            self.assertEqual(journal.read(TAG)["phase"], "post_started")

    @unittest.skipIf(os.name == "nt", "POSIX private-mode and link semantics")
    def test_insecure_or_linked_marker_refused(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)/"rents"
            journal = RentJournal(directory)
            journal.save(TAG, "checking")
            path = directory/(TAG+".json")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not_private"):
                journal.read(TAG)
            path.chmod(0o600)
            link = Path(root)/"linked"
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "linked_directory"):
                RentJournal(link).read(TAG)


if __name__ == "__main__":
    unittest.main()

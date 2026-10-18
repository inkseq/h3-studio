"""Independent cleanup guard contracts using local fixtures and fake HTTP."""
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from studio_platform.targon_cleanup import (TargonCleanupGuard, TargonDeadlineGuardian,
    _read, _write, _receipt_trust)


class TargonCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-guardian-test-")
        self.root = Path(self.temp.name)
        (self.root/"requests").mkdir()
        (self.root/"receipts").mkdir()
        self.now = 1800000000.0
        self.start, self.end, self.deadline = self.now-100, self.now+3600, self.now+120
        self.uid = "wkl-fixture"
        self.body = {"uid":self.uid,"name":"1"*32,"type":"VM","image":"ubuntu-fixture",
            "resource":{"name":"rtx6000b-small"},"state":{"status":"running"},
            "created_at":datetime.fromtimestamp(self.now,timezone.utc).isoformat()}
        self.get_status = 200
        self.client = Mock()
        self.client.get.side_effect = lambda route:httpx.Response(self.get_status,json=self.body)
        self.client.delete.return_value = httpx.Response(204)
        self.guard = TargonCleanupGuard(self.root,clock=lambda:self.now,wait_seconds=0)
        # Tests attest fake fixtures explicitly; production must verify root ACLs.
        self.trust = patch("studio_platform.targon_cleanup._receipt_trust")
        self.trust.start()
        self.addCleanup(self.trust.stop)

    def tearDown(self):
        self.temp.cleanup()

    def guardian(self):
        return TargonDeadlineGuardian(self.root,self.client,org_slug="fixture-org",
            resource_names=["rtx6000b-small"],image_names=["ubuntu-fixture"],
            approval_start=self.start,approval_end=self.end,maximum_seconds=600,clock=lambda:self.now)

    def arm(self):
        with self.assertRaisesRegex(ValueError,"ack_pending"):
            self.guard.arm(self.uid,self.deadline)
        self.guardian().tick()
        self.guard.arm(self.uid,self.deadline)

    def test_exact_acknowledgement_no_extension_and_freshness(self):
        self.arm()
        self.assertEqual(self.guard.proof(self.uid),
            {"instance_id":self.uid,"deadline":self.deadline,"independent":True,"armed":True})
        with self.assertRaisesRegex(ValueError,"immutable"):
            self.guard.arm(self.uid,self.deadline+1)
        self.now += 46
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        self.guardian().tick()
        self.assertTrue(self.guard.proof(self.uid)["armed"])
        self.client.delete.assert_not_called()

    def test_deadline_delete_and_restart_never_repeat_mutation(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.get_status = 404
        self.now += 60
        self.guardian().tick()
        proof = self.guard.removal_proof(self.uid)
        self.assertTrue(proof["removed"])
        self.assertEqual(proof["evidence"],"exact_uid_404_after_delete_ack")
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_removal_polling_survives_restart_and_keeps_heartbeat_fresh(self):
        self.arm()
        self.now = self.deadline-3
        self.guardian().tick()
        self.client.get.reset_mock()
        self.now = self.deadline
        self.guardian().tick()
        self.client.get.assert_called_once()  # First cleanup never waits for the poll interval.
        self.client.delete.assert_called_once()
        path = self.root/"receipts"/(self.uid+".json")
        pending = _read(path)
        self.assertEqual(pending["next_removal_observation_at"],self.deadline+60)
        self.client.get.reset_mock()
        self.get_status = 404
        for offset in (3,30,59):
            self.now = self.deadline+offset
            self.guardian().tick()  # Every tick uses a new guardian process identity.
            self.client.get.assert_not_called()
            self.assertEqual(_read(path),pending)
            heartbeat = _read(self.root/"heartbeat.json")
            self.assertEqual((heartbeat["process_state"],heartbeat["state"]),("running","degraded"))
            self.assertEqual(heartbeat["observed_at"],self.now)
            with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
                self.guard.removal_proof(self.uid)
        self.now = self.deadline+60
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.assertTrue(self.guard.removal_proof(self.uid)["removed"])
        self.client.delete.assert_called_once()

    def test_legacy_pending_receipt_uses_last_observation_without_resetting_obligation(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        path = self.root/"receipts"/(self.uid+".json")
        legacy = _read(path)
        del legacy["next_removal_observation_at"]
        _write(path,legacy)
        self.client.get.reset_mock()
        self.now += 59
        self.guardian().tick()
        self.client.get.assert_not_called()
        self.assertEqual(_read(path),legacy)
        self.now += 1
        self.guardian().tick()
        self.client.get.assert_called_once()
        retained = _read(path)
        for field in ("request","deadline","workload_identity","delete_started_at","delete_acknowledged"):
            self.assertEqual(retained[field],legacy[field])
        self.assertEqual(retained["next_removal_observation_at"],self.now+60)
        self.assertEqual(retained["delete_attempts"],2)

    def test_failed_removal_observations_persist_interval_before_io(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        path = self.root/"receipts"/(self.uid+".json")
        pending = _read(path)
        for failure in ("timeout","http","malformed","identity"):
            with self.subTest(failure=failure):
                _write(path,pending)
                self.now = self.deadline+60
                persisted_before_get = []
                def fail_get(route):
                    persisted_before_get.append(_read(path)["next_removal_observation_at"])
                    if failure == "timeout":
                        raise httpx.ReadTimeout("fake failure")
                    if failure == "http":
                        return httpx.Response(503)
                    if failure == "malformed":
                        return httpx.Response(200,content=b"not json")
                    return httpx.Response(200,json={**self.body,"name":"2"*32})
                self.client.get.side_effect = fail_get
                self.client.get.reset_mock()
                self.guardian().tick()
                self.client.get.assert_called_once()
                self.assertEqual(persisted_before_get,[self.now+60])
                retained = _read(path)
                self.assertEqual(retained,{**pending,"next_removal_observation_at":self.now+60})
                self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")
                self.now += 59
                self.guardian().tick()
                self.client.get.assert_called_once()
                self.assertEqual(_read(path),retained)
                self.now += 1
                self.client.get.side_effect = lambda route:httpx.Response(404)
                self.guardian().tick()
                self.assertEqual(self.client.get.call_count,2)
                self.assertTrue(self.guard.removal_proof(self.uid)["removed"])
        self.client.delete.assert_called_once()

    def test_first_deadline_get_failure_is_throttled_without_delaying_first_attempt(self):
        self.arm()
        path = self.root/"receipts"/(self.uid+".json")
        armed = _read(path)
        self.now = self.deadline
        self.client.get.reset_mock()
        self.client.get.side_effect = httpx.ReadTimeout("fake failure")
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.assertEqual(_read(path),{**armed,"next_removal_observation_at":self.now+60})
        self.client.delete.assert_not_called()
        self.now += 59
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.now += 1
        self.client.get.side_effect = lambda route:httpx.Response(200,json=self.body)
        self.guardian().tick()
        self.assertEqual(self.client.get.call_count,2)
        self.client.delete.assert_called_once()

    def test_provider_stopping_hint_polls_each_minute_without_postponing_deadline(self):
        self.arm()
        path = self.root/"receipts"/(self.uid+".json")
        original, started = _read(path), self.now
        for state in ({"status":"Stopping"},{"status":"pending","message":"sToPpInG"}):
            with self.subTest(state=state):
                _write(path,original)
                self.body["state"] = state
                self.now = started+1
                self.client.get.reset_mock()
                self.client.delete.reset_mock()
                self.guardian().tick()
                self.assertTrue(_read(path)["removal_poll_hint"])
                self.assertEqual(_read(path)["state"],"armed")
                for offset in (3,59):
                    self.now = started+1+offset
                    self.guardian().tick()
                    self.client.get.assert_called_once()
                self.now = started+61
                self.guardian().tick()
                self.assertEqual(self.client.get.call_count,2)
                self.assertGreater(_read(path)["next_removal_observation_at"],self.deadline)
                self.client.delete.assert_not_called()
                with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
                    self.guard.removal_proof(self.uid)
                self.now = self.deadline
                self.guardian().tick()
                self.assertEqual(self.client.get.call_count,3)
                self.client.delete.assert_called_once()
                receipt = _read(path)
                self.assertEqual((receipt["state"],receipt["deadline"]),("removal_pending",self.deadline))
                self.assertNotIn("removal_poll_hint",receipt)
                self.now += 59
                self.guardian().tick()
                self.assertEqual(self.client.get.call_count,3)

    def test_stopping_hint_deadline_override_is_consumed_before_failed_get(self):
        self.arm()
        self.now = self.deadline-3
        self.body["state"]["status"] = "Stopping"
        self.guardian().tick()
        self.now = self.deadline
        self.client.get.reset_mock()
        self.client.get.side_effect = httpx.ReadTimeout("fake failure")
        self.guardian().tick()
        self.client.get.assert_called_once()
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertNotIn("removal_poll_hint",receipt)
        self.assertEqual(receipt["next_removal_observation_at"],self.now+60)
        self.now += 59
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.client.delete.assert_not_called()

    def test_stopping_hint_clears_after_fresh_running_observation(self):
        self.arm()
        self.body["state"]["status"] = "Stopping"
        self.guardian().tick()
        self.now += 60
        self.body["state"]["status"] = "running"
        self.guardian().tick()
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertNotIn("removal_poll_hint",receipt)
        self.assertNotIn("next_removal_observation_at",receipt)
        self.client.get.reset_mock()
        self.now += 3
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.assertTrue(self.guard.proof(self.uid)["armed"])

    def test_disappearance_without_delete_ack_is_unknown(self):
        self.arm()
        self.get_status = 404
        self.guardian().tick()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.assertEqual(_read(self.root/"receipts"/(self.uid+".json"))["state"],"armed")
        self.client.delete.assert_not_called()

    def test_lost_delete_reconciles_without_retry_or_false_absence(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.side_effect = httpx.ReadTimeout("fake failure")
        self.guardian().tick()
        self.get_status = 404
        self.now += 60
        self.guardian().tick()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.client.delete.assert_called_once()
        self.get_status = 200
        self.body["state"]["status"] = "deleted"
        self.now += 60
        self.guardian().tick()
        self.assertEqual(self.guard.removal_proof(self.uid)["evidence"],"exact_uid_deleted")

    def test_foreign_workload_or_creation_outside_approval_is_not_armed(self):
        with self.assertRaises(ValueError):
            self.guard.arm(self.uid,self.deadline)
        original = self.body.copy()
        for changes in ({"name":"unrelated"},{"image":"another"},
                {"created_at":datetime.fromtimestamp(self.start-1,timezone.utc).isoformat()}):
            with self.subTest(changes=changes):
                self.body = {**original,**changes}
                self.guardian().tick()
                self.assertFalse((self.root/"receipts"/(self.uid+".json")).exists())
        self.client.delete.assert_not_called()

    def test_malformed_other_request_does_not_disable_existing_cleanup(self):
        self.arm()
        (self.root/"requests"/"aaa-invalid.json").write_text("not json")
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_transient_delete_retries_only_after_backoff_and_exact_fresh_get(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.side_effect = [httpx.Response(503),httpx.Response(204)]
        self.guardian().tick()
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")
        self.now += 29
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.now += 1
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.now += 30
        self.guardian().tick()
        self.assertEqual(self.client.delete.call_count,2)
        self.get_status = 404
        self.now += 60
        self.guardian().tick()
        self.assertTrue(self.guard.removal_proof(self.uid)["removed"])

    def test_delete_retry_exhaustion_is_explicitly_blocked(self):
        self.arm()
        self.now = self.deadline
        self.client.delete.return_value = httpx.Response(429)
        for _ in range(12):
            self.guardian().tick()
            receipt = _read(self.root/"receipts"/(self.uid+".json"))
            self.now = max(receipt["next_retry_at"],receipt["next_removal_observation_at"])
        self.guardian().tick()
        self.assertEqual(self.client.delete.call_count,12)
        self.assertEqual(_read(self.root/"receipts"/(self.uid+".json"))["state"],"cleanup_blocked")
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")
        self.client.get.reset_mock()
        self.now += 59
        self.guardian().tick()
        self.client.get.assert_not_called()
        self.now += 1
        self.guardian().tick()
        self.client.get.assert_called_once()
        self.assertEqual(self.client.delete.call_count,12)

    def arm_distinct_workload(self):
        uid, deadline = "wkl-distinct", self.now+120
        body = {**self.body,"uid":uid,"name":"2"*32,
            "created_at":datetime.fromtimestamp(self.now,timezone.utc).isoformat()}
        bodies = {self.uid:self.body,uid:body}
        self.client.get.side_effect = lambda route:httpx.Response(200,json=bodies[route.rsplit('/',1)[1]])
        with self.assertRaisesRegex(ValueError,"ack_pending"):
            self.guard.arm(uid,deadline)
        self.guardian().tick()
        self.guard.arm(uid,deadline)
        return uid,deadline

    def test_distinct_armed_workload_survives_old_pending_cleanup_without_losing_retry(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()  # DELETE acknowledged; stale exact GET is not removal.
        old_path = self.root/"receipts"/(self.uid+".json")
        original = _read(old_path)
        uid, deadline = self.arm_distinct_workload()
        heartbeat = _read(self.root/"heartbeat.json")
        self.assertEqual((heartbeat["process_state"],heartbeat["state"]),("running","degraded"))
        self.assertEqual(self.guard.proof(uid),
            {"instance_id":uid,"deadline":deadline,"independent":True,"armed":True})
        self.assertEqual(_read(old_path),original)
        self.assertEqual(self.client.delete.call_count,1)
        for method in (self.guard.proof,self.guard.removal_proof):
            with self.assertRaises(ValueError): method(self.uid)
        for offset in (3,30,59):
            self.now = self.deadline+offset
            self.client.get.reset_mock()
            self.guardian().tick()
            self.client.get.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+uid)
            self.assertEqual(_read(old_path),original)
            self.assertTrue(self.guard.proof(uid)["armed"])
        self.now = self.deadline+60
        self.guardian().tick()
        retried = _read(old_path)
        self.assertEqual(self.client.delete.call_count,2)
        self.assertEqual(retried["delete_attempts"],2)
        self.assertEqual(retried["deadline"],original["deadline"])
        self.assertEqual(retried["workload_identity"],original["workload_identity"])
        self.assertEqual(retried["state"],"removal_pending")
        self.assertNotIn("removal_evidence",retried)
        self.assertTrue(self.guard.proof(uid)["armed"])
        self.assertTrue(all(call.args[0].endswith('/'+self.uid) for call in self.client.delete.call_args_list))

    def test_slow_earlier_workload_cannot_shorten_another_removal_poll_interval(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        earlier_uid, _ = self.arm_distinct_workload()
        original_get = self.client.get.side_effect
        removal_reads = []
        delayed = False
        def slow_get(route):
            nonlocal delayed
            if route.endswith('/'+earlier_uid) and not delayed:
                self.now += 15
                delayed = True
            if route.endswith('/'+self.uid):
                removal_reads.append(self.now)
            return original_get(route)
        self.client.get.side_effect = slow_get
        self.now = self.deadline+60
        self.guardian().tick()
        self.assertEqual(removal_reads,[self.deadline+75])
        self.assertEqual(_read(self.root/"receipts"/(self.uid+".json"))["next_removal_observation_at"],
                         self.deadline+135)
        self.now = self.deadline+120
        self.guardian().tick()
        self.assertEqual(removal_reads,[self.deadline+75])
        self.now = self.deadline+135
        self.guardian().tick()
        self.assertEqual(removal_reads,[self.deadline+75,self.deadline+135])

    def test_distinct_workload_does_not_reset_exhausted_old_cleanup(self):
        self.arm()
        self.now = self.deadline
        old_path = self.root/"receipts"/(self.uid+".json")
        for _ in range(12):
            self.guardian().tick()
            receipt = _read(old_path)
            self.now = max(receipt["next_retry_at"],receipt["next_removal_observation_at"])
        self.guardian().tick()
        original = _read(old_path)
        uid, _ = self.arm_distinct_workload()
        retained = _read(old_path)
        self.assertTrue(self.guard.proof(uid)["armed"])
        self.assertEqual(self.client.delete.call_count,12)
        for field in ("state","deadline","request","workload_identity","delete_attempts",
                      "delete_started_at","delete_acknowledged","next_retry_at","reason_code"):
            self.assertEqual(retained[field],original[field])
        self.assertEqual(retained["state"],"cleanup_blocked")
        self.assertNotIn("removal_evidence",retained)
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)

    def test_explicit_process_health_and_known_aggregate_state_are_required(self):
        self.arm()
        path = self.root/"heartbeat.json"
        original = _read(path)
        cases = [{k:v for k,v in original.items() if k != "process_state"}]
        cases += [{**original,"process_state":state} for state in ("stopped","failed","unknown",None)]
        cases += [{**original,"state":"unknown"},
            {**original,"state":"degraded","reason_code":"unrecognized_failure"},
            {**original,"observed_at":self.now-46}, {**original,"observed_at":self.now+1}]
        for value in cases:
            with self.subTest(heartbeat=value):
                _write(path,value)
                with self.assertRaisesRegex(ValueError,"proof_unavailable"):
                    self.guard.proof(self.uid)
        path.unlink()
        with self.assertRaises(OSError): self.guard.proof(self.uid)
        self.client.delete.assert_not_called()

    def test_healthy_sweep_cannot_replace_exact_fresh_armed_receipt(self):
        self.arm()
        path = self.root/"receipts"/(self.uid+".json")
        original = _read(path)
        cases = [{**original,"state":state} for state in ("removal_pending","cleanup_blocked","removed","unknown")]
        cases += [{**original,"observed_at":self.now-46},
            {**original,"guardian_id":"another-guardian"}, {**original,"instance_id":"wkl-distinct"}]
        for value in cases:
            with self.subTest(receipt=value):
                _write(path,value)
                with self.assertRaises(ValueError): self.guard.proof(self.uid)
        path.unlink()
        with self.assertRaises(OSError): self.guard.proof(self.uid)
        self.client.delete.assert_not_called()

    def test_tampered_request_or_receipt_cannot_prove_guard_or_removal(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        value = _read(request_path)
        _write(request_path,{**value,"deadline":self.deadline+1})
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)

    def test_missing_request_cannot_drop_armed_cleanup_after_guardian_restart(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        original = _read(request_path)
        request_path.unlink()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertEqual(receipt["request"],original)
        self.assertEqual(receipt["deadline"],self.deadline)
        self.get_status = 404
        self.now += 60
        self.guardian().tick()
        self.assertEqual(self.guard.removal_proof(self.uid)["deadline"],self.deadline)
        self.guardian().tick()
        self.client.delete.assert_called_once()

    def test_changed_request_cannot_extend_deadline_or_retarget_armed_workload(self):
        self.arm()
        request_path = self.root/"requests"/(self.uid+".json")
        original = _read(request_path)
        _write(request_path,{**original,"instance_id":"another-workload","deadline":self.deadline+9999})
        with self.assertRaisesRegex(ValueError,"proof_unavailable"):
            self.guard.proof(self.uid)
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)
        receipt = _read(self.root/"receipts"/(self.uid+".json"))
        self.assertEqual(receipt["request"],original)
        self.assertEqual(receipt["workload_identity"]["name"],self.body["name"])
        self.get_status = 404
        self.now += 60
        self.guardian().tick()
        self.assertTrue(self.guard.removal_proof(self.uid)["removed"])

    def test_malformed_request_cannot_disable_retained_cleanup(self):
        self.arm()
        (self.root/"requests"/(self.uid+".json")).write_text('not json')
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once_with('/tha/v3/orgs/fixture-org/workloads/'+self.uid)

    def test_retained_provider_identity_must_still_match_before_delete(self):
        self.arm()
        self.now = self.deadline
        # Same UID and allowed resource alone cannot substitute another original
        # workload identity, even if the provider unexpectedly changes its name.
        self.body["name"] = "2"*32
        self.guardian().tick()
        self.client.delete.assert_not_called()
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")

    def test_other_org_cannot_turn_acknowledged_delete_into_false_absence_proof(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        self.client.delete.assert_called_once()
        self.get_status = 404
        self.client.get.reset_mock()
        other = TargonDeadlineGuardian(self.root,self.client,org_slug="another-org",
            resource_names=["rtx6000b-small"],image_names=["ubuntu-fixture"],
            approval_start=self.start,approval_end=self.end,maximum_seconds=600,clock=lambda:self.now)
        other.tick()
        self.client.get.assert_not_called()
        with self.assertRaisesRegex(ValueError,"removal_unconfirmed"):
            self.guard.removal_proof(self.uid)
        self.assertEqual(_read(self.root/"heartbeat.json")["state"],"degraded")

    def test_root_receipt_trust_rejects_controller_writable_or_nonroot_files(self):
        self.trust.stop()
        path = Mock()
        path.is_symlink.return_value = False
        with patch("studio_platform.targon_cleanup.os.name","posix"):
            for uid, mode in ((1000,stat.S_IFREG|0o640),(0,stat.S_IFREG|0o660)):
                with self.subTest(uid=uid,mode=mode):
                    path.lstat.return_value = SimpleNamespace(st_uid=uid,st_mode=mode)
                    with self.assertRaisesRegex(ValueError,"receipt_untrusted"):
                        _receipt_trust([path])
            path.lstat.return_value = SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o640)
            _receipt_trust([path])
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError,"trust_unsupported"):
                _receipt_trust([path])


if __name__ == "__main__":
    unittest.main()

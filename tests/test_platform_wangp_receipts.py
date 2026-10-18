"""Offline journal identity/concurrency tests; synthetic manifests only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from studio_platform.inference.protocol import BackendError, NotReady
from studio_platform.inference.wangp_contract import (
    EngineManifest, InputDescriptor, PreparedRequest, PROTOCOL_VERSION,
    UPSTREAM_REVISION, canonical_json,
)
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal


def manifest():
    return EngineManifest.from_dict({"engine": "wangp", "protocol_version": PROTOCOL_VERSION,
        "source_revision": UPSTREAM_REVISION, "compiler_id": "synthetic-compiler-v1",
        "profile_id": "synthetic-fl-v1", "runtime_digest": "synthetic-runtime-only",
        "components": {"synthetic-model": {"revision": "test-revision", "precision": "BF16"}},
        "memory_profile": "synthetic-offload", "kernel_profile": "synthetic-kernels",
        "topology": {"slots": 1, "gpu_count": 1}, "synthetic": True})


def prepared(*, tag="attempt-1", audio=True):
    return PreparedRequest("job-1", tag, "a" * 64, manifest().digest,
        canonical_json({"prompt": "PRIVATE_TEST_PROMPT", "steps": 50}),
        canonical_json({"width": 832, "height": 480, "fps": 24, "frame_count": 124}), audio)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "receipts.sqlite"
        self.journal = ReceiptJournal(self.path, slot_key="slot-1", manifest_digest=manifest().digest, create=True)
        self.addCleanup(self.journal.release_host)

    def reopen(self):
        return ReceiptJournal(self.path, slot_key="slot-1", manifest_digest=manifest().digest)

    def test_same_operation_claim_is_atomic_across_connections(self):
        other = self.reopen()
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda journal: journal.claim(prepared(), "incarnation-1"),
                                   (self.journal, other)))
        self.assertEqual(sum(fresh for _, fresh in values), 1)
        self.assertEqual(values[0][0], values[1][0])
        self.assertNotIn("PRIVATE_TEST_PROMPT", self.path.read_bytes().decode("utf-8", errors="ignore"))

    def test_distinct_operation_cannot_admit_while_unresolved(self):
        self.journal.claim(prepared(), "incarnation-1")
        with self.assertRaises(NotReady):
            self.reopen().claim(prepared(tag="attempt-2"), "incarnation-2")

    def test_conflicting_replay_preserves_original_identity_and_holds_slot(self):
        original, _ = self.journal.claim(prepared(), "incarnation-1")
        held, fresh = self.journal.claim(replace(prepared(), request_hash="b" * 64), "incarnation-1")
        self.assertFalse(fresh)
        self.assertEqual(held.identity_digest, original.identity_digest)
        self.assertEqual(held.request_hash, original.request_hash)
        self.assertEqual(held.hold_reason, "wangp_identity_conflict")
        self.assertTrue(self.journal.has_obligations())

    def test_restart_does_not_make_intent_ready(self):
        self.journal.claim(prepared(), "old")
        self.journal.transition(prepared().operation_id, expected={"prepared"}, state="dispatch_intent")
        journal = self.reopen()
        journal.recover("new")
        receipt = journal.get(prepared().operation_id)
        self.assertEqual(receipt.state, "unknown")
        self.assertEqual(receipt.incarnation, "old")
        self.assertEqual(receipt.reason, "wangp_host_restarted")
        self.assertTrue(journal.has_obligations())

    def test_terminal_state_requires_stop_proof_and_is_immutable(self):
        self.journal.claim(prepared(), "inc")
        with self.assertRaises(ValueError):
            self.journal.transition(prepared().operation_id, expected={"prepared"}, state="failed")
        self.assertEqual(self.journal.get(prepared().operation_id).state, "prepared")
        self.journal.transition(prepared().operation_id, expected={"prepared"}, state="cancelled", stop_proven=True)
        with self.assertRaises(BackendError):
            self.journal.transition(prepared().operation_id, expected={"cancelled"}, state="running")
        self.assertFalse(self.journal.has_obligations())

    def test_missing_replaced_or_other_manifest_journal_fails_closed(self):
        with self.assertRaises(NotReady):
            ReceiptJournal(self.path.with_name("missing.sqlite"), slot_key="slot-1", manifest_digest=manifest().digest)
        with self.assertRaises(NotReady):
            ReceiptJournal(self.path, slot_key="slot-1", manifest_digest="b" * 64)
        self.path.rename(self.path.with_name("original.sqlite"))
        self.path.write_bytes(b"not-a-journal")
        with self.assertRaises(NotReady):
            self.journal.has_obligations()

    def test_host_lock_is_exclusive_and_released_explicitly(self):
        other = self.reopen()
        self.addCleanup(other.release_host)
        self.journal.acquire_host()
        with self.assertRaises(NotReady):
            other.acquire_host()
        self.journal.release_host()
        other.acquire_host()

    def test_canonical_values_are_immutable_and_reject_hidden_nonfinite_data(self):
        value = prepared()
        settings = value.settings
        settings["steps"] = 20
        self.assertEqual(value.settings["steps"], 50)
        with self.assertRaises(ValueError):
            replace(value, settings_json='{"steps": NaN}')
        with self.assertRaises(ValueError):
            replace(value, inputs=[InputDescriptor("a", "h", "image", "a" * 64, 1)])
        with self.assertRaises(ValueError):
            replace(value, attempt_tag="../attempt")


if __name__ == "__main__":
    unittest.main()

"""Injected Session lifecycle tests; files are bytes, not claimed H3 media."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from studio_platform.inference.protocol import BackendError, NotReady
from studio_platform.inference.wangp_contract import RuntimeObservation, RuntimeOutput
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from test_platform_wangp_receipts import manifest, prepared


class Crash(BaseException):
    pass


class FakeHandle:
    def __init__(self):
        self.observation = RuntimeObservation("running")
        self.cancel_calls = 0

    def observe(self):
        return self.observation

    def cancel(self):
        self.cancel_calls += 1
        return True


class FakeSession:
    def __init__(self, journal=None):
        self.handle = FakeHandle()
        self.calls = 0
        self.journal = journal
        self.on_submit = None
        self.idle_override = None

    def is_idle(self):
        if self.idle_override is not None:
            return self.idle_override
        return not self.calls or self.handle.observation.stopped is True

    def submit_task(self, settings):
        self.calls += 1
        if self.journal:
            assert self.journal.get(prepared().operation_id).state == "dispatch_intent"
        if self.on_submit:
            self.on_submit()
        return self.handle


class HostFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "runtime-output"
        self.output.mkdir()
        self.journal_path = self.root / "journal" / "receipts.sqlite"
        self.journal = ReceiptJournal(self.journal_path, slot_key="slot-1", manifest_digest=manifest().digest, create=True)
        self.session = FakeSession(self.journal)
        self.host = self.make_host()

    def make_host(self, *, session=None, journal=None, fault_hook=None, **kwargs):
        host = WanGPHost(session=session or self.session, journal=journal or self.journal,
            manifest=manifest(), output_root=self.output, sealed_root=self.root / "sealed",
            fault_hook=fault_hook, **kwargs)
        self.addCleanup(host.close)
        return host

    def restart(self):
        self.host.close()
        journal = ReceiptJournal(self.journal_path, slot_key="slot-1", manifest_digest=manifest().digest)
        session = FakeSession(journal)
        self.host = self.make_host(session=session, journal=journal)
        return session

    def complete(self, *, audio=True):
        video = self.output / "video.mp4"
        video.write_bytes(b"synthetic-video-bytes")
        outputs = {"video": RuntimeOutput(video, "video/mp4")}
        if audio:
            sound = self.output / "audio.flac"
            sound.write_bytes(b"synthetic-audio-bytes")
            outputs["audio"] = RuntimeOutput(sound, "audio/flac")
        self.session.handle.observation = RuntimeObservation("succeeded", True, outputs)


class HostTests(HostFixture, unittest.TestCase):
    def test_concurrent_duplicate_starts_once_with_intent_visible_to_session(self):
        with ThreadPoolExecutor(4) as pool:
            receipts = list(pool.map(lambda _: self.host.submit(prepared()), range(4)))
        self.assertEqual(self.session.calls, 1)
        self.assertEqual({r.operation_id for r in receipts}, {prepared().operation_id})
        self.assertFalse(self.host.readiness().idle)
        self.assertIsNone(self.host.inspect("wangp-unseen"))

    def test_conflicting_replay_never_calls_session_again(self):
        original = self.host.submit(prepared())
        held = self.host.submit(replace(prepared(), settings_json='{"steps":20}'))
        self.assertEqual(self.session.calls, 1)
        self.assertEqual(held.identity_digest, original.identity_digest)
        self.assertTrue(held.hold_reason)
        self.assertFalse(self.host.readiness().idle)

    def test_exception_after_start_is_unknown_and_replay_does_not_start(self):
        def lost():
            raise RuntimeError("PRIVATE_RUNTIME_STREAM")
        self.session.on_submit = lost
        receipt = self.host.submit(prepared())
        self.assertEqual(receipt.state, "unknown")
        self.assertNotIn("PRIVATE_RUNTIME_STREAM", repr(receipt))
        self.host.submit(prepared())
        self.assertEqual(self.session.calls, 1)
        self.assertFalse(self.host.readiness().idle)

    def test_crash_around_dispatch_never_replays_on_restart(self):
        for point, starts in (("after_prepared", 0), ("after_intent", 0), ("after_handle", 1)):
            with self.subTest(point=point):
                # Separate protected state for each simulated process death.
                self.host.close()
                path = self.root / (point + ".sqlite")
                journal = ReceiptJournal(path, slot_key="slot-1", manifest_digest=manifest().digest, create=True)
                session = FakeSession(journal)
                def fault(stage):
                    if stage == point:
                        raise Crash()
                host = self.make_host(journal=journal, session=session, fault_hook=fault)
                with self.assertRaises(Crash):
                    host.submit(prepared())
                self.assertEqual(session.calls, starts)
                host.close()
                replacement = FakeSession(journal)
                reopened = self.make_host(journal=journal, session=replacement)
                self.assertEqual(reopened.submit(prepared()).state, "unknown")
                self.assertFalse(reopened.readiness().idle)
                self.assertEqual(replacement.calls, 0)
                reopened.close()

    def test_cancel_ack_is_not_stop_and_late_success_is_retained(self):
        op = self.host.submit(prepared()).operation_id
        self.assertTrue(self.host.cancel(op))
        receipt = self.host.inspect(op)
        self.assertEqual(receipt.state, "running")
        self.assertTrue(receipt.cancel_requested)
        self.assertFalse(receipt.stop_proven)
        self.complete()
        receipt = self.host.inspect(op)
        self.assertEqual(receipt.state, "succeeded")
        self.assertEqual({v.kind for v in receipt.artifacts}, {"video", "audio"})
        self.assertEqual(self.session.calls, 1)

    def test_terminal_failure_requires_proven_stop(self):
        op = self.host.submit(prepared()).operation_id
        self.session.handle.observation = RuntimeObservation("failed", False)
        self.assertEqual(self.host.inspect(op).state, "unknown")
        self.session.handle.observation = RuntimeObservation("cancelled", True)
        self.assertEqual(self.host.inspect(op).state, "cancelled")
        self.assertTrue(self.host.readiness().idle)

    def test_pre_dispatch_cancel_starts_nothing(self):
        self.host._fault_hook = lambda stage: self.host.cancel(prepared().operation_id) if stage == "after_prepared" else None
        receipt = self.host.submit(prepared())
        self.assertEqual(receipt.state, "cancelled")
        self.assertTrue(receipt.stop_proven)
        self.assertEqual(self.session.calls, 0)

    def test_missing_independent_audio_is_held_and_recoverable_without_regeneration(self):
        op = self.host.submit(prepared()).operation_id
        self.complete(audio=False)
        self.assertEqual(self.host.inspect(op).state, "unknown")
        self.assertFalse(self.host.readiness().idle)
        self.complete()
        self.assertEqual(self.host.inspect(op).state, "succeeded")
        self.assertEqual(self.session.calls, 1)

    def test_sealed_success_survives_restart_and_runtime_output_removal(self):
        op = self.host.submit(prepared()).operation_id
        self.complete()
        receipt = self.host.inspect(op)
        self.assertEqual(receipt.state, "succeeded")
        for item in self.output.iterdir():
            item.unlink()
        replacement = self.restart()
        self.assertEqual(self.host.inspect(op), receipt)
        self.assertEqual(b"".join(self.host.read_artifact(op, "video")), b"synthetic-video-bytes")
        self.host.submit(prepared())
        self.assertEqual(replacement.calls, 0)

    def test_unsealed_crash_preserves_unknown_and_never_guesses_existing_files(self):
        op = self.host.submit(prepared()).operation_id
        self.complete()
        def fault(stage):
            if stage == "after_seal_before_commit":
                raise Crash()
        self.host._fault_hook = fault
        with self.assertRaises(Crash):
            self.host.inspect(op)
        replacement = self.restart()
        self.assertEqual(self.host.inspect(op).state, "unknown")
        with self.assertRaises(NotReady):
            list(self.host.read_artifact(op, "video"))
        self.assertEqual(replacement.calls, 0)

    def test_unsafe_outputs_and_tampered_sealed_files_are_rejected(self):
        op = self.host.submit(prepared()).operation_id
        self.complete()
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        outputs = dict(self.session.handle.observation.outputs)
        outputs["video"] = RuntimeOutput(self.output / ".." / "outside.mp4", "video/mp4")
        self.session.handle.observation = RuntimeObservation("succeeded", True, outputs)
        self.assertEqual(self.host.inspect(op).state, "unknown")
        self.complete()
        receipt = self.host.inspect(op)
        descriptor = next(v for v in receipt.artifacts if v.kind == "video")
        sealed = self.root / "sealed" / op / ("video-" + descriptor.sha256 + ".bin")
        sealed.write_bytes(b"replaced-content")
        with self.assertRaises(BackendError):
            list(self.host.read_artifact(op, "video"))

    def test_hardlinked_runtime_output_is_rejected(self):
        op = self.host.submit(prepared()).operation_id
        self.complete()
        os.link(self.output / "video.mp4", self.output / "alias.mp4")
        self.assertEqual(self.host.inspect(op).state, "unknown")

    def test_truthy_runtime_idle_does_not_admit(self):
        self.session.idle_override = {"idle": True}
        self.assertFalse(self.host.readiness().idle)
        with self.assertRaises(NotReady):
            self.host.submit(prepared())
        self.assertEqual(self.session.calls, 0)


if __name__ == "__main__":
    unittest.main()

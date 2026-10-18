"""Worker-facing adapter tests, exclusively injected in-process fake transport."""
from dataclasses import replace
import subprocess
import sys
import unittest

from studio_platform.inference.protocol import BackendError, NotReady, SubmissionUncertain
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import HostReadiness, InputDescriptor, canonical_json
from test_platform_wangp_host import HostFixture
from test_platform_wangp_receipts import manifest, prepared


def job():
    return {"id": "job-1", "owner_id": "owner-1", "request_hash": "a" * 64,
        "execution_plan": {"backend": "wangp-worker", "engine_manifest_digest": manifest().digest},
        "request": {"request": {"prompt": "PRIVATE_TEST_PROMPT", "generate_audio": True},
                    "output_spec": {"width": 832, "height": 480, "fps": 24, "frame_count": 124}, "assets": {}}}


class Proxy:
    def __init__(self, host):
        self.host = host

    def __getattr__(self, name):
        return getattr(self.host, name)


class AdapterTests(HostFixture, unittest.TestCase):
    def backend(self, *, transport=None, compiler=None):
        return WanGPBackend(enabled=True, slot_key="slot-1", manifest=manifest(),
            transport=transport or self.host,
            compiler=compiler or (lambda job, tag, store, heartbeat: prepared(tag=tag)))

    def test_import_and_default_constructor_are_inert(self):
        backend = WanGPBackend()
        with self.assertRaises(NotReady):
            backend.prepare(job(), "attempt-1", None, lambda: None)
        code = ("import sys,threading; before=len(threading.enumerate()); "
                "import studio_platform.inference.wangp; import studio_platform.runtime_hosts.wangp; "
                "assert len(threading.enumerate())==before; "
                "assert not {'torch','wgp','mmgp','shared.api'}.intersection(sys.modules)")
        subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=20)
        self.assertEqual(self.session.calls, 0)

    def test_prepare_keeps_existing_compilation_shape_and_is_not_submission(self):
        backend = self.backend()
        result = backend.prepare(job(), "attempt-1", None, lambda: None)
        self.assertEqual(result, prepared())
        self.assertIsNone(self.journal.get(prepared().operation_id))
        self.assertEqual(self.session.calls, 0)

    def test_engine_binding_and_immutable_output_are_required(self):
        backend = self.backend()
        for update in ({"backend": "comfy-worker"}, {"engine_manifest_digest": "b" * 64}):
            value = job()
            value["execution_plan"].update(update)
            with self.assertRaises(BackendError):
                backend.prepare(value, "attempt-1", None, lambda: None)
        bad = self.backend(compiler=lambda *args: replace(prepared(), output_spec_json='{"width":512}'))
        with self.assertRaises(BackendError):
            bad.prepare(job(), "attempt-1", None, lambda: None)
        self.assertEqual(self.session.calls, 0)

    def test_asset_owner_checked_before_compiler_or_staging(self):
        called = []
        backend = self.backend(compiler=lambda *args: called.append(True))
        value = job()
        value["request"]["assets"] = {"asset": {"model": {"key": "owners/other/assets/a/model.png"}}}
        with self.assertRaises(BackendError):
            backend.prepare(value, "attempt-1", None, lambda: None)
        self.assertEqual(called, [])

    def test_compiler_must_preserve_input_hashes(self):
        value = job()
        value["request"]["assets"] = {"asset": {"model": {"key": "owners/owner-1/assets/a/model.png",
            "sha256": "a" * 64, "size_bytes": 12}, "metadata": {"kind": "image"}}}
        compiler = lambda *args: replace(prepared(), inputs=(InputDescriptor("asset", "handle", "image", "b" * 64, 12),))
        with self.assertRaises(BackendError):
            self.backend(compiler=compiler).prepare(value, "attempt-1", None, lambda: None)

    def test_lost_submit_response_reconciles_same_operation_without_second_start(self):
        class LostResponse(Proxy):
            def submit(self, value):
                self.host.submit(value)
                raise RuntimeError("PRIVATE_TRANSPORT_DETAIL")
        backend = self.backend(transport=LostResponse(self.host))
        with self.assertRaisesRegex(SubmissionUncertain, "^wangp_submission_unknown$"):
            backend.submit(prepared(), "attempt-1")
        outcome = backend.reconcile("attempt-1")
        self.assertEqual(outcome.state, "running")
        self.assertEqual(outcome.task_id, prepared().operation_id)
        self.assertEqual(self.session.calls, 1)
        self.assertIsNone(outcome.actual_cost_microusd)

    def test_missing_or_mismatched_receipt_is_unknown(self):
        backend = self.backend()
        self.assertEqual(backend.reconcile("attempt-1").state, "unknown")
        self.host.submit(prepared())
        class WrongIdentity(Proxy):
            def inspect(self, op):
                return replace(self.host.inspect(op), manifest_digest="b" * 64)
        self.assertEqual(self.backend(transport=WrongIdentity(self.host)).reconcile("attempt-1").state, "unknown")
        with self.assertRaises(BackendError):
            backend.poll("attempt-1", "wangp-another-attempt")
        self.assertEqual(self.session.calls, 1)

    def test_truthy_or_wrong_readiness_is_false(self):
        class WrongReady(Proxy):
            def readiness(self):
                return HostReadiness(manifest().digest, "slot-1", "inc", 1)
        self.assertFalse(self.backend(transport=WrongReady(self.host)).is_idle())
        class DifferentSlot(Proxy):
            def readiness(self):
                return HostReadiness(manifest().digest, "another-slot", "inc", True)
        self.assertFalse(self.backend(transport=DifferentSlot(self.host)).is_idle())

    def test_cancel_is_exact_attempt_and_not_terminal(self):
        backend = self.backend()
        op = backend.submit(prepared(), "attempt-1")
        with self.assertRaises(BackendError):
            backend.cancel("attempt-1", "wangp-another-attempt")
        self.assertEqual(self.session.handle.cancel_calls, 0)
        self.assertTrue(backend.cancel("attempt-1", op))
        self.assertEqual(backend.poll("attempt-1", op).state, "running")

    def test_fetch_retries_original_sealed_video_and_audio_without_regeneration(self):
        class FailsOnce(Proxy):
            fail = True
            def read_artifact(self, op, kind):
                for data in self.host.read_artifact(op, kind):
                    yield data
                    if self.fail:
                        self.fail = False
                        raise RuntimeError("PRIVATE_DOWNLOAD_FAILURE")
        backend = self.backend(transport=FailsOnce(self.host))
        op = backend.submit(prepared(), "attempt-1")
        self.complete()
        self.assertEqual(backend.poll("attempt-1", op).state, "succeeded")
        target = self.root / "attempt-download"
        with self.assertRaisesRegex(BackendError, "^wangp_collection_unavailable$"):
            backend.fetch(job(), "attempt-1", op, target, lambda: None)
        paths = backend.fetch(job(), "attempt-1", op, target, lambda: None)
        self.assertEqual(set(paths), {"video", "audio"})
        self.assertTrue(all(p.is_relative_to(target) for p in paths.values()))
        self.assertEqual(paths["video"].read_bytes(), b"synthetic-video-bytes")
        self.assertEqual(paths["audio"].read_bytes(), b"synthetic-audio-bytes")
        self.assertEqual(backend.fetch(job(), "attempt-1", op, target, lambda: None), paths)
        self.assertEqual(self.session.calls, 1)

    def test_corrupt_download_and_cross_job_collection_fail_closed(self):
        class Corrupt(Proxy):
            def read_artifact(self, op, kind):
                yield b"wrong-bytes"
        backend = self.backend(transport=Corrupt(self.host))
        op = backend.submit(prepared(), "attempt-1")
        self.complete()
        self.host.inspect(op)
        with self.assertRaises(BackendError):
            backend.fetch(job(), "attempt-1", op, self.root / "download", lambda: None)
        other = job()
        other["id"] = "another-job"
        with self.assertRaises(BackendError):
            backend.fetch(other, "attempt-1", op, self.root / "download", lambda: None)
        self.assertFalse(list((self.root / "download").glob("*.mp4")))


if __name__ == "__main__":
    unittest.main()

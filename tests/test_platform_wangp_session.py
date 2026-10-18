import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from studio_platform.inference.protocol import BackendError, NotReady
from studio_platform.runtime_hosts.wangp_session import (
    PinnedWanGPSession, _check_config, config_for_model_root)


class FakeJob:
    done = False
    def __init__(self, result):
        self.value, self.cancel_calls = result, 0
    def result(self, timeout=None):
        return self.value
    def cancel(self):
        self.cancel_calls += 1


class FakeSession:
    active_job = None
    def __init__(self, job):
        self.job, self.calls = job, []
    def get_default_settings(self, model):
        return {"model_type": model, "num_inference_steps": 20}
    def submit_task(self, settings):
        self.calls.append(settings)
        self.active_job = self.job
        return self.job
    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "result.mp4"
        self.video.write_bytes(b"cpu-test-video")
        self.result = NS(success=True, cancelled=False, total_tasks=1, successful_tasks=1,
            failed_tasks=0, errors=[], generated_files=[str(self.video)], artifacts=[NS(
            path=str(self.video), media_type="video", audio_tensor=[[.1, .2]], audio_sampling_rate=32000)])
        self.job = FakeJob(self.result)
        self.upstream = FakeSession(self.job)
        self.writes = []
        def writer(path, samples, rate):
            self.writes.append((samples, rate))
            path.write_bytes(b"cpu-test-wav")
        self.quiesced = []
        self.facade = PinnedWanGPSession(self.upstream, self.root, audio_writer=writer,
                                         quiesce=lambda: self.quiesced.append(1))

    def test_cancellation_is_intent_until_upstream_stops(self):
        handle = self.facade.submit_task({"num_inference_steps": 50})
        self.assertTrue(handle.cancel())
        self.assertEqual(self.job.cancel_calls, 1)
        self.assertFalse(handle.observe().stopped)
        self.result.success, self.result.cancelled = False, True
        self.job.done = True
        result = handle.observe()
        self.assertEqual((result.state, result.stopped), ("cancelled", True))
        self.assertEqual(self.writes, [])

    def test_native_mux_and_original_stereo_samples_are_both_retained(self):
        handle = self.facade.submit_task({"num_inference_steps": 50})
        self.assertFalse(self.facade.is_idle())
        self.job.done = True
        self.upstream.active_job = None  # Pinned api_cli finally ordering.
        result = handle.observe()
        self.assertTrue(self.facade.is_idle())
        self.assertEqual(set(result.outputs), {"video", "audio"})
        self.assertEqual(self.video.read_bytes(), b"cpu-test-video")
        self.assertEqual(self.writes, [([[.1, .2]], 32000)])
        self.assertEqual(self.upstream.calls[0]["num_inference_steps"], 50)
        self.assertIs(handle.observe(), result)
        self.assertEqual(len(self.writes), 1)

    def test_packaging_retry_uses_same_result_without_regeneration(self):
        attempts = []
        def writer(path, samples, rate):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("transient")
            path.write_bytes(b"recovered")
        self.facade.audio_writer = writer
        handle = self.facade.submit_task({})
        self.job.done = True
        with self.assertRaises(OSError):
            handle.observe()
        self.assertEqual(handle.observe().state, "succeeded")
        self.assertEqual(len(self.upstream.calls), 1)

    def test_no_mux_only_success_or_multioutput_ambiguity(self):
        handle = self.facade.submit_task({})
        self.job.done = True
        self.result.artifacts = []
        with self.assertRaisesRegex(BackendError, "audio_output_missing"):
            handle.observe()
        self.result.generated_files *= 2
        with self.assertRaisesRegex(BackendError, "result_shape_invalid"):
            handle.observe()

    def test_runtime_lookup_is_bound_to_the_verified_model_root(self):
        config = config_for_model_root(self.root)
        path = self.root / "wgp_config.json"
        path.write_text(json.dumps(config))
        _check_config(path, self.root)
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "model_root_mismatch"):
            _check_config(path, other)
        config["checkpoints_paths"].append(".")
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "model_root_mismatch"):
            _check_config(path)

    def test_output_escape_is_rejected(self):
        handle = self.facade.submit_task({})
        self.job.done = True
        self.result.generated_files = [str(self.root.parent / "unrelated.mp4")]
        with self.assertRaises(BackendError):
            handle.observe()

    def test_shutdown_refuses_an_active_job_and_blocks_later_submissions(self):
        self.facade.submit_task({})
        with self.assertRaisesRegex(NotReady, "shutdown_refused"):
            self.facade.close_when_idle()
        self.assertFalse(getattr(self.upstream, "closed", False))
        self.upstream.active_job = None
        self.facade.close_when_idle()
        self.assertTrue(self.upstream.closed)
        self.assertFalse(self.facade.is_idle())
        with self.assertRaises(NotReady):
            self.facade.submit_task({})

    def test_outer_thread_completion_cannot_hide_live_generation_thread(self):
        handle = self.facade.submit_task({})
        self.job.done = True
        self.upstream.active_job = None
        handle.worker_alive = lambda: True
        self.facade.worker_alive = lambda: True
        observed = handle.observe()
        self.assertEqual((observed.state, observed.stopped), ("unknown", False))
        self.assertEqual(self.quiesced, [])
        self.assertFalse(self.facade.is_idle())
        with self.assertRaises(NotReady):
            self.facade.close_when_idle()

    def test_cuda_quiescence_failure_is_not_terminal_stop_proof(self):
        def fail():
            raise RuntimeError("device context not proven idle")
        self.facade.quiesce = fail
        handle = self.facade.submit_task({})
        self.job.done = True
        with self.assertRaises(RuntimeError):
            handle.observe()
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()

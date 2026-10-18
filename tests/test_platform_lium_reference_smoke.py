"""State/graph unit tests with fake media metadata; no GPU or online calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.lium_bootstrap import BootError
from studio_platform.lium_reference_smoke import ReferenceSmoke
from studio_platform.worker import Outcome


class RefBackend:
    def __init__(self):
        self.uploads = self.submits = self.reconciles = 0
        self.busy = False
        self.unknown = False
        self.graph = None
        self.outcome = Outcome("running", "task-ref")

    def _json(self, method, path, **kwargs):
        if path == "/queue":
            return {"queue_running": [1] if self.busy else [], "queue_pending": []}
        assert path == "/upload/image"
        self.uploads += 1
        return {"name": kwargs["files"]["image"][0], "subfolder": "sixnine-qualification", "type": "input"}

    def submit(self, graph, tag):
        self.submits += 1
        self.graph = graph
        if self.unknown:
            raise OSError("lost")
        return "task-ref"

    def poll(self, *args):
        return self.outcome

    def reconcile(self, *args):
        self.reconciles += 1
        return self.outcome

    def fetch(self, job, tag, task, directory, heartbeat):
        return {"video": directory/"raw.mp4", "audio": directory/"raw.flac"}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bootstrap = {"tag": "boot-testidentity", "identity": {"intent_id": "test-intent"}, "evidence": {"outputs": {}}}
        for kind, filename in (("video", "raw.mp4"), ("audio", "raw.flac")):
            data = ("test-only-"+kind).encode()
            (self.directory/filename).write_bytes(data)
            self.bootstrap["evidence"]["outputs"][kind] = {"filename": filename, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.backend = RefBackend()
        self.save = lambda path, value: path.write_text(json.dumps(value))
        self.verifier = lambda paths, request: {"outputs": {"video": {}, "audio": {}}}
        self.inspect = patch("studio_platform.lium_reference_smoke.inspect", side_effect=lambda path, kind:
            {"kind": kind, "width": 512, "height": 512, "duration": 107/24, "has_audio": kind != "image"})
        self.ffmpeg = patch("studio_platform.lium_reference_smoke.ffmpeg", side_effect=lambda args: Path(args[-1]).write_bytes(b"fake-wav"))
        self.probe = patch("studio_platform.lium_reference_smoke.probe", return_value={"streams": [{"codec_type": "video", "avg_frame_rate": "24/1", "nb_frames": "107"}]})
        self.inspect.start(); self.ffmpeg.start()
        self.probe.start(); self.addCleanup(self.probe.stop)
        self.addCleanup(self.inspect.stop); self.addCleanup(self.ffmpeg.stop)

    def qualifier(self):
        return ReferenceSmoke(self.backend, lambda: 1000, self.save, self.verifier)

    def tick(self):
        return self.qualifier().tick(self.directory, self.bootstrap)

    def test_busy_never_uploads_or_submits(self):
        self.backend.busy = True
        self.assertEqual(self.tick()["state"], "reference_qualification_upstream_busy")
        self.assertEqual((self.backend.uploads, self.backend.submits), (0, 0))

    def test_actual_graph_has_all_input_families_and_guide(self):
        self.assertEqual(self.tick()["state"], "reference_running")
        graph = list(self.backend.graph.values())
        conditioning = next(n for n in graph if n["class_type"] == "MiniMaxH3ReferenceToVideo")["inputs"]
        for field in ("ref_images.ref_image_0", "ref_videos.ref_video_0", "ref_audios.ref_audio_0", "ref_video_audios.ref_video_audio_0"):
            self.assertIn(field, conditioning)
        self.assertTrue(any(n["class_type"] == "MiniMaxH3AddGuide" for n in graph))
        self.assertEqual((self.backend.uploads, self.backend.submits), (3, 1))
        self.backend.outcome = Outcome("succeeded", "task-ref")
        result = self.tick()
        self.assertEqual(result["state"], "qualified")
        self.assertEqual(set(result["evidence"]["input_evidence"]), {"own-image", "fl-video", "fl-audio"})
        self.tick()
        self.assertEqual((self.backend.uploads, self.backend.submits), (3, 1))

    def test_unknown_submission_restart_reconciles_never_reuploads_or_resubmits(self):
        self.backend.unknown = True
        self.assertEqual(self.tick()["state"], "reference_submission_unknown")
        self.backend.unknown = False
        self.assertEqual(self.tick()["state"], "reference_running")
        self.assertEqual((self.backend.uploads, self.backend.submits, self.backend.reconciles), (3, 1, 1))

    def test_bad_source_never_uploads(self):
        (self.directory/"raw.mp4").write_bytes(b"different")
        with self.assertRaisesRegex(BootError, "integrity"):
            self.tick()
        self.assertEqual((self.backend.uploads, self.backend.submits), (0, 0))

    def test_known_failure_not_retried(self):
        self.backend.outcome = Outcome("failed", "task-ref")
        self.assertEqual(self.tick()["state"], "reference_qualification_failed")
        self.assertEqual(self.tick()["state"], "reference_qualification_failed")
        self.assertEqual(self.backend.submits, 1)

    def test_full_profile_has_separate_receipt_and_does_not_reuse_small_smoke(self):
        self.backend.outcome = Outcome("succeeded", "task-ref")
        self.assertEqual(self.tick()["state"], "qualified")
        requests = []
        verify = lambda paths, request: requests.append(request) or {"outputs": {}}
        full = ReferenceSmoke(self.backend, lambda: 1000, self.save, verify, profile="full50_768p_5s")
        result = full.tick(self.directory, self.bootstrap)
        self.assertEqual(result["state"], "qualified")
        self.assertEqual(self.backend.submits, 2)
        self.assertEqual((requests[0]["steps"], requests[0]["resolution"], requests[0]["duration"]), (50, "768P", 5))
        self.assertTrue((self.directory/"reference-smoke/state.json").exists())
        self.assertTrue((self.directory/"reference-full-smoke/state.json").exists())
        full.tick(self.directory, self.bootstrap)
        self.assertEqual(self.backend.submits, 2)

    def test_unapproved_profile_is_rejected_without_io(self):
        with self.assertRaisesRegex(ValueError, "unsupported_reference"):
            ReferenceSmoke(self.backend, lambda: 1000, self.save, self.verifier, profile="arbitrary")
        self.assertEqual(self.backend.submits, 0)


if __name__ == "__main__":
    unittest.main()

"""Offline state/graph/fixture tests. No GPU, cloud or inference calls."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch

from studio_platform.lium_bootstrap import BootError
from studio_platform.lium_multimodal_smoke import FirstLastSmoke, BoundedReferenceSmoke
from studio_platform.lium_reference_smoke import ReferenceSmoke
from studio_platform.worker import Outcome
from test_platform_lium_reference_smoke import RefBackend


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU FFmpeg/ffprobe required")
class RealMultimodalFixtureTests(unittest.TestCase):
    def test_real_124_frame_aac_source_is_bounded_by_actual_reference_frames(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            video, audio = root/"raw.mp4", root/"raw.flac"
            commands = [
                ["-f", "lavfi", "-i", "color=c=red:s=1344x768:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000",
                 "-map", "0:v:0", "-map", "1:a:0", "-frames:v", "124", "-t", str(124/24), "-c:v", "libx264",
                 "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(video)],
                ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", str(124/24), "-ac", "2", "-c:a", "flac", str(audio)],
            ]
            for command in commands:
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2", *command],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=30)
            bootstrap = {"evidence": {"outputs": {
                kind: {"filename": path.name, "size_bytes": path.stat().st_size, "sha256": BoundedReferenceSmoke.file_hash(path)}
                for kind, path in (("video", video), ("audio", audio))}}}
            target = root/BoundedReferenceSmoke.name
            target.mkdir()
            helper = BoundedReferenceSmoke(None, lambda: 0, None, None)
            paths = helper.prepare(root, target, bootstrap)
            for ident, (path, kind) in paths.items():
                meta = helper.fixture_metadata(path, kind)
                if kind == "video":
                    self.assertEqual((meta["width"], meta["height"], meta["frame_count"], meta["fps"]), (832, 480, 107, 24))
                    self.assertEqual(meta["duration"], 107/24)
                    self.assertLessEqual(abs(meta["source_duration"]-meta["duration"]), .033)
                with self.subTest(kind=kind, metadata=meta):
                    helper.validate_fixture(ident, meta)


class MultimodalSmokeTests(unittest.TestCase):
    def test_fixture_muxer_rounding_preserves_exact_frame_duration_and_rejects_long_tracks(self):
        helper = self.qualifier(BoundedReferenceSmoke)
        for observed in (4.459, 4.48, 4.6):
            with self.subTest(container_duration=observed):
                self.inspect.side_effect = lambda path, kind: {"kind": "video", "width": 832, "height": 480,
                    "duration": observed, "source_duration": observed, "has_audio": True}
                meta = helper.fixture_metadata(Path("synthetic.mp4"), "video")
                self.assertEqual(meta["duration"], 107/24)
                self.assertEqual(meta["source_duration"], observed)
                if observed < 4.5:
                    helper.validate_fixture("video", meta)
                else:
                    with self.assertRaisesRegex(BootError, "fixture_shape"):
                        helper.validate_fixture("video", meta)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bootstrap = {"tag": "boot-synthetic", "identity": {"intent_id": "test"}, "evidence": {"outputs": {}}}
        for kind, filename in (("video", "raw.mp4"), ("audio", "raw.flac")):
            data = ("synthetic-"+kind).encode()
            (self.directory/filename).write_bytes(data)
            self.bootstrap["evidence"]["outputs"][kind] = {
                "filename": filename, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.backend = RefBackend()
        self.blocked = None
        self.acquired = True
        self.save = lambda p, v: p.write_text(json.dumps(v))
        self.verify = lambda paths, request: {"request": request, "outputs": {"video": {}, "audio": {}}}
        def metadata(path, kind):
            if kind == "image":
                return {"kind": kind, "width": 2048, "height": 2048, "duration": None, "has_audio": False}
            return {"kind": kind, "width": 832, "height": 480,
                    "duration": 107/24 if kind == "video" else 4.45, "has_audio": True}
        self.inspect = self.enterContext(patch("studio_platform.lium_multimodal_smoke.inspect", side_effect=metadata))
        self.ffmpeg = self.enterContext(patch("studio_platform.lium_multimodal_smoke.ffmpeg",
            side_effect=lambda args: Path(args[-1]).write_bytes(b"synthetic-normalized")))
        self.enterContext(patch("studio_platform.lium_multimodal_smoke.probe", return_value={"streams": [
            {"codec_type": "video", "avg_frame_rate": "24/1", "nb_frames": "107"}]}))

    def qualifier(self, cls=FirstLastSmoke):
        return cls(self.backend, lambda: 1000, self.save, self.verify,
            can_submit=lambda: self.blocked, collection_context=lambda: contextlib.nullcontext(self.acquired))

    def test_firstlast_both_images_are_in_real_graph_and_2048_fixture(self):
        helper = self.qualifier()
        result = helper.tick(self.directory, self.bootstrap)
        self.assertEqual(result["state"], "qualification_running")
        graph = list(self.backend.graph.values())
        inputs = next(n["inputs"] for n in graph if n["class_type"] == "MiniMaxH3ImageToVideo")
        self.assertIn("first_frame", inputs)
        self.assertIn("last_frame", inputs)
        self.assertEqual((self.backend.uploads, self.backend.submits), (2, 1))
        self.assertEqual(helper.request("synthetic")["steps"], 4)
        from PIL import Image
        with Image.open(self.directory/helper.name/"first.png") as image:
            self.assertEqual(image.size, (2048, 2048))

    def test_reference_normalizes_larger_fl_source_and_has_all_four_conditioning_families(self):
        helper = self.qualifier(BoundedReferenceSmoke)
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_running")
        graph = list(self.backend.graph.values())
        conditioning = next(n["inputs"] for n in graph if n["class_type"] == "MiniMaxH3ReferenceToVideo")
        for name in ("ref_images.ref_image_0", "ref_videos.ref_video_0", "ref_audios.ref_audio_0", "ref_video_audios.ref_video_audio_0"):
            self.assertIn(name, conditioning)
        self.assertTrue(any(n["class_type"] == "MiniMaxH3AddGuide" for n in graph))
        self.assertEqual((self.backend.uploads, self.backend.submits), (3, 1))
        self.assertEqual(helper.request("synthetic")["steps"], 4)
        calls = self.ffmpeg.call_args_list
        self.assertIn("scale=832:480,fps=24", calls[0].args[0])
        self.assertIn("107", calls[0].args[0])
        self.assertIn("4.45", calls[1].args[0])

    def test_unknown_then_drain_reconciles_same_tag_without_reupload_or_post(self):
        helper = self.qualifier()
        self.backend.unknown = True
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_submission_unknown")
        self.blocked = "qualification_not_started_draining"
        self.backend.unknown = False
        self.backend.outcome = Outcome("succeeded", "task-ref")
        self.assertEqual(self.qualifier().tick(self.directory, self.bootstrap)["state"], "qualified")
        self.assertEqual((self.backend.uploads, self.backend.submits, self.backend.reconciles), (2, 1, 1))
        self.assertEqual(self.qualifier().tick(self.directory, self.bootstrap)["state"], "qualified")
        self.assertEqual(self.backend.submits, 1)

    def test_drain_before_start_has_zero_uploads_or_inference(self):
        self.blocked = "qualification_not_started_draining"
        self.assertEqual(self.qualifier().tick(self.directory, self.bootstrap)["state"], self.blocked)
        self.assertEqual((self.backend.uploads, self.backend.submits), (0, 0))

    def test_preparation_expiring_deadline_does_not_post(self):
        helper = self.qualifier()
        original = helper.prepare
        def prepare(*args):
            value = original(*args)
            self.blocked = "qualification_deadline_insufficient"
            return value
        helper.prepare = prepare
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_deadline_insufficient")
        self.assertEqual((self.backend.uploads, self.backend.submits), (0, 0))

    def test_cpu_collection_waits_then_collects_without_resubmission(self):
        helper = self.qualifier()
        self.backend.outcome = Outcome("succeeded", "task-ref")
        self.acquired = False
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_collection_waiting")
        self.acquired = True
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualified")
        self.assertEqual(self.backend.submits, 1)

    def test_collected_bad_output_is_terminal_but_network_fetch_keeps_recovery(self):
        from studio_platform.worker import BackendError
        helper = self.qualifier()
        self.backend.outcome = Outcome("succeeded", "task-ref")
        original = self.backend.fetch
        self.backend.fetch = lambda *args: (_ for _ in ()).throw(OSError("synthetic connection lost"))
        with self.assertRaises(OSError):
            helper.tick(self.directory, self.bootstrap)
        self.backend.fetch = original
        helper.verify = lambda *args: (_ for _ in ()).throw(BackendError("output_video_verification_failed"))
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(self.backend.submits, 1)

    def test_completed_inference_missing_required_output_is_terminal(self):
        from studio_platform.worker import BackendError
        self.backend.outcome = Outcome("succeeded", "task-ref")
        self.backend.fetch = lambda *args: (_ for _ in ()).throw(BackendError("comfy_save_outputs_missing"))
        helper = self.qualifier()
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(self.backend.submits, 1)

    def test_wrong_fixture_dimensions_fail_before_any_upload(self):
        self.inspect.side_effect = lambda path, kind: {"kind": "image", "width": 512, "height": 512}
        self.assertEqual(self.qualifier().tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual((self.backend.uploads, self.backend.submits), (0, 0))

    def test_source_integrity_and_bootstrap_identity_are_bound(self):
        helper = self.qualifier(BoundedReferenceSmoke)
        (self.directory/"raw.mp4").write_bytes(b"different")
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(self.backend.uploads, 0)
        first = self.qualifier()
        first.tick(self.directory, self.bootstrap)
        self.bootstrap["identity"]["intent_id"] = "other"
        with self.assertRaisesRegex(BootError, "identity_conflict"):
            first.tick(self.directory, self.bootstrap)
        self.assertEqual(self.backend.submits, 1)

    def test_failed_inference_is_terminal_and_historical_receipt_is_not_reused(self):
        self.backend.outcome = Outcome("failed", "task-ref")
        helper = self.qualifier()
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(helper.tick(self.directory, self.bootstrap)["state"], "qualification_failed")
        self.assertEqual(self.backend.submits, 1)
        old = self.directory/"reference-full-smoke"
        old.mkdir()
        (old/"state.json").write_text(json.dumps({"phase": "qualified"}))
        self.backend.outcome = Outcome("succeeded", "task-ref")
        wrapped = ReferenceSmoke(self.backend, lambda: 1000, self.save, self.verify,
            profile=BoundedReferenceSmoke.name, can_submit=lambda: self.blocked)
        self.assertEqual(wrapped.tick(self.directory, self.bootstrap)["state"], "qualified")
        self.assertEqual(self.backend.submits, 2)


if __name__ == "__main__":
    unittest.main()

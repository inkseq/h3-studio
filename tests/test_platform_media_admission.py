"""Memory admission, fairness and durable retry tests; no cloud or GPU calls."""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import tempfile
import shutil
import unittest
import wave
import threading
import time
from unittest import mock
from sqlalchemy import create_engine

from studio_platform import media
import test_platform_assets as fixtures
from test_platform_storage import OfflineTest


class AdmissionTests(OfflineTest):
    def wait_for_queue(self, gate, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with gate._condition:
                if len(gate._waiting) == count:
                    return
            time.sleep(.001)
        self.fail("processing queue did not reach the expected bounded state")

    def test_two_light_preparations_but_no_third_or_video(self):
        gate = media.ProcessingAdmission(wait_seconds=.01)
        with gate.acquire("image"), gate.acquire("audio"):
            for kind in ("image", "video"):
                with self.assertRaises(media.MediaBusy):
                    with gate.acquire(kind):
                        self.fail("capacity exceeded")
        with gate.acquire("video"):
            with self.assertRaises(media.MediaBusy):
                with gate.acquire("audio"):
                    self.fail("video must be exclusive")
        self.assertEqual(gate._used, 0)
        self.assertEqual(len(gate._waiting), 0)

    def test_fifo_video_cannot_be_starved_by_later_small_work(self):
        gate = media.ProcessingAdmission(wait_seconds=3)
        order = []
        video_started, release_video = threading.Event(), threading.Event()
        def video():
            with gate.acquire("video"):
                order.append("video")
                video_started.set()
                self.assertTrue(release_video.wait(3))
        def image():
            with gate.acquire("image"):
                order.append("image")
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                with gate.acquire("image"):
                    first = executor.submit(video)
                    self.wait_for_queue(gate, 1)
                    second = executor.submit(image)
                    self.wait_for_queue(gate, 2)
                    self.assertEqual(order, [])
                self.assertTrue(video_started.wait(3))
                self.assertEqual(order, ["video"])
            finally:
                release_video.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(order, ["video", "image"])
        self.assertEqual(gate._used, 0)

    def test_timed_out_head_does_not_block_later_work(self):
        gate = media.ProcessingAdmission(wait_seconds=.05)
        with gate.acquire("image"), ThreadPoolExecutor(max_workers=1) as executor:
            def video():
                with gate.acquire("video"):
                    self.fail("video cannot share the image allocation")
            attempt = executor.submit(video)
            self.wait_for_queue(gate, 1)
            with self.assertRaises(media.MediaBusy):
                attempt.result(timeout=3)
            with gate.acquire("audio"):
                self.assertEqual(gate._used, 2)
        self.assertEqual(len(gate._waiting), 0)

    def test_all_exit_paths_release_allocation(self):
        gate = media.ProcessingAdmission(wait_seconds=0)
        for failure in (ValueError, KeyboardInterrupt):
            with self.assertRaises(failure):
                with gate.acquire("video"):
                    raise failure()
            with gate.acquire("video"):
                self.assertEqual(gate._used, 2)
        for invalid in (-1, float("nan"), float("inf"), True, "1"):
            with self.assertRaises(ValueError):
                media.ProcessingAdmission(wait_seconds=invalid)
        with self.assertRaises(media.MediaError):
            with gate.acquire("unknown"):
                self.fail("unknown media accepted")
        self.assertEqual(gate._used, 0)


class AssetAdmissionTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite:///" + (self.root / "assets.db").as_posix())
        self.addCleanup(self.engine.dispose)
        self.store = fixtures.FaultStore(self.root / "objects")
        self.service = self.make_service()

    def make_service(self):
        return fixtures.AssetService(self.engine, self.store, self.root, max_bytes=2*1024*1024)

    def upload(self, body=None):
        return self.service.upload("owner", "project", io.BytesIO(fixtures.png() if body is None else body),
                                   "reference.png", client_asset_id="local-image")

    def only_asset(self):
        return self.service.list("owner", "project")[0]

    def test_busy_upload_keeps_original_and_resumes_same_receipt(self):
        gate = media.ProcessingAdmission(wait_seconds=0)
        original = fixtures.png()
        with mock.patch.object(media, "PROCESSING_ADMISSION", gate):
            with gate.acquire("video"):
                with self.assertRaises(media.MediaBusy), mock.patch.object(media, "inspect") as inspect:
                    self.upload(original)
                inspect.assert_not_called()
            saved = self.only_asset()
            self.assertEqual(saved["status"], "failed")
            self.assertIn("恢复同一素材", saved["error"])
            receipt = self.service.journal.get("owner", saved["id"])
            self.assertEqual(self.service._file(receipt, "source.png").read_bytes(), original)
            self.assertEqual(self.service.usage("owner")["active_uploads"], 0)
            self.assertEqual(self.store.calls, [])
            resumed = self.make_service().resume("owner", saved["id"])
        self.assertEqual(resumed["id"], saved["id"])
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(len(self.service.list("owner", "project")), 1)

    def test_prepared_storage_recovery_does_not_wait_for_decoder(self):
        from studio_platform.storage import StorageWriteUncertain
        self.store.failure = "after"
        with self.assertRaises(StorageWriteUncertain):
            self.upload()
        saved = self.only_asset()
        gate = media.ProcessingAdmission(wait_seconds=0)
        with mock.patch.object(media, "PROCESSING_ADMISSION", gate), gate.acquire("video"):
            with mock.patch.object(media, "normalize", side_effect=AssertionError("must not decode again")):
                resumed = self.make_service().resume("owner", saved["id"])
        self.assertEqual(resumed["status"], "ready")

    def test_static_media_failure_reason_survives_refresh(self):
        message = "媒体处理失败、超时或达到资源限制；原素材保持不变"
        with mock.patch.object(media, "normalize", side_effect=media.MediaError(message)):
            with self.assertRaises(media.MediaError):
                self.upload()
        saved = self.make_service().list("owner", "project")[0]
        self.assertEqual(saved["status"], "failed")
        self.assertIn(message, saved["error"])
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires local CPU FFmpeg")
    def test_derivation_waits_before_copy_or_decode_and_resumes_same_id(self):
        original = io.BytesIO()
        with wave.open(original, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(32000)
            target.writeframes(b"\x00\x00" * (16*32000))
        parent = self.service.upload("owner", "project", io.BytesIO(original.getvalue()), "long.wav")
        self.assertEqual(parent["status"], "ready")
        self.assertFalse(parent["metadata"]["model_ready"])
        gate = media.ProcessingAdmission(wait_seconds=0)
        with mock.patch.object(media, "PROCESSING_ADMISSION", gate), gate.acquire("video"):
            with self.assertRaises(media.MediaBusy), mock.patch.object(media, "derive") as derive:
                self.service.derive("owner", parent["id"], 1, 3)
            derive.assert_not_called()
        selected = next(a for a in self.service.list("owner", "project") if a.get("parent_id"))
        receipt = self.service.journal.get("owner", selected["id"])
        self.assertFalse(self.service._stage(receipt).exists())
        self.assertEqual(selected["status"], "failed")
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)
        resumed = self.make_service().resume("owner", selected["id"])
        self.assertEqual(resumed["id"], selected["id"])
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["selection"], {"start": 1, "end": 3})

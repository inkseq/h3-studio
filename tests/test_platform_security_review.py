"""Public-boundary regressions with isolated files, fake inputs, no provider calls."""
from __future__ import annotations

import io
import asyncio
import json
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import unittest
import wave
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from studio_platform import media
from studio_platform.api import create_app
from studio_platform.assets import AssetService, AssetQuotaExceeded
from studio_platform.settings import Settings
from studio_platform.storage import LocalObjectStore
from test_platform_api import project


def wav(seconds=20):
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x7b\x00" * (8000*seconds))
    return out.getvalue()


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        original = socket.socket.connect
        def local_only(sock, address):
            # Windows asyncio creates a loopback socketpair for the ASGI client.
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original(sock, address)
            raise AssertionError("External network forbidden in security review")
        guard = mock.patch.object(socket.socket, "connect", local_only)
        guard.start()
        self.addCleanup(guard.stop)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required")
class DerivationAdmissionTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.engine = create_engine("sqlite:///"+(self.root / "test.sqlite").as_posix())
        self.addCleanup(self.engine.dispose)
        self.store = LocalObjectStore(self.root / "objects")
        self.service = self.restart()
        self.parent = self.service.upload("owner", "project", io.BytesIO(wav()), "original.wav")

    def restart(self):
        return AssetService(self.engine, self.store, self.root, max_bytes=1024*1024)

    def test_derivative_capacity_is_reserved_before_source_read_or_ffmpeg(self):
        self.service.journal.owner_limit = self.service.usage("owner")["accounted_bytes"]
        with mock.patch.object(self.store, "open", side_effect=AssertionError("Source read before admission")) as read:
            with mock.patch.object(media, "derive", side_effect=AssertionError("FFmpeg before admission")) as derive:
                with self.assertRaises(AssetQuotaExceeded):
                    self.service.derive("owner", self.parent["id"], 0, 3)
                read.assert_not_called()
                derive.assert_not_called()

    def test_derivative_active_limit_is_checked_before_ffmpeg(self):
        self.service.journal.owner_active = 1
        # A fake upload occupies the same persistent processing pool.
        receipt = self.service.journal.create("owner", dict(id="a"*32, project_id="project", status="validating", created_at=1))
        self.addCleanup(self.service.journal.release, receipt, 0)
        with mock.patch.object(media, "derive", side_effect=AssertionError("FFmpeg before admission")) as derive:
            with self.assertRaises(AssetQuotaExceeded):
                self.service.derive("owner", self.parent["id"], 0, 3)
            derive.assert_not_called()

    def test_interrupted_derivative_is_recoverable_without_new_asset(self):
        with mock.patch.object(media, "derive", side_effect=media.MediaError("Synthetic conversion interruption")):
            with self.assertRaises(media.MediaError):
                self.service.derive("owner", self.parent["id"], 0, 3)
        failed = next(a for a in self.service.list("owner", "project") if a["id"] != self.parent["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.service.usage("owner")["active_uploads"], 0)
        recovered = self.restart().resume("owner", failed["id"])
        self.assertEqual(recovered["id"], failed["id"])
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["parent_id"], self.parent["id"])
        self.assertAlmostEqual(recovered["metadata"]["source_duration"], 3, places=1)
        self.assertEqual(len(self.service.list("owner", "project")), 2)
        receipt = self.service.journal.get("owner", recovered["id"])
        self.assertFalse((self.service._stage(receipt) / receipt["derivation"]["input_name"]).exists())


class PublicMalformedInputTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.app = create_app(Settings(self.root, auth_mode="local-test"))
        self.addCleanup(self.app.state.repository.close)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_excessive_or_invalid_content_length_is_rejected(self):
        for value in ("9"*5000, "-1", "1x"):
            with self.subTest(length=value[:10]):
                response = self.client.post("/api/auth/login", content=b"{}", headers={"Content-Length": value})
                self.assertEqual(response.status_code, 413)

    def test_auth_database_failure_is_retryable_without_driver_details(self):
        for method, headers in (("session", {}), ("bearer", {"Authorization": "Bearer "+"a"*43})):
            with mock.patch.object(self.app.state.auth, method,
                    side_effect=OperationalError("SELECT synthetic-secret-canary", {}, RuntimeError("synthetic-driver-canary"))):
                result = self.client.get("/v1/projects", headers=headers)
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertNotIn("canary", result.text)

    def test_deep_json_does_not_escape_as_internal_error(self):
        response = self.client.post("/api/auth/login", content=("["*1500+"0"+"]"*1500),
                                    headers={"Content-Type": "application/json"})
        self.assertIn(response.status_code, {400, 413, 422})

    def test_unhashable_project_types_are_validation_errors(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "superdan"}).status_code, 200)
        for field in ("type", "status"):
            value = project()
            value["entities"][0][field] = []
            response = self.client.post("/v1/projects", json={"project": value})
            self.assertEqual(response.status_code, 422, field)

    def test_upload_admission_precedes_multipart_spooling_and_releases(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "superdan"}).status_code, 200)
        cookie = self.client.cookies.get("sixnine_session")

        async def exercise():
            released = asyncio.Event()
            entered = [asyncio.Event() for _ in range(3)]
            async def body(index):
                entered[index].set()
                await released.wait()
                yield b"--security-test--\r\n"
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                    headers={"Cookie": "sixnine_session="+cookie,
                             "Content-Type": "multipart/form-data; boundary=security-test"}) as client:
                tasks = [asyncio.create_task(client.post("/v1/assets", content=body(i))) for i in range(2)]
                try:
                    for event in entered[:2]:
                        await asyncio.wait_for(event.wait(), 3)
                    rejected = await asyncio.wait_for(client.post("/v1/assets", content=body(2)), 3)
                    self.assertEqual(rejected.status_code, 429)
                    self.assertFalse(entered[2].is_set(), "Rejected request body must not be read/spooled")
                finally:
                    released.set()
                    await asyncio.gather(*tasks)
                self.assertEqual(dict(self.app.state.upload_admission.owners), {})
                admitted = await client.post("/v1/assets", content=b"--security-test--\r\n")
                self.assertEqual(admitted.status_code, 422)
                self.assertEqual(dict(self.app.state.upload_admission.owners), {})
        asyncio.run(exercise())


class MediaBoundaryTests(IsolatedTests):
    def test_rejects_oversize_image_before_full_decode(self):
        image = mock.MagicMock()
        image.__enter__.return_value = image
        image.n_frames = 1
        image.format = "PNG"
        image.size = (10000, 10000)
        image.width, image.height = image.size
        with mock.patch.object(media.Image, "open", return_value=image), mock.patch.object(media.ImageOps, "exif_transpose", return_value=image):
            with self.assertRaises(media.MediaError):
                media.inspect(self.root / "not-needed.png", "image")
        image.load.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required")
    def test_disguised_concat_cannot_reference_other_file(self):
        (self.root / "private-canary.wav").write_bytes(wav(3))
        disguised = self.root / "uploaded.mp3"
        disguised.write_text("ffconcat version 1.0\nfile private-canary.wav\nduration 3\n", encoding="ascii")
        with self.assertRaises(media.MediaError):
            media.inspect(disguised, "audio")

    @unittest.skipUnless(shutil.which("ffprobe"), "CPU ffprobe required")
    def test_probe_does_not_export_unneeded_user_metadata(self):
        raw = wav(3)
        value = b"C"*65536+b"\x00\x00"
        info = b"INFO"+b"ICMT"+struct.pack("<I", len(value))+value
        raw = raw[:4]+struct.pack("<I", len(raw)-8+8+len(info))+raw[8:]+b"LIST"+struct.pack("<I", len(info))+info
        source = self.root / "metadata.wav"
        source.write_bytes(raw)
        result = media.probe(source)
        self.assertAlmostEqual(float(result["format"]["duration"]), 3)
        self.assertNotIn("tags", result["format"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required")
    def test_audio_normalization_and_derivation_reject_truncated_output(self):
        source = self.root / "source.wav"
        source.write_bytes(wav(3))
        metadata = media.inspect(source, "audio")
        for operation in (lambda: media.normalize(source, metadata, self.root, max_output_bytes=8192),
                          lambda: media.derive(source, metadata, self.root, 0, 3, max_output_bytes=8192)):
            with self.assertRaises(media.MediaError):
                operation()
        self.assertEqual(source.read_bytes(), wav(3))

    def test_image_normalization_writer_cannot_exceed_limit(self):
        from PIL import Image
        source = self.root / "source.png"
        Image.new("RGB", (256, 256), "navy").save(source)
        metadata = media.inspect(source, "image")
        with self.assertRaises(media.MediaError):
            media.normalize(source, metadata, self.root, max_output_bytes=64)
        self.assertLessEqual((self.root / "normalized.png").stat().st_size, 64)


if __name__ == "__main__":
    unittest.main()

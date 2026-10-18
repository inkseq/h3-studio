"""Local preview origin/identity boundaries; temporary CPU-only databases."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.settings import Settings
from tools.run_local_preview import preview_settings


class PreviewOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(preview_settings(self.temp.name))
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8845")
        self.addCleanup(self.app.state.repository.close)
        self.addCleanup(self.client.close)

    def test_vite_proxy_accepts_both_named_and_numeric_loopback_origins(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                headers = {"origin": f"http://{host}:8850", "sec-fetch-site": "same-origin"}
                result = self.client.post("/api/auth/login", json={"username": "superdan"}, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(self.client.get("/api/auth/me").json()["username"], "superdan")
                self.assertEqual(self.client.post("/api/auth/logout", headers=headers).status_code, 200)

    def test_other_origins_and_cross_site_headers_still_rejected(self):
        for origin in ("https://attacker.example", "http://127.0.0.1:9000", "http://192.168.1.2:8850"):
            with self.subTest(origin=origin):
                result = self.client.post("/api/auth/login", json={"username": "superdan"},
                                          headers={"origin": origin})
                self.assertEqual(result.status_code, 403)
        result = self.client.post("/api/auth/login", json={"username": "superdan"},
            headers={"origin": "http://127.0.0.1:8850", "sec-fetch-site": "cross-site"})
        self.assertEqual(result.status_code, 403)

    def test_local_preview_cannot_inherit_production_configuration(self):
        with patch.dict(os.environ, {"SIXNINE_DATABASE_URL": "unusable-production-source",
            "SIXNINE_PUBLIC_ORIGIN": "https://www.sixnine.art", "SIXNINE_EXECUTION_BACKEND": "comfy-worker",
            "SIXNINE_STORAGE_PROVIDER": "s3"}):
            settings = preview_settings(self.temp.name)
        self.assertEqual(settings.database_url, "sqlite:///" + (Path(self.temp.name) / "platform.sqlite3").as_posix())
        self.assertEqual(settings.public_origin, "")
        self.assertEqual(settings.storage_provider, "local")
        self.assertEqual(settings.execution_backend, "mock")

    def test_preview_exceptions_cannot_be_enabled_for_public_service(self):
        with self.assertRaises(ValueError):
            Settings(Path(self.temp.name), public_origin="https://www.sixnine.art",
                     local_ui_origins=("http://127.0.0.1:8850",))
        with self.assertRaises(ValueError):
            preview_settings(self.temp.name, api_port=8844)
        with self.assertRaises(ValueError):
            preview_settings(self.temp.name, ui_ports=())


if __name__ == "__main__":
    unittest.main()

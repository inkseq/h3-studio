"""Independent static releases; local temporary files only, no provider calls."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.frontend import FRONTEND_CONTRACT, HTML_ROUTES, select_html
from studio_platform.settings import Settings


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release_root = self.root / "frontend"
        self.bundle = self.root / "bundled"
        self.release_root.mkdir()
        self.bundle.mkdir()
        (self.bundle / "index.html").write_text(
            "<html>bundled</html>", encoding="utf-8"
        )
        (self.bundle / "assets").mkdir()
        (self.release_root / "assets").mkdir()
        self.settings = Settings(
            self.root / "data",
            auth_mode="local-test",
            frontend_dir=self.bundle,
            frontend_release_dir=self.release_root,
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def release(self, commit, body=None):
        folder = self.release_root / "releases" / commit
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(
            body or f"<html>{commit}</html>", encoding="utf-8"
        )

    def switch(self, commit):
        pointer = self.release_root / "next.json"
        pointer.write_text(
            json.dumps(
                {"version": 1, "commit": commit, "api_contract": FRONTEND_CONTRACT}
            ),
            encoding="utf-8",
        )
        os.replace(pointer, self.release_root / "current.json")

    def login(self):
        response = self.client.post("/api/auth/login", json={"username": "superdan"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_root_service_worker_scope_is_only_for_exact_hashed_video_cache_asset(self):
        for name in (
            "video-cache-worker-abcdefgh.js",
            "index-abcdefgh.js",
            "video-cache-worker.js",
        ):
            (self.release_root / "assets" / name).write_text(
                "// reviewed public source", encoding="utf-8"
            )
        folder = self.release_root / "assets" / "nested"
        folder.mkdir()
        (folder / "video-cache-worker-abcdefgh.js").write_text(
            "// nested", encoding="utf-8"
        )
        for method in ("GET", "HEAD"):
            response = self.client.request(
                method, "/assets/video-cache-worker-abcdefgh.js"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["service-worker-allowed"], "/")
            self.assertIn("immutable", response.headers["cache-control"])
            for path in (
                "/assets/index-abcdefgh.js",
                "/assets/video-cache-worker.js",
                "/assets/nested/video-cache-worker-abcdefgh.js",
                "/assets/missing-abcdefgh.js",
            ):
                self.assertNotIn(
                    "service-worker-allowed", self.client.request(method, path).headers
                )

    def test_known_html_routes_fall_back_only_before_first_publication(self):
        self.assertTrue(
            {
                "/library",
                "/library/",
                "/trash",
                "/trash/",
                "/connect/telegram",
                "/connect/telegram/",
            }.issubset(HTML_ROUTES)
        )
        for route in sorted(HTML_ROUTES):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "<html>bundled</html>")
                self.assertTrue(
                    response.headers["content-type"].startswith("text/html")
                )
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(
                    response.headers["x-sixnine-frontend-contract"], FRONTEND_CONTRACT
                )
                head = self.client.head(route)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(
                    head.headers["content-length"], response.headers["content-length"]
                )

    def test_switch_and_rollback_need_no_api_restart_and_keep_selected_path_stable(
        self,
    ):
        a, b = "a" * 40, "b" * 40
        self.release(a, "<html>release A</html>")
        self.release(b, "<html>release B longer</html>")
        self.switch(a)
        selected_path, selected_stat, selected_commit = select_html(self.settings)
        for commit, expected in (
            (a, "release A"),
            (b, "release B longer"),
            (a, "release A"),
        ):
            self.switch(commit)
            response = self.client.get("/freestyle/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.text)
            self.assertEqual(response.headers["x-sixnine-frontend-commit"], commit)
            self.assertEqual(
                int(response.headers["content-length"]), len(response.content)
            )
        self.assertEqual(selected_commit, a)
        self.assertEqual(
            selected_path, self.release_root / "releases" / a / "index.html"
        )
        self.assertEqual(
            selected_path.read_text(encoding="utf-8"), "<html>release A</html>"
        )
        self.assertEqual(selected_stat.st_size, selected_path.stat().st_size)

    def test_quick_chat_deep_link_is_public_shell_but_session_api_stays_private(self):
        for route in ("/quick-chat", "/quick-chat/"):
            response = self.client.get(route, params={"session": "session-example"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("<html>", response.text)
            self.assertEqual(self.client.head(route).status_code, 200)
        self.assertEqual(self.client.get("/v1/quick-chat/sessions").status_code, 401)
        self.assertEqual(
            self.client.get("/v1/quick-chat/sessions/session-example").status_code, 401
        )

    def test_assets_survive_switch_and_prefer_shared_external_copy(self):
        old_name = "index-old12345.js"
        new_name = "index-new12345.js"
        (self.bundle / "assets" / old_name).write_text("old chunk", encoding="utf-8")
        (self.bundle / "assets" / new_name).write_text(
            "bundle duplicate", encoding="utf-8"
        )
        (self.release_root / "assets" / new_name).write_text(
            "new chunk", encoding="utf-8"
        )
        (self.release_root / "assets" / "plain.svg").write_text(
            "<svg/>", encoding="utf-8"
        )
        for commit in ("a" * 40, "b" * 40):
            self.release(commit)
            self.switch(commit)
            for name, expected in ((old_name, "old chunk"), (new_name, "new chunk")):
                response = self.client.get("/assets/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, expected)
                self.assertEqual(
                    response.headers["cache-control"],
                    "public, max-age=31536000, immutable",
                )
                self.assertEqual(self.client.head("/assets/" + name).content, b"")
        self.assertEqual(
            self.client.get("/assets/plain.svg").headers["cache-control"], "no-cache"
        )
        missing = self.client.get("/assets/missing12345678.js")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["cache-control"], "no-store")

    def test_corrupt_or_incompatible_pointer_never_returns_bundled_html(self):
        self.release("a" * 40)
        valid = {"version": 1, "commit": "a" * 40, "api_contract": FRONTEND_CONTRACT}
        invalid = [
            b"not json",
            b"\xff",
            b"x" * 1025,
            b"[]",
            b"null",
            json.dumps({**valid, "version": True}).encode(),
            json.dumps({**valid, "version": 2}).encode(),
            json.dumps({**valid, "commit": "../bundled"}).encode(),
            json.dumps({**valid, "commit": "a" * 39}).encode(),
            json.dumps({**valid, "api_contract": "unreviewed-v2"}).encode(),
            json.dumps({**valid, "unrecognized": True}).encode(),
            (
                '{"version":1,"version":1,"commit":"'
                + "a" * 40
                + '","api_contract":"'
                + FRONTEND_CONTRACT
                + '"}'
            ).encode(),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:32]):
                (self.release_root / "current.json").write_bytes(raw)
                response = self.client.get("/app")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("bundled", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["retry-after"], "5")
        self.switch("f" * 40)
        self.assertEqual(self.client.get("/").status_code, 503)

    def test_metadata_and_unknown_paths_are_not_a_static_file_browser(self):
        commit = "a" * 40
        self.release(commit)
        self.switch(commit)
        (self.bundle / "source-manifest.json").write_text(
            '{"internal":"not public"}', encoding="utf-8"
        )
        self.assertEqual(self.client.get("/v1/projects").status_code, 401)
        self.assertEqual(
            self.client.get("/api/auth/config").headers["cache-control"], "no-store"
        )
        self.login()
        for path in (
            "/current.json",
            f"/releases/{commit}/index.html",
            "/source-manifest.json",
            "/unknown",
            "/v1/unknown",
            "/api/unknown",
            "/assets/.hidden",
            "/assets/%2e%2e/current.json",
            "/assets/%5c..%5ccurrent.json",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("not public", response.text)
                self.assertFalse(
                    response.headers["content-type"].startswith("text/html")
                )
        self.assertEqual(self.client.get("/v1/projects").status_code, 200)
        self.assertEqual(self.client.post("/app").status_code, 405)

    def test_static_fault_does_not_disable_api_or_health_contract(self):
        (self.release_root / "current.json").write_text("broken", encoding="utf-8")
        self.assertEqual(self.client.get("/").status_code, 503)
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["frontend_contract"], FRONTEND_CONTRACT)
        self.assertFalse(health.json()["generation_enabled"])
        self.assertFalse(health.json()["cloud_creation_enabled"])
        self.login()
        self.assertEqual(self.client.get("/v1/projects").status_code, 200)

    def test_external_only_and_bundled_only_configuration(self):
        for external in (False, True):
            with self.subTest(external=external):
                settings = Settings(
                    self.root / ("data-external" if external else "data-bundle"),
                    auth_mode="local-test",
                    frontend_dir=None if external else self.bundle,
                    frontend_release_dir=self.release_root if external else None,
                )
                with TestClient(create_app(settings)) as client:
                    self.assertEqual(
                        client.get("/").status_code, 503 if external else 200
                    )
                    if external:
                        self.release("a" * 40)
                        self.switch("a" * 40)
                        self.assertEqual(client.get("/app").status_code, 200)

    def make_link(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not available for this account")

    def test_pointer_and_asset_links_are_not_followed(self):
        outside = self.root / "private.txt"
        outside.write_text("private content", encoding="utf-8")
        pointer = self.release_root / "current.json"
        self.make_link(pointer, outside)
        self.assertEqual(self.client.get("/").status_code, 503)
        asset_name = "private-12345678.js"
        self.make_link(self.release_root / "assets" / asset_name, outside)
        (self.bundle / "assets" / asset_name).write_text(
            "must not mask external link", encoding="utf-8"
        )
        response = self.client.get("/assets/" + asset_name)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("private content", response.text)

    def test_release_directory_link_cannot_escape_fixed_root(self):
        commit = "a" * 40
        (self.release_root / "releases").mkdir()
        self.make_link(
            self.release_root / "releases" / commit, self.bundle, directory=True
        )
        self.switch(commit)
        self.assertEqual(self.client.get("/").status_code, 503)


class FrontendSettingsTests(unittest.TestCase):
    def test_quick_chat_preview_ignores_ambient_cloud_settings(self):
        from tools.run_quick_chat_preview import preview_settings

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    "SIXNINE_DATABASE_URL": "postgresql://invalid.example/not-preview",
                    "SIXNINE_GENERATION_ENABLED": "1",
                    "SIXNINE_EXECUTION_BACKEND": "wangp-worker",
                    "SIXNINE_RENDER_ENABLED": "1",
                    "SIXNINE_PUBLIC_ORIGIN": "https://example.invalid",
                    "SIXNINE_STORAGE_PROVIDER": "s3",
                },
            ),
        ):
            root = Path(temporary)
            settings = preview_settings(root / "isolated", root / "frontend")
            self.assertEqual(
                settings.database_url,
                "sqlite:///" + (root / "isolated/platform.sqlite3").as_posix(),
            )
            self.assertEqual(settings.auth_mode, "local-test")
            self.assertFalse(settings.generation_enabled)
            self.assertFalse(settings.render_enabled)
            self.assertEqual(settings.execution_backend, "disabled")
            self.assertEqual(settings.storage_provider, "local")
            self.assertEqual(settings.public_origin, "")

    def test_explicit_existing_directory_from_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "SIXNINE_DATA": str(root / "data"),
                    "SIXNINE_FRONTEND_RELEASE_DIR": str(root),
                },
                clear=True,
            ):
                settings = Settings.from_environment()
            self.assertEqual(settings.frontend_release_dir, root.resolve())
            self.assertIsNone(settings.frontend_dir)

    def test_invalid_roots_are_rejected_without_creating_or_moving_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary_file = root / "ordinary-file"
            ordinary_file.write_text("plain", encoding="utf-8")
            for invalid in (Path("relative"), root / "missing", ordinary_file):
                with self.subTest(path=str(invalid)), self.assertRaises(ValueError):
                    Settings(root / "data", frontend_release_dir=invalid)
            self.assertFalse((root / "missing").exists())
            self.assertEqual(ordinary_file.read_text(encoding="utf-8"), "plain")
            link = root / "linked"
            try:
                link.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                return  # The non-link rejection cases above remain applicable.
            with self.assertRaisesRegex(ValueError, "real directory"):
                Settings(root / "data", frontend_release_dir=link)


if __name__ == "__main__":
    unittest.main()

"""Protected runtime configuration with fake credentials and no network."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import inky_config as config, protected_config
from studio_platform.inky_runtime import configure
from studio_platform.settings import Settings


def valid():
    return {
        "enabled": True,
        **{
            service: {
                "profile": profile,
                "base_url": origin,
                "api_key": "FAKE_CREDENTIAL_FOR_TEST_ONLY",
            }
            for service, (profile, origin) in config.PROFILES.items()
        },
    }


class InkyConfigTests(unittest.TestCase):
    def test_closed_profiles_origins_caps_and_secret_safe_repr(self):
        credentials = config.validate(valid())
        self.assertNotIn("FAKE_CREDENTIAL", repr(credentials))
        self.assertIsNone(config.validate({"enabled": False}))
        cases = [{"enabled": 0}, {**valid(), "monthly_cap": 999}, {"enabled": True}]
        for field, value in (
            ("profile", "openai--rig-root"),
            ("base_url", "https://example.test"),
            ("api_key", "bad\nkey"),
            ("base_url", None),
        ):
            changed = copy.deepcopy(valid())
            changed["openai"][field] = value
            cases.append(changed)
        for value in cases:
            with self.subTest(value=list(value)):
                with self.assertRaisesRegex(
                    config.InkyConfigError, "^inky_runtime_config_invalid$"
                ):
                    config.validate(value)
        with self.assertRaises(config.InkyConfigError):
            json.loads(
                '{"enabled":false,"enabled":true}', object_pairs_hook=config.unique
            )

    def test_file_reader_rejects_missing_relative_and_unprotected_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"
            for name in ("relative", path):
                with self.assertRaisesRegex(
                    config.InkyConfigError, "^inky_runtime_config_invalid$"
                ):
                    config.runtime_config(name)
            path.write_text(json.dumps(valid()), encoding="utf-8")
            real = path.stat()
            protected = SimpleNamespace(
                st_mode=0o100440,
                st_uid=0,
                st_nlink=1,
                st_size=real.st_size,
                st_dev=real.st_dev,
                st_ino=real.st_ino,
            )
            with patch.object(protected_config.os, "fstat", return_value=protected):
                self.assertEqual(set(config.runtime_config(path)), set(config.PROFILES))
                protected.st_nlink = 2
                with self.assertRaises(config.InkyConfigError):
                    config.runtime_config(path)
                protected.st_nlink = 1
                protected.st_ino += 1
                with self.assertRaises(config.InkyConfigError):
                    config.runtime_config(path)
            path.write_text("x" * 8193, encoding="utf-8")
            with self.assertRaises(config.InkyConfigError):
                config.runtime_config(path)

    def test_disabled_missing_or_invalid_runtime_has_no_fallback_or_requests(self):
        app = SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(inky_config_file=None))
        )
        with patch("studio_platform.inky_runtime.runtime_config") as read:
            configure(app)
            read.assert_not_called()
        self.assertIsNone(app.state.inky_worker)
        app.state.settings.inky_config_file = "/run/secrets/inky"
        with patch(
            "studio_platform.inky_runtime.runtime_config",
            side_effect=RuntimeError("SECRET"),
        ):
            with self.assertLogs("studio_platform.inky_runtime", "WARNING") as logs:
                configure(app)
        self.assertEqual(
            logs.output,
            ["WARNING:studio_platform.inky_runtime:inky_runtime_unavailable"],
        )
        self.assertIsNone(app.state.inky_worker)

    def test_explicit_valid_config_constructs_without_loading_registry_or_http(self):
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(inky_config_file="/run/secrets/inky"),
                inky_store=SimpleNamespace(repo=object()),
                assets=object(),
            )
        )
        with (
            patch(
                "studio_platform.inky_runtime.runtime_config",
                return_value=config.validate(valid()),
            ),
            patch(
                "studio_platform.inky_providers.load_api",
                side_effect=AssertionError("registry forbidden"),
            ),
            patch("httpx.Client", side_effect=AssertionError("HTTP forbidden")),
        ):
            configure(app)
        self.assertIsNotNone(app.state.inky_worker)
        self.assertEqual(app.state.inky_worker.examples.max_cost_micros, 250000)
        with self.assertRaisesRegex(
            ValueError, "Inky credential path must be absolute"
        ):
            Settings(Path("."), inky_config_file=Path("relative"))

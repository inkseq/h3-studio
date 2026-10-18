"""Protected runtime DSN loading with synthetic local values only."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.settings import Settings, database_url_from_environment


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "database-url"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, value):
        self.path.write_bytes(value)
        self.path.chmod(0o600)
        os.environ["SIXNINE_DATABASE_URL_FILE"] = str(self.path)

    def test_default_and_explicit_direct_source(self):
        settings = Settings.from_environment()
        self.assertTrue(settings.database_url.startswith("sqlite:///"))
        os.environ["SIXNINE_DATABASE_URL"] = "sqlite:///synthetic.db"
        self.assertEqual(database_url_from_environment(), "sqlite:///synthetic.db")
        self.assertNotIn("synthetic", repr(Settings.from_environment()))

    def test_file_loaded_without_changing_environment(self):
        self.write(b"postgresql+psycopg://fake:synthetic-only@localhost/fake\r\n")
        self.assertEqual(
            database_url_from_environment(),
            "postgresql+psycopg://fake:synthetic-only@localhost/fake",
        )
        self.assertNotIn("SIXNINE_DATABASE_URL", os.environ)
        self.assertNotIn("synthetic-only", repr(Settings.from_environment()))

    def test_conflicting_sources_never_pick_one(self):
        self.write(b"sqlite:///synthetic.db")
        os.environ["SIXNINE_DATABASE_URL"] = "sqlite:///another.db"
        with self.assertRaisesRegex(ValueError, "only one"):
            database_url_from_environment()

    def test_admin_is_explicit_and_does_not_follow_rental_privilege(self):
        os.environ["SIXNINE_OPERATOR_CAPACITY_OWNERS"] = "superdan,supervan"
        self.assertEqual(Settings.from_environment().operator_admin_owners, ())
        os.environ["SIXNINE_OPERATOR_ADMIN_OWNERS"] = "superdan"
        self.assertEqual(
            Settings.from_environment().operator_admin_owners, ("superdan",)
        )
        for value in ("unknown", "superdan,superdan"):
            os.environ["SIXNINE_OPERATOR_ADMIN_OWNERS"] = value
            with self.assertRaisesRegex(ValueError, "explicit subset"):
                Settings.from_environment()
        os.environ["SIXNINE_OPERATOR_CAPACITY_OWNERS"] = "supervan"
        os.environ["SIXNINE_OPERATOR_ADMIN_OWNERS"] = "superdan"
        with self.assertRaisesRegex(ValueError, "explicit subset"):
            Settings.from_environment()

    def test_storyboard_flag_is_default_off_and_separate_from_operator_privilege(self):
        self.assertEqual(Settings.from_environment().storyboard_owners, ())
        os.environ["SIXNINE_STORYBOARD_OWNERS"] = " superdan, supervan "
        settings = Settings.from_environment()
        self.assertEqual(settings.storyboard_owners, ("superdan", "supervan"))
        self.assertEqual(settings.operator_capacity_owners, ())
        self.assertEqual(settings.operator_admin_owners, ())
        for value in ("unknown", "superdan,superdan"):
            with self.subTest(value=value):
                os.environ["SIXNINE_STORYBOARD_OWNERS"] = value
                with self.assertRaisesRegex(ValueError, "explicit existing"):
                    Settings.from_environment()
        for value in (["superdan"], ([],), (True,)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "explicit existing"):
                    Settings(self.path.parent, storyboard_owners=value)

    def test_invalid_files_reject_without_content_in_error(self):
        for raw in (
            b"",
            b"x" * 16385,
            b"postgresql://fake:synthetic-secret@host/db\nother",
            b"\xff",
            b" sqlite:///fake",
            b"sqlite:///fake\x00",
        ):
            with self.subTest(size=len(raw)):
                self.write(raw)
                with self.assertRaises(ValueError) as caught:
                    database_url_from_environment()
                self.assertNotIn("synthetic-secret", str(caught.exception))
        os.environ["SIXNINE_DATABASE_URL_FILE"] = "relative.txt"
        with self.assertRaisesRegex(ValueError, "absolute"):
            database_url_from_environment()
        os.environ["SIXNINE_DATABASE_URL_FILE"] = str(self.path.parent / "missing")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            database_url_from_environment()

    def test_stage_queries_are_disabled_without_explicit_absolute_config(self):
        self.assertIsNone(Settings.from_environment().stage_query_config_file)
        os.environ["SIXNINE_STAGE_QUERY_CONFIG_FILE"] = "relative.json"
        with self.assertRaisesRegex(ValueError, "absolute"):
            Settings.from_environment()
        os.environ["SIXNINE_STAGE_QUERY_CONFIG_FILE"] = str(self.path)
        self.assertEqual(Settings.from_environment().stage_query_config_file, self.path)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits; verified in Linux container")
    def test_posix_permissions(self):
        self.write(b"sqlite:///fake.db")
        for mode in (0o644, 0o666, 0o620, 0o601):
            self.path.chmod(mode)
            with self.assertRaisesRegex(ValueError, "permissions"):
                database_url_from_environment()
        self.path.chmod(0o640)
        self.assertEqual(database_url_from_environment(), "sqlite:///fake.db")


if __name__ == "__main__":
    unittest.main()

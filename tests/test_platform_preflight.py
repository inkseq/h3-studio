"""Synthetic host metadata only; never read a real secret or call a daemon."""

import copy
import importlib.util
from pathlib import Path, PurePosixPath
import stat
import tempfile
import unittest
from unittest.mock import patch


source = (
    Path(__file__).resolve().parents[1] / "deploy" / "platform" / "preflight_host.py"
)
spec = importlib.util.spec_from_file_location("sixnine_host_preflight_test", source)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def info(uid=0, gid=0, mode=0o755, *, regular=False):
    return {
        "mode": (stat.S_IFREG if regular else stat.S_IFDIR) | mode,
        "uid": uid,
        "gid": gid,
        "links": 1,
        "bytes": 64,
    }


def valid_host():
    dirs = {
        name: info() for name in ("root", "incoming", "releases", "approved-releases")
    }
    dirs["incoming"] = info(gid=1200, mode=0o2770)
    for name in ("platform-data", "upload-spool"):
        dirs[name] = info(10001, 10001, 0o700)
    for name in ("secret-root", "docker-config", "postgres"):
        dirs[name] = info(mode=0o700)
    dirs["postgres"] = info(70, 70, 0o700)
    return {
        "platform": "posix",
        "euid": 0,
        "parents": [info(), info()],
        "directories": dirs,
        "docker_config_entries": {},
        "secrets": {
            "db_admin_password": info(mode=0o400, regular=True),
            "app_database_url": info(gid=10001, mode=0o440, regular=True),
        },
        "secret_filesystems": dict.fromkeys(
            ("secret-root", "db_admin_password", "app_database_url"), "tmpfs"
        ),
        "docker": {"os": "linux", "cpus": 2, "memory_bytes": 4 * 1024**3},
    }


class PreflightTests(unittest.TestCase):
    def test_optional_title_file_requires_same_private_tmpfs_and_app_group(self):
        snapshot = valid_host()
        snapshot['secrets']['google_titles'] = info(gid=10001, mode=0o440, regular=True)
        snapshot['secret_filesystems']['google_titles'] = 'tmpfs'
        self.assertEqual(
            preflight.validate_snapshot(snapshot)['state'], 'host_metadata_ready'
        )
        for mutate in (
            lambda s: s['secret_filesystems'].update(google_titles='ext4'),
            lambda s: s['secrets']['google_titles'].update(gid=0),
            lambda s: s['secrets']['google_titles'].update(mode=stat.S_IFLNK | 0o440),
            lambda s: s['secrets']['google_titles'].update(mode=stat.S_IFREG | 0o644),
        ):
            bad = copy.deepcopy(snapshot)
            mutate(bad)
            with self.assertRaises(preflight.PreflightError):
                preflight.validate_snapshot(bad)

    def test_reviewed_two_core_host_is_accepted_without_process_or_secret_reads(self):
        with (
            patch.object(
                preflight.subprocess,
                "run",
                side_effect=AssertionError("No process in pure validation"),
            ),
            patch("builtins.open", side_effect=AssertionError("No content read")),
        ):
            result = preflight.validate_snapshot(valid_host())
        self.assertEqual(result["state"], "host_metadata_ready")
        self.assertEqual(result["cpu_count"], 2)
        self.assertFalse(result["secret_contents_checked"])
        self.assertFalse(result["services_started"])

    def test_actual_app_identity_and_secret_group_are_required(self):
        cases = [
            lambda s: s["directories"]["platform-data"].update(uid=0),
            lambda s: s["directories"]["upload-spool"].update(
                mode=stat.S_IFDIR | 0o777
            ),
            lambda s: s["secrets"]["app_database_url"].update(gid=0),
            lambda s: s["secrets"]["db_admin_password"].update(
                mode=stat.S_IFREG | 0o644
            ),
            lambda s: s["secrets"]["app_database_url"].update(links=2),
            lambda s: s["secrets"]["db_admin_password"].update(bytes=0),
            lambda s: s["directories"]["secret-root"].update(mode=stat.S_IFDIR | 0o770),
        ]
        for change in cases:
            value = valid_host()
            change(value)
            with (
                self.subTest(change=change),
                self.assertRaises(preflight.PreflightError),
            ):
                preflight.validate_snapshot(value)

    def test_links_rootless_small_host_and_persistent_secrets_fail_closed(self):
        cases = [
            lambda s: s.update(euid=1000),
            lambda s: s["parents"][0].update(mode=stat.S_IFLNK | 0o755),
            lambda s: s["directories"]["releases"].update(mode=stat.S_IFDIR | 0o777),
            lambda s: s["directories"]["postgres"].update(uid=10001, gid=10001),
            lambda s: s["directories"]["postgres"].update(uid=0, gid=0),
            lambda s: s["docker"].update(cpus=1),
            lambda s: s["docker"].update(memory_bytes=3584 * 1024**2 - 1),
            lambda s: s["docker"].update(os="windows"),
            lambda s: s["secret_filesystems"].update(app_database_url="ext4"),
            lambda s: s.update(
                docker_config_entries={"config.json": info(regular=True)}
            ),
        ]
        for change in cases:
            value = valid_host()
            change(value)
            with (
                self.subTest(change=change),
                self.assertRaises(preflight.PreflightError),
            ):
                preflight.validate_snapshot(value)

    def test_reviewed_postgres_data_identity_survives_first_boot(self):
        for uid in (70, 999):
            value = valid_host()
            value["directories"]["postgres"].update(uid=uid, gid=uid)
            self.assertEqual(
                preflight.validate_snapshot(value)["state"], "host_metadata_ready"
            )

    def test_mountinfo_uses_deepest_mount_including_bind_mounted_secret(self):
        mounts = (
            "1 0 8:1 / / rw - ext4 /dev/root rw\n"
            "2 1 0:20 / /run rw - tmpfs tmpfs rw\n"
            "3 2 8:1 /private/secret /run/sixnine-secrets/app_database_url ro - ext4 /dev/root ro\n"
        )
        self.assertEqual(
            preflight.filesystem_for(PurePosixPath("/run/sixnine-secrets"), mounts),
            "tmpfs",
        )
        self.assertEqual(
            preflight.filesystem_for(
                PurePosixPath("/run/sixnine-secrets/app_database_url"), mounts
            ),
            "ext4",
        )
        self.assertEqual(
            preflight.filesystem_for(PurePosixPath("/runaway/secret"), mounts), "ext4"
        )
        with self.assertRaises(preflight.PreflightError):
            preflight.filesystem_for(PurePosixPath("/run/secret"), "malformed")

    def test_empty_official_buildx_scaffold_allows_next_release_without_content_reads(
        self,
    ):
        entries = {
            name: info(mode=0o700)
            for name in (
                "buildx",
                "buildx/activity",
                "buildx/defaults",
                "buildx/instances",
            )
        }
        entries["buildx/.lock"] = info(mode=0o600, regular=True)
        entries["buildx/.lock"]["bytes"] = 0
        for value in ({}, {"buildx": entries["buildx"]}, entries):
            snapshot = valid_host()
            snapshot["docker_config_entries"] = value
            with (
                self.subTest(entries=set(value)),
                patch.object(
                    preflight.subprocess, "run", side_effect=AssertionError("No daemon")
                ),
                patch(
                    "builtins.open",
                    side_effect=AssertionError("No credential content read"),
                ),
            ):
                self.assertEqual(
                    preflight.validate_snapshot(snapshot)["state"],
                    "host_metadata_ready",
                )

    def test_cli_credentials_context_plugins_and_nonempty_buildx_state_remain_rejected(
        self,
    ):
        for name in (
            "config.json",
            "contexts",
            "cli-plugins",
            "buildx/config.toml",
            "buildx/current",
            "buildx/instances/default",
            "buildx/activity/default",
        ):
            entries = {"buildx": info(mode=0o700), name: info(mode=0o600, regular=True)}
            with self.subTest(name=name), self.assertRaises(preflight.PreflightError):
                preflight.validate_docker_config(entries)
        for name in (
            "buildx",
            "buildx/activity",
            "buildx/defaults",
            "buildx/instances",
            "buildx/.lock",
        ):
            for change in (
                lambda i: i.update(uid=10001),
                lambda i: i.update(gid=10001),
                lambda i: i.update(mode=stat.S_IFLNK | 0o700),
                lambda i: i.update(mode=stat.S_IFDIR | 0o770),
            ):
                entries = {"buildx": info(mode=0o700)}
                entry = (
                    info(mode=0o600, regular=True)
                    if name.endswith(".lock")
                    else info(mode=0o700)
                )
                entry["bytes"] = 0
                entries[name] = entry
                change(entry)
                with (
                    self.subTest(name=name, change=change),
                    self.assertRaises(preflight.PreflightError),
                ):
                    preflight.validate_docker_config(entries)
        for change in (
            lambda i: i.update(bytes=1),
            lambda i: i.update(links=2),
            lambda i: i.update(mode=stat.S_IFREG | 0o644),
        ):
            entries = {
                "buildx": info(mode=0o700),
                "buildx/.lock": info(mode=0o600, regular=True),
            }
            entries["buildx/.lock"]["bytes"] = 0
            change(entries["buildx/.lock"])
            with (
                self.subTest(change=change),
                self.assertRaises(preflight.PreflightError),
            ):
                preflight.validate_docker_config(entries)

    def test_collector_rejects_nonempty_scaffold_without_reading_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            buildx = root / "buildx"
            buildx.mkdir()
            for name in ("activity", "defaults", "instances"):
                (buildx / name).mkdir()
            (buildx / ".lock").write_bytes(b"")

            def fixture_metadata(path):
                value = path.lstat()
                result = info(
                    mode=0o600 if stat.S_ISREG(value.st_mode) else 0o700,
                    regular=stat.S_ISREG(value.st_mode),
                )
                result["bytes"] = value.st_size
                return result

            with (
                patch.object(preflight, "metadata", side_effect=fixture_metadata),
                patch.object(
                    Path, "open", side_effect=AssertionError("No file content read")
                ),
            ):
                self.assertEqual(
                    set(preflight.docker_config_metadata(root)),
                    {
                        "buildx",
                        "buildx/activity",
                        "buildx/defaults",
                        "buildx/instances",
                        "buildx/.lock",
                    },
                )
            for path in (root / "config.json", buildx / "instances" / "default"):
                path.write_bytes(b"unapproved state is never read")
                with (
                    self.subTest(path=path.relative_to(root)),
                    patch.object(preflight, "metadata", side_effect=fixture_metadata),
                    patch.object(
                        Path, "open", side_effect=AssertionError("No file content read")
                    ),
                    self.assertRaises(preflight.PreflightError),
                ):
                    preflight.docker_config_metadata(root)
                path.unlink()

    def test_collector_refuses_link_metadata_before_scanning_below_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "buildx").mkdir()
            original_scandir = preflight.os.scandir

            def root_only(path):
                self.assertEqual(
                    Path(path), root, "A rejected Buildx link was traversed"
                )
                return original_scandir(path)

            linked = info(mode=0o700)
            linked["mode"] = stat.S_IFLNK | 0o700
            with (
                patch.object(preflight, "metadata", return_value=linked),
                patch.object(preflight.os, "scandir", side_effect=root_only),
                self.assertRaises(preflight.PreflightError),
            ):
                preflight.docker_config_metadata(root)


if __name__ == "__main__":
    unittest.main()

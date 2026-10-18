"""Offline archive transfer checks: real temp files, no SSH/provider/GPU calls."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from studio_platform.lium_bootstrap import BootError
from studio_platform import wangp_bootstrap
from studio_platform.wangp_bootstrap import (
    DEPENDENCY_NAME, MAX_DEPENDENCY_BYTES, WanGPSSHHost, dependency_source,
)


CHUNK = 8 * 1024**2
REMOTE_CACHE = "/root/sixnine-cache"


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            value.update(chunk)
    return value.hexdigest()


class GuardedSource:
    """Fail on an unbounded/full-archive read while recording actual read sizes."""
    def __init__(self, path):
        self.path = path
        self.read_sizes = []
        self.read_bytes = 0

    def open(self, mode):
        if mode != "rb":
            raise AssertionError("The local archive is read-only")
        owner, source = self, self.path.open(mode)

        class Reader:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                source.close()

            def read(self, amount=-1):
                if not 0 < amount <= CHUNK:
                    raise AssertionError("Archive read is not bounded to 8 MiB")
                value = source.read(amount)
                owner.read_sizes.append(amount)
                owner.read_bytes += len(value)
                return value

            def tell(self):
                return source.tell()

        return Reader()


class LocalRemote:
    """Run the actual remote verification snippets against an isolated local root."""
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        self.opens = []
        self.writes = []
        self.pipelined = []
        self.scripts = []
        self.corrupt_write = False
        self.interrupt_after_write = False
        self.after_write = lambda: None
        self.channels = []
        self.open_timeouts = []
        self.open_error = None

    def get_transport(self):
        return self

    def open_session(self, *, timeout):
        self.open_timeouts.append(timeout)
        if self.open_error is not None:
            raise self.open_error

        class Channel:
            def __init__(self):
                self.closed = False
                self.timeout = None
                self.subsystems = []

            def settimeout(self, value):
                self.timeout = value

            def invoke_subsystem(self, name):
                if self.closed:
                    raise EOFError("Synthetic closed SFTP channel")
                self.subsystems.append(name)

            def close(self):
                self.closed = True

        channel = Channel()
        self.channels.append(channel)
        return channel

    def sftp_client(self, channel):
        if channel.closed:
            raise EOFError("Synthetic closed SFTP channel")
        return self.open_sftp()

    def run(self, script, **kwargs):
        anchor = "Path('/root/sixnine-cache')"
        if script.count(anchor) != 1:
            raise AssertionError("Only the bounded archive verification scripts may run")
        self.scripts.append(script)
        output = io.StringIO()
        with redirect_stdout(output):
            # All paths in these source-owned snippets derive from this one root.
            exec(compile(script.replace(anchor, "Path(" + repr(str(self.root)) + ")"),
                         "<offline-transfer-verification>", "exec"), {})
        return json.loads(output.getvalue())

    def open_sftp(self):
        remote = self

        class SFTP:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def close(self):
                pass

            def open(self, name, mode):
                expected = REMOTE_CACHE + "/" + DEPENDENCY_NAME + ".partial"
                if name != expected or mode not in {"wx", "ab"}:
                    raise AssertionError("Unexpected SFTP target or write mode")
                remote.opens.append((name, mode))
                target = (remote.root / (DEPENDENCY_NAME + ".partial")).open(
                    "xb" if mode == "wx" else "ab")

                class Writer:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        target.close()

                    def set_pipelined(self, enabled):
                        remote.pipelined.append(enabled)

                    def write(self, data):
                        if not 0 < len(data) <= CHUNK:
                            raise AssertionError("Archive write is not bounded to 8 MiB")
                        remote.writes.append(len(data))
                        if remote.corrupt_write:
                            data = bytes([data[0] ^ 1]) + data[1:]
                            remote.corrupt_write = False
                        count = target.write(data)
                        remote.after_write()
                        if remote.interrupt_after_write:
                            remote.interrupt_after_write = False
                            raise ConnectionResetError("Synthetic transfer interruption")
                        return count

                return Writer()

        return SFTP()


class WanGPTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / DEPENDENCY_NAME
        # More than two transport chunks, without constructing the archive in RAM.
        block = bytes(range(256)) * 4096
        with self.archive.open("xb") as target:
            for _ in range(18):
                target.write(block)
            target.write(b"archive-end")
        self.expected = checksum(self.archive)
        self.size = self.archive.stat().st_size
        self.remote = LocalRemote(self.root / "remote")
        self.host = object.__new__(WanGPSSHHost)
        self.host.ensure_connected = Mock()
        self.host.run = self.remote.run
        self.host.client = self.remote
        self.host.start = Mock(side_effect=AssertionError("Transfer must not launch a runtime"))
        sftp = patch("paramiko.SFTPClient", side_effect=self.remote.sftp_client)
        sftp.start()
        self.addCleanup(sftp.stop)
        self.source = GuardedSource(self.archive)
        self.final = self.remote.root / DEPENDENCY_NAME
        self.partial = self.remote.root / (DEPENDENCY_NAME + ".partial")

    def copy_prefix(self, length, destination):
        with self.archive.open("rb") as source, destination.open("xb") as target:
            remaining = length
            while remaining:
                data = source.read(min(CHUNK, remaining))
                target.write(data)
                remaining -= len(data)

    def transfer(self, expected=None):
        self.host._upload_dependency(self.source, self.expected if expected is None else expected, self.size)

    def assert_published(self, prefix_size, mode):
        self.assertEqual(checksum(self.final), self.expected)
        self.assertEqual(self.final.stat().st_size, self.size)
        self.assertFalse(self.partial.exists())
        self.assertEqual([value[1] for value in self.remote.opens], [mode])
        self.assertEqual(sum(self.remote.writes), self.size - prefix_size)
        self.assertTrue(all(size <= CHUNK for size in self.remote.writes))
        self.assertTrue(all(size <= CHUNK for size in self.source.read_sizes))
        self.assertEqual(self.source.read_bytes, self.size)
        self.assertEqual(self.remote.pipelined, [True])
        self.assertEqual(len(self.remote.scripts), 2)
        self.assertTrue(all(0 < value <= wangp_bootstrap.SFTP_OPEN_SECONDS
                            for value in self.remote.open_timeouts))
        self.assertTrue(all(channel.closed and 0 < channel.timeout <= wangp_bootstrap.SFTP_IO_SECONDS
                            and channel.subsystems == ["sftp"] for channel in self.remote.channels))
        self.host.start.assert_not_called()

    def test_new_archive_streams_bounded_chunks_then_publishes(self):
        self.transfer()
        self.assert_published(0, "wx")
        self.assertGreater(len(self.remote.writes), 2)

    def test_valid_partial_resumes_after_hashing_prefix(self):
        length = CHUNK + 13
        self.copy_prefix(length, self.partial)
        self.transfer()
        self.assert_published(length, "ab")

    def test_existing_zero_byte_partial_is_appended_not_recreated(self):
        self.partial.touch()
        self.transfer()
        self.assert_published(0, "ab")

    def test_complete_matching_archive_never_reopens_or_republishes(self):
        self.copy_prefix(self.size, self.final)
        self.transfer()
        self.assertEqual(self.remote.opens, [])
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(len(self.remote.scripts), 1)
        self.assertEqual(checksum(self.final), self.expected)
        self.host.start.assert_not_called()

    def test_complete_archive_with_wrong_expected_digest_is_preserved(self):
        self.copy_prefix(self.size, self.final)
        with self.assertRaisesRegex(BootError, "wangp_dependency_existing_mismatch"):
            self.transfer("0" * 64)
        self.assertEqual(checksum(self.final), self.expected)
        self.assertEqual(self.remote.opens, [])
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(len(self.remote.scripts), 1)
        self.host.start.assert_not_called()

    def test_bad_partial_prefix_is_not_appended_or_deleted(self):
        self.partial.write_bytes(b"incorrect existing prefix")
        previous = checksum(self.partial)
        with self.assertRaisesRegex(BootError, "wangp_dependency_existing_mismatch"):
            self.transfer()
        self.assertEqual(checksum(self.partial), previous)
        self.assertEqual(self.remote.opens, [])
        self.assertFalse(self.final.exists())
        self.host.start.assert_not_called()

    def test_remote_corruption_never_publishes_a_complete_artifact(self):
        self.remote.corrupt_write = True
        with self.assertRaisesRegex(ValueError, "artifact_digest"):
            self.transfer()
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.is_file())
        self.assertNotEqual(checksum(self.partial), self.expected)
        self.host.start.assert_not_called()

    def test_local_digest_mismatch_does_not_publish_or_start(self):
        with self.assertRaisesRegex(BootError, "wangp_dependency_source_hash_mismatch"):
            self.transfer("0" * 64)
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.is_file())
        self.assertEqual(len(self.remote.scripts), 1)
        self.host.start.assert_not_called()

    def test_interrupted_write_preserves_valid_prefix_then_resumes(self):
        self.remote.interrupt_after_write = True
        with self.assertRaises(ConnectionResetError):
            self.transfer()
        self.assertEqual(self.partial.stat().st_size, CHUNK)
        self.assertFalse(self.final.exists())
        self.assertTrue(self.remote.channels[0].closed)
        self.host.start.assert_not_called()
        self.transfer()
        self.assertEqual(checksum(self.final), self.expected)
        self.assertEqual([item[1] for item in self.remote.opens], ["wx", "ab"])
        self.assertEqual(sum(self.remote.writes), self.size)
        self.assertFalse(self.partial.exists())
        self.host.start.assert_not_called()

    def test_overall_deadline_interrupts_transfer_and_retains_resume_prefix(self):
        clock = [10.0]
        self.remote.after_write = lambda: clock.__setitem__(0, 10.0 + wangp_bootstrap.DEPENDENCY_TRANSFER_SECONDS + 1)
        with patch.object(wangp_bootstrap.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(BootError, "wangp_dependency_transfer_timeout"):
                self.transfer()
        self.assertEqual(self.partial.stat().st_size, CHUNK)
        self.assertFalse(self.final.exists())
        self.assertTrue(self.remote.channels[0].closed)
        self.host.start.assert_not_called()
        self.remote.after_write = lambda: None
        self.transfer()
        self.assertEqual(checksum(self.final), self.expected)
        self.assertEqual([item[1] for item in self.remote.opens], ["wx", "ab"])
        self.host.start.assert_not_called()

    def test_channel_open_has_explicit_timeout_and_never_writes_on_failure(self):
        self.remote.open_error = TimeoutError("Synthetic channel open timeout")
        with self.assertRaises(TimeoutError):
            self.transfer()
        self.assertEqual(len(self.remote.open_timeouts), 1)
        self.assertLessEqual(self.remote.open_timeouts[0], wangp_bootstrap.SFTP_OPEN_SECONDS)
        self.assertEqual(self.remote.opens, [])
        self.assertFalse(self.partial.exists())
        self.host.start.assert_not_called()

    def test_subsystem_guard_closes_channel_even_before_negotiation_returns(self):
        guards = []

        class ExpiringTimer:
            def __init__(self, seconds, callback):
                self.seconds, self.callback, self.cancelled = seconds, callback, False
                guards.append(self)

            def start(self):
                self.callback()

            def cancel(self):
                self.cancelled = True

        with patch.object(wangp_bootstrap.threading, "Timer", ExpiringTimer):
            with self.assertRaisesRegex(BootError, "wangp_dependency_transfer_timeout"):
                self.transfer()
        self.assertTrue(guards[0].cancelled)
        self.assertLessEqual(guards[0].seconds, wangp_bootstrap.SFTP_OPEN_SECONDS)
        self.assertTrue(self.remote.channels[0].closed)
        self.assertEqual(self.remote.opens, [])
        self.host.start.assert_not_called()


class WanGPDependencySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / DEPENDENCY_NAME
        self.path.write_bytes(b"offline package")
        self.config = SimpleNamespace(source_dir=self.root)
        self.runtime = {"dependency_artifact_path": REMOTE_CACHE + "/" + DEPENDENCY_NAME,
                        "dependency_artifact_sha256": checksum(self.path)}

    def test_source_is_bound_to_exact_target_and_recorded_size(self):
        self.assertEqual(dependency_source(self.config, self.runtime),
                         (self.path, self.runtime["dependency_artifact_sha256"], self.path.stat().st_size))
        self.assertIsNone(dependency_source(self.config, {"dependency_artifact_path": ""}))

    def test_wrong_target_or_missing_digest_fails_before_transfer(self):
        for change in ({"dependency_artifact_path": "/workspace/" + DEPENDENCY_NAME},
                       {"dependency_artifact_sha256": ""},
                       {"dependency_artifact_sha256": "A" * 64}):
            with self.subTest(change=change), self.assertRaisesRegex(BootError, "wangp_dependency_binding_invalid"):
                dependency_source(self.config, {**self.runtime, **change})

    def test_linked_source_is_rejected(self):
        target = self.root / "actual-package"
        self.path.rename(target)
        try:
            self.path.symlink_to(target)
        except OSError as error:
            self.skipTest("This host cannot create test symlinks: " + type(error).__name__)
        with self.assertRaisesRegex(BootError, "wangp_dependency_source_untrusted"):
            dependency_source(self.config, self.runtime)

    def test_hardlinked_source_is_rejected(self):
        os.link(self.path, self.root / "second-link")
        with self.assertRaisesRegex(BootError, "wangp_dependency_source_untrusted"):
            dependency_source(self.config, self.runtime)

    def test_directory_empty_or_oversized_source_is_rejected(self):
        for mode, size in ((stat.S_IFDIR | 0o700, 64), (stat.S_IFREG | 0o600, 0),
                           (stat.S_IFREG | 0o600, MAX_DEPENDENCY_BYTES + 1)):
            info = SimpleNamespace(st_mode=mode, st_size=size, st_nlink=1)
            with self.subTest(mode=mode, size=size), patch.object(Path, "lstat", return_value=info):
                with self.assertRaisesRegex(BootError, "wangp_dependency_source_untrusted"):
                    dependency_source(self.config, self.runtime)


if __name__ == "__main__":
    unittest.main()

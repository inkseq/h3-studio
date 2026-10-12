"""Protected R2 runtime ingress; synthetic files and no credential/network IO."""
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from studio_platform import storage_config
from studio_platform.settings import Settings
from studio_platform.storage_config import (S3Credentials, S3StorageConfig, StorageConfigurationError,
    load_configured_storage_credentials, load_runtime_storage_credentials)


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sixnine-r2-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "r2_storage"
        self.config = S3StorageConfig("r2", "https://"+"a"*32+".r2.cloudflarestorage.com", "auto",
            "sixnine-test-private", "cloudflare-r2", "cloudflare-r2--rig-root", enabled=True)
        self.payload = {"schema_version": 1, "service": self.config.service, "profile": self.config.profile,
            "endpoint_url": self.config.endpoint_url, "region": self.config.region, "bucket": self.config.bucket,
            "access_key_id": "synthetic-access", "secret_access_key": "synthetic-secret-never-display"}
        network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("No network allowed"))
        network.start()
        self.addCleanup(network.stop)
        self.original_fstat = os.fstat
        if os.name != "nt" and os.geteuid() != 0:
            # CI is unprivileged. Simulate only the owner of the opened fixture;
            # permission/type/inode/size checks still use real temporary files.
            ownership = mock.patch.object(storage_config.os, "fstat", side_effect=lambda fd: self.info(fd, st_uid=0))
            ownership.start()
            self.addCleanup(ownership.stop)
        self.write()

    def info(self, fd, **changes):
        actual = self.original_fstat(fd)
        fields = {name: getattr(actual, name) for name in
                  ("st_mode", "st_nlink", "st_dev", "st_ino", "st_uid", "st_size")}
        return SimpleNamespace(**dict(fields, **changes))

    def write(self, payload=None, raw=None):
        if self.path.exists():
            self.path.chmod(0o600)
        self.path.write_bytes(raw if raw is not None else json.dumps(self.payload if payload is None else payload).encode())
        self.path.chmod(0o440)

    def test_exact_runtime_identity_loaded_without_environment_mutation(self):
        before = dict(os.environ)
        with mock.patch.object(storage_config, "load_storage_credentials", side_effect=AssertionError("No central loader")):
            result = load_configured_storage_credentials(self.config, credentials_file=self.path,
                registry_root=self.root / "not-loaded")
        self.assertIsInstance(result, S3Credentials)
        self.assertEqual(result._access, self.payload["access_key_id"])
        self.assertEqual(result._secret, self.payload["secret_access_key"])
        self.assertNotIn("synthetic", repr(result))
        self.assertEqual(dict(os.environ), before)

    def test_explicit_file_failure_does_not_fall_back_to_central(self):
        with mock.patch.object(storage_config, "load_storage_credentials", side_effect=AssertionError("No fallback")):
            with self.assertRaises(StorageConfigurationError) as caught:
                load_configured_storage_credentials(self.config, credentials_file=self.root / "missing",
                    registry_root=self.root)
        self.assertNotIn("missing", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_absent_file_keeps_existing_central_profile_interface(self):
        expected = S3Credentials("synthetic-access", "synthetic-secret")
        with mock.patch.object(storage_config, "load_storage_credentials", return_value=expected) as central:
            actual = load_configured_storage_credentials(self.config, registry_root=self.root)
        self.assertIs(actual, expected)
        central.assert_called_once_with(self.config, fields=storage_config.R2_CREDENTIAL_FIELDS, registry_root=self.root)

    def test_runtime_identity_mismatch_and_invalid_json_are_static_errors(self):
        invalid = [dict(self.payload, **{field: value}) for field, value in (
            ("service", "aws-s3"), ("profile", "cloudflare-r2--other"), ("endpoint_url", "https://private.invalid"),
            ("region", "us-east-1"), ("bucket", "another-bucket"), ("schema_version", True),
            ("secret_access_key", "synthetic-secret\x00"), ("session_token", None))]
        invalid += [dict(self.payload, unexpected="synthetic-secret-never-display")]
        for payload in invalid:
            with self.subTest(fields=set(payload)):
                self.write(payload)
                with self.assertRaises(StorageConfigurationError) as caught:
                    load_runtime_storage_credentials(self.config, self.path)
                self.assertEqual(str(caught.exception), "Protected storage credentials are unavailable or invalid")
        for raw in (b"", b"{bad-secret", b"x"*16385, b"\xff",
                    json.dumps(self.payload).encode()[:-1]+b',"profile":"cloudflare-r2--rig-root"}'):
            self.write(raw=raw)
            with self.assertRaises(StorageConfigurationError):
                load_runtime_storage_credentials(self.config, self.path)

    def test_session_token_is_optional_and_explicit_when_present(self):
        self.write(dict(self.payload, session_token="synthetic-session"))
        self.assertEqual(load_runtime_storage_credentials(self.config, self.path)._token, "synthetic-session")
        self.write()
        self.assertIsNone(load_runtime_storage_credentials(self.config, self.path)._token)

    def test_runtime_file_requires_enabled_r2(self):
        for config in (S3StorageConfig("r2", self.config.endpoint_url, "auto", self.config.bucket,
                                      self.config.service, self.config.profile, enabled=False),
                       S3StorageConfig("aws-s3", "https://s3.ap-southeast-1.amazonaws.com", "ap-southeast-1",
                                      self.config.bucket, "aws-s3", "aws-s3--test", enabled=True)):
            with self.assertRaises(StorageConfigurationError):
                load_runtime_storage_credentials(config, self.path)

    def test_nonregular_and_hardlinked_files_rejected_before_open(self):
        directory = self.root / "directory"
        directory.mkdir()
        os.link(self.path, self.root / "second-link")
        with mock.patch.object(storage_config.os, "open", side_effect=AssertionError("No unsafe open")):
            for path in (directory, self.path):
                with self.assertRaises(StorageConfigurationError):
                    load_runtime_storage_credentials(self.config, path)

    def test_relative_path_and_symbolic_link_rejected(self):
        with self.assertRaises(StorageConfigurationError):
            load_runtime_storage_credentials(self.config, "relative.json")
        linked = self.root / "linked"
        try:
            linked.symlink_to(self.path)
        except OSError:
            self.skipTest("Windows fixture has no symlink capability")
        with self.assertRaises(StorageConfigurationError):
            load_runtime_storage_credentials(self.config, linked)

    def test_changed_open_inode_rejected(self):
        with mock.patch.object(storage_config.os, "fstat", side_effect=lambda fd: self.info(fd, st_uid=0, st_ino=0)):
            with self.assertRaises(StorageConfigurationError):
                load_runtime_storage_credentials(self.config, self.path)

    @unittest.skipIf(os.name == "nt", "Linux runtime ownership and mode contract")
    def test_linux_root_ownership_and_private_readonly_group(self):
        for mode in (0o644, 0o660, 0o450, 0o441):
            self.path.chmod(mode)
            with self.assertRaises(StorageConfigurationError):
                load_runtime_storage_credentials(self.config, self.path)
        self.path.chmod(0o440)
        with mock.patch.object(storage_config.os, "fstat", side_effect=lambda fd: self.info(fd, st_uid=1000)):
            with self.assertRaises(StorageConfigurationError):
                load_runtime_storage_credentials(self.config, self.path)

    def test_settings_load_path_without_reading_credential_or_starting_cloud(self):
        remote = {"SIXNINE_STORAGE_PROVIDER": "r2", "SIXNINE_STORAGE_ENDPOINT": self.config.endpoint_url,
            "SIXNINE_STORAGE_BUCKET": self.config.bucket, "SIXNINE_STORAGE_PROFILE": self.config.profile,
            "SIXNINE_STORAGE_REGION": "auto", "SIXNINE_STORAGE_CREDENTIAL_FILE": str(self.path)}
        with mock.patch.dict(os.environ, remote, clear=True), mock.patch.object(Path, "open", side_effect=AssertionError("Settings cannot read secrets")):
            self.assertEqual(Settings.from_environment().storage_credential_file, self.path)
        for changes in ({"SIXNINE_STORAGE_PROVIDER": "local"}, {"SIXNINE_STORAGE_CREDENTIAL_FILE": "relative"}):
            with mock.patch.dict(os.environ, dict(remote, **changes), clear=True), self.assertRaises(ValueError):
                Settings.from_environment()


if __name__ == "__main__":
    unittest.main()

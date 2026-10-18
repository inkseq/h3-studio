"""Offline tests for the exact SSH dependency repair; no real package install."""
import copy
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.runtime_hosts import wangp_system_restore as repair
from studio_platform.runtime_hosts.wangp_environment import digest, sha_file
from test_platform_wangp_package import package


class SystemRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kit = self.root / "kit"
        self.kit.mkdir()
        self.lock = {"base_image": "pytorch/pytorch@sha256:" + "a" * 64,
                     "system_packages": {"libsystemd0:amd64": repair.TARGET, "libc6:amd64": "2.35-0ubuntu3.8"}}
        entries, pins = [], {}
        for name in repair.PINNED_DEBS:
            path = self.kit / f"{name}_{repair.TARGET}_amd64.deb"
            path.write_bytes(name.encode())
            pins[name] = (path.stat().st_size, sha_file(path))
            entries.append({"name": name, "version": repair.TARGET, "architecture": "amd64", "file": path.name,
                            "size_bytes": pins[name][0], "sha256": pins[name][1]})
        patcher = patch.object(repair, "PINNED_DEBS", pins)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifest = {"format": repair.FORMAT, "base_image": self.lock["base_image"],
                         "environment_lock_sha256": digest(self.lock), "from_version": repair.OBSERVED,
                         "target_version": repair.TARGET, "packages": entries}
        self.write_manifest()

    def write_manifest(self):
        (self.kit / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def observed(self, version):
        return {**self.lock["system_packages"], **{repair.package_key(name): version for name in repair.PINNED_DEBS}}

    def process(self, args, **kwargs):
        output = ""
        if args[:2] == ["dpkg-deb", "--field"]:
            name = Path(args[2]).name.split("_", 1)[0]
            output = f"Package: {name}\nVersion: {repair.TARGET}\nArchitecture: amd64\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    def test_exact_family_repair_preserves_all_original_pins_and_uses_only_finite_commands(self):
        original = copy.deepcopy(self.lock)
        with patch.object(repair, "system_packages", side_effect=[self.observed(repair.OBSERVED), self.observed(repair.TARGET)]), \
                patch.object(repair.subprocess, "run", side_effect=self.process) as run:
            result = repair.restore_system(self.kit, self.lock)
        self.assertEqual(result["state"], "restored")
        self.assertEqual(self.lock, original)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([item[:2] for item in commands], [["dpkg-deb", "--field"]] * 6 +
                         [["dpkg", "--install"], ["dpkg", "--audit"], ["apt-get", "check"], ["/usr/sbin/sshd", "-t"]])
        self.assertEqual(len(commands[6][2:]), 6)

    def test_original_environment_does_not_install_provider_extras_or_run_processes(self):
        with patch.object(repair, "system_packages", return_value=self.lock["system_packages"]), \
                patch.object(repair.subprocess, "run") as run:
            self.assertEqual(repair.restore_system(self.kit, self.lock)["state"], "not_needed")
        run.assert_not_called()

    def test_unrecognized_version_missing_family_or_unrelated_drift_never_installs(self):
        cases = [self.observed("249.11-0ubuntu3.23"), {**self.observed(repair.OBSERVED), "libc6:amd64": "2.35-0ubuntu3.9"},
                 {name: value for name, value in self.observed(repair.OBSERVED).items() if name != "systemd"}]
        for observed in cases:
            with self.subTest(observed=observed), patch.object(repair, "system_packages", return_value=observed), \
                    patch.object(repair.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "unrecognized_drift"):
                    repair.restore_system(self.kit, self.lock)
                run.assert_not_called()

    def test_tamper_path_wrong_lock_and_extra_command_fields_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        for change in (lambda x: x.update(command="echo arbitrary"), lambda x: x.update(environment_lock_sha256="f" * 64),
                       lambda x: x["packages"][0].update(file="../escape.deb"),
                       lambda x: x["packages"][0].update(sha256="f" * 64),
                       lambda x: x["packages"][0].update(name="unapproved")):
            self.manifest = copy.deepcopy(original)
            change(self.manifest)
            self.write_manifest()
            with self.assertRaises(ValueError):
                repair.validate_kit(self.kit, self.lock)
        self.manifest = original
        self.write_manifest()
        (self.kit / original["packages"][0]["file"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "package_mismatch"):
            repair.validate_kit(self.kit, self.lock)

    def test_extra_files_cannot_enter_private_source_bundle(self):
        (self.kit / "unapproved.sh").write_text("exit 1")
        lockfile = self.root / "lock.json"
        lockfile.write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(ValueError, "extra_file"):
            package.small_bundle(self.root / "output.tar.gz", self.kit, lockfile)
        self.assertFalse((self.root / "output.tar.gz").exists())

    def test_optional_bundle_contains_exact_kit_and_requires_original_lock(self):
        with self.assertRaisesRegex(ValueError, "environment_lock_required"):
            package.small_bundle(self.root / "missing.tar.gz", self.kit)
        lockfile = self.root / "lock.json"
        lockfile.write_text(json.dumps(self.lock))
        output = self.root / "bundle.tar.gz"
        with patch.object(repair.subprocess, "run") as run:
            package.small_bundle(output, self.kit, lockfile)
        run.assert_not_called()
        with tarfile.open(output) as archive:
            names = archive.getnames()
        extras = {name for name in names if name.startswith("system-restore/")}
        self.assertEqual(extras, {"system-restore/manifest.json", *("system-restore/" + item["file"] for item in self.manifest["packages"])})

    def test_bad_deb_metadata_prevents_install_and_post_install_audit_is_required(self):
        with patch.object(repair, "system_packages", return_value=self.observed(repair.OBSERVED)), \
                patch.object(repair.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Package: different\n", "")) as run:
            with self.assertRaisesRegex(ValueError, "metadata_mismatch"):
                repair.restore_system(self.kit, self.lock)
        self.assertEqual(run.call_count, 1)
        def fail_audit(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "unconfigured package" if args == ["dpkg", "--audit"] else self.process(args).stdout, "")
        with patch.object(repair, "system_packages", side_effect=[self.observed(repair.OBSERVED), self.observed(repair.TARGET)]), \
                patch.object(repair.subprocess, "run", side_effect=fail_audit):
            with self.assertRaisesRegex(ValueError, "audit_failed"):
                repair.restore_system(self.kit, self.lock)


if __name__ == "__main__":
    unittest.main()

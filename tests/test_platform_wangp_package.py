"""Offline package boundaries; no resolver, installer, upstream import or GPU."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from studio_platform.runtime_hosts import wangp_environment as env
from studio_platform.runtime_hosts.wangp_session import CORE_VERSIONS

ROOT = Path(__file__).resolve().parents[1]


def load_file(name):
    spec = importlib.util.spec_from_file_location("test_wangp_" + name, ROOT / "deploy/wangp" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_file("bootstrap")
package = load_file("package_tool")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        (self.source / "wgp.py").write_bytes(b"# pinned source\n")
        self.lock = {
            "format": env.FORMAT, "python": env.PYTHON, "source_revision": env.REVISION,
            "requirements_sha256": env.REQUIREMENTS_SHA,
            "base_image": "example/runtime@sha256:" + "a" * 64,
            "requirements_lock_sha256": "b" * 64,
            "source_files": {"wgp.py": {"size_bytes": 16, "sha256": env.sha_file(self.source / "wgp.py")}},
            "wheels": [{"file": "example-1.0-py3-none-any.whl", "name": "example", "version": "1.0",
                        "size_bytes": 1, "sha256": "c" * 64}],
            "installed_packages": {"example": "1.0", **CORE_VERSIONS}, "system_packages": {"libc6:amd64": "2.36-9"},
        }

    def test_source_hash_and_extra_executable_file_are_enforced(self):
        env.verify_source(self.source, self.lock)
        (self.source / "shadow.py").write_text("pass", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "untracked_code"):
            env.verify_source(self.source, self.lock)
        (self.source / "wgp.py").write_bytes(b"# changed source")
        with self.assertRaisesRegex(ValueError, "source_modified"):
            env.verify_source(self.source, self.lock)

    def test_wheel_paths_cannot_escape_dependency_directory(self):
        for name in ("../evil.whl", "sub/evil.whl", "C:/evil.whl", "evil\\a.whl"):
            value = copy.deepcopy(self.lock)
            value["wheels"][0]["file"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                env.validate_lock(value)

    def test_full_package_and_system_versions_must_match(self):
        with patch.object(env.platform, "system", return_value="Linux"), \
                patch.object(env.platform, "machine", return_value="x86_64"), \
                patch.object(env.platform, "python_version", return_value=env.PYTHON), \
                patch.object(env, "installed_packages", return_value=self.lock["installed_packages"]), \
                patch.object(env, "system_packages", return_value=self.lock["system_packages"]):
            self.assertFalse(env.verify_environment(self.lock)["inference_verified"])
            with patch.object(env, "system_packages", return_value={**self.lock["system_packages"], "openssh-server": "1.0"}):
                self.assertEqual(env.verify_environment(self.lock)["additional_system_packages"], ["openssh-server"])
            with patch.object(env, "installed_packages", return_value={"example": "2.0"}):
                with self.assertRaisesRegex(ValueError, "dependency_mismatch"):
                    env.verify_environment(self.lock)
            with patch.object(env, "system_packages", return_value={"libc6:amd64": "other"}):
                with self.assertRaisesRegex(ValueError, "system_mismatch"):
                    env.verify_environment(self.lock)

    def test_package_mismatch_diagnostics_are_bounded_and_exclude_untrusted_fields(self):
        expected = {"libtest"+str(i): "1.0" for i in range(25)}
        diagnostic = env.system_package_diagnostics(expected, {"libtest0": "1.1", "extra-package": "2.0"})
        self.assertEqual(diagnostic["total"], 25)
        self.assertTrue(diagnostic["truncated"])
        self.assertEqual(len(diagnostic["mismatches"]), 16)
        self.assertEqual(diagnostic["mismatches"][0], {"package": "libtest0", "expected": "1.0", "observed": "1.1"})
        self.assertIsNone(diagnostic["mismatches"][1]["observed"])
        value = {"total": 3, "truncated": False, "log": "SECRET", "mismatches": [
            {"package": "libc6:amd64", "expected": "2.35-0ubuntu3.8", "observed": "2.35-0ubuntu3.9", "url": "SECRET"},
            {"package": "https://private.invalid/?token=SECRET", "expected": "1.0", "observed": None},
            {"package": "openssl", "expected": "3.0.2", "observed": "SECRET_TOKEN"}]}
        safe = env.safe_system_package_diagnostics(value)
        self.assertEqual(len(safe["mismatches"]), 1)
        self.assertTrue(safe["truncated"])
        self.assertNotIn("SECRET", json.dumps(safe))
        for total in (-1, True, 10001, "3"):
            self.assertIsNone(env.safe_system_package_diagnostics({**value, "total": total}))

    def test_bootstrap_retains_actual_system_failure_phase_and_versions_without_start(self):
        value = self.config()
        for key in ("source_bundle_path", "dependency_artifact_path"):
            Path(value[key]).write_bytes(b"synthetic fixture")
        value["source_bundle_sha256"] = env.sha_file(value["source_bundle_path"])
        value["dependency_artifact_sha256"] = env.sha_file(value["dependency_artifact_path"])
        manifest = json.loads((ROOT/"deploy/wangp/manifest.json").read_text())
        manifest.update(runtime_digest_kind="sixnine-environment-lock-sha256", runtime_digest=env.digest(self.lock))
        Path(value["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
        def unpack(archive, directory, **kwargs):
            directory.mkdir()
            if directory.name == "dependencies":
                runtime = directory/"upstream"
                runtime.mkdir()
                (runtime/".sixnine-environment.json").write_bytes(env.canonical(self.lock))
        with patch.object(bootstrap.platform, "system", return_value="Linux"), \
                patch.object(bootstrap.platform, "python_version", return_value=env.PYTHON), \
                patch.object(bootstrap.sys, "path", list(bootstrap.sys.path)), \
                patch.object(bootstrap, "extract", side_effect=unpack), \
                patch.object(env, "system_packages", return_value={"libc6:amd64": "2.36-10"}), \
                patch.object(bootstrap.subprocess, "run") as run, \
                patch.object(bootstrap.subprocess, "Popen") as popen, patch.object(bootstrap, "download") as download:
            result = bootstrap.install(value, "test-slot", str(self.root/"token"))
        self.assertEqual((result["state"], result["code"], result["error_type"]),
                         ("failed", "system_package_mismatch", "ValueError"))
        self.assertEqual(result["failure_phase"], "system_package_verification")
        self.assertEqual(result["phase"], "setup_failed")
        self.assertEqual(result["system_package_diagnostics"], {"total": 1, "truncated": False,
            "mismatches": [{"package": "libc6:amd64", "expected": "2.36-9", "observed": "2.36-10"}]})
        self.assertEqual(json.loads(Path(value["status_path"]).read_text()), result)
        run.assert_not_called(); popen.assert_not_called(); download.assert_not_called()

    def test_nested_vendor_metadata_does_not_change_wheel_identity(self):
        wheel = self.root / "example.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("setuptools-84.0.0.dist-info/METADATA", "Name: setuptools\nVersion: 84.0.0\n")
            archive.writestr("setuptools/_vendor/example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
        self.assertEqual(package.wheel_metadata(wheel)["Name"], "setuptools")

    def prepared_import_fixture(self):
        value = self.config()
        prepared = self.root / "prepared"
        dependency = prepared / "dependencies"
        runtime = dependency / "upstream"
        runtime.mkdir(parents=True)
        (runtime / "wgp.py").write_bytes(b"# pinned source\n")
        requirements = dependency / "requirements.lock"
        requirements.write_bytes(b"# synthetic locked fixture\n")
        self.lock["requirements_lock_sha256"] = env.sha_file(requirements)
        wheels = dependency / "wheels"
        wheels.mkdir()
        wheel = wheels / self.lock["wheels"][0]["file"]
        wheel.write_bytes(b"x")
        self.lock["wheels"][0]["sha256"] = env.sha_file(wheel)
        (runtime / ".sixnine-environment.json").write_bytes(env.canonical(self.lock))
        value.update(prepared_root=str(prepared), dependency_artifact_path="")
        Path(value["source_bundle_path"]).write_bytes(b"synthetic source fixture")
        value["source_bundle_sha256"] = env.sha_file(value["source_bundle_path"])
        manifest = json.loads((ROOT / "deploy/wangp/manifest.json").read_text())
        manifest.update(runtime_digest_kind="sixnine-environment-lock-sha256", runtime_digest=env.digest(self.lock))
        Path(value["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
        imported = {"state": "imports_verified", "environment_lock_sha256": env.digest(self.lock),
                    "inference_verified": False}
        return value, imported

    def test_model_transfer_failure_cannot_reach_runtime_verification_or_launch(self):
        from studio_platform.runtime_hosts import wangp_download
        value, imported = self.prepared_import_fixture()
        with patch.object(bootstrap.platform, "system", return_value="Linux"), \
                patch.object(bootstrap.platform, "python_version", return_value=env.PYTHON), \
                patch.object(bootstrap.sys, "path", list(bootstrap.sys.path)), \
                patch.object(bootstrap, "extract", side_effect=lambda _, directory, **kw: directory.mkdir()), \
                patch.object(env, "system_packages", return_value=self.lock["system_packages"]), \
                patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps(imported))) as run, \
                patch.object(bootstrap.subprocess, "Popen") as launch, \
                patch.object(wangp_download, "run_download", side_effect=ValueError("model_download_timeout")) as transfer:
            result = bootstrap.install(value, "test-slot", str(self.root / "token"))
        self.assertEqual((result["state"], result["failure_phase"], result["code"]),
                         ("failed", "model_download", "model_download_timeout"))
        transfer.assert_called_once()
        self.assertEqual(transfer.call_args.args[2], value["manifest_path"])
        self.assertEqual(run.call_count, 2)  # pip check and inert mocked import probe only
        launch.assert_not_called()
        self.assertFalse(Path(value["config_path"]).exists())
        self.assertTrue((Path(value["install_root"]) / "wangp-bootstrap-started.json").exists())

    def test_download_failure_and_unconfirmed_stop_reach_durable_setup_status(self):
        from studio_platform.runtime_hosts import wangp_download
        value, imported = self.prepared_import_fixture()
        failure = wangp_download.DownloadFailure("model_download_disk_headroom", "unconfirmed")
        with patch.object(bootstrap.platform, "system", return_value="Linux"), \
                patch.object(bootstrap.platform, "python_version", return_value=env.PYTHON), \
                patch.object(bootstrap.sys, "path", list(bootstrap.sys.path)), \
                patch.object(bootstrap, "extract", side_effect=lambda _, directory, **kw: directory.mkdir()), \
                patch.object(env, "system_packages", return_value=self.lock["system_packages"]), \
                patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps(imported))) as run, \
                patch.object(bootstrap.subprocess, "Popen") as launch, \
                patch.object(wangp_download, "run_download", side_effect=failure) as transfer:
            result = bootstrap.install(value, "test-slot", str(self.root / "token"))
        self.assertEqual((result["state"], result["failure_phase"], result["code"], result["error_type"]),
            ("failed", "model_download", "model_download_stop_unconfirmed", "ValueError"))
        self.assertEqual(result["download_failure"], failure.diagnosis)
        self.assertEqual(json.loads(Path(value["status_path"]).read_text()), result)
        self.assertEqual(run.call_count, 2); transfer.assert_called_once(); launch.assert_not_called()
        self.assertFalse(Path(value["config_path"]).exists())
        with patch.object(wangp_download, "run_download", side_effect=AssertionError("no replay")):
            self.assertEqual(bootstrap.install(value, "test-slot", str(self.root / "token")),
                {"state": "reconcile_required", "code": "bootstrap_marker_exists"})
        self.assertEqual(json.loads(Path(value["status_path"]).read_text()), result)

    def bootstrap_after_download(self, *, launch=True, receipt="valid", ready_incarnation="a"*32, startup_failure=None):
        from studio_platform.inference.wangp_contract import EngineManifest
        from studio_platform.runtime_hosts import wangp_download, wangp_launcher
        value, imported = self.prepared_import_fixture()
        manifest = EngineManifest.from_dict(json.loads(Path(value["manifest_path"]).read_text()))
        token = self.root / "token"
        token.write_text("synthetic-private-bootstrap-token-" + "x"*32)
        token.chmod(0o600)
        child = SimpleNamespace(pid=os.getpid(), poll=lambda: 1 if startup_failure else None)
        evidence = {"manifest_digest": manifest.digest, "inference_verified": False}

        def command(args, **kwargs):
            if args[0] == "nvidia-smi":
                return SimpleNamespace(stdout="GPU-test123, 141000\n")
            return SimpleNamespace(returncode=0, stdout=json.dumps(evidence if "--verify-only" in args else imported))

        def start(args, **kwargs):
            self.assertNotIn("--verify-only", args)
            self.assertIn("--create-journal", args)
            if startup_failure:
                from studio_platform.runtime_hosts.wangp_startup import write_startup_failure
                write_startup_failure(args[args.index("--startup-status-file")+1], slot_key="test-slot",
                    manifest_digest=manifest.digest, launch_id=args[args.index("--startup-id")+1],
                    phase="runtime_manifest", error=ValueError("wangp_startup_stage_failed"),
                    pid=child.pid + (1 if startup_failure == "wrong-pid" else 0))
            if receipt != "missing":
                state = Path(args[args.index("--state-dir")+1])
                state.mkdir()
                host = SimpleNamespace(manifest=manifest, journal=SimpleNamespace(slot_key="test-slot"), incarnation="a"*32)
                wangp_launcher.write_verification_receipt(state, host, evidence)
                if receipt == "wrong-pid":
                    path = state / wangp_launcher.VERIFICATION_RECEIPT
                    document = json.loads(path.read_text())
                    document["pid"] += 1
                    path.write_text(json.dumps(document))
            return child

        ready = {"manifest_digest": manifest.digest, "slot_key": "test-slot", "idle": True,
                 "incarnation": ready_incarnation}
        actual_fstat = os.fstat
        token_identity = (token.stat().st_dev, token.stat().st_ino)
        def token_fstat(fd):
            info = actual_fstat(fd)
            # The production bootstrap is Linux-only. Windows chmod does not
            # model POSIX read bits; synthesize only this fixture token's mode.
            if os.name == "nt" and (info.st_dev, info.st_ino) == token_identity:
                return SimpleNamespace(st_nlink=info.st_nlink, st_mode=info.st_mode & ~0o077)
            return info
        with patch.object(bootstrap.platform, "system", return_value="Linux"), \
                patch.object(bootstrap.platform, "python_version", return_value=env.PYTHON), \
                patch.object(bootstrap.sys, "path", list(bootstrap.sys.path)), \
                patch.object(bootstrap, "extract", side_effect=lambda _, directory, **kw: directory.mkdir()), \
                patch.object(env, "system_packages", return_value=self.lock["system_packages"]), \
                patch.object(bootstrap.subprocess, "run", side_effect=command) as runs, \
                patch.object(bootstrap.subprocess, "Popen", side_effect=start) as starts, \
                patch.object(bootstrap, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(json.dumps(ready).encode())), \
                patch.object(bootstrap.time, "monotonic", side_effect=[0, 1, 901]), \
                patch.object(bootstrap.time, "sleep"), \
                patch.object(bootstrap.os, "fstat", side_effect=token_fstat), \
                patch.object(wangp_download, "run_download"):
            result = bootstrap.install(value, "test-slot", str(token), launch=launch)
        return result, runs, starts, value

    def test_startup_failure_is_projected_but_dispatch_remains_unknown(self):
        result, _, starts, value = self.bootstrap_after_download(receipt="missing", startup_failure="valid")
        self.assertEqual((result["state"], result["failure_phase"], result["code"]),
                         ("unknown", "runtime_manifest", "wangp_startup_stage_failed"))
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(json.loads(Path(value["status_path"]).read_text()), result)
        starts.assert_called_once()

    def test_wrong_startup_identity_falls_back_without_claiming_stop(self):
        result, _, starts, _ = self.bootstrap_after_download(receipt="missing", startup_failure="wrong-pid")
        self.assertEqual((result["state"], result["code"]), ("unknown", "runtime_process_exited"))
        starts.assert_called_once()

    def test_normal_bootstrap_starts_once_without_a_separate_full_verification(self):
        result, runs, starts, value = self.bootstrap_after_download()
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["runtime_verified"])
        starts.assert_called_once()
        self.assertEqual(len(runs.call_args_list), 3)  # pip check, GPU import probe, GPU observation.
        self.assertFalse(any("--verify-only" in call.args[0] for call in runs.call_args_list))
        self.assertFalse((Path(value["install_root"]) / "runtime-verification.json").exists())

    def test_nonlaunch_verification_remains_explicit_and_cannot_start_runtime(self):
        result, runs, starts, value = self.bootstrap_after_download(launch=False)
        self.assertEqual(result["state"], "verified_not_started")
        self.assertEqual(sum("--verify-only" in call.args[0] for call in runs.call_args_list), 1)
        starts.assert_not_called()
        self.assertTrue((Path(value["install_root"]) / "runtime-verification.json").exists())
        self.assertFalse((Path(value["install_root"]) / "slot-state").exists())

    def test_ready_endpoint_without_the_current_child_receipt_remains_unknown(self):
        result, runs, starts, value = self.bootstrap_after_download(receipt="missing")
        self.assertEqual((result["state"], result["code"]), ("unknown", "runtime_readiness_timeout"))
        starts.assert_called_once()
        self.assertTrue((Path(value["install_root"]) / "wangp-bootstrap-started.json").exists())

    def test_wrong_child_receipt_cannot_be_consumed_or_trigger_a_relaunch(self):
        result, _, starts, _ = self.bootstrap_after_download(receipt="wrong-pid")
        self.assertEqual((result["state"], result["code"]), ("unknown", "verification_receipt_mismatch"))
        starts.assert_called_once()

    def test_receipt_without_matching_live_incarnation_is_not_ready(self):
        result, _, starts, _ = self.bootstrap_after_download(ready_incarnation="b"*32)
        self.assertEqual((result["state"], result["code"]), ("unknown", "runtime_readiness_timeout"))
        starts.assert_called_once()

    def test_environment_lock_is_bound_before_source_or_package_verification(self):
        (self.source / ".sixnine-environment.json").write_bytes(env.canonical(self.lock))
        with patch.object(env, "verify_source") as source, patch.object(env, "verify_environment"):
            with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                env.verify_bound_environment(self.source, {"runtime_digest_kind": "sixnine-environment-lock-sha256",
                                                           "runtime_digest": "0" * 64})
            source.assert_not_called()

    def test_actual_python_patch_is_frozen_and_deb_escape_is_rejected(self):
        lock = copy.deepcopy(self.lock)
        lock["python"] = "3.11.13"
        env.validate_lock(lock)
        with patch.object(env.platform, "system", return_value="Linux"), \
                patch.object(env.platform, "machine", return_value="x86_64"), \
                patch.object(env.platform, "python_version", return_value="3.11.14"):
            with self.assertRaisesRegex(ValueError, "platform_mismatch"):
                env.verify_environment(lock)
        lock["debs"] = [{"file": "../libc.deb", "name": "libc6", "version": "2.36", "architecture": "amd64",
                         "size_bytes": 1, "sha256": "a" * 64}]
        with self.assertRaises(ValueError):
            env.validate_lock(lock)

    def archive(self, member, *, kind=tarfile.REGTYPE, size=1):
        path = self.root / ("input-" + str(len(list(self.root.glob("input-*")))) + ".tar.gz")
        with tarfile.open(path, "w:gz") as target:
            item = tarfile.TarInfo(member)
            item.type = kind
            item.size = size if kind == tarfile.REGTYPE else 0
            item.linkname = "outside" if kind != tarfile.REGTYPE else ""
            target.addfile(item, io.BytesIO(b"x" * size) if kind == tarfile.REGTYPE else None)
        return path

    def test_archive_traversal_links_and_limits_rejected(self):
        for index, (name, kind, size, limit) in enumerate([
            ("../escape", tarfile.REGTYPE, 1, 10), ("/escape", tarfile.REGTYPE, 1, 10),
            ("link", tarfile.SYMTYPE, 0, 10), ("hardlink", tarfile.LNKTYPE, 0, 10),
            ("large", tarfile.REGTYPE, 11, 10),
        ]):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bootstrap.extract(self.archive(name, kind=kind, size=size), self.root / f"out{index}", maximum=limit)

    def config(self):
        value = json.loads((ROOT / "deploy/wangp/runtime-config.template.json").read_text(encoding="utf-8"))
        for key in ("install_root", "source_bundle_path", "manifest_path", "model_root", "config_path", "status_path",
                    "dependency_artifact_path"):
            value[key] = str(self.root / key)
        value["config_path"] = str(self.root / "wgp_config.json")
        value.update(source_bundle_sha256="a" * 64, dependency_artifact_sha256="b" * 64)
        return value

    def test_unknown_existing_bootstrap_is_never_relaunched_or_status_overwritten(self):
        value = self.config()
        base = Path(value["install_root"])
        base.mkdir()
        marker = base / "wangp-bootstrap-started.json"
        marker.write_text('{"slot_key":"original"}', encoding="utf-8")
        status = Path(value["status_path"])
        status.write_text('{"state":"unknown"}', encoding="utf-8")
        with patch.object(bootstrap.subprocess, "Popen") as popen, patch.object(bootstrap, "download") as download:
            result = bootstrap.install(value, "new-slot", str(self.root / "token"))
        self.assertEqual(result["state"], "reconcile_required")
        self.assertEqual(json.loads(marker.read_text())["slot_key"], "original")
        self.assertEqual(json.loads(status.read_text())["state"], "unknown")
        popen.assert_not_called()
        download.assert_not_called()

    def test_unsigned_config_and_dependency_selection_fail_closed(self):
        invalid = self.config()
        invalid["config_path"] = str(self.root / "wgp-config.json")
        with self.assertRaisesRegex(ValueError, "filename_invalid"):
            bootstrap.validate_config(invalid)
        value = self.config()
        value.update(dependency_artifact_path="", dependency_artifact_url="https://example.invalid/file?token=secret")
        with self.assertRaisesRegex(ValueError, "unsigned_https"):
            bootstrap.validate_config(value)
        value = self.config()
        value["prepared_root"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "one_dependency_source"):
            bootstrap.validate_config(value)

    def test_source_bundle_is_explicit_and_contains_no_configuration_or_credentials(self):
        output = self.root / "source.tar.gz"
        receipt = package.small_bundle(output)
        self.assertEqual(receipt["sha256"], env.sha_file(output))
        self.assertLess(receipt["size_bytes"], bootstrap.MAX_SOURCE)
        with tarfile.open(output) as source:
            self.assertEqual(set(source.getnames()), set(package.PRIVATE_FILES))
            self.assertTrue(all(item.isfile() for item in source))
        from studio_platform.runtime_catalog import PROFILE_IDS
        profile_files = {f"deploy/wangp/profiles/{identity}.json" for identity in PROFILE_IDS}
        self.assertEqual({name for name in package.PRIVATE_FILES if not name.endswith('.py')}, profile_files)

    def test_bound_manifest_is_new_identity_and_never_claims_inference(self):
        lock_file = self.root / "environment.json"
        lock_file.write_bytes(env.canonical(self.lock))
        output = self.root / "manifest.json"
        image = "example/runtime@sha256:" + "d" * 64
        result = package.bind_manifest(ROOT / "deploy/wangp/manifest.json", lock_file, output, image)
        value = json.loads(output.read_text())
        self.assertEqual(value["runtime_digest"], env.digest(self.lock))
        self.assertEqual(value["runtime_recipe"]["python"], self.lock["python"])
        self.assertEqual(value["runtime_recipe"]["full_dependency_lock"], env.digest(self.lock))
        self.assertFalse(value["inference_qualified"])
        self.assertFalse(result["inference_verified"])
        with self.assertRaises(FileExistsError):
            package.bind_manifest(ROOT / "deploy/wangp/manifest.json", lock_file, output, image)
        self.lock["installed_packages"]["torch"] = "2.7.1"
        lock_file.write_bytes(env.canonical(self.lock))
        with self.assertRaisesRegex(ValueError, "core_versions_mismatch"):
            package.bind_manifest(ROOT / "deploy/wangp/manifest.json", lock_file, self.root / "wrong.json", image)


if __name__ == "__main__":
    unittest.main()

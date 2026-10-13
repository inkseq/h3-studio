"""Tiny fake SDK/files/processes only: no downloads, installations or GPU calls."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from studio_platform.inference.wangp_contract import EngineManifest
from studio_platform.runtime_hosts import wangp_download as fetch
from studio_platform.runtime_hosts import wangp_session as session


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / "models"
        self.models.mkdir()
        self.progress = self.root / "progress.json"
        self.manifest = json.loads((Path(__file__).parent / "deploy/wangp/manifest.json").read_text())
        self.manifest["runtime_digest_kind"] = "sixnine-environment-lock-sha256"
        self.payloads = {"tiny.bin": b"safe", "second/config.json": b"{}"}
        self.manifest["components"] = {"model": {"repository": "example/pinned", "revision": "a"*40,
            "precision": "bf16", "files": [
                {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in self.payloads.items()]}}
        self.digest = EngineManifest.from_dict(self.manifest).digest
        self.path = self.root / "manifest.json"
        self.path.write_text(json.dumps(self.manifest))
        disk = patch.object(fetch.shutil, "disk_usage", return_value=NS(free=fetch.MAX_TOTAL + fetch.HEADROOM))
        disk.start()
        self.addCleanup(disk.stop)

    def sdk(self, **kwargs):
        self.assertIs(kwargs["token"], False)
        self.assertEqual(kwargs["endpoint"], "https://huggingface.co")
        self.assertEqual(kwargs["revision"], "a"*40)
        self.assertEqual(kwargs["repo_id"], "example/pinned")
        self.assertFalse(kwargs["force_download"])
        self.assertIn(kwargs["filename"], self.payloads)
        target = Path(kwargs["local_dir"]) / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payloads[kwargs["filename"]])
        return str(target)

    def run_fetch(self, **kwargs):
        return fetch.fetch_files(self.manifest, self.digest, self.models, self.progress,
                                 downloader=kwargs.pop("downloader", self.sdk), **kwargs)

    def receipt(self):
        return {"state": "downloaded_unverified", "manifest_digest": self.digest,
                "files_complete": 2, "files_total": 2, "bytes_complete": 6,
                "bytes_total": 6, "inference_verified": False}

    def test_exact_files_public_auth_and_completion_are_not_runtime_qualification(self):
        self.run_fetch()
        self.assertEqual(json.loads(self.progress.read_text()), self.receipt())
        self.assertEqual(sorted(p.relative_to(self.models).as_posix() for p in self.models.rglob("*") if p.is_file()),
                         sorted(self.payloads))

    def test_v3_fl_ref_locator_keeps_original_manifest_and_complete_content(self):
        from studio_platform.runtime_catalog import engine_manifest
        digests = {
            "fl": "e32a141d43774d2e1b61bb45432aea2a2bde3e66e36318c84dd8d4538e64d530",
            "ref": "47e46d47404c198c667bcb5114b4680bab5ed0aac60c5f77c610a197d62d7367",
        }
        for mode in ("fl", "ref"):
            with self.subTest(mode=mode):
                manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", mode)
                document = copy.deepcopy(manifest.document)
                records = fetch.selected_files(document, manifest.digest)
                self.assertEqual(manifest.digest, digests[mode])
                self.assertEqual(len(records), 10)
                self.assertEqual(fetch._content_preserving_revision(document, manifest.digest, records),
                                 "8d9093438ffb3e33d6a058cc20fa3b7ca31f5752")
                self.assertTrue(all(row[2] == "adc81ccb71352192214d83d5fafb9487e860be39" for row in records))
                self.assertEqual(document, manifest.document)

    def test_v4_same_content_locator_accepts_only_exact_new_manifest(self):
        from studio_platform.runtime_catalog import engine_manifest
        for mode in ('fl','ref'):
            manifest = engine_manifest('h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4',mode)
            records = fetch.selected_files(manifest.document,manifest.digest)
            self.assertEqual(fetch._content_preserving_revision(manifest.document,manifest.digest,records),
                             '8d9093438ffb3e33d6a058cc20fa3b7ca31f5752')
            self.assertEqual(len(records),10)
            original=copy.deepcopy(manifest.document)
            document=copy.deepcopy(original)
            document['components']['transformer']['files'][0]['sha256']='0'*64
            changed=EngineManifest.from_dict(document).digest
            sdk=Mock()
            with self.assertRaisesRegex(ValueError,'model_download_locator_invalid'):
                fetch.fetch_files(document,changed,self.models,self.progress,downloader=sdk)
            sdk.assert_not_called()
            self.assertEqual(manifest.document,original)

    def test_v3_changed_content_or_revision_refuses_before_sdk(self):
        from studio_platform.runtime_catalog import engine_manifest
        manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", "ref")
        sdk = Mock()
        for change in ("missing", "size", "sha256", "git_blob_sha1", "repository", "revision"):
            document = copy.deepcopy(manifest.document)
            transformer = document["components"]["transformer"]
            files = document["components"]["text_encoder"]["files"]
            if change == "missing": files.pop()
            elif change == "size": transformer["files"][0]["size_bytes"] += 1
            elif change == "sha256": transformer["files"][0]["sha256"] = "0" * 64
            elif change == "git_blob_sha1": files[1]["git_blob_sha1"] = "0" * 40
            elif change == "repository": transformer["repository"] = "other/repository"
            else: transformer["revision"] = "b" * 40
            digest = EngineManifest.from_dict(document).digest
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "model_download_locator_invalid"):
                fetch.fetch_files(document, digest, self.models, self.progress, downloader=sdk)
        sdk.assert_not_called()

    def test_v3_sdk_initial_and_retry_use_only_approved_revision(self):
        from studio_platform.runtime_catalog import engine_manifest
        for mode in ("fl", "ref"):
            with self.subTest(mode=mode):
                manifest = engine_manifest("h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3", mode)
                original = copy.deepcopy(manifest.document)
                calls = []
                def unavailable(**kwargs):
                    calls.append(kwargs)
                    raise RuntimeError("HTTP404 https://signed.invalid/?token=PRIVATE")
                with self.assertRaisesRegex(ValueError, "^model_download_failed$"):
                    fetch.fetch_files(manifest.document, manifest.digest, self.models, self.progress,
                                      downloader=unavailable, workers=1, sleep=lambda _: None)
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertEqual(call, {
                        "repo_id": "DeepBeepMeep/MiniMax-H3",
                        "filename": fetch.selected_files(original, manifest.digest)[0][3],
                        "revision": "8d9093438ffb3e33d6a058cc20fa3b7ca31f5752",
                        "local_dir": str(self.models), "token": False, "endpoint": fetch.ENDPOINT,
                        "force_download": False, "etag_timeout": 30,
                    })
                self.assertEqual(manifest.document, original)
                self.assertEqual(json.loads(self.progress.read_text())["state"], "failed")
                self.assertNotIn("PRIVATE", self.progress.read_text())

    def test_bad_allowlist_and_unpinned_revision_fail_before_sdk(self):
        sdk = Mock()
        changes = [lambda c: c.update(revision="main"), lambda c: c.update(repository="https://evil.invalid"),
                   lambda c: c["files"][0].update(path="../secret"),
                   lambda c: c["files"][0].update(path="*.bin"),
                   lambda c: c["files"][0].update(path=".cache/token"),
                   lambda c: c["files"].append(copy.deepcopy(c["files"][0])),
                   lambda c: c["files"][0].update(sha256="invalid")]
        for index, change in enumerate(changes):
            document = copy.deepcopy(self.manifest)
            change(document["components"]["model"])
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "manifest_invalid"):
                # digest is not a substitute for validating the allowlist.
                digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                fetch.fetch_files(document, digest, self.models, self.progress, downloader=sdk)
        sdk.assert_not_called()
        with self.assertRaisesRegex(ValueError, "manifest_changed"):
            fetch.fetch_files(self.manifest, "b"*64, self.models, self.progress, downloader=sdk)

    def test_sdk_retry_keeps_its_partial_and_never_restarts_bootstrap(self):
        calls = {}
        partial = self.models / ".cache" / "download.incomplete"
        def sdk(**kwargs):
            name = kwargs["filename"]
            calls[name] = calls.get(name, 0) + 1
            if name == "tiny.bin":
                partial.parent.mkdir(exist_ok=True)
                if calls[name] == 1:
                    partial.write_bytes(b"sa")
                    raise TimeoutError("https://signed.invalid/?token=DO_NOT_RECORD")
                self.assertEqual(partial.read_bytes(), b"sa")
                with partial.open("ab") as stream:
                    stream.write(b"fe")
                partial.replace(self.models / name)
                return str(self.models / name)
            return self.sdk(**kwargs)
        self.run_fetch(downloader=sdk, sleep=lambda _: None)
        self.assertEqual(calls, {"tiny.bin": 2, "second/config.json": 1})
        self.assertEqual((self.models / "tiny.bin").read_bytes(), b"safe")
        self.assertNotIn("DO_NOT_RECORD", self.progress.read_text())

    def test_concurrency_is_bounded_to_two_and_reports_completed_files_only(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = peak = 0
        def sdk(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=5)
            result = self.sdk(**kwargs)
            with lock:
                active -= 1
            return result
        self.run_fetch(downloader=sdk)
        self.assertEqual(peak, 2)
        self.assertNotIn("bytes_received", self.progress.read_text())

    def test_sdk_error_and_output_are_not_exposed_by_cli(self):
        calls = []
        def sdk(**kwargs):
            calls.append(kwargs)
            print("SIGNED_URL_TOKEN")
            print("SIGNED_URL_TOKEN", file=sys.stderr)
            raise RuntimeError("https://private.invalid/?sig=SIGNED_URL_TOKEN")
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"huggingface_hub": NS(hf_hub_download=sdk)}), \
                patch.object(fetch.time, "sleep"), redirect_stdout(out), redirect_stderr(err):
            # Use one worker; the real child has an independent process/logging state.
            code = fetch.main(["--manifest", str(self.path), "--expected-digest", self.digest,
                "--model-root", str(self.models), "--progress", str(self.progress), "--workers", "1",
                "--owner-pid", str(os.getpid())], guard=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertNotIn("SIGNED_URL_TOKEN", self.progress.read_text())
        self.assertEqual(json.loads(self.progress.read_text())["code"], "model_download_failed")
        self.assertTrue(all(call["token"] is False for call in calls))

    def test_first_failure_latches_before_blocked_peer_or_queued_files_finish(self):
        third = {"path": "queued.bin", "size_bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
        self.manifest["components"]["model"]["files"].append(third)
        self.digest = EngineManifest.from_dict(self.manifest).digest
        blocked, release = threading.Event(), threading.Event()
        errors, calls = [], []
        def sdk(**kwargs):
            name = kwargs["filename"]
            calls.append(name)
            if name == "tiny.bin":
                self.assertTrue(blocked.wait(timeout=5))
                raise RuntimeError("https://signed.invalid/?token=DO_NOT_RECORD")
            if name == "second/config.json":
                blocked.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test peer release timed out")
                return self.sdk(**kwargs)
            self.fail("a queued file must not start after the first-failure latch")
        def invoke():
            try:
                self.run_fetch(downloader=sdk, sleep=lambda _: None)
            except Exception as error:
                errors.append(str(error))
        caller = threading.Thread(target=invoke)
        caller.start()
        try:
            deadline = time.monotonic() + 5
            failed = False
            while time.monotonic() < deadline:
                if self.progress.exists():
                    failed = json.loads(self.progress.read_text()).get("state") == "failed"
                    if failed:
                        break
                time.sleep(.01)
            self.assertTrue(failed, "parent must observe failure before the other SDK call returns")
            self.assertTrue(caller.is_alive())
            self.assertNotIn("queued.bin", calls)
        finally:
            release.set()
            caller.join(timeout=5)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, ["model_download_failed"])
        self.assertEqual(json.loads(self.progress.read_text())["state"], "failed")
        self.assertNotIn("DO_NOT_RECORD", self.progress.read_text())

    def test_same_size_corrupt_download_is_rejected_by_retained_full_verifier(self):
        def corrupt(**kwargs):
            target = self.sdk(**kwargs)
            if kwargs["filename"] == "tiny.bin":
                Path(target).write_bytes(b"EVIL")
            return target
        self.run_fetch(downloader=corrupt)
        self.assertEqual(json.loads(self.progress.read_text())["state"], "downloaded_unverified")
        config = self.root / "wgp_config.json"
        config.write_text(json.dumps(session.config_for_model_root(self.models)))
        with patch("studio_platform.runtime_hosts.wangp_environment.verify_bound_environment", return_value={}), \
                patch.object(session.importlib.metadata, "version", side_effect=session.CORE_VERSIONS.__getitem__):
            with self.assertRaisesRegex(ValueError, "component_hash_mismatch"):
                session.verify_runtime(self.root, config, self.path, self.models)

    def test_wrong_size_and_outside_sdk_result_are_rejected(self):
        for outside in (False, True):
            def sdk(**kwargs):
                target = self.root / "outside" if outside else Path(self.sdk(**kwargs))
                target.write_bytes(b"incorrect")
                return str(target)
            with self.subTest(outside=outside), self.assertRaisesRegex(ValueError, "model_download_(size_mismatch|path_invalid)"):
                self.run_fetch(downloader=sdk)
            for p in self.models.rglob("*"):
                if p.is_file():
                    p.unlink()

    def test_total_missing_disk_preflight_prevents_sdk_calls(self):
        sdk = Mock()
        with patch.object(fetch.shutil, "disk_usage", return_value=NS(free=fetch.HEADROOM+5)):
            with self.assertRaisesRegex(ValueError, "disk_headroom"):
                self.run_fetch(downloader=sdk)
        sdk.assert_not_called()

    def test_environment_ignores_inherited_identity_endpoint_and_native_logs(self):
        value = fetch.download_environment({"PATH": "retained", "HF_TOKEN": "SECRET", "HF_ENDPOINT": "https://bad",
            "HUGGING_FACE_HUB_TOKEN": "SECRET", "HF_XET_LOG_DEST": "/private/log", "RUST_LOG": "debug"}, self.root)
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertEqual(value["HF_ENDPOINT"], fetch.ENDPOINT)
        self.assertEqual(value["HF_XET_LOG_DEST"], os.devnull)
        self.assertEqual(value["HF_XET_LOG_FILE"], os.devnull)
        self.assertEqual(value["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
        self.assertEqual(value["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(value["HF_HUB_DISABLE_PROGRESS_BARS"], "1")
        self.assertEqual(value["RUST_LOG"], "off")

    def run_child(self, child, **kwargs):
        return fetch.run_download(sys.executable, self.root, self.path, self.digest, self.models,
            self.root / "download-state", {}, kwargs.pop("progress", Mock()), popen=child, **kwargs)

    def test_child_receipt_is_required_and_stdout_stderr_are_discarded(self):
        seen = []
        def launch(command, **kwargs):
            seen.append(kwargs)
            progress = Path(command[command.index("--progress")+1])
            fetch.write_progress(progress, self.receipt())
            return NS(poll=lambda: 0, wait=lambda **_: 0)
        value = self.run_child(launch)
        self.assertEqual(value["state"], "downloaded_unverified")
        self.assertEqual(seen[0]["stdout"], subprocess.DEVNULL)
        self.assertEqual(seen[0]["stderr"], subprocess.DEVNULL)
        self.assertTrue(seen[0]["start_new_session"])
        self.assertEqual(seen[0]["env"]["HF_HUB_OFFLINE"], "0")

    def test_final_receipt_arriving_between_observation_and_exit_is_reread(self):
        def launch(command, **kwargs):
            progress=Path(command[command.index('--progress')+1])
            earlier=self.receipt(); earlier['state']='downloading'
            fetch.write_progress(progress,earlier)
            def exited():
                fetch.write_progress(progress,self.receipt())
                return 0
            return NS(poll=exited,wait=lambda **_:0)
        value=self.run_child(launch)
        self.assertEqual(value,self.receipt())
        self.assertFalse((self.root/'download-state/failure.json').exists())

    def test_post_exit_receipt_is_still_fully_validated(self):
        def launch(command, **kwargs):
            progress=Path(command[command.index('--progress')+1])
            earlier=self.receipt(); earlier['state']='downloading'
            fetch.write_progress(progress,earlier)
            def exited():
                changed=self.receipt(); changed['manifest_digest']='0'*64
                fetch.write_progress(progress,changed)
                return 0
            return NS(poll=exited,wait=lambda **_:0)
        stop=Mock()
        with self.assertRaisesRegex(ValueError,'model_download_progress_invalid'):
            self.run_child(launch,stop=stop)
        stop.assert_called_once()
        failure=json.loads((self.root/'download-state/failure.json').read_text())
        self.assertEqual(failure['download_failure']['stop_status'],'confirmed')

    def test_final_incomplete_receipts_and_nonzero_exit_cannot_pass(self):
        for case in ('nonzero','files','bytes','unfinished'):
            with self.subTest(case=case):
                def launch(command, **kwargs):
                    path=Path(command[command.index('--progress')+1])
                    earlier=self.receipt(); earlier['state']='downloading'
                    fetch.write_progress(path,earlier)
                    def exited():
                        changed=self.receipt()
                        if case=='files':changed['files_complete']-=1
                        elif case=='bytes':changed['bytes_complete']-=1
                        elif case=='unfinished':changed['state']='downloading'
                        fetch.write_progress(path,changed)
                        return 1 if case=='nonzero' else 0
                    return NS(poll=exited,wait=lambda **_:0)
                stop=Mock()
                with self.assertRaisesRegex(ValueError,'^model_download_failed$'):
                    fetch.run_download(sys.executable,self.root,self.path,self.digest,self.models,
                        self.root/('download-state-'+case),{},Mock(),popen=launch,stop=stop)
                stop.assert_called_once()

    def test_deadline_stops_and_reaps_exact_child_with_no_relaunch(self):
        child = NS(poll=lambda: None)
        launch, stop = Mock(return_value=child), Mock()
        clock = iter([0, 0, 2])
        with self.assertRaisesRegex(ValueError, "model_download_timeout"):
            self.run_child(launch, timeout=1, clock=lambda: next(clock), sleep=lambda _: None, stop=stop)
        launch.assert_called_once()
        stop.assert_called_once_with(child)
        with self.assertRaisesRegex(ValueError, "state_exists"):
            self.run_child(launch)
        launch.assert_called_once()

    def test_failed_or_malformed_progress_never_reports_success(self):
        value = self.receipt()
        value["url"] = "SECRET"
        def launch(command, **kwargs):
            fetch.write_progress(command[command.index("--progress")+1], value)
            return NS(poll=lambda: 0, wait=lambda **_: 0)
        stop, progress = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, "progress_invalid"):
            self.run_child(launch, stop=stop, progress=progress)
        stop.assert_called_once()
        progress.assert_not_called()

    def test_failure_receipt_stops_and_reaps_child_while_sdk_peer_still_runs(self):
        value = self.receipt()
        value.update(state="failed", code="model_download_failed", files_complete=0, bytes_complete=0)
        child = NS(poll=Mock(return_value=None))
        def launch(command, **kwargs):
            fetch.write_progress(command[command.index("--progress") + 1], value)
            return child
        stop = Mock()
        with self.assertRaisesRegex(ValueError, "model_download_failed"):
            self.run_child(launch, stop=stop)
        stop.assert_called_once_with(child)
        child.poll.assert_not_called()  # failure is actionable before an exit acknowledgement

    def test_failed_stop_preserves_initiating_failure_and_owner_receipt_without_relaunch(self):
        value = self.receipt()
        value.update(state="failed", code="model_download_size_mismatch")
        child = NS(poll=Mock(return_value=None))
        def launch(command, **kwargs):
            fetch.write_progress(command[command.index("--progress") + 1], value)
            return child
        launcher = Mock(side_effect=launch)
        stop = Mock(side_effect=ValueError("model_download_stop_unconfirmed"))
        with self.assertRaisesRegex(fetch.DownloadFailure, "^model_download_stop_unconfirmed$") as caught:
            self.run_child(launcher, stop=stop)
        expected = {"error_code": "model_download_size_mismatch", "stop_status": "unconfirmed"}
        self.assertEqual(caught.exception.diagnosis, expected)
        failure = self.root / "download-state/failure.json"
        saved = json.loads(failure.read_text())
        self.assertEqual(saved, {"state": "failed", "manifest_digest": self.digest,
            "error_code": "model_download_stop_unconfirmed", "download_failure": expected})
        # A native peer's late progress cannot overwrite the owner's stop facts.
        fetch.write_progress(self.root / "download-state/progress.json", self.receipt())
        self.assertEqual(json.loads(failure.read_text()), saved)
        with self.assertRaisesRegex(ValueError, "state_exists"):
            self.run_child(launcher, stop=stop)
        launcher.assert_called_once(); stop.assert_called_once_with(child)

    def test_raw_failure_and_failed_stop_messages_are_never_retained(self):
        child = NS(poll=Mock(side_effect=RuntimeError("https://signed.invalid/?token=SECRET")))
        stop = Mock(side_effect=OSError("SECRET private process details"))
        with self.assertRaisesRegex(fetch.DownloadFailure, "stop_unconfirmed") as caught:
            self.run_child(Mock(return_value=child), stop=stop)
        self.assertEqual(caught.exception.diagnosis,
            {"error_code": "model_download_failed", "stop_status": "unconfirmed"})
        raw = (self.root / "download-state/failure.json").read_text()
        self.assertNotIn("SECRET", raw); self.assertNotIn("signed.invalid", raw)

    def test_confirmed_stop_keeps_original_timeout_and_explicit_proof(self):
        child = NS(poll=lambda: None)
        clock = iter([0, 2]); stop = Mock()
        with self.assertRaisesRegex(fetch.DownloadFailure, "^model_download_timeout$") as caught:
            self.run_child(Mock(return_value=child), timeout=1, clock=lambda: next(clock), stop=stop)
        self.assertEqual(caught.exception.diagnosis,
            {"error_code": "model_download_timeout", "stop_status": "confirmed"})
        saved = json.loads((self.root / "download-state/failure.json").read_text())
        self.assertEqual(saved["download_failure"], caught.exception.diagnosis)
        stop.assert_called_once_with(child)

    def test_failure_receipt_write_error_cannot_prevent_stop_or_mask_original(self):
        child = NS(poll=Mock(side_effect=ValueError("model_download_cache_limit")))
        stop = Mock(side_effect=OSError("SECRET"))
        with patch.object(fetch, "record_failure", side_effect=OSError("private full disk")), \
                self.assertRaisesRegex(fetch.DownloadFailure, "stop_unconfirmed") as caught:
            self.run_child(Mock(return_value=child), stop=stop)
        self.assertEqual(caught.exception.diagnosis["error_code"], "model_download_cache_limit")
        stop.assert_called_once_with(child)

    def test_no_child_handle_is_not_reported_as_confirmed_stop(self):
        stop = Mock()
        with self.assertRaisesRegex(fetch.DownloadFailure, "^model_download_failed$") as caught:
            self.run_child(Mock(side_effect=OSError("private launch details")), stop=stop)
        self.assertEqual(caught.exception.diagnosis,
            {"error_code": "model_download_failed", "stop_status": "not_observed"})
        stop.assert_not_called()

    def test_keyboard_interrupt_remains_interrupt_after_stop(self):
        child = NS(poll=Mock(side_effect=KeyboardInterrupt)); stop = Mock()
        with self.assertRaises(KeyboardInterrupt):
            self.run_child(Mock(return_value=child), stop=stop)
        self.assertEqual(json.loads((self.root / "download-state/failure.json").read_text())["download_failure"],
            {"error_code": "model_download_failed", "stop_status": "confirmed"})
        stop.assert_called_once_with(child)

    def test_cache_overflow_stops_child(self):
        child = NS(poll=lambda: None)
        def launch(*args, **kwargs):
            cache = self.root / "download-state/cache"
            cache.mkdir()
            (cache / "oversize").write_bytes(b"12345")
            return child
        stop = Mock()
        with patch.object(fetch, "CACHE_LIMIT", 4), self.assertRaisesRegex(ValueError, "cache_limit"):
            self.run_child(launch, stop=stop)
        stop.assert_called_once_with(child)

    @unittest.skipUnless(os.name == "posix", "POSIX process group contract")
    def test_real_idle_child_is_killed_and_reaped_without_running_sdk(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        fetch.stop_child(child)
        self.assertIsNotNone(child.returncode)

    def test_failed_reap_is_not_stop_proof(self):
        child = NS(poll=lambda: 1, wait=Mock(side_effect=subprocess.TimeoutExpired("fake", 10)))
        with self.assertRaisesRegex(ValueError, "stop_unconfirmed"):
            fetch.stop_child(child)

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death and timer contract")
    def test_os_deadline_terminates_inert_child_without_sdk_or_python_callback(self):
        program = "import os,time;from studio_platform.runtime_hosts.wangp_download import child_guard;child_guard(os.getppid(),.2);time.sleep(60)"
        child = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertEqual(child.wait(timeout=5), -fetch.signal.SIGALRM)

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death contract")
    def test_original_owner_exit_kills_guarded_child_even_in_its_own_session(self):
        child_code = "import os,time;from studio_platform.runtime_hosts.wangp_download import child_guard;child_guard(os.getppid(),30);print(os.getpid(),flush=True);time.sleep(60)"
        owner_code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdout=subprocess.PIPE,text=True,start_new_session=True);print(p.stdout.readline().strip(),flush=True)"
        owner = subprocess.Popen([sys.executable, "-c", owner_code, child_code], stdout=subprocess.PIPE, text=True)
        raw, _ = owner.communicate(timeout=5)
        self.assertEqual(owner.returncode, 0)
        pid = int(raw.strip())
        alive = True
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().split()
                    alive = fields[2] not in {"Z", "X"}
                except FileNotFoundError:
                    alive = False
                if not alive:
                    break
                time.sleep(.02)
            self.assertFalse(alive, "original owner exit must not leave the isolated download running")
        finally:
            if alive:
                os.kill(pid, fetch.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()

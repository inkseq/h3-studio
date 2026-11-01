"""CPU media and injected MockTransport only. No real GPU or paid API requests."""

import io
import json
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import patch

import httpx

from studio_platform.storage import LocalObjectStore
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.worker import (
    BackendError,
    ComfyBackend,
    DisabledBackend,
    MockBackend,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
    WorkerRunner,
    _slot_lock,
    _shape,
    _lease_keepalive,
)
from test_platform_repository import LedgerCase


class WorkerTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name) / "worker"
        self.store = LocalObjectStore(Path(self.temp.name) / "objects")

    def worker_job(self, *, backend="mock", audio=False, cost=100_000):
        request = {
            "request": {
                "prompt": "test-only",
                "duration": 4,
                "resolution": "custom",
                "width": 256,
                "height": 256,
                "generate_audio": audio,
                "export_crf": 18,
            },
            "output_spec": {"width": 256, "height": 256},
            "assets": {},
        }
        plan = self.repo.create_plan(
            self.scope,
            request,
            {
                "pool": "worker-test",
                "backend": backend,
                "enabled": True,
                "expected_runtime_s": 1,
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=cost,
        )
        return self.repo.create_job(
            self.scope, plan["id"], "worker-test", budget_account_ids=["owner-budget"]
        )

    def mock(self):
        return MockBackend(self.work / "mock", enabled=True)

    def test_render_shape_allows_chapter_bounds_without_relaxing_h3(self):
        def render(frames=1, width=256, height=256):
            return {
                "owner_id": "superdan",
                "request": {
                    "recipe_id": "chapter-roughcut-v1",
                    "request": {
                        "duration": frames / 24,
                        "generate_audio": False,
                        "export_crf": 18,
                        "render": {
                            "version": 1,
                            "shots": [
                                {
                                    "shot_id": "shot",
                                    "source_id": "video",
                                    "frames": frames,
                                }
                            ],
                            "audio_tracks": [],
                        },
                    },
                    "output_spec": {
                        "width": width,
                        "height": height,
                        "fps": 24,
                        "frame_count": frames,
                    },
                    "sources": {
                        "video": {
                            "kind": "video",
                            "simulation": False,
                            "object": {
                                "key": "owners/superdan/assets/source/video.mp4",
                                "size_bytes": 1,
                                "sha256": "0" * 64,
                                "content_type": "video/mp4",
                            },
                        }
                    },
                },
            }

        self.assertEqual(_shape(render()), (256, 256, 1 / 24, False))
        self.assertEqual(_shape(render(14400, 1280, 720)), (1280, 720, 600, False))
        for args in ((14401, 256, 256), (96, 1280, 1280), (96, 257, 256)):
            with self.assertRaises(BackendError):
                _shape(render(*args))
        with self.assertRaises(BackendError):
            _shape(
                {
                    "request": {
                        "request": {"duration": 600, "width": 1280, "height": 720},
                        "output_spec": {"width": 1280, "height": 720},
                    }
                }
            )

    def test_blocking_collection_renews_lease_and_surfaces_renewal_loss(self):
        import threading
        from studio_platform.repository import LeaseLost

        renewed = threading.Event()
        with _lease_keepalive(renewed.set, interval_s=0.01):
            self.assertTrue(renewed.wait(1))
        failed = threading.Event()

        def lost():
            failed.set()
            raise LeaseLost("test-only")

        with self.assertRaises(LeaseLost):
            with _lease_keepalive(lost, interval_s=0.01):
                self.assertTrue(failed.wait(1))
        for interval in (0, float("nan"), 61):
            with self.assertRaises(ValueError):
                with _lease_keepalive(lambda: None, interval_s=interval):
                    pass

    def controlled_mock(self):
        control = WorkerControl(self.repo)
        control.register(
            WorkerSpec(
                "controlled-worker",
                "worker-test",
                "mock",
                "local-test-only",
                ("cpu",),
                (),
                "SIMULATION",
                "simulation-v1",
                backend="mock",
            )
        )
        control.mark_ready("controlled-worker", upstream_idle_confirmed=True)
        return control

    def test_defaults_disabled_do_not_claim_or_create_worker_directory(self):
        job = self.worker_job()
        runner = WorkerRunner(self.repo, self.store, self.work)
        with (
            patch("httpx.Client", side_effect=AssertionError("network forbidden")),
            patch("subprocess.run", side_effect=AssertionError("process forbidden")),
        ):
            self.assertEqual(
                runner.run_once("worker", "worker-test")["state"], "disabled"
            )
        self.assertFalse(self.work.exists())
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        self.assertFalse(ComfyBackend().enabled)
        self.assertFalse(MockBackend(self.work).enabled)

    def test_comfy_requires_exact_allowlist_no_credential_urls(self):
        for endpoint, allowed in (
            ("https://example.invalid", []),
            ("http://example.invalid", ["http://example.invalid"]),
            (
                "https://user:password@example.invalid",
                ["https://user:password@example.invalid"],
            ),
            ("https://example.invalid?key=test", ["https://example.invalid?key=test"]),
        ):
            with self.assertRaises(ValueError):
                ComfyBackend(endpoint=endpoint, enabled=True, allowed_origins=allowed)

    def test_real_submit_requires_guard_before_preparation_and_again_before_intent(
        self,
    ):
        for sequence in (None, [False], [True, False]):
            with self.subTest(sequence=sequence):
                # Each case uses a fresh owned job and no HTTP or GPU calls.
                import uuid

                plan = self.repo.create_plan(
                    self.scope,
                    {"request": {"duration": 4, "generate_audio": False}},
                    {"pool": "guard-test", "backend": "comfy-worker", "enabled": True},
                    expires_at=self.now + 1000,
                    estimated_cost_microusd=100_000,
                )
                job = self.repo.create_job(
                    self.scope,
                    plan["id"],
                    uuid.uuid4().hex,
                    budget_account_ids=["owner-budget"],
                )
                backend = ComfyBackend(
                    endpoint="http://127.0.0.1:8188",
                    enabled=True,
                    allowed_origins=["http://127.0.0.1:8188"],
                    transport=httpx.MockTransport(
                        lambda _: self.fail("guard denial must not contact provider")
                    ),
                )
                self.addCleanup(backend.close)
                preparations = []
                backend.prepare = lambda *args: preparations.append(True) or {}
                responses = iter(sequence) if sequence else None
                guard = (lambda _: next(responses)) if responses else None
                result = WorkerRunner(
                    self.repo,
                    self.store,
                    self.work,
                    backend=backend,
                    submission_guard=guard,
                ).run_once("worker", "guard-test")
                self.assertEqual(result["state"], "failed")
                self.assertEqual(
                    len(preparations), 1 if sequence == [True, False] else 0
                )
                failed = self.repo.get_job(self.scope, job["id"])
                self.assertEqual(
                    failed["error_code"],
                    "execution_policy_unavailable_before_submission",
                )
                self.assertEqual(
                    self.repo.get_budget("owner-budget")["reserved_microusd"], 0
                )

    def test_closed_worker_window_defers_without_failing_or_submitting(self):
        job = self.worker_job()
        control, inner, posts = self.controlled_mock(), self.mock(), []

        class Guarded:  # Not a MockBackend, so both pre-submit checks apply.
            def __getattr__(self, name):
                return getattr(inner, name)

        backend = Guarded()
        original = inner.submit
        inner.submit = lambda prepared, tag: (
            posts.append(tag) or original(prepared, tag)
        )
        answers = iter([False, True, True])
        control.submission_allowed = lambda _: next(answers)
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            control=control,
            submission_guard=lambda _: True,
        )
        result = runner.run_once("controlled-worker", "worker-test")
        deferred = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(result["state"], "queued")
        self.assertEqual(
            (deferred["status"], deferred["error_code"]),
            ("queued", "worker_window_closed"),
        )
        self.assertEqual(posts, [])
        self.now += 31
        self.assertEqual(
            runner.run_once("controlled-worker", "worker-test")["state"], "succeeded"
        )
        self.assertEqual(len(posts), 1)

    def test_running_reconciliation_does_not_require_new_submission_guard(self):
        job = self.worker_job(backend="comfy-worker")
        from studio_platform.queue import TaskQueue

        queue = TaskQueue(self.repo)
        claim = queue.claim("worker", "worker-test")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "known-task")
        queue.release(claim.lease, retry_after_s=0)
        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(lambda _: self.fail("no HTTP needed")),
        )
        self.addCleanup(backend.close)
        backend.poll = lambda tag, task_id: Outcome("running", task_id)
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: self.fail(
                "existing attempts must not need new consent"
            ),
        )
        self.assertEqual(runner.run_once("recovery", "worker-test")["state"], "running")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_mock_real_video_and_audio_are_verified_stored_and_labelled(self):
        job = self.worker_job(audio=True)
        runner = WorkerRunner(self.repo, self.store, self.work, backend=self.mock())
        result = runner.run_once("worker", "worker-test")
        self.assertEqual(result["state"], "succeeded", result)
        self.assertTrue(result["simulation"])
        done = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(done["result"]["actual_cost_microusd"], 0)
        artifacts = self.repo.list_artifacts(self.scope, job["id"])
        self.assertEqual({a["metadata"]["kind"] for a in artifacts}, {"video", "audio"})
        for artifact in artifacts:
            evidence = artifact["metadata"]
            self.assertTrue(evidence["validated"])
            self.assertEqual(
                self.store.stat(evidence["object_key"]).sha256, evidence["sha256"]
            )
        # Decode a real frame and verify the permanent bright-yellow simulation label.
        from studio_platform.media import ffmpeg
        from PIL import Image

        video = next(a for a in artifacts if a["metadata"]["kind"] == "video")
        path = Path(self.temp.name) / "stored.mp4"
        with self.store.open(video["metadata"]["object_key"]) as source:
            path.write_bytes(source.read())
        frame = Path(self.temp.name) / "frame.png"
        ffmpeg(["-i", path, "-frames:v", "1", frame])
        with Image.open(frame) as image:
            yellow = sum(
                r > 140 and g > 140 and b < 90
                for r, g, b in image.convert("RGB").get_flattened_data()
            )
        self.assertGreater(yellow, 80)

    def test_audio_artifact_records_verified_duration_within_requested_tolerance(self):
        from studio_platform.media import ffmpeg, probe

        job = self.worker_job(audio=True)
        backend = self.mock()
        fetch = backend.fetch

        def shorter_audio(*args):
            files = fetch(*args)
            short = Path(self.temp.name) / "shorter-source.flac"
            # Real 126400 samples at 32kHz: 3.95s, accepted by the existing
            # 0.1s generation tolerance but too short for a later 4s range.
            ffmpeg(
                [
                    "-i",
                    files["audio"],
                    "-af",
                    "atrim=end_sample=126400",
                    "-c:a",
                    "flac",
                    "-ar",
                    "32000",
                    "-ac",
                    "2",
                    short,
                ]
            )
            return {**files, "audio": short}

        backend.fetch = shorter_audio
        result = WorkerRunner(
            self.repo, self.store, self.work, backend=backend
        ).run_once("worker", "worker-test")
        self.assertEqual(result["state"], "succeeded", result)
        done = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(done["request"]["request"]["duration"], 4)
        artifact = next(
            a
            for a in self.repo.list_artifacts(self.scope, job["id"])
            if a["metadata"]["kind"] == "audio"
        )
        evidence = artifact["metadata"]
        stored = Path(self.temp.name) / "verified-stored.flac"
        with self.store.open(evidence["object_key"]) as stream:
            stored.write_bytes(stream.read())
        actual = float(probe(stored)["format"]["duration"])
        self.assertAlmostEqual(actual, 3.95, places=6)
        self.assertEqual(evidence["duration_s"], actual)
        self.assertLess(evidence["duration_s"], 4)
        self.assertTrue(evidence["validated"])
        self.assertEqual(
            self.store.stat(evidence["object_key"]).sha256, evidence["sha256"]
        )

    def test_backend_mismatch_never_fake_comfy_as_mock(self):
        job = self.worker_job(backend="comfy-worker")
        runner = WorkerRunner(self.repo, self.store, self.work, backend=self.mock())
        result = runner.run_once("worker", "worker-test")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["error_code"],
            "worker_backend_not_authorized",
        )
        self.assertFalse((self.work / "mock").exists())

    def test_submit_intent_is_saved_before_upstream_call(self):
        job = self.worker_job()
        backend = self.mock()
        original = backend.submit

        def checked(prepared, tag):
            self.assertEqual(
                self.repo.get_job(self.scope, job["id"])["status"], "submitting"
            )
            return original(prepared, tag)

        backend.submit = checked
        result = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        ).run_once("worker", "worker-test")
        self.assertEqual(result["state"], "succeeded")

    def test_unknown_submission_is_never_resubmitted_on_worker_restart(self):
        job = self.worker_job()
        backend = self.mock()
        calls = []

        def uncertain(prepared, tag):
            calls.append(tag)
            raise SubmissionUncertain("safe-error-only")

        backend.submit = uncertain
        first = WorkerRunner(
            self.repo, self.store, self.work, backend=backend
        ).run_once("worker", "worker-test")
        self.assertEqual(first["state"], "submission_unknown")
        for i in range(3):
            self.now += 31
            WorkerRunner(self.repo, self.store, self.work, backend=backend).run_once(
                "worker-restart", "worker-test"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_accepted_response_lost_reconciles_cpu_receipt_without_recreation(self):
        job = self.worker_job()
        backend = self.mock()
        original = backend.submit
        calls = []

        def accepted_then_lost(prepared, tag):
            calls.append(tag)
            original(prepared, tag)
            raise SubmissionUncertain("response_lost")

        backend.submit = accepted_then_lost
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        )
        self.assertEqual(
            runner.run_once("worker", "worker-test")["state"], "submission_unknown"
        )
        self.assertEqual(
            runner.run_once("worker-again", "worker-test")["state"], "succeeded"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_collection_failure_resumes_original_task_and_immutable_object_key(self):
        job = self.worker_job()
        backend = self.mock()
        original = backend.fetch
        fetches = []

        def flaky(*args):
            fetches.append(1)
            if len(fetches) == 1:
                raise BackendError("fake-download-failure")
            return original(*args)

        backend.fetch = flaky
        runner = WorkerRunner(self.repo, self.store, self.work, backend=backend)
        self.assertEqual(
            runner.run_once("worker", "worker-test")["state"], "collecting"
        )
        self.now += 31
        self.assertEqual(
            runner.run_once("collector", "worker-test")["state"], "succeeded"
        )
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(len(fetches), 2)

    def test_drain_does_not_claim_and_local_single_slot_lock_prevents_other_runner(
        self,
    ):
        job = self.worker_job()
        backend = self.mock()
        runner = WorkerRunner(self.repo, self.store, self.work, backend=backend)
        runner.drain()
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "draining")
        self.work.mkdir(parents=True)
        with _slot_lock(self.work, backend.slot_key) as acquired:
            self.assertTrue(acquired)
            second = WorkerRunner(self.repo, self.store, self.work, backend=backend)
            self.assertEqual(
                second.run_once("other", "worker-test")["state"], "slot_busy"
            )
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")

    def test_local_slot_lock_is_held_across_processes_and_released_on_exit(self):
        self.work.mkdir(parents=True)
        code = (
            "from pathlib import Path\nimport sys\nfrom studio_platform.worker import _slot_lock\n"
            "with _slot_lock(Path(sys.argv[1]),sys.argv[2]) as acquired:\n"
            " print('acquired' if acquired else 'busy')\n"
        )
        command = [sys.executable, "-c", code, str(self.work), "test-only-slot"]
        with _slot_lock(self.work, "test-only-slot") as acquired:
            self.assertTrue(acquired)
            other = subprocess.run(
                command, check=True, capture_output=True, timeout=15, text=True
            )
            self.assertEqual(other.stdout.strip(), "busy")
        other = subprocess.run(
            command, check=True, capture_output=True, timeout=15, text=True
        )
        self.assertEqual(other.stdout.strip(), "acquired")

    def test_durable_slot_prevents_parallel_runner_submit_even_with_separate_work_dirs(
        self,
    ):
        job = self.worker_job()
        control, backend, posts = self.controlled_mock(), self.mock(), []
        backend.submit = lambda prepared, tag: posts.append(tag) or "mock-" + tag
        backend.poll = lambda tag, task_id: Outcome("running", task_id)

        def turn(i):
            return WorkerRunner(
                self.repo,
                self.store,
                self.work / str(i),
                backend=backend,
                control=control,
            ).run_once("controlled-worker", "worker-test")

        results = self.parallel(turn)
        self.assertEqual(sum(r["state"] == "running" for r in results), 1)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(control.get("controlled-worker")["current_job_id"], job["id"])

    def test_live_idle_turns_renew_registration_but_expired_slot_is_not_revived(self):
        control = self.controlled_mock()
        runner = WorkerRunner(
            self.repo, self.store, self.work, backend=self.mock(), control=control
        )
        for _ in range(12):
            self.now += 90
            self.assertEqual(
                runner.run_once("controlled-worker", "worker-test")["state"], "idle"
            )
            worker = control.get("controlled-worker")
            self.assertEqual(worker["state"], "ready")
            self.assertGreater(worker["expires_at"], self.now)
        self.now += 121
        runner.run_once("controlled-worker", "worker-test")
        self.assertEqual(control.get("controlled-worker")["state"], "unknown")
        self.assertLess(control.get("controlled-worker")["expires_at"], self.now)
        runner.run_once("controlled-worker", "worker-test")
        self.assertEqual(control.get("controlled-worker")["state"], "unknown")

    def test_crash_after_submission_intent_keeps_slot_unknown_and_does_not_post_again(
        self,
    ):
        job = self.worker_job()
        control, backend, posts = self.controlled_mock(), self.mock(), []
        claimed = control.claim("controlled-worker", "worker-test", lease_seconds=10)
        control.queue.begin_submission(claimed.lease)
        self.now += 11
        backend.submit = lambda prepared, tag: posts.append(tag) or "should-not-submit"
        runner = WorkerRunner(
            self.repo, self.store, self.work, backend=backend, control=control
        )
        result = runner.run_once("controlled-worker", "worker-test")
        self.assertEqual(result["state"], "submission_unknown")
        self.assertEqual(posts, [])
        worker = control.get("controlled-worker")
        self.assertEqual(
            (worker["state"], worker["current_job_id"]), ("unknown", job["id"])
        )
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_sigterm_before_submit_safely_defers_and_restores_handlers(self):
        job = self.worker_job()
        control, backend = self.controlled_mock(), self.mock()
        prior = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        original = backend.prepare

        def stop_before_submit(*args):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return original(*args)

        backend.prepare = stop_before_submit
        backend.submit = lambda *_: self.fail("drained preparation must not submit")
        runner = WorkerRunner(
            self.repo, self.store, self.work, backend=backend, control=control
        )
        runner.run_forever("controlled-worker", "worker-test", poll_interval_s=0.01)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        worker = control.get("controlled-worker")
        self.assertEqual(
            (worker["state"], worker["drain_requested"], worker["current_job_id"]),
            ("draining", 1, None),
        )
        for s, handler in prior.items():
            self.assertEqual(signal.getsignal(s), handler)

    def test_sigterm_after_accepted_submit_preserves_receipt_and_resumes_collection_only(
        self,
    ):
        job = self.worker_job()
        control, backend, posts = self.controlled_mock(), self.mock(), []
        original = backend.submit

        def accepted_then_stop(prepared, tag):
            posts.append(tag)
            task_id = original(prepared, tag)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return task_id

        backend.submit = accepted_then_stop
        WorkerRunner(
            self.repo, self.store, self.work, backend=backend, control=control
        ).run_forever("controlled-worker", "worker-test", poll_interval_s=0.01)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "running")
        self.assertEqual(control.get("controlled-worker")["drain_requested"], 1)
        self.now += 6
        resumed = WorkerRunner(
            self.repo, self.store, self.work, backend=backend, control=control
        )
        self.assertEqual(
            resumed.run_once("controlled-worker", "worker-test")["state"], "succeeded"
        )
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(control.get("controlled-worker")["state"], "draining")
        self.assertEqual(control.get("controlled-worker")["drain_requested"], 1)

    def test_shutdown_database_error_still_restores_handlers_and_closes_client(self):
        backend, closed = self.mock(), []
        backend.close = lambda: closed.append(True)

        class UnavailableControl:
            def drain(self, worker_id):
                raise RuntimeError("test-only-database-unavailable")

        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            control=UnavailableControl(),
        )
        runner.run_once = lambda *_: runner.drain()
        prior = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        with self.assertRaises(RuntimeError):
            runner.run_forever("controlled-worker", "worker-test", poll_interval_s=0.01)
        self.assertEqual(closed, [True])
        for s, handler in prior.items():
            self.assertEqual(signal.getsignal(s), handler)

    def test_comfy_contract_tags_reconcile_cancel_and_download_no_user_urls(self):
        tag = "attempt-test"
        graph = {
            "1": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": "h3-studio/" + tag},
            }
        }
        record = {
            "prompt": [1, "task-test", graph, {"sixnine_attempt_id": tag}],
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "1": {
                    "videos": [
                        {
                            "filename": tag + "_00001_.mp4",
                            "subfolder": "h3-studio",
                            "type": "output",
                        }
                    ]
                }
            },
        }
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/prompt":
                data = json.loads(request.content)
                self.assertEqual(data["extra_data"]["sixnine_attempt_id"], tag)
                return httpx.Response(200, json={"prompt_id": "task-test"})
            if request.url.path == "/history":
                return httpx.Response(200, json={"task-test": record})
            if request.url.path == "/history/task-test":
                return httpx.Response(200, json={"task-test": record})
            if request.url.path == "/queue":
                return httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            if request.url.path == "/api/jobs/task-test/cancel":
                return httpx.Response(200, json={"cancelled": False})
            if request.url.path == "/view":
                self.assertEqual(request.url.params["filename"], tag + "_00001_.mp4")
                return httpx.Response(200, content=b"contract-bytes-only")
            raise AssertionError("unexpected fake request")

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(handler),
            comfy_revision="e9027f2b30f37bb3052714eb08fcf479542f4fc0",
        )
        self.addCleanup(backend.close)
        self.assertEqual(backend.submit(graph, tag), "task-test")
        self.assertEqual(backend.reconcile(tag).state, "succeeded")
        self.assertFalse(backend.cancel(tag, "task-test"))
        target = Path(self.temp.name) / "download"
        target.mkdir()
        job = {
            "request": {
                "request": {"duration": 4, "generate_audio": False},
                "output_spec": {"width": 256, "height": 256},
            }
        }
        self.assertEqual(
            backend.fetch(job, tag, "task-test", target, lambda: None)[
                "video"
            ].read_bytes(),
            b"contract-bytes-only",
        )
        self.assertNotIn(("POST", "/interrupt"), requests)

    def test_comfy_missing_history_is_unknown_and_foreign_history_rejected(self):
        def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            return httpx.Response(200, json={})

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(backend.close)
        self.assertEqual(backend.reconcile("attempt-missing").state, "unknown")
        self.assertEqual(backend.poll("attempt-missing", "known-task").state, "unknown")
        foreign = {
            "task": {
                "prompt": [0, "task", {}, {"sixnine_attempt_id": "other"}],
                "status": {"completed": True},
            }
        }
        backend.close()
        backend.transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json=foreign)
        )
        with self.assertRaises(BackendError):
            backend.poll("ours", "task")

    def test_unverified_comfy_only_deletes_tagged_pending_and_never_interrupts_running(
        self,
    ):
        tag = "attempt"
        graph = {
            "1": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": "h3-studio/attempt"},
            }
        }
        prompt = [0, "task", graph, {"sixnine_attempt_id": tag}]
        state = {"running": False, "deleted": False, "requests": []}

        def handler(request):
            state["requests"].append((request.method, request.url.path))
            if request.url.path.startswith("/history"):
                return httpx.Response(200, json={})
            if request.url.path == "/queue" and request.method == "POST":
                self.assertEqual(json.loads(request.content), {"delete": ["task"]})
                state["deleted"] = True
                return httpx.Response(200, content=b"")
            if request.url.path == "/queue":
                return httpx.Response(
                    200,
                    json={
                        "queue_running": [prompt] if state["running"] else [],
                        "queue_pending": []
                        if state["deleted"] or state["running"]
                        else [prompt],
                    },
                )
            raise AssertionError(
                "unverified version must not use a guessed cancellation route"
            )

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(backend.close)
        self.assertTrue(backend.cancel(tag, "task"))
        self.assertEqual(backend.poll(tag, "task").state, "cancelled")
        state["running"], state["deleted"] = True, False
        self.assertFalse(backend.cancel(tag, "task"))
        self.assertNotIn(("POST", "/interrupt"), state["requests"])
        self.assertNotIn(("POST", "/api/jobs/task/cancel"), state["requests"])

    def test_comfy_rejection_and_response_loss_are_distinct(self):
        def reject(_):
            return httpx.Response(400, json={"error": "not stored"})

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(reject),
        )
        self.addCleanup(backend.close)
        with self.assertRaises(SubmissionRejected):
            backend.submit({}, "attempt")
        backend.close()

        def timeout(request):
            raise httpx.ReadTimeout("test-response-loss", request=request)

        backend.transport = httpx.MockTransport(timeout)
        with self.assertRaises(SubmissionUncertain):
            backend.submit({}, "attempt")

    def fake_comfy_output(self, *, cost_resolver=None):
        job = self.worker_job(backend="comfy-worker")
        # Local fixture is visibly simulated, but transport exercises the production
        # adapter schema and collector against actual media bytes, not a real model.
        fixture = self.mock()
        fixture.submit({"shape": (256, 256, 4, False)}, "fixture")
        video_bytes = (fixture.state_dir / "fixture.mp4").read_bytes()
        state = {"posts": 0, "record": None}

        def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            if request.url.path == "/prompt":
                self.assertEqual(
                    self.repo.get_job(self.scope, job["id"])["status"], "submitting"
                )
                state["posts"] += 1
                body = json.loads(request.content)
                tag = body["extra_data"]["sixnine_attempt_id"]
                state["record"] = {
                    "prompt": [0, "task", body["prompt"], body["extra_data"]],
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "1": {
                            "videos": [
                                {
                                    "filename": tag + "_00001_.mp4",
                                    "type": "output",
                                    "subfolder": "h3-studio",
                                }
                            ]
                        }
                    },
                }
                return httpx.Response(200, json={"prompt_id": "task"})
            if request.url.path == "/history/task":
                return httpx.Response(200, json={"task": state["record"]})
            if request.url.path == "/view":
                return httpx.Response(200, content=video_bytes)
            raise AssertionError("unexpected local contract request")

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(handler),
            actual_cost_resolver=cost_resolver,
        )
        self.addCleanup(backend.close)
        # Only graph preparation is injected: no GPU or weights exist in this test.
        backend.prepare = lambda j, tag, store, heartbeat: {
            "1": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": "h3-studio/" + tag},
            }
        }
        return job, backend, state

    def test_full_fake_comfy_pipeline_outputs_available_while_invoice_pending(self):
        job, backend, state = self.fake_comfy_output()
        result = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        ).run_once("worker", "worker-test")
        self.assertEqual(result["state"], "succeeded")
        done = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(done["result"]["billing_status"], "pending")
        self.assertEqual(state["posts"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 0
        )  # Invoice remains pending; terminal admission hold is released.
        self.assertEqual(len(self.repo.list_artifacts(self.scope, job["id"])), 1)

    def test_invoice_timeout_preserves_verified_output_pending_budget_and_later_idempotent_settlement(
        self,
    ):
        def unavailable(*_):
            raise TimeoutError("synthetic-private-billing-response")

        job, backend, state = self.fake_comfy_output(cost_resolver=unavailable)
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        )
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "succeeded")
        done = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(done["result"]["billing_status"], "pending")
        self.assertIsNone(done["result"]["actual_cost_microusd"])
        self.assertIsNone(done["error_code"])
        self.assertNotIn("synthetic-private", json.dumps(done["result"]))
        outputs = self.repo.list_artifacts(self.scope, job["id"])
        self.assertEqual(len(outputs), 1)
        evidence = outputs[0]["metadata"]
        self.assertTrue(evidence["validated"])
        self.assertEqual(
            self.store.stat(evidence["object_key"]).sha256, evidence["sha256"]
        )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 0
        )  # Invoice remains pending; terminal admission hold is released.
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "idle")
        self.assertEqual(state["posts"], 1)
        for _ in range(2):
            self.repo.settle_completed_job(
                self.scope, job["id"], actual_cost_microusd=80_000
            )
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(
            (budget["reserved_microusd"], budget["spent_microusd"]), (0, 80_000)
        )
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["result"]["billing_status"],
            "settled",
        )
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), outputs)
        from studio_platform.repository import Conflict

        with self.assertRaises(Conflict):
            self.repo.settle_completed_job(
                self.scope, job["id"], actual_cost_microusd=81_000
            )

    def test_malformed_provider_invoice_is_unknown_not_zero_and_does_not_block_outputs(
        self,
    ):
        # Exercise the real collector with a decoded local SIMULATION fixture.
        job, backend, state = self.fake_comfy_output(cost_resolver=lambda *_: "0")
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        )
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "succeeded")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["result"]["billing_status"],
            "pending",
        )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 0
        )  # Invoice remains pending; terminal admission hold is released.
        self.assertEqual(len(self.repo.list_artifacts(self.scope, job["id"])), 1)
        self.assertEqual(state["posts"], 1)
        for invalid in ("0", True, -1, 0.0, float("nan"), 9_000_000_000_000_001):
            with self.subTest(invalid=repr(invalid)):
                backend.cost_resolver = lambda *_, value=invalid: value
                self.assertIsNone(runner._cost(job, "task"))
                self.assertIsNone(
                    runner._cost(job, "task", Outcome("failed", "task", invalid))
                )
        backend.cost_resolver = lambda *_: 0
        self.assertEqual(runner._cost(job, "task"), 0)

    def test_corrupt_download_stays_collecting_without_second_prompt(self):
        job = self.worker_job(backend="comfy-worker")
        state = {"posts": 0, "record": None}

        def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            if request.url.path == "/prompt":
                state["posts"] += 1
                body = json.loads(request.content)
                tag = body["extra_data"]["sixnine_attempt_id"]
                state["record"] = {
                    "prompt": [0, "task", body["prompt"], body["extra_data"]],
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "1": {
                            "videos": [
                                {
                                    "filename": tag + "_00001_.mp4",
                                    "type": "output",
                                    "subfolder": "h3-studio",
                                }
                            ]
                        }
                    },
                }
                return httpx.Response(200, json={"prompt_id": "task"})
            if request.url.path == "/history/task":
                return httpx.Response(200, json={"task": state["record"]})
            if request.url.path == "/view":
                return httpx.Response(200, content=b"not-a-video")
            raise AssertionError("unexpected local fake request")

        backend = ComfyBackend(
            endpoint="http://127.0.0.1:8188",
            enabled=True,
            allowed_origins=["http://127.0.0.1:8188"],
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(backend.close)
        backend.prepare = lambda j, tag, store, heartbeat: {
            "1": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": "h3-studio/" + tag},
            }
        }
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.work,
            backend=backend,
            submission_guard=lambda _: True,
        )
        self.assertEqual(
            runner.run_once("worker", "worker-test")["state"], "collecting"
        )
        self.now += 31
        self.assertEqual(
            runner.run_once("worker-restart", "worker-test")["state"], "collecting"
        )
        self.assertEqual(state["posts"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), [])

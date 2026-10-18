"""Original CPU jobs/receipts prove observer integration without paid inference."""
import json
from pathlib import Path
from unittest.mock import patch
import uuid

from studio_platform.storage import LocalObjectStore
from studio_platform.telemetry import StageTelemetry
from studio_platform.worker import BackendError, MockBackend, Outcome, SubmissionUncertain, WorkerRunner
from test_platform_repository import LedgerCase
from test_platform_telemetry import RecordingLogger, RecordingMeter


class WorkerTelemetryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        from opentelemetry.trace import NonRecordingSpan, SpanContext
        self.work = Path(self.temp.name) / "worker"
        self.store = LocalObjectStore(Path(self.temp.name) / "objects")
        self.logger = RecordingLogger()
        tracer = type("Tracer", (), {"start_span": lambda _self, *_args, **_kwargs:
            NonRecordingSpan(SpanContext(1, 2, False))})()
        self.telemetry = StageTelemetry(logger=self.logger, tracer=tracer, meter=RecordingMeter())

    def job(self, key=None, *, audio=False):
        request = {"request": {"prompt": "SECRET private prompt", "duration": 4, "resolution": "custom",
            "width": 256, "height": 256, "generate_audio": audio, "export_crf": 18},
            "output_spec": {"width": 256, "height": 256}, "assets": {}}
        plan = self.repo.create_plan(self.scope, request,
            {"pool": "worker-test", "backend": "mock", "enabled": True, "expected_runtime_s": 1},
            expires_at=self.now+1000, estimated_cost_microusd=100000)
        return self.repo.create_job(self.scope, plan["id"], key or uuid.uuid4().hex,
            budget_account_ids=["owner-budget"])

    def runner(self, backend=None, **kwargs):
        return WorkerRunner(self.repo, self.store, self.work,
            backend=backend or MockBackend(self.work / "mock", enabled=True), retry_after_s=0,
            telemetry=self.telemetry, telemetry_context={"provider": "local", "url": "SECRET URL"}, **kwargs)

    def finished(self, name):
        return [event for event in self.logger.events if event["stage"] == name and event["event"] == "stage_finished"]

    def test_original_video_audio_flow_reports_real_stage_boundaries_and_commit(self):
        job = self.job(audio=True)
        self.now += 8
        result = self.runner().run_once("worker", "worker-test")
        self.assertEqual(result["state"], "succeeded")
        names = [e["stage"] for e in self.logger.events if e["event"] == "stage_finished"]
        self.assertEqual(names, ["queue_wait", "input_transfer", "generate", "collect",
            "validate_output", "upload", "commit_result", "end_to_end"])
        self.assertEqual(self.finished("queue_wait")[0]["duration_seconds"], 8)
        self.assertEqual(self.finished("end_to_end")[0]["timing_basis"], "durable_cpu_interval")
        self.assertEqual(len(self.repo.list_artifacts(self.scope, job["id"])), 2)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        self.assertTrue(all(e["simulation"] for e in self.logger.events))
        self.assertNotIn("SECRET", json.dumps(self.logger.events))

    def test_generation_interval_spans_waiting_polls_instead_of_measuring_one_poll(self):
        job = self.job()
        backend = MockBackend(self.work / "mock", enabled=True)
        original_poll = backend.poll
        calls = []
        def poll(tag, task_id):
            calls.append(tag)
            return Outcome("running", task_id) if len(calls) == 1 else original_poll(tag, task_id)
        backend.poll = poll
        runner = self.runner(backend)
        self.now += 10
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "running")
        self.assertEqual(self.finished("generate"), [])
        self.now += 47
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "succeeded")
        measured = self.finished("generate")
        self.assertEqual(len(measured), 1)
        self.assertEqual(measured[0]["duration_seconds"], 47)
        self.assertEqual(measured[0]["timing_basis"], "durable_cpu_interval")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_lost_submit_response_reconstructs_original_interval_without_resubmission(self):
        job = self.job()
        backend = MockBackend(self.work / "mock", enabled=True)
        original_submit, posts = backend.submit, []
        def lost(prepared, tag):
            posts.append(tag)
            original_submit(prepared, tag)
            raise SubmissionUncertain("SECRET private upstream response")
        backend.submit = lost
        first = self.runner(backend)
        self.now += 5
        self.assertEqual(first.run_once("worker", "worker-test")["state"], "submission_unknown")
        self.assertEqual(self.finished("generate"), [])
        self.assertTrue(any(e["event"] == "stage_observation" and e["error_code"] == "submission_response_unknown"
            for e in self.logger.events))
        self.now += 31
        self.assertEqual(self.runner(backend).run_once("worker-restarted", "worker-test")["state"], "succeeded")
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.finished("generate")[0]["duration_seconds"], 31)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertNotIn("SECRET", json.dumps(self.logger.events))

    def test_failed_validation_does_not_publish_success_and_recovers_same_attempt(self):
        job = self.job()
        runner = self.runner()
        with patch("studio_platform.worker._validate_video", side_effect=BackendError("output_video_verification_failed")):
            self.assertEqual(runner.run_once("worker", "worker-test")["state"], "collecting")
        self.assertEqual(self.finished("validate_output")[0]["outcome"], "failure")
        self.assertEqual(self.finished("validate_output")[0]["error_code"], "output_video_verification_failed")
        self.assertFalse(self.finished("upload"))
        self.assertFalse(self.finished("commit_result"))
        self.assertFalse(self.finished("end_to_end"))
        self.now += 31
        self.assertEqual(runner.run_once("worker", "worker-test")["state"], "succeeded")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(len(self.finished("generate")), 1)
        self.assertEqual(len(self.finished("end_to_end")), 1)

    def test_broken_injected_start_finish_and_note_cannot_change_original_job(self):
        class BrokenStage:
            def finish(self, *_args, **_kwargs):
                raise RuntimeError("SECRET observer finish")
            def note(self, *_args, **_kwargs):
                raise RuntimeError("SECRET observer note")
        class Broken:
            def __init__(self, start_fails):
                self.start_fails = start_fails
            def start(self, *_args, **_kwargs):
                if self.start_fails:
                    raise RuntimeError("SECRET observer start")
                return BrokenStage()
        for start_fails in (True, False):
            job = self.job()
            runner = self.runner()
            runner.telemetry = Broken(start_fails)
            self.assertEqual(runner.run_once("worker", "worker-test")["state"], "succeeded")
            self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
            self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()

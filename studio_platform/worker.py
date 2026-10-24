"""Explicit single-slot worker. Disabled by default, with no import-time requests.

Mock output is CPU-generated and visibly labelled SIMULATION. Comfy transport
requires an exact operator allowlist; it never follows provider output URLs.
The pinned workflow compiler is pure; legacy server/cloud controllers are unused.
"""

from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time

from .media import ffmpeg, probe
from .queue import TaskQueue
from .repository import Conflict, LeaseLost, Scope, money
from .storage import ObjectAlreadyExists, ObjectNotFound, StorageWriteUncertain
from .telemetry import ERROR_CODES as TELEMETRY_ERROR_CODES, NullStage, NullTelemetry

# Compatibility exports retain the same objects for existing worker callers.
from .inference.comfy import ComfyBackend
from .inference.outputs import _request, _shape, delivery_spec
from .inference.protocol import (
    BackendError,
    InferenceBackend,
    NotReady,
    Outcome,
    RenderCacheCapacityExceeded,
    SubmissionRejected,
    SubmissionUncertain,
    TAG,
    TASK,
    safe_failure_code,
)


class DisabledBackend:
    enabled = False
    slot_key = "disabled"


class MockBackend:
    """Local CPU demonstration, not an AI model or video benchmark."""

    def __init__(self, state_dir, *, enabled=False):
        self.enabled = bool(enabled)
        self.state_dir = Path(state_dir).resolve()
        self.slot_key = (
            "mock-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()
        )
        self.kind = "mock"

    def prepare(self, job, tag, store, heartbeat):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        return dict(shape=_shape(job), tag=tag)

    def submit(self, prepared, tag):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        receipt = self.state_dir / (tag + ".json")
        if receipt.exists():
            raise SubmissionUncertain("mock_submission_already_recorded")
        width, height, duration, audio = prepared["shape"]
        video = self.state_dir / (tag + ".mp4")
        # Pillow bundles its default font. Do not depend on a host Fontconfig or
        # silently omit the watermark when an OS font cannot be found.
        from PIL import Image, ImageDraw, ImageFont

        frame = Image.new("RGB", (width, height), (23, 32, 66))
        draw = ImageDraw.Draw(frame)
        for label, size, y, color in (
            ("SIMULATION", max(18, width // 14), height // 2, "yellow"),
            ("CPU demo - NOT H3", max(12, width // 24), height - 40, "white"),
        ):
            font = ImageFont.load_default(size=size)
            box = draw.textbbox((0, 0), label, font=font)
            draw.text(
                ((width - (box[2] - box[0])) / 2, y), label, fill=color, font=font
            )
        image = self.state_dir / (tag + "-simulation.png")
        frame.save(image)
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "24",
            "-i",
            str(image),
        ]
        if audio:
            command += [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=32000:duration={duration}",
            ]
        command += [
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
        command += ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
        command += ["-movflags", "+faststart", str(video)]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=180)
            if audio:
                ffmpeg(
                    [
                        "-i",
                        video,
                        "-vn",
                        "-c:a",
                        "flac",
                        self.state_dir / (tag + ".flac"),
                    ]
                )
            task_id = "mock-" + tag
            payload = {
                "task_id": task_id,
                "tag": tag,
                "simulation": True,
                "audio": audio,
            }
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return task_id
        except (OSError, subprocess.SubprocessError, ValueError):
            raise SubmissionRejected("mock_cpu_generation_failed") from None

    def reconcile(self, tag, task_id=None):
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        if task_id is not None and task_id != "mock-" + tag:
            raise BackendError("mock_task_identity_mismatch")
        receipt = self.state_dir / (tag + ".json")
        if not receipt.exists():
            return Outcome("unknown")
        try:
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if result["task_id"] != "mock-" + tag or result["simulation"] is not True:
                raise ValueError
        except (ValueError, KeyError):
            raise BackendError("invalid_mock_receipt") from None
        return Outcome("succeeded", result["task_id"], 0)

    def poll(self, tag, task_id):
        return self.reconcile(tag, task_id)

    def cancel(self, tag, task_id):
        # CPU submit finishes synchronously: already completed results are retained.
        return False

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        outcome = self.reconcile(tag, task_id)
        if outcome.state != "succeeded":
            raise BackendError("mock_output_unknown")
        files = {"video": self.state_dir / (tag + ".mp4")}
        if _shape(job)[3]:
            files["audio"] = self.state_dir / (tag + ".flac")
        return files


class WorkerRunner:
    def __init__(
        self,
        repository,
        store,
        work_dir,
        *,
        backend: InferenceBackend | None = None,
        retry_after_s=5,
        control=None,
        submission_guard=None,
        stop_requested=None,
        telemetry=None,
        telemetry_context=None,
        selected_job_id=None,
    ):
        self.repo, self.store = repository, store
        self.queue = TaskQueue(repository)
        self.work_dir = Path(work_dir).resolve()
        self.backend = backend or DisabledBackend()
        self.retry_after_s = retry_after_s
        self.control = control
        self.submission_guard = submission_guard
        self.stop_requested = stop_requested
        # Durable transport selects an original ledger identity, never a recipe
        # or a second submission. Fairness and physical slot claims remain below.
        self.selected_job_id = selected_job_id
        self.telemetry = telemetry if telemetry is not None else NullTelemetry()
        self.telemetry_context = (
            dict(telemetry_context) if type(telemetry_context) is dict else {}
        )
        # One observed generation per physical slot; this is not execution state.
        self._generation_observation = None
        self._qualification_delivery_ack_dropped = False
        self._drain = threading.Event()

    def _telemetry_context(self, job, lease):
        from .generation_diagnostics import failure_log_context

        compiled = job.get("request", {})
        request, output = compiled.get("request", {}), compiled.get("output_spec", {})
        context = dict(self.telemetry_context)
        context.update(
            job_id=job["id"],
            attempt_id=lease.attempt_id,
            profile_id=job.get("execution_plan", {}).get("deployment_profile_id")
            or compiled.get("deployment_profile_id")
            or "unknown",
            mode=request.get("mode", "unknown"),
            simulation=self._summary(job)["simulation"],
        )
        for name in ("width", "height", "fps", "frames"):
            value = output.get(
                "frame_count" if name == "frames" else name, request.get(name)
            )
            if value is not None:
                context[name] = value
        if "steps" in request:
            context["steps"] = request["steps"]
        inputs = request.get("inputs", {})
        if type(inputs) is dict:
            for name, plural in (
                ("image_refs", "images"),
                ("video_refs", "videos"),
                ("audio_refs", "audios"),
            ):
                if type(inputs.get(plural)) is list:
                    context[name] = len(inputs[plural])
        context.update(failure_log_context(job))
        return context

    def _fail(
        self, job, lease, error_code, *, actual_cost_microusd, upstream_stopped=False
    ):
        try:
            context = self._telemetry_context(job, lease)
        except Exception:
            context = (
                None  # Optional observation never prevents original failure/settlement.
            )
        result = self.queue.fail(
            lease,
            error_code,
            actual_cost_microusd=actual_cost_microusd,
            upstream_stopped=upstream_stopped,
            failure_context=context,
        )
        return self._summary(result)

    def _stage_start(self, name, job, lease, *, started_at=None):
        try:
            return self.telemetry.start(
                name, self._telemetry_context(job, lease), started_at=started_at
            )
        except Exception:
            return NullStage()

    @staticmethod
    def _stage_finish(
        stage,
        outcome="success",
        *,
        error_code=None,
        duration_seconds=None,
        timing_basis=None,
    ):
        try:
            stage.finish(
                outcome,
                error_code=error_code,
                duration_seconds=duration_seconds,
                timing_basis=timing_basis,
            )
        except Exception:
            pass

    @contextmanager
    def _stage(self, name, job, lease):
        stage = self._stage_start(name, job, lease)
        try:
            yield
        except BaseException as error:
            code = {
                "collect": "collection_failed",
                "validate_output": "artifact_validation_failed",
                "upload": "upload_failed",
                "input_transfer": "worker_preparation_failed",
            }.get(name, "stage_failed")
            if (
                len(error.args) == 1
                and type(error.args[0]) is str
                and error.args[0] in TELEMETRY_ERROR_CODES
            ):
                code = error.args[0]
            self._stage_finish(stage, "failure", error_code=code)
            raise
        else:
            self._stage_finish(stage)

    def _observe_interval(
        self, name, job, lease, started_at, *, outcome="success", error_code=None
    ):
        try:
            ended_at = self.repo.clock()
            if (
                type(started_at) not in (float, int)
                or not math.isfinite(started_at)
                or not math.isfinite(ended_at)
                or ended_at < started_at
            ):
                return
            stage = self._stage_start(name, job, lease, started_at=started_at)
            self._stage_finish(
                stage,
                outcome,
                error_code=error_code,
                duration_seconds=ended_at - started_at,
                timing_basis="durable_cpu_interval",
            )
        except Exception:
            pass

    def _generation_start(self, job, lease, started_at):
        # Polls/reconciliation keep the same span; replacing an old observation
        # does not free, cancel or otherwise change its original execution.
        if type(started_at) not in (float, int) or not math.isfinite(started_at):
            return
        old = self._generation_observation
        if old is not None and old[0] == lease.attempt_id:
            return
        if old is not None:
            self._stage_finish(
                old[2], "unknown", error_code="worker_reconciliation_needed"
            )
        self._generation_observation = (
            lease.attempt_id,
            started_at,
            self._stage_start("generate", job, lease, started_at=started_at),
        )

    def _generation_note(self, error_code):
        observation = self._generation_observation
        if observation is not None:
            try:
                observation[2].note(error_code)
            except Exception:
                pass

    def _generation_finish(self, lease, outcome, *, error_code=None):
        observation = self._generation_observation
        if observation is None or observation[0] != lease.attempt_id:
            return
        self._generation_observation = None
        try:
            duration = self.repo.clock() - observation[1]
        except Exception:
            duration = None
        self._stage_finish(
            observation[2],
            outcome,
            error_code=error_code,
            duration_seconds=duration,
            timing_basis="durable_cpu_interval",
        )

    def _commit_result(self, job, lease, specs, cost, settlement, moderation=None):
        with self._stage("commit_result", job, lease):
            result = self.queue.complete(
                lease,
                specs,
                actual_cost_microusd=cost,
                settlement=settlement,
                moderation=moderation,
            )
        # Complete only after validation, object publication and the original
        # result transaction. This observation never authorizes a replay.
        self._observe_interval("end_to_end", result, lease, job.get("created_at"))
        return self._summary(result)

    def _cleanup_collection(self, writer, job, lease, tag, *, publish_renditions=True):
        try:
            writer.cleanup_settled(job, lease.attempt_id, tag)
        except Exception:
            # Acceptance already committed. Retained scratch is a cleanup
            # obligation, never a reason to resubmit or fail the original job.
            logging.getLogger(__name__).warning("r2_artifact_scratch_cleanup_pending")
            return  # The original manifest must complete before display scratch is introduced.
        if not publish_renditions:
            return
        try:
            from .artifact_renditions import ensure

            ensure(writer, job, lease.attempt_id, tag)
        except Exception:
            # Display failures never invalidate acceptance or trigger replay.
            logging.getLogger(__name__).warning(
                "artifact_poster_pending_original_receipt"
            )

    def _complete_collection(self, writer, job, lease, tag, specs, cost, receipt):
        from .content_policy import ContentPolicyError, check_output

        try:
            # The classifier may use IO; only its immutable receipt crosses the
            # original money/storage transaction in queue.complete.
            moderation = check_output(self.repo, job, lease.attempt_id, specs)
            result = self._commit_result(
                job, lease, specs, cost, writer.settlement(receipt), moderation
            )
        except ContentPolicyError as error:
            return self._summary(
                self.queue.collection_failed(lease, error_code=error.code)
            )
        self._cleanup_collection(
            writer,
            job,
            lease,
            tag,
            publish_renditions=result["state"] == "succeeded",
        )
        return result

    def drain(self):
        self._drain.set()

    def _check_external_stop(self):
        if self.stop_requested is not None and self.stop_requested() is True:
            self.drain()

    def _submission_allowed(self, job):
        if isinstance(self.backend, MockBackend):
            return True
        try:
            return (
                callable(self.submission_guard)
                and self.submission_guard(job) is True
                and (self.control is None or self.control.submission_allowed(job))
            )
        except Exception:
            return False

    @staticmethod
    def _scope(job):
        return Scope(
            job["tenant_id"], job["owner_id"], job["project_id"], job["actor_id"]
        )

    def _summary(self, job, state=None):
        return {
            "job_id": job["id"],
            "state": state or job["status"],
            "simulation": isinstance(self.backend, MockBackend)
            or (
                self.backend.kind == "cpu-render"
                and any(
                    source.get("simulation") is True
                    for source in job["request"].get("sources", {}).values()
                )
            ),
        }

    def _observe(self, worker_id, job_id):
        """Profile-specific observation runs while the physical slot lock is held."""
        return self.control.observe(worker_id, job_id)

    def run_once(self, worker_id, pool):
        if not self.backend.enabled:
            return {"state": "disabled", "simulation": False}
        self._check_external_stop()
        if self._drain.is_set():
            return {
                "state": "draining",
                "simulation": isinstance(self.backend, MockBackend),
            }
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(self.work_dir, self.backend.slot_key) as acquired:
            if not acquired:
                return {
                    "state": "slot_busy",
                    "simulation": isinstance(self.backend, MockBackend),
                }
            if self.control:
                # Keep an already valid registration alive even when the queue
                # is empty. Never renew/revive an expired or UNKNOWN slot merely
                # because this process is running; it needs reconciliation/proof.
                worker = self.control.get(worker_id)
                if worker["expires_at"] > self.repo.clock() and worker["state"] not in (
                    "unknown",
                    "retired",
                ):
                    try:
                        self.control.heartbeat(worker_id, worker["fence"])
                    except Conflict:
                        return {
                            "state": "slot_reconciliation_required",
                            "simulation": isinstance(self.backend, MockBackend),
                        }
                self.control.recover_expired()
            result = self._run_once(worker_id, pool)
            if self.control and result.get("job_id"):
                self._observe(worker_id, result["job_id"])
            return result

    def _claim(self, worker_id, pool, *, purpose):
        options = (
            {"selected_job_id": self.selected_job_id} if self.selected_job_id else {}
        )
        return (self.control or self.queue).claim(
            worker_id, pool, lease_seconds=900, purpose=purpose, **options
        )

    def _run_once(self, worker_id, pool):
        self.queue.recover_expired(summary=True)
        claim, purpose = None, None
        for phase in ("collect", "reconcile", "generate"):
            claim = self._claim(worker_id, pool, purpose=phase)
            if claim:
                purpose = phase
                break
        if claim is None:
            return {
                "state": "idle",
                "simulation": isinstance(self.backend, MockBackend),
            }
        job, lease = claim.job, claim.lease
        tag = lease.attempt_id
        if purpose == "generate":
            self._observe_interval("queue_wait", job, lease, job.get("created_at"))
        slot_fence = self.control.get(worker_id)["fence"] if self.control else None

        def heartbeat():
            self._check_external_stop()
            self.queue.heartbeat(lease, lease_seconds=900)
            if self.control:
                self.control.heartbeat(worker_id, slot_fence, lease_seconds=900)
            if self.backend.kind == "cpu-render":
                current = self.repo.get_job(self._scope(job), job["id"])
                if current["status"] == "cancel_requested":
                    self.backend.cancel(tag, "cpu-render-" + tag)

        task_id = None
        try:
            if (
                job["execution_plan"].get("backend") != self.backend.kind
                or job["execution_plan"].get("enabled") is not True
            ):
                return self._summary(
                    self.queue.defer_unsubmitted(
                        lease, error_code="worker_backend_not_authorized"
                    )
                )
            if purpose == "generate":
                if not self._submission_allowed(job):
                    return self._fail(
                        job,
                        lease,
                        "execution_policy_unavailable_before_submission",
                        actual_cost_microusd=0,
                        upstream_stopped=True,
                    )
                try:
                    delivery_spec(
                        job
                    )  # Reject unknown/malformed export policy before any engine preparation.
                    with self._stage("input_transfer", job, lease):
                        prepared = self.backend.prepare(job, tag, self.store, heartbeat)
                except RenderCacheCapacityExceeded:
                    # No submission intent/POST exists. Retained input/cache
                    # evidence stays untouched; do not grow attempts forever
                    # against an operator-managed cache with no automatic GC.
                    if self.backend.kind == "cpu-render":
                        return self._fail(
                            job,
                            lease,
                            "render_cache_capacity_exhausted",
                            actual_cost_microusd=0,
                            upstream_stopped=True,
                        )
                    return self._summary(
                        self.queue.defer_unsubmitted(
                            lease,
                            retry_after_s=30,
                            error_code="worker_preparation_not_ready",
                        )
                    )
                except (BackendError, OSError, ValueError):
                    job = self.queue.defer_unsubmitted(
                        lease,
                        retry_after_s=30,
                        error_code="worker_preparation_not_ready",
                    )
                    return self._summary(job)
                self._check_external_stop()
                if self._drain.is_set():
                    return self._summary(
                        self.queue.defer_unsubmitted(
                            lease, error_code="worker_draining"
                        )
                    )
                if not self._submission_allowed(job):
                    return self._fail(
                        job,
                        lease,
                        "execution_policy_unavailable_before_submission",
                        actual_cost_microusd=0,
                        upstream_stopped=True,
                    )
                submitting = self.queue.begin_submission(lease)
                self._generation_start(job, lease, submitting["updated_at"])
                try:
                    task_id = self.backend.submit(prepared, tag)
                except SubmissionRejected:
                    self._generation_finish(
                        lease, "failure", error_code="submission_rejected"
                    )
                    return self._fail(
                        job,
                        lease,
                        "submission_rejected",
                        actual_cost_microusd=0,
                        upstream_stopped=True,
                    )
                except Exception:
                    self._generation_note("submission_response_unknown")
                    return self._summary(self.queue.mark_submission_unknown(lease))
                job = self.queue.record_submitted(lease, task_id)
            else:
                attempt = self.queue.get_attempt(self._scope(job), job["id"])
                task_id = attempt["upstream_task_id"]
                if attempt["status"] not in (
                    "collecting",
                    "succeeded",
                    "failed",
                    "cancelled",
                ):
                    self._generation_start(job, lease, attempt["submission_started_at"])
                if not task_id:
                    outcome = self.backend.reconcile(tag)
                    if not outcome.task_id:
                        self._generation_note("submission_needs_reconciliation")
                        return self._summary(
                            self.queue.release(
                                lease,
                                retry_after_s=30,
                                error_code="submission_needs_reconciliation",
                            )
                        )
                    task_id = outcome.task_id
                    job = self.queue.record_submitted(lease, task_id)
            self._check_external_stop()
            if self._drain.is_set():
                return self._summary(
                    self.queue.release(lease, retry_after_s=self.retry_after_s)
                )
            if (
                job["status"] == "cancel_requested"
                and job["cancel_from_status"] != "collecting"
            ):
                self.backend.cancel(tag, task_id)
            if (
                job["status"] == "collecting"
                or job.get("cancel_from_status") == "collecting"
            ):
                return self._collect(job, lease, tag, task_id, heartbeat)
            outcome = self.backend.poll(tag, task_id)
            if (
                outcome.state == "unknown"
                and self.backend.kind == "cpu-render"
                and job["status"] != "cancel_requested"
                and callable(getattr(self.backend, "resume", None))
            ):
                # Only the local renderer supports checkpoint recovery. Its
                # original process fence, payload identity and output hashes
                # must positively authorize continuation of this SAME attempt.
                try:
                    resumed = self.backend.resume(job, tag, task_id, heartbeat)
                    if resumed != task_id:
                        raise BackendError("render_resume_task_identity_changed")
                    outcome = self.backend.poll(tag, task_id)
                except SubmissionUncertain:
                    pass  # Original process is still fenced; observe it later.
                except BackendError as error:
                    if error.args != ("render_resume_identity_unconfirmed",):
                        raise
                    return self._summary(
                        self.queue.release(
                            lease,
                            retry_after_s=30,
                            error_code="render_resume_identity_unconfirmed",
                        )
                    )
            if outcome.state == "running" and self.backend.kind == "wangp-worker":
                from .capacity_watchdog import record_engine_progress

                record_engine_progress(self.repo, self.queue, lease)
            if outcome.state == "succeeded":
                self._generation_finish(lease, "success")
                job = self.queue.begin_collection(lease)
                return self._collect(job, lease, tag, task_id, heartbeat)
            if outcome.state in ("failed", "cancelled"):
                self._generation_finish(
                    lease,
                    "cancelled" if outcome.state == "cancelled" else "failure",
                    error_code=safe_failure_code(getattr(outcome, "error_code", None))
                    or "upstream_generation_failed",
                )
                cost = self._cost(job, task_id, outcome)
                if job["status"] == "cancel_requested":
                    return self._summary(
                        self.queue.confirm_cancel(
                            lease, upstream_stopped=True, actual_cost_microusd=cost
                        )
                    )
                # Only a closed diagnostic vocabulary may cross from an adapter
                # into public/durable error fields. It cannot authorize retry.
                code = (
                    safe_failure_code(getattr(outcome, "error_code", None))
                    if self.backend.kind == "wangp-worker" and outcome.state == "failed"
                    else None
                )
                return self._fail(
                    job,
                    lease,
                    code or "upstream_generation_failed",
                    actual_cost_microusd=cost,
                    upstream_stopped=True,
                )
            if outcome.state == "unknown":
                self._generation_note("upstream_status_unknown")
            return self._summary(
                self.queue.release(
                    lease,
                    retry_after_s=self.retry_after_s,
                    error_code="upstream_status_unknown"
                    if outcome.state == "unknown"
                    else None,
                )
            )
        except LeaseLost:
            self._generation_note("lease_lost_reconcile_required")
            return self._summary(job, "lease_lost_reconcile_required")
        except Exception:
            self._generation_note("worker_reconciliation_needed")
            # No raw exception response, URL, request or prompt escapes this worker.
            fresh = self.repo.get_job(self._scope(job), job["id"])
            try:
                if fresh["status"] == "claimed":
                    fresh = self.queue.defer_unsubmitted(
                        lease, error_code="worker_preparation_failed"
                    )
                elif fresh["status"] == "submitting":
                    fresh = self.queue.mark_submission_unknown(lease)
                elif (
                    fresh["status"] == "collecting"
                    or fresh.get("cancel_from_status") == "collecting"
                ):
                    fresh = self.queue.collection_failed(lease)
                else:
                    fresh = self.queue.release(
                        lease,
                        retry_after_s=30,
                        error_code="worker_reconciliation_needed",
                    )
            except (LeaseLost, Conflict):
                return self._summary(fresh, "lease_lost_reconcile_required")
            return self._summary(fresh)

    def _cost(self, job, task_id, outcome=None):
        if isinstance(self.backend, MockBackend):
            return 0
        # Billing availability is independent of verified output availability.
        # A timeout or malformed invoice is unknown cost, never a free task and
        # never a reason to repeat collection/submission. The ledger retains its
        # reservation until an explicit, validated settlement arrives later.
        try:
            if outcome and outcome.actual_cost_microusd is not None:
                cost = outcome.actual_cost_microusd
            else:
                resolver = getattr(self.backend, "cost_resolver", None)
                cost = resolver(job, task_id) if resolver is not None else None
            return None if cost is None else money(cost)
        except Exception:
            return None

    def _collect(self, job, lease, tag, task_id, heartbeat):
        from .artifact_writer import ArtifactWriter
        from .inference.outputs import gpu_delivery

        writer = ArtifactWriter(
            self.repo.engine, self.store, self.work_dir, tenant=job["tenant_id"]
        )
        receipt = writer.get(job, lease.attempt_id, tag)
        if receipt is not None and receipt["phase"] == "settled":
            current = self.repo.get_job(self._scope(job), job["id"])
            refused = (
                current["status"] == "failed"
                and current.get("error_code") == "content_moderation_refused"
                and (current.get("result") or {}).get("moderation", {}).get("status")
                == "refused"
            )
            if current["status"] != "succeeded" and not refused:
                raise BackendError("artifact_settlement_job_mismatch")
            self._cleanup_collection(
                writer, current, lease, tag, publish_renditions=not refused
            )
            return self._summary(current)
        if gpu_delivery(job):
            return self._collect_remote(
                writer, job, lease, tag, task_id, heartbeat, receipt
            )
        if receipt is not None and receipt["phase"] != "staging":
            with _lease_keepalive(heartbeat), self._stage("upload", job, lease):
                specs = writer.write(receipt, heartbeat)
            return self._complete_collection(
                writer, job, lease, tag, specs, self._cost(job, task_id), receipt
            )
        width, height, duration, audio = _shape(job)
        delivery = delivery_spec(job)
        if delivery is not None:
            duration = delivery["duration_s"]
        with self._stage("collect", job, lease):
            writer.begin_staging(
                job,
                lease.attempt_id,
                tag,
                kinds=("video", "audio") if audio else ("video",),
            )
            directory = self.work_dir / tag
            directory.mkdir(parents=True, exist_ok=True)
            heartbeat()
            paths = self.backend.fetch(job, tag, task_id, directory, heartbeat)
        with self._stage("validate_output", job, lease):
            raw = probe(paths["video"])
            stream = next(
                (s for s in raw.get("streams", []) if s.get("codec_type") == "video"),
                {},
            )
            if (stream.get("width"), stream.get("height")) != (width, height):
                raise BackendError("output_dimensions_mismatch")
            if delivery is not None:
                _validate_native_timing(stream, delivery)
                if audio:
                    if "audio" not in paths:
                        raise BackendError("independent_audio_missing")
                    _validate_audio(paths["audio"], duration)
            try:
                if float(raw["format"]["duration"]) < duration - 0.1:
                    raise BackendError("output_too_short")
            except (ValueError, KeyError):
                raise BackendError("output_duration_unknown") from None
            final = directory / "verified.mp4"
            request = _request(job)
            media_timeout = (
                1800
                if job["request"].get("recipe_id") == "chapter-roughcut-v1"
                else 180
            )
            heartbeat()
            args = [
                "-i",
                paths["video"],
                "-t",
                duration,
                "-frames:v",
                round(duration * 24),
                "-vf",
                "fps=24",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                request.get("export_crf", 18),
                "-pix_fmt",
                "yuv420p",
            ]
            args += ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
            if delivery is not None:
                # Keep the complete native timeline. Both audio deliveries use the
                # original generated waveform; no trim, pad, fps filter or atempo.
                args = ["-i", paths["video"]]
                if audio:
                    args += ["-i", paths["audio"], "-map", "0:v:0", "-map", "1:a:0"]
                args += [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    request.get("export_crf", 18),
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode",
                    "passthrough",
                ]
                args += (
                    ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
                )
            with _lease_keepalive(heartbeat):
                # media.ffmpeg bounds input decoding; this output-side limit
                # bounds encoder buffers inside the shared CPU worker cgroup.
                ffmpeg(
                    [
                        *args,
                        "-threads",
                        "1",
                        "-fs",
                        512 * 1024 * 1024 + 1,
                        "-movflags",
                        "+faststart",
                        final,
                    ],
                    timeout=media_timeout,
                )
                video_evidence = _validate_video(
                    final, width, height, duration, audio, timeout=media_timeout
                )
                if delivery is not None:
                    observed = probe(final)
                    observed_video = next(
                        s for s in observed["streams"] if s.get("codec_type") == "video"
                    )
                    _validate_native_timing(observed_video, delivery)
                    video_evidence.update(
                        delivery_spec=delivery,
                        frame_count=delivery["frame_count"],
                        container_duration_s=float(observed["format"]["duration"]),
                    )
                    if audio:
                        observed_audio = next(
                            s
                            for s in observed["streams"]
                            if s.get("codec_type") == "audio"
                        )
                        video_evidence["audio_duration_s"] = float(
                            observed_audio.get(
                                "duration", observed["format"]["duration"]
                            )
                        )
            files = [("video", final, "video/mp4", video_evidence)]
            if audio:
                if "audio" not in paths:
                    raise BackendError("independent_audio_missing")
                output = directory / "verified.flac"
                heartbeat()
                with _lease_keepalive(heartbeat):
                    ffmpeg(
                        [
                            "-i",
                            paths["audio"],
                            *([] if delivery is not None else ["-t", duration]),
                            "-c:a",
                            "flac",
                            "-threads",
                            "1",
                            "-ar",
                            "32000",
                            "-ac",
                            "2",
                            "-fs",
                            512 * 1024 * 1024 + 1,
                            output,
                        ],
                        timeout=media_timeout,
                    )
                    actual_audio_duration = _validate_audio(
                        output, duration, flac=True, timeout=media_timeout
                    )
                files.append(
                    (
                        "audio",
                        output,
                        "audio/flac",
                        {
                            "duration_s": actual_audio_duration,
                            "has_audio": True,
                            **(
                                {"delivery_spec": delivery}
                                if delivery is not None
                                else {}
                            ),
                        },
                    )
                )
            with _lease_keepalive(heartbeat):
                receipt = writer.prepare(
                    job, lease.attempt_id, tag, files, raw_paths=paths.values()
                )
        with _lease_keepalive(heartbeat), self._stage("upload", job, lease):
            specs = writer.write(receipt, heartbeat)
        return self._complete_collection(
            writer, job, lease, tag, specs, self._cost(job, task_id), receipt
        )

    def _collect_remote(self, writer, job, lease, tag, task_id, heartbeat, receipt):
        """Frozen GPU/R2 delivery: CPU authorizes and hashes, never handles media."""
        if receipt is None:
            kinds = ("video", "audio") if _shape(job)[3] else ("video",)
            receipt = writer.begin_remote(job, lease.attempt_id, tag, kinds=kinds)
        if receipt["phase"] == "staging":
            with _lease_keepalive(heartbeat), self._stage("collect", job, lease):
                description = self.backend.prepare_delivery(
                    job, tag, task_id, heartbeat
                )
            with self._stage("validate_output", job, lease):
                receipt = writer.prepare_remote(
                    job, lease.attempt_id, tag, task_id, description
                )
        with _lease_keepalive(heartbeat), self._stage("upload", job, lease):
            try:
                specs = writer.verify_remote(receipt, heartbeat)
            except ObjectNotFound:
                unresolved = {
                    kind
                    for kind, role in receipt["roles"].items()
                    if role["status"] in {"putting", "unknown"}
                }
                stopped = False
                if unresolved:
                    status = self.backend.delivery_status(job, tag, task_id)
                    # Validate the original sealed identities before using any
                    # mutable upload-stop fact. Missing HEAD is not that proof.
                    receipt = writer.prepare_remote(
                        job, lease.attempt_id, tag, task_id, status
                    )
                    objects = status.get("objects", {})
                    stopped = all(
                        isinstance(objects.get(kind), dict)
                        and objects[kind].get("state") in {"not_started", "rejected"}
                        and objects[kind].get("stop_proven") is True
                        for kind in unresolved
                    )
                targets = writer.remote_targets(
                    receipt, heartbeat, upload_stopped=stopped
                )
                if targets:
                    try:
                        uploaded = self.backend.upload_delivery(
                            job, tag, task_id, targets, heartbeat
                        )
                        self._drop_qualification_delivery_ack(
                            job, tag, task_id, receipt, uploaded
                        )
                    except Exception:
                        writer.remote_unknown(receipt)
                        # A lost response may already have delivered both files.
                        # This is readback only, never another upload or encoding.
                        try:
                            specs = writer.verify_remote(receipt, heartbeat)
                        except ObjectNotFound:
                            raise BackendError(
                                "artifact_put_needs_original_reconciliation"
                            ) from None
                    else:
                        specs = writer.verify_remote(receipt, heartbeat)
                else:
                    specs = writer.verify_remote(receipt, heartbeat)
        return self._complete_collection(
            writer, job, lease, tag, specs, self._cost(job, task_id), receipt
        )

    def _drop_qualification_delivery_ack(self, job, tag, task_id, receipt, uploaded):
        """One root-protected qualification control; normal jobs are unaffected."""
        if (
            self._qualification_delivery_ack_dropped
            or type(uploaded) is not dict
            or os.name != "posix"
        ):
            return
        try:
            from .inference.wangp_delivery import validate_delivery_receipt

            uploaded = validate_delivery_receipt(uploaded)
            sealed = receipt["gpu_delivery"]
            if any(uploaded[name] != sealed[name] for name in sealed):
                return
            identity = uploaded["identity"]
            expected_roles = (
                {"video", "audio"} if uploaded["generate_audio"] else {"video"}
            )
            if (
                uploaded["state"] != "uploaded"
                or set(uploaded["roles"]) != expected_roles
                or set(uploaded["objects"]) != expected_roles
                or any(
                    value["state"] != "uploaded" or value["stop_proven"] is not True
                    for value in uploaded["objects"].values()
                )
                or any(
                    value["key"] != receipt["roles"][kind]["key"]
                    or value["storage_binding"] != receipt["binding"]
                    for kind, value in uploaded["objects"].items()
                )
                or uploaded["owner_id"] != job["owner_id"]
                or identity["job_id"] != job["id"]
                or identity["attempt_tag"] != tag
                or identity["operation_id"] != task_id
                or identity["request_hash"] != job["request_hash"]
                or identity["manifest_digest"]
                != job["execution_plan"]["engine_manifest_digest"]
            ):
                return
            path = self.work_dir / "qualification-drop-delivery-ack.json"
            before = path.lstat()

            def trusted(info):
                return (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == 0
                    and info.st_gid in {0, 10001}
                    and info.st_nlink == 1
                    and not info.st_mode & 0o027
                    and 0 < info.st_size <= 4096
                    and not getattr(info, "st_file_attributes", 0) & 0x400
                )

            def unchanged(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                )

            if not trusted(before):
                return
            descriptor = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)
            )
            try:
                opened = os.fstat(descriptor)
                if not trusted(opened) or unchanged(opened) != unchanged(before):
                    return
                raw = os.read(descriptor, 4097)
                if len(raw) != opened.st_size or unchanged(
                    os.fstat(descriptor)
                ) != unchanged(opened):
                    return
                if unchanged(path.lstat()) != unchanged(opened):
                    return
            finally:
                os.close(descriptor)

            def unique_pairs(pairs):
                result = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError("duplicate_qualification_control_field")
                    result[name] = value
                return result

            control = json.loads(raw, object_pairs_hook=unique_pairs)
            expected = {
                "version": 1,
                "mode": "drop_delivery_rpc_ack_once",
                "owner_id": job["owner_id"],
                "job_id": job["id"],
                "attempt_tag": tag,
                "request_hash": job["request_hash"],
                "deployment_profile_id": job["execution_plan"]["deployment_profile_id"],
                "manifest_digest": identity["manifest_digest"],
                "slot_key": identity["slot_key"],
                "incarnation": identity["incarnation"],
            }
            now = self.repo.clock()
            if (
                type(control) is not dict
                or set(control) != set(expected) | {"expires_at"}
                or type(control["version"]) is not int
                or any(control[name] != value for name, value in expected.items())
                or type(control["expires_at"]) not in (int, float)
                or not math.isfinite(control["expires_at"])
                or type(now) not in (int, float)
                or not math.isfinite(now)
                or not now < control["expires_at"] <= now + 3600
            ):
                return
        except Exception:
            # Missing, malformed or inaccessible controls never change collection.
            return
        self._qualification_delivery_ack_dropped = True
        logging.getLogger(__name__).warning(
            "qualification_delivery_rpc_ack_dropped_once job_id=%s attempt_tag=%s",
            job["id"],
            tag,
        )
        raise BackendError("qualification_delivery_rpc_ack_dropped_once")

    def run_forever(self, worker_id, pool, *, poll_interval_s=1):
        """Explicit CLI lifecycle; SIGTERM drains without globally interrupting Comfy."""
        if not math.isfinite(poll_interval_s) or not 0.01 <= poll_interval_s <= 60:
            raise ValueError("invalid_poll_interval")
        prior = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                prior[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: self.drain())
        try:
            while not self._drain.is_set():
                self.run_once(worker_id, pool)
                self._drain.wait(poll_interval_s)
        finally:
            try:
                if self.control:
                    self.control.drain(worker_id)
            finally:
                # Database loss during shutdown must not leave process handlers
                # replaced or a provider client open. Durable leases expire to
                # UNKNOWN; inability to persist drain is never an idle claim.
                for signum, handler in prior.items():
                    signal.signal(signum, handler)
                if hasattr(self.backend, "close"):
                    self.backend.close()


@contextmanager
def _lease_keepalive(heartbeat, *, interval_s=30):
    """Bounded background lease renewals during blocking local media stages."""
    if not math.isfinite(interval_s) or not 0 < interval_s <= 60:
        raise ValueError("invalid_media_heartbeat_interval")
    stopped, lost = threading.Event(), []

    def renew():
        while not stopped.wait(interval_s):
            try:
                heartbeat()
            except Exception:
                lost.append(True)
                return

    thread = threading.Thread(target=renew, name="sixnine-media-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
    if lost:
        raise LeaseLost("lease_lost_during_media_stage")


def _validate_native_timing(stream, delivery):
    """Reject an unexpected native timeline instead of coercing its frame rate."""
    try:
        if (
            Fraction(stream["avg_frame_rate"]) != delivery["fps"]
            or int(stream["nb_frames"]) != delivery["frame_count"]
            or abs(float(stream["duration"]) - delivery["duration_s"]) > 0.001
            or abs(float(stream.get("start_time", 0))) > 0.001
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise BackendError("native_output_timing_mismatch") from None


def _validate_video(path, width, height, duration, audio, *, timeout=180):
    data = probe(path)
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    try:
        fps = float(Fraction(stream["avg_frame_rate"]))
        actual = float(data["format"]["duration"])
        if (
            (stream["width"], stream["height"]) != (width, height)
            or abs(fps - 24) > 0.001
            or abs(actual - duration) > 0.1
            or stream.get("codec_name") != "h264"
            or int(stream.get("nb_frames", 0)) != round(duration * 24)
        ):
            raise ValueError
    except (ValueError, KeyError, ZeroDivisionError):
        raise BackendError("output_video_verification_failed") from None
    if audio:
        _validate_audio(path, duration, timeout=timeout)
    elif any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise BackendError("unexpected_output_audio")
    ffmpeg(
        [
            "-xerror",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-threads",
            "1",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    return {
        "width": width,
        "height": height,
        "duration_s": duration,
        "fps": 24,
        "has_audio": audio,
    }


def _validate_audio(path, duration, *, flac=False, timeout=180):
    """Verify the requested tolerance and return the file's observed duration."""
    data = probe(path)
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {}
    )
    try:
        actual = float(stream.get("duration", data["format"].get("duration")))
        if (
            int(stream["sample_rate"]) != 32000
            or stream["channels"] != 2
            or not math.isfinite(actual)
            or abs(actual - duration) > 0.1
            or flac
            and stream.get("codec_name") != "flac"
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise BackendError("output_audio_verification_failed") from None
    ffmpeg(
        ["-xerror", "-i", path, "-map", "0:a:0", "-threads", "1", "-f", "null", "-"],
        timeout=timeout,
    )
    return actual


@contextmanager
def _slot_lock(directory, key):
    """OS-held local lock; all runners for an endpoint must share this directory."""
    path = Path(directory) / (hashlib.sha256(key.encode()).hexdigest() + ".slot")
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    """Explicit local CLI. No cloud provisioning or automatic GPU discovery."""
    import argparse
    from dataclasses import replace
    from .control import WorkerControl, WorkerSpec
    from .repository import Repository
    from .settings import Settings
    from .storage import LocalObjectStore

    parser = argparse.ArgumentParser(
        description="Sixnine worker; disabled unless explicitly selected"
    )
    parser.add_argument(
        "--backend", choices=("disabled", "mock", "comfy-worker"), default="disabled"
    )
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--provider", default="")
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--gpu-id", action="append", default=[])
    parser.add_argument("--recipe-id", action="append", default=[])
    parser.add_argument("--model-id", default="")
    parser.add_argument("--configuration-id", default="")
    parser.add_argument("--comfy-revision", default="")
    parser.add_argument("--confirmed-idle", action="store_true")
    args = parser.parse_args(argv)
    if args.backend == "disabled":
        print(json.dumps({"state": "disabled", "simulation": False}))
        return 0
    repo, backend = None, None
    try:
        settings = Settings.from_environment()
        if args.data_dir:
            # An explicit data-dir changes the derived SQLite DB only; an explicitly
            # configured database URL is retained. Never print that URL.
            configured_url = (
                settings.database_url
                if (
                    os.environ.get("SIXNINE_DATABASE_URL")
                    or os.environ.get("SIXNINE_DATABASE_URL_FILE")
                )
                else ""
            )
            settings = replace(
                settings, data_dir=args.data_dir, database_url=configured_url
            )
        if settings.storage_provider != "local":
            raise ValueError("remote_worker_store_requires_runtime_adapter")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        repo = Repository(settings.database_url)
        repo.create_schema()
        control = WorkerControl(repo)
        if args.backend == "mock":
            backend = MockBackend(args.work_dir / "simulation", enabled=True)
            spec = WorkerSpec(
                args.worker_id,
                args.pool,
                "mock",
                "local-" + args.worker_id,
                ("cpu",),
                tuple(args.recipe_id),
                "SIMULATION",
                "simulation-v1",
                backend="mock",
            )
            # Mock-only registration consumes no GPUs/instances and grants no cloud permission.
            # Do not overwrite an existing approved real global ceiling.
            with repo.engine.connect() as connection:
                from .repository import capacity_gate

                present = connection.execute(
                    __import__("sqlalchemy").select(capacity_gate)
                ).first()
            if present is None:
                repo.configure_capacity()
        else:
            if not all(
                (
                    args.provider,
                    args.instance_id,
                    args.gpu_id,
                    args.recipe_id,
                    args.model_id,
                    args.configuration_id,
                    args.comfy_revision,
                )
            ):
                raise ValueError(
                    "real_worker_requires_explicit_slot_and_model_manifest"
                )
            backend = ComfyBackend(
                endpoint=args.endpoint,
                enabled=True,
                allowed_origins=args.allowed_origin,
                comfy_revision=args.comfy_revision,
            )
            spec = WorkerSpec(
                args.worker_id,
                args.pool,
                args.provider,
                args.instance_id,
                tuple(args.gpu_id),
                tuple(args.recipe_id),
                args.model_id,
                args.configuration_id,
            )
        worker = control.register(spec)
        if worker["state"] in ("registered", "ready"):
            control.mark_ready(
                args.worker_id,
                upstream_idle_confirmed=args.backend == "mock" or args.confirmed_idle,
            )
        elif (
            worker["state"] in ("unknown", "draining")
            and worker["current_job_id"] is None
        ):
            control.mark_ready(
                args.worker_id,
                upstream_idle_confirmed=args.backend == "mock" or args.confirmed_idle,
            )
        store = LocalObjectStore(settings.data_dir / "objects")
        from .execution_policy import ExecutionPolicies

        runner = WorkerRunner(
            repo,
            store,
            args.work_dir,
            backend=backend,
            control=control,
            submission_guard=ExecutionPolicies(settings, repo).submission_allowed,
        )
        if args.once:
            print(json.dumps(runner.run_once(args.worker_id, args.pool)))
        else:
            runner.run_forever(args.worker_id, args.pool)
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "state": "worker_configuration_or_runtime_error",
                    "detail": "Check explicit worker configuration; no secrets are logged",
                }
            )
        )
        return 1
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Use a real, already accepted job as runtime evidence; never submit a smoke job.

The ordinary queue owns submission, reconciliation, cancellation, media
validation, billing and the result. This profile only records bounded, nonsecret
evidence after that chain and quarantines failed workers atomically at observe.
"""

import json
import math
import os
from pathlib import Path
import re
import stat

from sqlalchemy import and_, or_, select

from .drain_safe_runner import DrainSafeRunner
from .repository import artifacts, attempts, canonical, jobs


QUEUED_TASK_PROFILE = "queued-task-first-v1"
_MAX_EVIDENCE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_FIELDS = {
    "intent_id",
    "instance_id",
    "configuration_id",
    "sources",
    "qualification_profile",
}
_ROOT_FIELDS = {
    "version",
    "qualification_profile",
    "identity",
    "worker_id",
    "instance_id",
    "configuration_id",
    "model_id",
    "verified",
    "evidence",
    "failures",
}
_CONTROL_FIELDS = {
    "mode",
    "duration",
    "resolution",
    "aspect_ratio",
    "steps",
    "generate_audio",
    "video_decode",
    "audio_decode",
    "encoder_device",
}
_OUTPUT_FIELDS = {
    "artifact_id",
    "kind",
    "sha256",
    "size_bytes",
    "width",
    "height",
    "duration_s",
    "fps",
    "has_audio",
    "frame_count",
}


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("queued_task_evidence_identity_invalid")
    return value


def _identity(value):
    value = canonical(value)
    if not isinstance(value, dict):
        raise ValueError("queued_task_evidence_identity_invalid")
    fields = _IDENTITY_FIELDS
    if "output_delivery" in value:
        from .inference.outputs import NATIVE_DELIVERY, validate_delivery_policy

        validate_delivery_policy(value.get("backend"), value["output_delivery"])
        if value["output_delivery"] != NATIVE_DELIVERY:
            raise ValueError("queued_task_output_delivery_invalid")
        fields = fields | {"output_delivery"}
    if "backend" in value or "engine_manifest_digest" in value:
        fields = fields | {"backend", "engine_manifest_digest"}
        if (
            value.get("backend") != "wangp-worker"
            or not isinstance(value.get("engine_manifest_digest"), str)
            or not _SHA.fullmatch(value["engine_manifest_digest"])
        ):
            raise ValueError("queued_task_evidence_engine_invalid")
    if set(value) != fields or value["qualification_profile"] != QUEUED_TASK_PROFILE:
        raise ValueError("queued_task_evidence_identity_invalid")
    for field in ("intent_id", "instance_id", "configuration_id"):
        _id(value[field])
    sources = value["sources"]
    if (
        not isinstance(sources, dict)
        or not 1 <= len(sources) <= 16
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key)
            or not isinstance(digest, str)
            or not _SHA.fullmatch(digest)
            for key, digest in sources.items()
        )
    ):
        raise ValueError("queued_task_evidence_sources_invalid")
    return value


def _path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("queued_task_evidence_absolute_path_required")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("queued_task_evidence_link_not_allowed")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or os.name != "nt"
            and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("queued_task_evidence_file_not_protected")
    return path


def _load(path, identity):
    path = _path(path)
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_EVIDENCE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > _MAX_EVIDENCE_BYTES:
        raise ValueError("queued_task_evidence_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("queued_task_evidence_invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != _ROOT_FIELDS
        or value["version"] != 1
        or value["qualification_profile"] != QUEUED_TASK_PROFILE
        or value["identity"] != identity
        or value["instance_id"] != identity["instance_id"]
        or value["configuration_id"] != identity["configuration_id"]
        or type(value["verified"]) is not bool
        or not isinstance(value["evidence"], list)
        or not isinstance(value["failures"], list)
        or len(value["evidence"]) > 1024
        or len(value["failures"]) > 1024
    ):
        raise ValueError("queued_task_evidence_identity_conflict")
    for field in ("worker_id", "instance_id", "configuration_id", "model_id"):
        _id(value[field])
    if value["verified"] != bool(value["evidence"]):
        raise ValueError("queued_task_evidence_verification_invalid")
    for scope in value["evidence"]:
        if (
            not isinstance(scope, dict)
            or set(scope)
            != {
                "job_id",
                "attempt_id",
                "request_hash",
                "recipe_id",
                "controls",
                "reference_kind_counts",
                "outputs",
                "completed_at",
            }
            or not isinstance(scope["request_hash"], str)
            or not _SHA.fullmatch(scope["request_hash"])
            or not isinstance(scope["controls"], dict)
            or set(scope["controls"]) - _CONTROL_FIELDS
            or not isinstance(scope["reference_kind_counts"], dict)
            or set(scope["reference_kind_counts"]) != {"image", "video", "audio"}
            or any(
                type(count) is not int or not 0 <= count <= 100
                for count in scope["reference_kind_counts"].values()
            )
            or not isinstance(scope["outputs"], list)
            or not 1 <= len(scope["outputs"]) <= 4
            or type(scope["completed_at"]) not in (int, float)
            or not math.isfinite(scope["completed_at"])
        ):
            raise ValueError("queued_task_evidence_scope_invalid")
        for field in ("job_id", "attempt_id", "recipe_id"):
            _id(scope[field])
        # Reuse the writer's bounded public control representation on reads too.
        if scope["controls"] != _controls(scope["controls"]):
            raise ValueError("queued_task_evidence_controls_invalid")
        for output in scope["outputs"]:
            if (
                not isinstance(output, dict)
                or set(output) - _OUTPUT_FIELDS
                or not {"artifact_id", "kind", "sha256", "size_bytes"} <= set(output)
                or output["kind"] not in {"video", "audio"}
                or not isinstance(output["sha256"], str)
                or not _SHA.fullmatch(output["sha256"])
                or type(output["size_bytes"]) is not int
                or output["size_bytes"] <= 0
            ):
                raise ValueError("queued_task_evidence_outputs_invalid")
            _id(output["artifact_id"])
            for field in set(output) - {"artifact_id", "kind", "sha256", "size_bytes"}:
                item = output[field]
                if field == "has_audio":
                    if type(item) is not bool:
                        raise ValueError("queued_task_evidence_outputs_invalid")
                elif (
                    type(item) not in (int, float)
                    or not math.isfinite(item)
                    or item <= 0
                ):
                    raise ValueError("queued_task_evidence_outputs_invalid")
    for failure in value["failures"]:
        if (
            not isinstance(failure, dict)
            or set(failure)
            != {"job_id", "attempt_id", "error_code", "status", "observed_at"}
            or failure["status"] not in {"failed", "queued", "collecting"}
            or type(failure["observed_at"]) not in (int, float)
            or not math.isfinite(failure["observed_at"])
        ):
            raise ValueError("queued_task_evidence_failure_invalid")
        for field in ("job_id", "attempt_id", "error_code"):
            _id(failure[field])
    return value


def _controls(request):
    controls = {}
    for field in _CONTROL_FIELDS:
        value = request.get(field)
        if field == "generate_audio":
            if type(value) is bool:
                controls[field] = value
        elif field in {"duration", "steps"}:
            if (
                type(value) in (int, float)
                and math.isfinite(value)
                and 0 < value <= 10000
            ):
                controls[field] = value
        elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,50}", value):
            controls[field] = value
    return controls


def read_verification_summary(path, *, expected_identity):
    """Read only exact, protected runtime evidence; an absent file is unverified.

    The scopes are evidence for these concrete jobs, not universal model, input
    modality, hardware or maximum-parameter qualification.
    """
    identity = _identity(expected_identity)
    value = _load(path, identity)
    if value is None:
        return {"generation_verified": False, "verified_job_scopes": []}
    return {
        "generation_verified": value["verified"],
        "verified_job_scopes": value["evidence"],
        "qualification_profile": value["qualification_profile"],
        "worker_id": value["worker_id"],
        "instance_id": value["instance_id"],
        "configuration_id": value["configuration_id"],
        "model_id": value["model_id"],
        "runtime_quarantined": bool(value["failures"]),
        "failures": value["failures"],
    }


class QueuedTaskRunner(DrainSafeRunner):
    def __init__(self, *args, qualification_evidence_file, evidence_identity, **kwargs):
        self.qualification_evidence_file = _path(qualification_evidence_file)
        self.evidence_identity = _identity(evidence_identity)
        # Existing evidence is identity-bound, never overwritten after a changed
        # model/runtime/operator source or a malformed prior receipt.
        previous = _load(self.qualification_evidence_file, self.evidence_identity)
        super().__init__(*args, **kwargs)
        if self.backend.kind != self.evidence_identity.get("backend", "comfy-worker"):
            raise ValueError("queued_task_requires_real_gpu_backend")
        if (
            self.backend.kind == "wangp-worker"
            and getattr(getattr(self.backend, "manifest", None), "digest", None)
            != self.evidence_identity["engine_manifest_digest"]
        ):
            raise ValueError("queued_task_backend_manifest_conflict")
        if previous is not None and previous["failures"]:
            self._stop_new.set()  # A process restart is not failure recovery.

    def verification_summary(self):
        return read_verification_summary(
            self.qualification_evidence_file, expected_identity=self.evidence_identity
        )

    def run_once(self, worker_id, pool):
        worker = self.control.get(worker_id)
        spec = worker["spec"]
        self._check_worker(worker)
        previous = _load(self.qualification_evidence_file, self.evidence_identity)
        if previous is not None and (
            previous["worker_id"] != worker_id
            or previous["model_id"] != spec["model_id"]
        ):
            raise ValueError("queued_task_worker_evidence_conflict")
        return super().run_once(worker_id, pool)

    def _check_worker(self, worker):
        spec, identity = worker["spec"], self.evidence_identity
        if (
            spec["backend"] != identity.get("backend", "comfy-worker")
            or spec.get("engine_manifest_digest", "")
            != identity.get("engine_manifest_digest", "")
            or spec.get("output_delivery", "") != identity.get("output_delivery", "")
            or worker["instance_id"] != identity["instance_id"]
            or spec["configuration_id"] != identity["configuration_id"]
        ):
            raise ValueError("queued_task_worker_identity_conflict")

    def stopped(self):
        try:
            previous = _load(self.qualification_evidence_file, self.evidence_identity)
            if previous is not None and previous["failures"]:
                self._stop_new.set()
        except Exception:
            return True  # Changed or unavailable proof cannot admit another job.
        if self._worker_id is not None:
            try:
                # SQL history closes the crash window between the atomic
                # quarantine and the evidence-file write. Even an old ready
                # initializer must not erase a failed runtime's evidence.
                with self.repo.engine.connect() as connection:
                    failed = connection.execute(
                        select(attempts.c.id)
                        .join(jobs, attempts.c.job_id == jobs.c.id)
                        .where(
                            attempts.c.worker_id == self._worker_id,
                            or_(
                                attempts.c.status == "failed",
                                and_(
                                    attempts.c.status == "deferred",
                                    attempts.c.error_code.in_(
                                        (
                                            "worker_preparation_not_ready",
                                            "worker_preparation_failed",
                                        )
                                    ),
                                ),
                                and_(
                                    attempts.c.collection_failures > 0,
                                    jobs.c.status.not_in(
                                        ("cancel_requested", "cancelled")
                                    ),
                                ),
                            ),
                        )
                        .limit(1)
                    ).first()
                if (
                    failed is not None
                    or self.control.get(self._worker_id)["drain_requested"]
                ):
                    self._stop_new.set()
            except Exception:
                return True
        return super().stopped()

    def _save_evidence(self, value):
        path = _path(self.qualification_evidence_file)
        encoded = json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if len(encoded) > _MAX_EVIDENCE_BYTES:
            raise ValueError("queued_task_evidence_too_large")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        _path(temporary)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    def _observe(self, worker_id, job_id):
        worker = self.control.observe(worker_id, job_id, quarantine_failures=True)
        try:
            self._record_evidence(worker, job_id)
        except Exception:
            # A verified result remains downloadable even if control evidence
            # cannot be persisted. Do not authorize another job in that case.
            self.control.drain(worker_id)
            self._stop_new.set()
        return worker

    def _run_once(self, worker_id, pool):
        # SQL completion precedes observation/evidence publication. Recover a
        # crash in that interval using the original, stopped attempt; terminal
        # jobs are intentionally not claimable by the ordinary queue anymore.
        # This runs inside WorkerRunner's physical slot lock, never an extra
        # submit or collection. Unknown/manual status labels retain ownership.
        worker = self.control.get(worker_id)
        if worker["current_job_id"] is not None:
            with self.repo.engine.connect() as connection:
                row = (
                    connection.execute(
                        select(jobs).where(jobs.c.id == worker["current_job_id"])
                    )
                    .mappings()
                    .first()
                )
                job = dict(row) if row is not None else None
                attempt = (
                    connection.execute(
                        select(attempts).where(
                            attempts.c.id == job["current_attempt_id"],
                            attempts.c.job_id == job["id"],
                            attempts.c.worker_id == worker_id,
                        )
                    )
                    .mappings()
                    .first()
                    if job
                    else None
                )
            if job is not None and attempt is not None:
                unsubmitted = (
                    attempt["submission_started_at"] is None
                    and attempt["upstream_task_id"] is None
                )
                completed = (
                    job["status"] in {"succeeded", "failed", "cancelled"}
                    and attempt["status"] == job["status"]
                    and (
                        attempt["upstream_stopped"] == 1
                        or job["status"] == "cancelled"
                        and unsubmitted
                        and attempt["actual_cost_microusd"] == 0
                    )
                )
                deferred = (
                    job["status"] == "queued"
                    and attempt["status"] == "deferred"
                    and unsubmitted
                )
                if completed or deferred:
                    # WorkerRunner performs its single observation before
                    # releasing this slot lock, just as for a normal turn.
                    return self._summary(job)
        return super()._run_once(worker_id, pool)

    def _record_evidence(self, worker, job_id):
        spec = worker["spec"]
        identity = self.evidence_identity
        self._check_worker(worker)
        with self.repo.engine.connect() as connection:
            job = dict(
                connection.execute(select(jobs).where(jobs.c.id == job_id))
                .mappings()
                .one()
            )
            attempt = (
                connection.execute(
                    select(attempts).where(
                        attempts.c.id == job["current_attempt_id"],
                        attempts.c.job_id == job_id,
                        attempts.c.worker_id == worker["id"],
                    )
                )
                .mappings()
                .first()
            )
            if (
                attempt is None
                or not self.control.matches(worker, job)
                or attempt["number"] != job["attempt_no"]
            ):
                raise ValueError("queued_task_attempt_identity_conflict")
            output_rows = (
                list(
                    connection.execute(
                        select(artifacts).where(
                            artifacts.c.job_id == job_id,
                            artifacts.c.attempt_id == attempt["id"],
                        )
                    ).mappings()
                )
                if job["status"] == "succeeded"
                else []
            )
        failure = (
            job["status"] == "failed"
            or job["status"] == "queued"
            and job.get("error_code")
            in {"worker_preparation_not_ready", "worker_preparation_failed"}
            or job["status"] == "collecting"
            and job.get("error_code") == "collection_failed"
        )
        if job["status"] != "succeeded" and not (worker["drain_requested"] and failure):
            return  # Running, uncertain and cancelled are never success evidence.
        value = _load(self.qualification_evidence_file, identity) or {
            "version": 1,
            "qualification_profile": QUEUED_TASK_PROFILE,
            "identity": identity,
            "worker_id": worker["id"],
            "instance_id": worker["instance_id"],
            "configuration_id": spec["configuration_id"],
            "model_id": spec["model_id"],
            "verified": False,
            "evidence": [],
            "failures": [],
        }
        if value["worker_id"] != worker["id"] or value["model_id"] != spec["model_id"]:
            raise ValueError("queued_task_worker_evidence_conflict")
        if job["status"] == "succeeded":
            if (
                attempt["status"] != "succeeded"
                or attempt["upstream_stopped"] != 1
                or not attempt["upstream_task_id"]
                or not output_rows
            ):
                raise ValueError("queued_task_completion_not_verified")
            outputs = []
            for row in output_rows:
                metadata = row["metadata"]
                if metadata.get("validated") is not True or metadata.get(
                    "kind"
                ) not in {"video", "audio"}:
                    raise ValueError("queued_task_outputs_not_verified")
                outputs.append(
                    {
                        "artifact_id": row["id"],
                        **{
                            key: metadata[key]
                            for key in _OUTPUT_FIELDS - {"artifact_id"}
                            if key in metadata
                        },
                    }
                )
            request = job["request"].get("request", job["request"])
            counts = {"image": 0, "video": 0, "audio": 0}
            for snapshot in job["request"].get("assets", {}).values():
                kind = (
                    snapshot.get("metadata", {}).get("kind")
                    if isinstance(snapshot, dict)
                    else None
                )
                if kind in counts:
                    counts[kind] += 1
            scope = {
                "job_id": job_id,
                "attempt_id": attempt["id"],
                "request_hash": job["request_hash"],
                "recipe_id": job["request"]["recipe_id"],
                "controls": _controls(request),
                "reference_kind_counts": counts,
                "outputs": outputs,
                "completed_at": job["updated_at"],
            }
            existing_attempt = next(
                (
                    item
                    for item in value["evidence"]
                    if item["attempt_id"] == attempt["id"]
                ),
                None,
            )
            if existing_attempt is not None and existing_attempt != scope:
                raise ValueError("queued_task_success_evidence_conflict")
            # The job ledger is the result history. This bounded control
            # receipt keeps only the first successful concrete scope per
            # recipe, without claiming that other controls were validated.
            existing_recipe = next(
                (
                    item
                    for item in value["evidence"]
                    if item["recipe_id"] == scope["recipe_id"]
                ),
                None,
            )
            if existing_recipe is None:
                value["evidence"].append(scope)
            elif existing_attempt is None:
                return  # Deliver later results without growing the proof file.
            value["verified"] = True
        else:
            failure = {
                "job_id": job_id,
                "attempt_id": attempt["id"],
                "status": job["status"],
                "error_code": _id(job["error_code"] or "queued_task_execution_failed"),
                "observed_at": job["updated_at"],
            }
            if not any(
                all(
                    item[field] == failure[field]
                    for field in ("job_id", "attempt_id", "status", "error_code")
                )
                for item in value["failures"]
            ):
                value["failures"].append(failure)
        self._save_evidence(value)
        # The reader's strict schema is also the publication gate. Output media
        # and user prompts/asset keys never enter this control-state receipt.
        _load(self.qualification_evidence_file, identity)

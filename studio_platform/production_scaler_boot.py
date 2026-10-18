"""Finite scaler's boot/qualification and drain-safe worker child.

Same CPU container owns SSH tunnels and its fleet. This module has no provider
credential reader, rent path, budget initialization or automatic child restart.
"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

from sqlalchemy import insert, select, update

from .control import WorkerControl
from .fleet import FleetSupervisor, read_config as read_fleet, run_slot
from .lium_bootstrap import BootConfig, BootController, BootError, SSHHost
from .lium_provider import LiumManifest, InferenceIdleProof
from .lium_multimodal_smoke import FirstLastSmoke, BoundedReferenceSmoke
from .lium_reference_smoke import ReferenceSmoke
from .qualification_profiles import (
    MULTIMODAL_PROFILE,
    QUEUED_TASK_PROFILE,
    STAGE_RUNTIME_S,
)
from .repository import Repository, instance_intents, registered_workers, scaler_actions
from .worker import ComfyBackend, SubmissionRejected, _slot_lock


def _approved_boot_min_gpu_bytes(repo, finite, intent):
    """Select this intent's trusted manifest, never the cheapest/first candidate.

    The VRAM filter only permits boot qualification. It is not proof of a
    successful inference and does not alter any of the qualification stages.
    Historical unfiltered manifests retain BootConfig's 90-GiB default.
    """
    default = BootConfig.__dataclass_fields__["min_gpu_bytes"].default
    if all(manifest.get("minimum_vram_mib", 0) == 0 for manifest in finite.manifests):
        return default
    with repo.engine.connect() as conn:
        launch = conn.execute(
            select(scaler_actions.c.launch_spec).where(
                scaler_actions.c.intent_id == intent["id"],
                scaler_actions.c.pool == finite.pool,
            )
        ).scalar_one_or_none()
    matching = [
        index for index, approved in enumerate(finite.launches) if approved == launch
    ]
    if len(matching) != 1 or matching[0] >= len(finite.manifests):
        raise BootError("finite_boot_manifest_identity_unconfirmed")
    manifest = LiumManifest(**finite.manifests[matching[0]])
    if (
        manifest.configuration_id != finite.configuration_id
        or manifest.model_id != launch.get("model_id")
        or manifest.executor_id != launch.get("offer_id")
        or manifest.template_id != launch.get("image_id")
        or manifest.region != launch.get("region", "")
    ):
        raise BootError("finite_boot_manifest_identity_unconfirmed")
    return manifest.minimum_vram_mib * 1024**2 if manifest.minimum_vram_mib else default


class ProductionBoot(BootController):
    def __init__(
        self,
        repo,
        provider,
        finite,
        intent,
        local_port,
        *,
        config_path=None,
        ssh_factory=None,
        backend_factory=ComfyBackend,
        verify_smoke=None,
        popen=None,
    ):
        self.finite, self.operator_path = (
            finite,
            Path(config_path) if config_path else None,
        )
        self.intent_id = intent["id"]
        self._popen_impl = popen or subprocess.Popen
        self._stopping = False
        if ssh_factory is None:
            if finite.execution_backend == "wangp-worker":
                from .wangp_bootstrap import WanGPSSHHost

                ssh_factory = WanGPSSHHost
            else:
                ssh_factory = SSHHost
        config = BootConfig(
            finite.work_dir / "boot",
            finite.source_dir,
            finite.ssh_key_file,
            finite.known_hosts_file,
            local_port,
            finite.configuration_id,
            enabled=True,
            smoke_enabled=finite.qualification_profile != QUEUED_TASK_PROFILE,
            fleet_enabled=True,
            qualification_profile=QUEUED_TASK_PROFILE
            if finite.qualification_profile == QUEUED_TASK_PROFILE
            else "",
            trust_first_host_key=finite.trust_first_host_key,
            minimum_remaining_s=finite.drain_margin_s,
            recipe_ids=finite.recipe_ids,
            execution_backend=finite.execution_backend,
            engine_manifest_digest=finite.engine_manifest_digest,
            output_delivery=finite.output_delivery,
            min_gpu_bytes=_approved_boot_min_gpu_bytes(repo, finite, intent),
        )
        super().__init__(
            repo,
            provider,
            config,
            ssh_factory=ssh_factory,
            backend_factory=backend_factory,
            verify_smoke=verify_smoke,
            fleet_factory=self._fleet,
        )
        preparation_receipt = config.work_dir / self.intent_id / "bootstrap-state.json"
        self._member_preparation_owner = (
            not preparation_receipt.exists() and not preparation_receipt.is_symlink()
        )

    def _fleet(self, config, repo, path):
        return FleetSupervisor(config, repo, path, popen=self._popen)

    def _popen(self, argv, **kwargs):
        fleet_file = self.config.work_dir / self.intent_id / "fleet.json"
        expected = [
            sys.executable,
            "-m",
            "studio_platform.fleet",
            "--config",
            str(fleet_file),
            "--slot",
            "lium-" + self.intent_id.replace("-", ""),
            "--config-hash",
            self.fleet.config.fingerprint(),
        ]
        if argv != expected or self.operator_path is None:
            raise BootError("finite_unexpected_child_command")
        # stdin must never inherit the one-shot provider credential pipe.
        kwargs["stdin"] = subprocess.DEVNULL
        return self._popen_impl(
            [
                sys.executable,
                "-m",
                "studio_platform.production_scaler",
                "--config",
                str(self.operator_path),
                "--enabled",
                "--slot",
                self.intent_id,
                "--config-hash",
                self.fleet.config.fingerprint(),
            ],
            **kwargs,
        )

    def request_drain(self):
        self._stopping = True
        self.cancel_preparation()
        if self.fleet:
            self.fleet.drain()

    def _lifetime(self, intent):
        path = self.finite.work_dir / ("lifetime-" + intent["id"] + ".json")
        if path.exists():
            value = json.loads(path.read_text())
        else:
            value = self.provider.lifetime(
                intent["id"],
                intent["provider_instance_id"],
                local_created_at=intent["created_at"],
                maximum_hours=4,
            )
            from .production_scaler import save

            save(path, value)
        if value.get("instance_id") != intent["provider_instance_id"] or type(
            value.get("safe_deadline")
        ) not in (int, float):
            raise BootError("finite_lifetime_identity_unconfirmed")
        with self.repo.transaction() as conn:
            row = self.repo._locked(
                conn,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(hard_deadline=min(row["hard_deadline"], value["safe_deadline"]))
            )
        return min(intent["hard_deadline"], value["safe_deadline"])

    def _collection_backend(self, intent, state):
        """Reconnect only an existing pinned runtime; never relaunch its setup."""
        if self.backend is not None:
            return
        files, manifest = self._sources()
        expected = self._identity(intent, files)
        if (
            state.get("identity") != expected
            or state.get("local_port") != self.config.local_port
        ):
            raise BootError("finite_collection_identity_conflict")
        self.bound_intent, self.bound_instance = (
            intent["id"],
            intent["provider_instance_id"],
        )
        if self.host is None:
            coordinates = self.provider.ssh_connection(
                intent["id"], intent["provider_instance_id"]
            )
            self.host = self.ssh_factory(self.config, coordinates)
        report = self.host.report()
        if report.get("identity") != expected or report.get("state") != "ready":
            raise BootError("finite_collection_runtime_unconfirmed")
        self._validate_report(report, manifest)
        self._connect_backend(intent, self.config.work_dir / intent['id'], state)

    def tick(self, intent_id, *, stopping=False):
        from .production_scaler import verify_sources

        verify_sources(self.finite)
        with self.repo.engine.connect() as conn:
            intent = dict(
                conn.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
        if intent_id != self.intent_id:
            raise BootError("finite_boot_identity_mismatch")
        deadline = self._lifetime(intent)
        from .runtime_adoption import prepare_adoption

        adoption = (
            prepare_adoption(self, {**intent, "hard_deadline": deadline})
            if self.config.execution_backend == 'comfy-worker'
            else None
        )
        if adoption is not None:
            return adoption
        self._stopping = (
            self._stopping
            or stopping
            or intent["state"] in ("draining", "destroying", "destroyed")
            or self.repo.clock() >= deadline - self.finite.drain_margin_s
        )
        if self._stopping:
            self.request_drain()
            if self.preparation_pending():
                return {
                    "state": "draining",
                    "children_done": False,
                    "preparation": self.preparation_status(),
                }
            # A real queued task may still be executing or collecting after
            # admission closes. Keep its original tunnel reachable even though
            # this branch deliberately never enters boot/start again.
            if self.host is not None and hasattr(self.host, 'ensure_connected'):
                self.host.ensure_connected()
            # Preserve pending boot qualification collection too. No new
            # qualification POST is permitted once draining begins.
            receipt = self.config.work_dir / intent_id / "bootstrap-state.json"
            if receipt.exists():
                state = json.loads(receipt.read_text())
                self.record_preparation_stop(receipt, state)
                if state.get("smoke_submission_started") and state.get("phase") not in (
                    "qualified",
                    "qualification_failed",
                    "fleet_starting",
                    "fleet_started",
                ):
                    self._collection_backend(intent, state)
                    result = self._smoke(receipt.parent, receipt, state)
                    if result["state"] != "qualified":
                        return result
                for helper in self._multimodal_helpers():
                    extra_receipt = receipt.parent / helper.name / "state.json"
                    if not extra_receipt.exists():
                        continue
                    extra = json.loads(extra_receipt.read_text())
                    if extra.get("submission_started") and extra.get("phase") not in (
                        "qualified",
                        "failed",
                    ):
                        self._collection_backend(intent, state)
                        helper.backend = self.backend
                        result = helper.tick(receipt.parent, state)
                        if result["state"] not in ("qualified", "qualification_failed"):
                            return result
            if self.fleet:
                self.fleet.tick()
                self._retire_idle_children()
            return {"state": "draining", "children_done": self.children_done()}
        result = super().tick(intent_id)
        # The base boot module remains compatible with its historical 4-step
        # smoke; only this subclass supplies the stronger immutable request.
        if self.finite.qualification_profile == QUEUED_TASK_PROFILE:
            # Runtime readiness is not an inference receipt. The actual queue
            # runner records success only after validating/storing real output.
            result["qualification_scope"] = "runtime_ready_awaiting_real_task"
            receipt = self.config.work_dir / intent_id / "bootstrap-state.json"
            if receipt.exists() and result.get("state") in {
                "runtime_ready",
                "fleet_running",
                "fleet_attention_required",
            }:
                try:
                    state = json.loads(receipt.read_text(encoding="utf-8"))
                    from .queued_task_runner import read_verification_summary

                    summary = read_verification_summary(
                        receipt.parent / "queued-task-evidence.json",
                        expected_identity={
                            **state["identity"],
                            "qualification_profile": QUEUED_TASK_PROFILE,
                        },
                    )
                    expected_worker = "lium-" + intent_id.replace("-", "")
                    if (
                        summary.get("worker_id", expected_worker) != expected_worker
                        or summary.get("model_id", self.config.model_id)
                        != self.config.model_id
                    ):
                        raise ValueError("finite_real_task_evidence_identity_conflict")
                except (OSError, ValueError, TypeError, KeyError):
                    # Losing a proof does not prove that the upstream task
                    # stopped. Quarantine admissions, retain the real task and
                    # let the existing drain/idle/reconciliation path decide.
                    self.request_drain()
                    return {
                        **result,
                        "state": "fleet_attention_required",
                        "generation_verified": False,
                        "runtime_quarantined": True,
                        "verification_evidence_unconfirmed": True,
                        "error_code": "finite_real_task_evidence_unconfirmed",
                    }
                result.update(summary)
                result["awaiting_real_task"] = not summary["generation_verified"]
                if summary["generation_verified"]:
                    result["qualification_scope"] = (
                        "single_host_completed_queued_jobs_only"
                    )
                if summary.get("runtime_quarantined"):
                    self.request_drain()
                    result["state"] = "fleet_attention_required"
        elif result.get("generation_verified"):
            result["qualification_scope"] = (
                "single_host_fl50_firstlast4_ref4_2048_inputs_compatibility_not_ref50_quality"
                if self.finite.qualification_profile == MULTIMODAL_PROFILE
                else "single_host_fl2va_5s_768p_50steps_audio_not_all_controls"
            )
        return result

    def _collection_context(self):
        lock = self.finite.work_dir / "collection-lock"
        lock.mkdir(exist_ok=True)
        return _slot_lock(lock, "production-cpu-collection-v1")

    def _stage_admission(self, stage):
        root = self.finite.work_dir
        flags = [root / "drain.flag", root / "rollover.flag"]
        if root.parent.name == "cycles":
            flags.append(root.parent.parent / "drain.flag")
        if self._stopping or any(path.exists() for path in flags):
            self.request_drain()
            return "qualification_not_started_draining"
        remaining = STAGE_RUNTIME_S[stage]
        if self.finite.qualification_profile == MULTIMODAL_PROFILE:
            if stage == "fl50":
                remaining += STAGE_RUNTIME_S["firstlast4"] + STAGE_RUNTIME_S["ref4"]
            elif stage == "firstlast4":
                remaining += STAGE_RUNTIME_S["ref4"]
        with self.repo.engine.connect() as conn:
            deadline = conn.execute(
                select(instance_intents.c.hard_deadline).where(
                    instance_intents.c.id == self.intent_id
                )
            ).scalar_one()
        if self.repo.clock() + remaining + self.finite.collection_margin_s >= min(
            deadline, self.finite.hard_deadline
        ):
            self.request_drain()
            return "qualification_deadline_insufficient"
        return None

    def _multimodal_helpers(self):
        if self.finite.qualification_profile != MULTIMODAL_PROFILE:
            return ()
        return (
            FirstLastSmoke(
                self.backend,
                self.repo.clock,
                self._save,
                self.verify_smoke,
                can_submit=lambda: self._stage_admission("firstlast4"),
                collection_context=self._collection_context,
            ),
            BoundedReferenceSmoke(
                self.backend,
                self.repo.clock,
                self._save,
                self.verify_smoke,
                can_submit=lambda: self._stage_admission("ref4"),
                collection_context=self._collection_context,
            ),
        )

    def _additional_qualification(self, directory, state):
        helpers = self._multimodal_helpers()
        if not helpers:
            return super()._additional_qualification(directory, state)
        receipts = []
        for helper in helpers:
            receipt = directory / helper.name / "state.json"
            if state["phase"] in ("fleet_starting", "fleet_started"):
                if (
                    state.get("fleet_recipe_ids") != list(self.config.recipe_ids)
                    or not receipt.exists()
                    or json.loads(receipt.read_text()).get("phase") != "qualified"
                ):
                    raise BootError(
                        "finite_multimodal_evidence_missing_requires_reconciliation"
                    )
            # Both helper implementations bind request/profile/bootstrap identity
            # even for existing receipts. No old small smoke can qualify this.
            if isinstance(helper, BoundedReferenceSmoke):
                result = ReferenceSmoke(
                    self.backend,
                    self.repo.clock,
                    self._save,
                    self.verify_smoke,
                    profile=helper.name,
                    can_submit=helper.can_submit,
                    collection_context=self._collection_context,
                ).tick(directory, state)
            else:
                result = helper.tick(directory, state)
            if result["state"] != "qualified":
                return result
            receipts.append(result["evidence"])
        return {"state": "qualified", "multimodal_evidence": receipts}

    def _smoke(self, directory, receipt, state):
        from comfy_workflow import build_workflow

        request = {
            "mode": "fl",
            "prompt": "A red ceramic teapot on a wooden table, slow cinematic camera move, gentle ambient sound.",
            "duration": 5,
            "resolution": "768P",
            "aspect_ratio": "16:9",
            "steps": 50,
            "seed": "12345",
            "generate_audio": True,
            "video_decode": "tiled",
            "encoder_device": "cpu",
            "_job_id": state["tag"],
        }
        from .repository import request_hash

        digest = request_hash(request)
        if state.get("smoke_request_hash") not in (None, digest):
            raise BootError("finite_qualification_request_changed")
        if state["phase"] in ("qualified", "fleet_starting", "fleet_started"):
            if (
                state.get("smoke_request_hash") != digest
                or state.get("evidence", {}).get("scope")
                != "single_host_fl2va_5s_768p_50steps_audio_not_all_controls"
            ):
                raise BootError("finite_full_qualification_evidence_required")
            return {
                "state": "qualified",
                "generation_verified": True,
                "evidence": state["evidence"],
            }
        if state["phase"] == "qualification_failed":
            return {"state": "qualification_failed"}
        if not state.get("smoke_submission_started"):
            blocked = self._stage_admission("fl50")
            if blocked:
                return {"state": blocked}
            # Qualification is itself real GPU work and does not pass through
            # WorkerControl's job deadline check. Twenty minutes is a bounded
            # conservative allowance for this exact 50-step envelope, not an
            # inference SLA; keep the configured collection margin in addition.
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {"state": "qualification_upstream_busy"}
            graph = build_workflow(request, {}, {})
            blocked = self._stage_admission("fl50")
            if blocked:
                return {"state": blocked}
            state.update(
                smoke_request_hash=digest,
                smoke_submission_started=self.repo.clock(),
                phase="smoke_submitting",
            )
            self._save(receipt, state)
            try:
                state["smoke_task_id"] = self.backend.submit(graph, state["tag"])
                state["phase"] = "smoke_running"
                self._save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "qualification_failed"
                self._save(receipt, state)
                return {"state": "qualification_failed"}
            except Exception:
                return {"state": "smoke_submission_unknown"}
        task = state.get("smoke_task_id")
        outcome = (
            self.backend.poll(state["tag"], task)
            if task
            else self.backend.reconcile(state["tag"])
        )
        if outcome.task_id and not task:
            state["smoke_task_id"] = task = outcome.task_id
            self._save(receipt, state)
        if outcome.state in ("failed", "cancelled"):
            state["phase"] = "qualification_failed"
            self._save(receipt, state)
            return {"state": "qualification_failed"}
        if outcome.state != "succeeded" or not task:
            return {
                "state": "smoke_running"
                if outcome.state == "running"
                else "smoke_submission_unknown"
            }
        lock = self.finite.work_dir / "collection-lock"
        lock.mkdir(exist_ok=True)
        with _slot_lock(lock, "production-cpu-collection-v1") as acquired:
            if not acquired:
                return {"state": "qualification_collection_waiting"}
            from .media import MediaError, MediaBusy
            from .worker import BackendError

            try:
                paths = self.backend.fetch(
                    {"request": {"request": request}},
                    state["tag"],
                    task,
                    directory,
                    lambda: None,
                )
            except BackendError as error:
                if str(error) != "comfy_save_outputs_missing":
                    raise
                state.update(
                    phase="qualification_failed",
                    failure="completed_inference_outputs_missing",
                )
                self._save(receipt, state)
                return {"state": "qualification_failed"}
            try:
                evidence = self.verify_smoke(paths, request)
            except MediaBusy:
                return {"state": "qualification_collection_waiting"}
            except (BootError, BackendError, MediaError):
                state.update(
                    phase="qualification_failed",
                    failure="collected_output_validation_failed",
                )
                self._save(receipt, state)
                return {"state": "qualification_failed"}
        evidence.update(
            scope="single_host_fl2va_5s_768p_50steps_audio_not_all_controls",
            elapsed_wall_seconds=self.repo.clock() - state["smoke_submission_started"],
            completed_at=self.repo.clock(),
        )
        state.update(phase="qualified", evidence=evidence)
        self._save(receipt, state)
        return {"state": "qualified", "generation_verified": True, "evidence": evidence}

    def idle_probe(self, tag, instance_id):
        receipt = self.config.work_dir / tag / "bootstrap-state.json"
        if receipt.exists():
            state = json.loads(receipt.read_text())
            if state.get("smoke_submission_started") and state.get("phase") not in (
                "qualified",
                "qualification_failed",
                "fleet_starting",
                "fleet_started",
            ):
                raise BootError("finite_qualification_result_still_unresolved")
            for helper in self._multimodal_helpers():
                extra_receipt = receipt.parent / helper.name / "state.json"
                if extra_receipt.exists():
                    extra = json.loads(extra_receipt.read_text())
                    if extra.get("submission_started") and extra.get("phase") not in (
                        "qualified",
                        "failed",
                    ):
                        raise BootError("finite_qualification_result_still_unresolved")
            # A preparation failure has no Comfy backend to query. Require a
            # fresh, identity-bound process/socket observation instead of
            # treating the missing endpoint or a failed status as idle proof.
            prestart = state.get("phase") in {"staging_failed", "staging_cancelled"}
            if self.backend is None and (
                state.get("phase") == "bootstrap_failed" or prestart
            ):
                if (
                    tag != self.intent_id
                    or self.fleet is not None
                    or state.get("smoke_submission_started")
                    or any(
                        (receipt.parent / helper.name / "state.json").exists()
                        for helper in self._multimodal_helpers()
                    )
                ):
                    raise BootError("finite_preparation_idle_unconfirmed")
                if prestart and (
                    self.config.execution_backend != "wangp-worker"
                    or self.preparation_stopped_before_start(state) is None
                    or (receipt.parent / "fleet.json").exists()
                    or (receipt.parent / "fleet" / "fleet-state.json").exists()
                ):
                    raise BootError("finite_preparation_idle_unconfirmed")
                with self.repo.engine.connect() as conn:
                    intent = dict(
                        conn.execute(
                            select(instance_intents).where(instance_intents.c.id == tag)
                        )
                        .mappings()
                        .one()
                    )
                    if (
                        intent["provider_instance_id"] != instance_id
                        or conn.execute(
                            select(registered_workers.c.id).where(
                                registered_workers.c.instance_id == instance_id
                            )
                        ).first()
                    ):
                        raise BootError("finite_preparation_idle_unconfirmed")
                files, _ = self._sources()
                identity = self._identity(intent, files)
                if state.get("identity") != identity:
                    raise BootError("finite_preparation_idle_unconfirmed")
                if self.host is None:
                    coordinates = self.provider.ssh_connection(tag, instance_id)
                    self.host = self.ssh_factory(self.config, coordinates)
                report = (
                    self.host.preparation_idle_report(
                        expected_prestart_identity=identity
                    )
                    if prestart
                    else self.host.preparation_idle_report()
                )
                now = self.repo.clock()
                runtime_field = (
                    'runtime_process_count'
                    if self.config.execution_backend == 'wangp-worker'
                    else 'comfy_process_count'
                )
                listener_field = (
                    'runtime_port_listening'
                    if self.config.execution_backend == 'wangp-worker'
                    else 'comfy_port_listening'
                )
                idle = (
                    report.get("identity") == identity
                    and report.get("state") == ("not_started" if prestart else "failed")
                    and (not prestart or report.get("setup_markers_absent") is True)
                    and report.get("process_visibility_complete") is True
                    and type(report.get("bootstrap_process_count")) is int
                    and report["bootstrap_process_count"] == 0
                    and type(report.get(runtime_field)) is int
                    and report[runtime_field] == 0
                    and report.get(listener_field) is False
                )
                self.bound_intent, self.bound_instance = tag, instance_id
                self.idle_since = (
                    (self.idle_since if self.idle_since is not None else now)
                    if idle
                    else None
                )
                return InferenceIdleProof(
                    instance_id, now, self.idle_since or now, idle
                )
        return super().idle_probe(tag, instance_id)

    def _retire_idle_children(self):
        if not self.children_done() or self.backend is None:
            return
        proof = self.idle_probe(self.bound_intent, self.bound_instance)
        if not proof.idle:
            return
        for worker_id in self.fleet.children:
            control = WorkerControl(self.repo)
            worker = control.get(worker_id)
            if worker["state"] == "retired":
                continue
            if (
                worker["current_job_id"] is None
                and worker["expires_at"] > self.repo.clock()
                and worker["drain_requested"]
            ):
                control.retire(worker_id, upstream_idle_confirmed=True)

    def children_done(self):
        # A restarted controller has no Popen handles. That is not evidence that
        # an already launched CPU fleet exited, even after its GPU is destroyed.
        # Only the owning process can currently prove child exit; full fleet
        # reconstruction remains a separate recovery operation.
        try:
            if self.preparation_pending():
                return False
            if self.fleet is not None:
                expected = {
                    slot.spec.worker_id
                    for slot in self.fleet.config.slots
                    if slot.enabled
                }
                return (
                    bool(expected)
                    and set(self.fleet.children) == expected
                    and all(
                        proc.poll() is not None for proc in self.fleet.children.values()
                    )
                )
            directory = self.config.work_dir / self.intent_id
            if (directory / "fleet.json").exists() or (
                directory / "fleet" / "fleet-state.json"
            ).exists():
                return False
            receipt = directory / "bootstrap-state.json"
            if receipt.exists():
                with receipt.open("rb") as source:
                    raw = source.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    return False
                state = json.loads(raw)
                if (
                    not isinstance(state, dict)
                    or state.get("phase")
                    not in {
                        "reserved",
                        "staging",
                        "staged",
                        "staging_failed",
                        "staging_cancelled",
                        "bootstrap_starting",
                        "booting",
                        "bootstrap_failed",
                        "ready_for_qualification",
                        "runtime_ready",
                        "smoke_submitting",
                        "smoke_running",
                        "qualification_failed",
                        "qualified",
                    }
                    or "fleet_recipe_ids" in state
                    or not isinstance(state.get("identity"), dict)
                    or state["identity"].get("intent_id") != self.intent_id
                    or state["identity"].get("configuration_id")
                    != self.config.configuration_id
                ):
                    return False
            # Registration precedes Popen. Retain its obligation if launch
            # metadata is missing rather than treating absent files as proof.
            with self.repo.engine.connect() as conn:
                worker = conn.execute(
                    select(registered_workers.c.id).where(
                        registered_workers.c.id
                        == "lium-" + self.intent_id.replace("-", "")
                    )
                ).first()
            return worker is None
        except Exception:
            return False

    def close_if_safe(self, *, destroyed=False):
        if not destroyed or not self.children_done():
            return False
        replacement = (getattr(self.finite, "service_policy", None) or {}).get(
            "member_replacement"
        )
        proof = self._member_closure_proof() if replacement is not None else None
        if self.backend:
            self.backend.close()
            self.backend = None
        if self.host:
            self.host.close()
            self.host = None
        if proof is not None:
            # Only the process that owns these Popen/SSH handles can certify
            # closure. A restart cannot reconstruct it from missing handles.
            from .repository import canonical, scaler_receipts
            from .pool_member_generations import one_receipt
            import uuid

            with self.repo.transaction() as conn:
                self.repo._lock_capacity(conn)
                row = self.repo._locked(
                    conn,
                    select(instance_intents).where(
                        instance_intents.c.id == self.intent_id
                    ),
                )
                if (
                    row is None
                    or row["state"] != "destroyed"
                    or row["provider_instance_id"] != proof["instance_id"]
                ):
                    raise BootError("member_replacement_removal_unconfirmed")
                existing = one_receipt(conn, self.intent_id, "member_local_closed")
                if existing is None:
                    conn.execute(
                        insert(scaler_receipts).values(
                            id=str(uuid.uuid4()),
                            intent_id=self.intent_id,
                            operation="member_local_closed",
                            observed_at=self.repo.clock(),
                            facts=canonical(proof),
                        )
                    )
                elif existing != proof:
                    raise BootError("member_replacement_closure_conflict")
        return True

    def _member_closure_proof(self):
        """Bound the exact local ownership observation; never infer dead children."""
        from .pool_member_generations import one_receipt

        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == self.intent_id
                    )
                )
                .mappings()
                .one()
            )
            existing = one_receipt(conn, self.intent_id, "member_local_closed")
        expected = {
            "version": 1,
            "intent_id": self.intent_id,
            "instance_id": row["provider_instance_id"],
            "worker_id": "lium-" + self.intent_id.replace("-", ""),
            "config_hash": self.finite.fingerprint(),
            "sources": self.finite.source_sha256,
            "local_port": self.config.local_port,
        }
        if existing is not None:
            if any(existing.get(k) != v for k, v in expected.items()):
                raise BootError("member_replacement_closure_conflict")
            return existing
        if (
            row["state"] != "destroyed"
            or not row["provider_instance_id"]
            or not self.children_done()
        ):
            return None
        if self.host is None:
            # A reconstructed object cannot attest the previous controller's
            # transport closure merely because it has no local handle. The
            # never-bootstrap barrier is handled separately by the member gate.
            return None
        path = self.config.work_dir / self.intent_id / "bootstrap-state.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
            return None
        state = json.loads(path.read_text())
        files, _ = self._sources()
        if state.get("identity") != self._identity(row, files):
            return None
        if self.fleet is not None:
            if (
                state.get("phase") not in ("fleet_starting", "fleet_started")
                or state.get("local_port") != self.config.local_port
            ):
                return None
            expected_workers = {
                slot.spec.worker_id for slot in self.fleet.config.slots if slot.enabled
            }
            if (
                expected_workers != {expected["worker_id"]}
                or set(self.fleet.children) != expected_workers
            ):
                return None
            children = [
                {"worker_id": key, "pid": proc.pid, "exit_code": proc.poll()}
                for key, proc in sorted(self.fleet.children.items())
            ]
            if any(
                type(child["pid"]) is not int
                or child["pid"] <= 0
                or type(child["exit_code"]) is not int
                for child in children
            ):
                return None
            detail = {
                "kind": "owned_fleet_exited",
                "fleet_hash": self.fleet.config.fingerprint(),
                "children": children,
            }
        else:
            if (
                not self._member_preparation_owner
                or state.get("phase")
                not in (
                    "bootstrap_failed",
                    "staging_failed",
                    "staging_cancelled",
                    "qualification_failed",
                )
                or state.get("fleet_recipe_ids") is not None
            ):
                return None
            with self.repo.engine.connect() as conn:
                if conn.execute(
                    select(registered_workers.c.id).where(
                        registered_workers.c.id == expected["worker_id"]
                    )
                ).first():
                    return None
            detail = {
                "kind": "owned_preparation_never_registered",
                "phase": state["phase"],
            }
        # Caller publishes only AFTER close() returns for this exact backend,
        # tunnel and SSH client. Failed close leaves no usable certificate.
        return {
            **expected,
            **detail,
            "bootstrap_identity": state["identity"],
            "local_transport_closed": True,
        }

    def close(self):
        # Intentionally cannot use BootController.close's finite shutdown
        # timeout as proof that SSH may be closed.
        raise BootError("finite_close_requires_confirmed_destroyed_and_children_done")


def run_child(config, intent_id, expected_hash, settings):
    from .drain_safe_runner import DrainSafeRunner
    from .production_scaler import ScalerError, job_scope_filter, job_scope_allowed

    repo = Repository(settings.database_url)
    try:
        fleet_path = config.work_dir / "boot" / intent_id / "fleet.json"
        fleet = read_fleet(fleet_path)
        receipt = json.loads((fleet_path.parent / "bootstrap-state.json").read_text())
        identity = receipt.get("identity", {})
        if (
            not expected_hash
            or fleet.fingerprint() != expected_hash
            or identity.get("intent_id") != intent_id
            or identity.get("sources") != config.source_sha256
            or identity.get("configuration_id") != config.configuration_id
            or receipt.get("phase") not in ("fleet_starting", "fleet_started")
        ):
            raise ScalerError("finite_child_configuration_mismatch")
        queued_task = config.qualification_profile == QUEUED_TASK_PROFILE
        if queued_task:
            if (
                receipt.get("qualification_profile") != QUEUED_TASK_PROFILE
                or receipt.get("runtime_validation")
                != {
                    "profile": QUEUED_TASK_PROFILE,
                    "state": "runtime_ready",
                    "generation_verified": False,
                }
                or receipt.get("smoke_submission_started") is not None
                or receipt.get("smoke_task_id") is not None
                or receipt.get("evidence") is not None
            ):
                raise ScalerError("finite_child_runtime_validation_required")
        elif receipt.get("qualification_profile") == QUEUED_TASK_PROFILE:
            raise ScalerError("finite_child_qualification_profile_mismatch")
        worker_id = "lium-" + intent_id.replace("-", "")
        spec = fleet.slot(worker_id).spec
        if (
            len(fleet.slots) != 1
            or spec.pool != config.pool
            or spec.configuration_id != config.configuration_id
            or spec.backend != config.execution_backend
            or spec.engine_manifest_digest != config.engine_manifest_digest
            or identity.get('backend', 'comfy-worker') != config.execution_backend
            or identity.get('engine_manifest_digest', '')
            != config.engine_manifest_digest
            or spec.instance_id != identity.get("instance_id")
            or spec.model_id != config.launches[0]["model_id"]
            or tuple(spec.recipe_ids) != config.recipe_ids
            or receipt.get("fleet_recipe_ids") != list(config.recipe_ids)
        ):
            raise ScalerError("finite_child_identity_mismatch")
        with repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
        deadline = min(config.hard_deadline, row["hard_deadline"])

        def stop_new():
            return (
                config.work_dir / "drain.flag"
            ).exists() or repo.clock() >= deadline - config.drain_margin_s

        def job_allowed(job):
            # Called under the worker/queue transaction; no separate DB read.
            duration = job.get("expected_runtime_s")
            return (
                job_scope_allowed(config, job)
                and job["execution_plan"].get("policy_hash")
                == config.execution_policy_sha256
                and type(duration) in (float, int)
                and duration > 0
                and repo.clock() + duration + config.collection_margin_s < deadline
            )

        predicate = job_scope_filter(config)
        if queued_task:
            from .queued_task_runner import QueuedTaskRunner, read_verification_summary

            runner_type = QueuedTaskRunner
            queued_kwargs = {
                "qualification_evidence_file": fleet_path.parent
                / "queued-task-evidence.json",
                "evidence_identity": {
                    **identity,
                    "qualification_profile": QUEUED_TASK_PROFILE,
                },
            }
            summary = read_verification_summary(
                queued_kwargs["qualification_evidence_file"],
                expected_identity=queued_kwargs["evidence_identity"],
            )
            if (
                summary.get("worker_id", worker_id) != worker_id
                or summary.get("model_id", spec.model_id) != spec.model_id
            ):
                raise ScalerError("finite_child_real_task_evidence_identity_conflict")
            with repo.engine.connect() as conn:
                worker = (
                    conn.execute(
                        select(registered_workers).where(
                            registered_workers.c.id == worker_id
                        )
                    )
                    .mappings()
                    .first()
                )
            if (
                summary.get("runtime_quarantined")
                or worker is not None
                and worker["drain_requested"]
            ):
                if worker is not None:
                    WorkerControl(repo).drain(worker_id)
                if worker is None or worker["current_job_id"] is None:
                    # mark_ready normally clears a drain. An exited process or
                    # a restart must never revive a quarantined runtime.
                    return 0
        else:
            runner_type, queued_kwargs = DrainSafeRunner, {}
        return (
            run_slot(
                fleet,
                worker_id,
                settings,
                repository=repo,
                runner_factory=lambda *a, **kw: runner_type(
                    *a,
                    stop_new=stop_new,
                    job_allowed=job_allowed,
                    job_filter=predicate,
                    collection_lock_dir=config.work_dir / "collection-lock",
                    **queued_kwargs,
                    **kw,
                ),
            )
            or 0
        )
    finally:
        repo.close()

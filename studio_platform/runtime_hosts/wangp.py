"""One-slot private host around an explicitly injected headless Session facade.

No WanGP imports, listener, model loading, background polling or automatic retry.
The caller supplies a protected output directory and a pinned runtime manifest.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Callable
import uuid

from ..inference.protocol import BackendError, NotReady, safe_failure_code
from ..inference.wangp_contract import (
    ArtifactDescriptor, EngineManifest, HostReadiness, PreparedRequest,
    RuntimeObservation, RuntimeOutput, TERMINAL, canonical_json, _object,
    safe_model_progress,
)
from .wangp_receipts import ReceiptJournal, checked_directory, checked_reader, sync_directory


class WanGPHost:
    def __init__(self, *, session, journal: ReceiptJournal, manifest: EngineManifest,
                 output_root: Path, sealed_root: Path, settings_resolver: Callable | None = None,
                 max_artifact_bytes=512 * 1024 * 1024, max_transfer_seconds=180,
                 fault_hook: Callable[[str], None] | None = None):
        if (manifest.digest != journal.manifest_digest or type(max_artifact_bytes) is not int
                or max_artifact_bytes <= 0 or not 0 < max_transfer_seconds < float("inf")):
            raise ValueError("wangp_invalid_host_configuration")
        if manifest.document.get('deployment_profile_id') is not None:
            from ..runtime_catalog import validate_manifest
            validate_manifest(manifest)
        self.session, self.journal, self.manifest = session, journal, manifest
        self.output_root = checked_directory(output_root)
        self.sealed_root = checked_directory(sealed_root, create=True)
        if (self.output_root.is_relative_to(self.sealed_root)
                or self.sealed_root.is_relative_to(self.output_root)):
            raise ValueError("wangp_sealed_root_must_be_separate")
        self.settings_resolver = settings_resolver
        self.max_bytes, self.max_seconds = max_artifact_bytes, max_transfer_seconds
        self.incarnation = uuid.uuid4().hex
        self._handles = {}
        self._lock = threading.RLock()
        self._closed = False
        self._fault_hook = fault_hook
        journal.acquire_host()
        try:
            journal.recover(self.incarnation)
        except BaseException:
            journal.release_host()
            raise

    def _fault(self, point):
        if self._fault_hook:
            self._fault_hook(point)

    def _open(self):
        if self._closed:
            raise NotReady("wangp_host_closed")

    def close(self):
        """Release ownership only; never cancel a job or erase its receipt.

        A replacement incarnation will quarantine nonterminal receipts. Session
        lifetime belongs to the process supervisor, not this method.
        """
        with self._lock:
            if not self._closed:
                self._closed = True
                self.journal.release_host()

    def readiness(self):
        with self._lock:
            self._open()
            try:
                pending = self.journal.has_obligations()
                idle = not pending and self.session.is_idle() is True
            except Exception:
                idle, pending = False, True
            preparation = None
            try:
                probe = getattr(self.session, "preparation", None)
                source = probe() if callable(probe) else None
                model = safe_model_progress(source.get("model")) if type(source) is dict else None
                if model is not None:
                    preparation = {"model": model}
            except Exception:
                pass  # Optional telemetry cannot alter slot/obligation checks.
            return HostReadiness(self.manifest.digest, self.journal.slot_key, self.incarnation,
                                 idle, "" if idle else "wangp_slot_obligation_or_runtime_busy", preparation)

    def submit(self, prepared: PreparedRequest):
        if not isinstance(prepared, PreparedRequest):
            raise BackendError("wangp_invalid_prepared_request")
        with self._lock:
            self._open()
            existing = self.journal.get(prepared.operation_id)
            if existing is not None:
                # Even a conflicting replay must preserve evidence of a prior call.
                return self.journal.claim(prepared, self.incarnation)[0]
            if prepared.manifest_digest != self.manifest.digest:
                raise NotReady("wangp_manifest_mismatch")
            try:
                idle = self.session.is_idle() is True
            except Exception:
                idle = False
            if not idle:
                raise NotReady("wangp_runtime_not_idle")
            # Resolver is trusted code from D2. It must bind staged handles without
            # changing controls; no arbitrary paths or runtime defaults are exposed.
            if prepared.inputs and self.settings_resolver is None:
                raise BackendError("wangp_input_resolver_required")
            try:
                if self.manifest.document.get('deployment_profile_id') is not None:
                    from ..inference.wangp_profile_compiler import validate_prepared
                    validate_prepared(prepared, self.manifest)
                settings = (self.settings_resolver(prepared) if self.settings_resolver
                            else prepared.settings)
                settings = _object(canonical_json(settings))
            except Exception:
                raise BackendError("wangp_settings_resolution_failed") from None
            receipt, fresh = self.journal.claim(prepared, self.incarnation)
            if not fresh:
                return receipt
            self._fault("after_prepared")
            receipt = self.journal.get(prepared.operation_id)
            if receipt.cancel_requested:
                return self.journal.transition(prepared.operation_id, expected={"prepared"},
                    state="cancelled", stop_proven=True, reason="wangp_cancelled_before_dispatch")
            receipt = self.journal.transition(prepared.operation_id, expected={"prepared"},
                                              state="dispatch_intent")
            if receipt.state != "dispatch_intent" or receipt.hold_reason:
                return receipt
            # Journal transaction is committed and closed before the external call.
            self._fault("after_intent")
            try:
                handle = self.session.submit_task(settings)
                if not callable(getattr(handle, "observe", None)) or not callable(getattr(handle, "cancel", None)):
                    raise ValueError
            except Exception:
                return self.journal.transition(prepared.operation_id, expected={"dispatch_intent"},
                    state="unknown", reason="wangp_dispatch_unknown")
            self._handles[prepared.operation_id] = handle
            self._fault("after_handle")
            return self.journal.transition(prepared.operation_id, expected={"dispatch_intent"}, state="running")

    def inspect(self, operation_id):
        with self._lock:
            self._open()
            receipt = self.journal.get(operation_id)
            if receipt is None or receipt.state in TERMINAL or receipt.hold_reason:
                return receipt
            handle = self._handles.get(operation_id)
            if handle is None:
                return receipt
            try:
                observation = handle.observe()
                if not isinstance(observation, RuntimeObservation):
                    raise ValueError
                if observation.state == "running" and observation.stopped is False:
                    return self.journal.transition(operation_id, expected={"running", "unknown"}, state="running")
                if observation.state not in TERMINAL or observation.stopped is not True:
                    return self.journal.transition(operation_id, expected={"running", "unknown", "sealing"},
                        state="unknown", reason="wangp_stop_unproven")
                if observation.state in {"failed", "cancelled"}:
                    reason = (safe_failure_code(observation.error_code)
                        if observation.state == "failed" else None) or "wangp_runtime_stop_confirmed"
                    result = self.journal.transition(operation_id, expected={"running", "unknown"},
                        state=observation.state, stop_proven=True, reason=reason)
                    # Retain the original handle/diagnostic if persistence fails.
                    self._handles.pop(operation_id, None)
                    return result
                self.journal.transition(operation_id, expected={"running", "unknown"},
                                        state="sealing", stop_proven=True)
                self._fault("after_sealing")
                descriptors = self._seal(receipt, dict(observation.outputs))
                self._fault("after_seal_before_commit")
                result = self.journal.transition(operation_id, expected={"sealing"}, state="succeeded",
                                                 stop_proven=True, artifacts=descriptors)
                self._fault("after_success")
                self._handles.pop(operation_id, None)
                return result
            except Exception:
                # Output-copy failure is recoverable from the same handle/result.
                # Never expose raw upstream errors or declare stopped from an error.
                latest = self.journal.get(operation_id)
                return self.journal.transition(operation_id, expected={"running", "unknown", "sealing"},
                    state="unknown", stop_proven=latest.stop_proven,
                    reason="wangp_observation_or_collection_unknown")

    def cancel(self, operation_id):
        with self._lock:
            self._open()
            receipt = self.journal.request_cancel(operation_id)
            if receipt is None or receipt.state in TERMINAL:
                return False
            if receipt.state == "prepared":
                self.journal.transition(operation_id, expected={"prepared"}, state="cancelled",
                    stop_proven=True, reason="wangp_cancelled_before_dispatch")
                return True
            handle = self._handles.get(operation_id)
            if handle is None or receipt.hold_reason:
                return False
            try:
                return handle.cancel() is True
            except Exception:
                return False

    def _seal(self, receipt, outputs):
        expected = {"video", "audio"} if receipt.generate_audio else {"video"}
        if set(outputs) != expected or any(not isinstance(v, RuntimeOutput) for v in outputs.values()):
            raise BackendError("wangp_incomplete_outputs")
        folder = checked_directory(self.sealed_root / receipt.operation_id, create=True)
        descriptors = []
        for kind in sorted(expected):
            output = outputs[kind]
            # Validate kind/type before reading an arbitrary large output file.
            ArtifactDescriptor(kind, "0" * 64, 1, output.media_type)
            temporary = folder / (uuid.uuid4().hex + ".part")
            total, hasher, start = 0, hashlib.sha256(), time.monotonic()
            try:
                with checked_reader(output.path, self.output_root) as source, temporary.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > self.max_bytes or time.monotonic() - start > self.max_seconds:
                            raise BackendError("wangp_artifact_limit")
                        hasher.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                descriptor = ArtifactDescriptor(kind, hasher.hexdigest(), total, output.media_type)
                final = folder / (kind + "-" + descriptor.sha256 + ".bin")
                if final.exists():
                    self._verify_sealed(final, descriptor)
                else:
                    os.replace(temporary, final)
                    sync_directory(folder)
                descriptors.append(descriptor)
            finally:
                # Only this call's freshly created temporary filename is eligible.
                if temporary.exists():
                    temporary.unlink()
        return tuple(descriptors)

    def _verify_sealed(self, path, descriptor):
        size, hasher = 0, hashlib.sha256()
        with checked_reader(path, self.sealed_root) as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > descriptor.size_bytes or size > self.max_bytes:
                    raise BackendError("wangp_sealed_output_changed")
                hasher.update(chunk)
        if size != descriptor.size_bytes or hasher.hexdigest() != descriptor.sha256:
            raise BackendError("wangp_sealed_output_changed")

    def read_artifact(self, operation_id, kind):
        self._open()
        receipt = self.journal.get(operation_id)
        if receipt is None or receipt.state != "succeeded" or receipt.hold_reason:
            raise NotReady("wangp_output_not_sealed")
        descriptor = next((v for v in receipt.artifacts if v.kind == kind), None)
        if descriptor is None:
            raise BackendError("wangp_artifact_missing")
        path = self.sealed_root / operation_id / (kind + "-" + descriptor.sha256 + ".bin")
        size, hasher, start = 0, hashlib.sha256(), time.monotonic()
        with checked_reader(path, self.sealed_root) as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if (size > descriptor.size_bytes or size > self.max_bytes
                        or time.monotonic() - start > self.max_seconds):
                    raise BackendError("wangp_artifact_limit")
                hasher.update(chunk)
                yield chunk
        if size != descriptor.size_bytes or hasher.hexdigest() != descriptor.sha256:
            raise BackendError("wangp_sealed_output_changed")

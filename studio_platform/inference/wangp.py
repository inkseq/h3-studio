"""Default-disabled worker adapter for the private, identity-bound WanGP host."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import uuid

from .protocol import BackendError, NotReady, Outcome, SubmissionUncertain, safe_failure_code
from .wangp_contract import (EngineManifest, HostReadiness, OperationReceipt,
    PreparedRequest, canonical_json, operation_id, _identifier)
from ..runtime_hosts.wangp_receipts import checked_directory, checked_reader, sync_directory
from ..storage import key_belongs_to, validate_key


class WanGPBackend:
    kind = "wangp-worker"

    def __init__(self, *, enabled=False, slot_key="wangp-disabled", manifest=None,
                 transport=None, compiler=None, max_download_bytes=512 * 1024 * 1024,
                 max_transfer_seconds=180, expected_incarnation=None):
        self.enabled = enabled is True
        self.slot_key, self.manifest = slot_key, manifest
        self.transport, self.compiler = transport, compiler
        self.max_bytes, self.max_seconds = max_download_bytes, max_transfer_seconds
        if expected_incarnation is not None:
            import re
            if not isinstance(expected_incarnation, str) or not re.fullmatch(r'[0-9a-f]{32}', expected_incarnation):
                raise ValueError('wangp_invalid_runtime_incarnation')
        self.expected_incarnation = expected_incarnation
        _identifier(slot_key)
        if (type(max_download_bytes) is not int or max_download_bytes <= 0
                or not 0 < max_transfer_seconds < float("inf")):
            raise ValueError("wangp_invalid_transfer_limits")
        if self.enabled and (not isinstance(manifest, EngineManifest) or transport is None or not callable(compiler)):
            raise ValueError("wangp_explicit_dependencies_required")

    def _ready(self):
        if not self.enabled:
            raise NotReady("backend_disabled")

    def is_idle(self):
        self._ready()
        try:
            info = self.transport.readiness()
            return (isinstance(info, HostReadiness) and info.idle is True
                    and info.manifest_digest == self.manifest.digest and info.slot_key == self.slot_key
                    and (self.expected_incarnation is None or info.incarnation == self.expected_incarnation))
        except Exception:
            return False

    def _binding(self, job):
        plan = job.get("execution_plan", {})
        if (plan.get("backend") != self.kind
                or plan.get("engine_manifest_digest") != self.manifest.digest):
            raise BackendError("wangp_job_engine_binding_mismatch")
        if not isinstance(job.get("request", {}).get("output_spec"), dict) or not job["request"]["output_spec"]:
            raise BackendError("wangp_frozen_output_spec_required")

    def prepare(self, job, tag, store, heartbeat):
        self._ready()
        operation_id(tag)
        self._binding(job)
        if self.is_idle() is not True:
            raise NotReady("wangp_slot_not_idle")
        # Validate source ownership before calling the trusted compiler/stager.
        snapshots = job["request"].get("assets", {})
        try:
            for snapshot in snapshots.values():
                key = validate_key(snapshot["model"]["key"])
                if not key_belongs_to(key, job["owner_id"]):
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise BackendError("wangp_asset_owner_mismatch") from None
        try:
            prepared = self.compiler(job, tag, store, heartbeat)
        except BackendError:
            raise
        except Exception:
            raise BackendError("wangp_compilation_failed") from None
        raw = job["request"].get("request", job["request"])
        if (not isinstance(prepared, PreparedRequest) or prepared.job_id != job["id"]
                or prepared.attempt_tag != tag or prepared.request_hash != job["request_hash"]
                or prepared.manifest_digest != self.manifest.digest
                or prepared.output_spec_json != canonical_json(job["request"]["output_spec"])
                or prepared.generate_audio is not raw.get("generate_audio", True)
                or {v.asset_id for v in prepared.inputs} != set(snapshots)):
            raise BackendError("wangp_compiler_identity_mismatch")
        for item in prepared.inputs:
            snapshot = snapshots[item.asset_id]
            if (item.sha256 != snapshot["model"]["sha256"]
                    or item.size_bytes != snapshot["model"]["size_bytes"]
                    or item.kind != snapshot["metadata"]["kind"]):
                raise BackendError("wangp_compiler_input_mismatch")
        return prepared

    def _receipt(self, value, tag, task_id=None):
        expected = operation_id(tag)
        if task_id is not None and task_id != expected:
            raise BackendError("wangp_task_identity_mismatch")
        if (not isinstance(value, OperationReceipt) or value.operation_id != expected
                or value.attempt_tag != tag or value.manifest_digest != self.manifest.digest
                or self.expected_incarnation is not None and value.incarnation != self.expected_incarnation
                or value.slot_key != self.slot_key):
            raise BackendError("wangp_receipt_identity_mismatch")
        return value

    def submit(self, prepared, tag):
        self._ready()
        if (not isinstance(prepared, PreparedRequest) or prepared.attempt_tag != tag
                or prepared.manifest_digest != self.manifest.digest):
            raise SubmissionUncertain("wangp_submission_identity_unknown")
        try:
            receipt = self._receipt(self.transport.submit(prepared), tag)
            if receipt.identity_digest != prepared.identity_digest or receipt.hold_reason:
                raise ValueError
            return receipt.operation_id
        except Exception:
            # A transport error may happen after the private host started work.
            raise SubmissionUncertain("wangp_submission_unknown") from None

    def reconcile(self, tag, task_id=None):
        self._ready()
        expected = operation_id(tag)
        if task_id is not None and task_id != expected:
            raise BackendError("wangp_task_identity_mismatch")
        try:
            value = self.transport.inspect(expected)
            if value is None:
                return Outcome("unknown")
            receipt = self._receipt(value, tag, task_id)
            state = receipt.state
            if (receipt.hold_reason or state not in {"running", "succeeded", "failed", "cancelled"}
                    or state in {"succeeded", "failed", "cancelled"} and receipt.stop_proven is not True):
                state = "unknown"
            return Outcome(state, receipt.operation_id, None,
                safe_failure_code(receipt.reason) if state == "failed" else None)
        except Exception:
            return Outcome("unknown", task_id, None)

    def poll(self, tag, task_id):
        return self.reconcile(tag, task_id)


    def _delivery_request(self,job,tag,task_id):
        from .wangp_delivery import POLICY, delivery_fingerprint, validate_delivery_request
        from .outputs import delivery_spec
        self._ready();self._binding(job)
        if task_id!=operation_id(tag) or self.expected_incarnation is None or self.manifest.document.get('output_delivery')!=POLICY or job['execution_plan'].get('output_delivery')!=POLICY:
            raise BackendError('wangp_delivery_capability_unavailable')
        raw=job['request'].get('request',job['request'])
        identity={'operation_id':task_id,'job_id':job['id'],'attempt_tag':tag,'request_hash':job['request_hash'],
            'manifest_digest':self.manifest.digest,'slot_key':self.slot_key,'incarnation':self.expected_incarnation}
        spec=delivery_spec(job);crf=raw.get('export_crf',18);audio=raw.get('generate_audio',True)
        request={'version':1,'identity':identity,'owner_id':job['owner_id'],'delivery_spec':spec,
            'export_crf':crf,'generate_audio':audio,
            'fingerprint':delivery_fingerprint(identity,spec,crf,audio,job['owner_id'])}
        return validate_delivery_request(request)

    @staticmethod
    def _delivery_receipt(value,request):
        from .wangp_delivery import validate_delivery_receipt
        value=validate_delivery_receipt(value)
        if any(value[name]!=expected or type(value[name]) is not type(expected) for name,expected in request.items()):
            raise BackendError('wangp_delivery_receipt_mismatch')
        return value

    def prepare_delivery(self,job,tag,task_id,heartbeat):
        request=self._delivery_request(job,tag,task_id);heartbeat()
        try:value=self._delivery_receipt(self.transport.prepare_delivery(task_id,request),request)
        except Exception:raise BackendError('wangp_delivery_prepare_unconfirmed') from None
        heartbeat();return value

    def delivery_status(self,job,tag,task_id):
        request=self._delivery_request(job,tag,task_id)
        try:
            value=self.transport.delivery_status(task_id,request['identity'])
            return None if value is None else self._delivery_receipt(value,request)
        except Exception:raise BackendError('wangp_delivery_status_unconfirmed') from None

    def upload_delivery(self,job,tag,task_id,targets,heartbeat):
        from ..storage import SecretURL
        from .wangp_delivery import validate_upload_target
        request=self._delivery_request(job,tag,task_id)
        receipt=self.delivery_status(job,tag,task_id)
        if receipt is None or type(targets) is not dict or not targets:
            raise BackendError('wangp_delivery_not_prepared')
        wire={}
        try:
            for kind,target in targets.items():
                if type(target) is not dict or set(target)!={'object_key','size_bytes','sha256','content_md5','content_type','upload','storage_binding'}:
                    raise ValueError
                signed=target['upload']
                if not isinstance(signed,SecretURL) or signed.method!='PUT':raise ValueError
                wire[kind]={'key':target['object_key'],'size_bytes':target['size_bytes'],'sha256':target['sha256'],
                    'content_md5':target['content_md5'],'content_type':target['content_type'],'storage_binding':target['storage_binding'],
                    'expires_at':signed.expires_at,'url':signed.reveal(),'headers':signed.required_headers}
                validate_upload_target(kind,wire[kind],receipt,now=time.time())
            heartbeat()
            return self._delivery_receipt(self.transport.upload_delivery(task_id,request['identity'],request['fingerprint'],wire),request)
        except Exception:raise BackendError('wangp_delivery_upload_unconfirmed') from None
        finally:
            wire.clear()  # Bearer URLs are ephemeral private transport inputs.

    def cancel(self, tag, task_id):
        self._ready()
        if task_id != operation_id(tag):
            raise BackendError("wangp_task_identity_mismatch")
        try:
            receipt = self._receipt(self.transport.inspect(task_id), tag, task_id)
            return not receipt.hold_reason and self.transport.cancel(task_id) is True
        except Exception:
            return False

    def _local_matches(self, path, root, descriptor):
        hasher, total, started = hashlib.sha256(), 0, time.monotonic()
        with checked_reader(path, root) as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > descriptor.size_bytes or time.monotonic() - started > self.max_seconds:
                    raise BackendError("wangp_local_output_changed")
                hasher.update(chunk)
        if total != descriptor.size_bytes or hasher.hexdigest() != descriptor.sha256:
            raise BackendError("wangp_local_output_changed")

    def fetch(self, job, tag, task_id, target_dir: Path, heartbeat):
        self._ready()
        self._binding(job)
        try:
            receipt = self._receipt(self.transport.inspect(task_id), tag, task_id)
            if (receipt.state != "succeeded" or receipt.hold_reason
                    or receipt.job_id != job["id"] or receipt.request_hash != job["request_hash"]
                    or receipt.generate_audio is not job["request"].get("request", job["request"]).get("generate_audio", True)):
                raise BackendError("wangp_outputs_not_bound")
            root = checked_directory(target_dir, create=True)
            result = {}
            suffixes = {"video/mp4": ".mp4", "audio/flac": ".flac", "audio/wav": ".wav"}
            for descriptor in receipt.artifacts:
                if descriptor.size_bytes > self.max_bytes:
                    raise BackendError("wangp_artifact_limit")
                destination = root / (task_id + "-" + descriptor.kind + "-" + descriptor.sha256 + suffixes[descriptor.media_type])
                if destination.exists():
                    self._local_matches(destination, root, descriptor)
                    result[descriptor.kind] = destination
                    continue
                temporary = root / (uuid.uuid4().hex + ".part")
                hasher, total, started = hashlib.sha256(), 0, time.monotonic()
                try:
                    with temporary.open("xb") as stream:
                        for chunk in self.transport.read_artifact(task_id, descriptor.kind):
                            heartbeat()
                            if not isinstance(chunk, bytes):
                                raise BackendError("wangp_invalid_artifact_bytes")
                            total += len(chunk)
                            if (total > descriptor.size_bytes or total > self.max_bytes
                                    or time.monotonic() - started > self.max_seconds):
                                raise BackendError("wangp_artifact_limit")
                            hasher.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if total != descriptor.size_bytes or hasher.hexdigest() != descriptor.sha256:
                        raise BackendError("wangp_artifact_digest_mismatch")
                    if destination.exists():
                        self._local_matches(destination, root, descriptor)
                    else:
                        os.replace(temporary, destination)
                        sync_directory(root)
                    result[descriptor.kind] = destination
                finally:
                    if temporary.exists():
                        temporary.unlink()
            return result
        except BackendError:
            raise
        except Exception:
            raise BackendError("wangp_collection_unavailable") from None

    def close(self):
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

"""Dedicated Comfy transport; no worker, runtime or cloud initialization."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from ..storage import key_belongs_to, validate_key
from .outputs import _request, _shape
from .protocol import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
    TAG,
    TASK,
)


class ComfyBackend:
    """One dedicated Comfy slot with bounded HTTP and attempt-tag reconciliation.

    actual_cost_resolver(job, task_id) is a trusted accounting adapter; None means
    pending billing. Never use a quote as an invoice. HTTP endpoint must be local
    or an explicitly approved HTTPS gateway, without credentials/query/fragment.
    """

    def __init__(
        self,
        *,
        endpoint="",
        enabled=False,
        allowed_origins=(),
        transport=None,
        actual_cost_resolver=None,
        max_download_bytes=512 * 1024 * 1024,
        timeout_s=30,
        max_transfer_seconds=180,
        comfy_revision=None,
    ):
        self.enabled = bool(enabled)
        self.endpoint = endpoint.rstrip("/")
        self.cost_resolver = actual_cost_resolver
        self.max_bytes = max_download_bytes
        self.max_transfer_seconds = max_transfer_seconds
        self.timeout_s = timeout_s
        self.transport = transport
        self.comfy_revision = comfy_revision
        self._confirmed_dequeued = set()
        self._client = None
        self.slot_key = "comfy-" + hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.kind = "comfy-worker"
        if self.enabled:
            parsed = urlsplit(self.endpoint)
            allowed = set(allowed_origins)
            if (
                self.endpoint not in allowed
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path
                or parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("comfy_endpoint_not_explicitly_allowed")
            if (
                type(self.max_bytes) is not int
                or self.max_bytes <= 0
                or not math.isfinite(timeout_s)
                or timeout_s <= 0
                or not math.isfinite(max_transfer_seconds)
                or max_transfer_seconds <= 0
            ):
                raise ValueError("invalid_comfy_limits")

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def _http(self):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.endpoint,
                timeout=self.timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            )
        return self._client

    def _json(self, method, path, **kwargs):
        try:
            # All request paths are constants or validated task IDs, never provider URLs.
            with self._http().stream(method, path, **kwargs) as response:
                if response.status_code >= 400 or 300 <= response.status_code < 400:
                    raise BackendError("comfy_http_error")
                content = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if (
                        len(content) > 16 * 1024 * 1024
                        or time.monotonic() - started > self.max_transfer_seconds
                    ):
                        raise BackendError("comfy_response_limit")
                return json.loads(content)
        except (httpx.HTTPError, json.JSONDecodeError):
            raise BackendError("comfy_response_unavailable") from None

    def is_idle(self):
        """Current dedicated queue evidence, not model qualification or recovery."""
        queue = self._json("GET", "/queue")
        return (
            isinstance(queue, dict)
            and queue.get("queue_running") == []
            and queue.get("queue_pending") == []
        )

    def prepare(self, job, tag, store, heartbeat):
        from comfy_workflow import build_workflow

        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        if self.is_idle() is not True:
            raise NotReady("comfy_slot_not_idle")
        request = {**_request(job), "_job_id": tag}
        snapshots = job["request"].get("assets", {})
        metadata, filenames = {}, {}
        for asset_id, snapshot in snapshots.items():
            heartbeat()
            model = snapshot["model"]
            key = validate_key(model["key"])
            if not key_belongs_to(key, job["owner_id"]):
                raise BackendError("asset_owner_mismatch")
            extension = {"image": ".png", "video": ".mp4", "audio": ".wav"}[
                snapshot["metadata"]["kind"]
            ]
            filename = "sixnine-" + hashlib.sha256(key.encode()).hexdigest() + extension
            with store.open(key) as source:
                response = self._json(
                    "POST",
                    "/upload/image",
                    files={"image": (filename, source, "application/octet-stream")},
                    data={
                        "type": "input",
                        "overwrite": "true",
                        "subfolder": "sixnine-inputs",
                    },
                )
            name, folder = response.get("name"), response.get("subfolder", "")
            if (
                name != filename
                or folder != "sixnine-inputs"
                or response.get("type", "input") != "input"
            ):
                raise BackendError("comfy_upload_path_invalid")
            filenames[asset_id] = folder + "/" + name
            metadata[asset_id] = snapshot["metadata"]
        return build_workflow(request, metadata, filenames)

    def submit(self, prepared, tag):
        try:
            with self._http().stream(
                "POST",
                "/prompt",
                json={
                    "prompt": prepared,
                    "client_id": tag,
                    "extra_data": {"sixnine_attempt_id": tag},
                },
            ) as response:
                if response.status_code in (400, 401, 403, 404, 422):
                    raise SubmissionRejected("comfy_submission_rejected")
                if response.status_code != 200:
                    raise SubmissionUncertain("comfy_submission_unknown")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1024 * 1024:
                        raise SubmissionUncertain("comfy_submission_unknown")
                body = json.loads(content)
            task_id = body.get("prompt_id") if isinstance(body, dict) else None
            if not isinstance(task_id, str) or not TASK.fullmatch(task_id):
                raise SubmissionUncertain("comfy_submission_unknown")
            return task_id
        except (httpx.HTTPError, json.JSONDecodeError):
            raise SubmissionUncertain("comfy_submission_unknown") from None

    @staticmethod
    def _tagged(prompt, tag):
        if not isinstance(prompt, list) or len(prompt) < 4:
            return False
        extra, graph = prompt[3], prompt[2]
        if (
            not isinstance(extra, dict)
            or extra.get("sixnine_attempt_id") != tag
            or not isinstance(graph, dict)
        ):
            return False
        return any(
            isinstance(node, dict)
            and node.get("class_type") == "SaveVideo"
            and node.get("inputs", {}).get("filename_prefix") == "h3-studio/" + tag
            for node in graph.values()
        )

    def reconcile(self, tag, task_id=None):
        if (
            not TAG.fullmatch(tag)
            or task_id is not None
            and not TASK.fullmatch(task_id)
        ):
            raise BackendError("invalid_attempt_identity")
        if task_id:
            return self.poll(tag, task_id)
        history = self._json("GET", "/history", params={"max_items": 200})
        queue = self._json("GET", "/queue")
        if not isinstance(history, dict) or not isinstance(queue, dict):
            raise BackendError("comfy_reconciliation_invalid")
        ids = set()
        for tid, record in history.items():
            if isinstance(record, dict) and self._tagged(record.get("prompt"), tag):
                ids.add(tid)
        for key in ("queue_running", "queue_pending"):
            if not isinstance(queue.get(key), list):
                raise BackendError("comfy_reconciliation_invalid")
            for prompt in queue[key]:
                if self._tagged(prompt, tag):
                    ids.add(prompt[1])
        if len(ids) != 1:
            # History may be evicted or the old process may still accept a POST.
            # No matching task is never treated as proof it is safe to repost.
            return Outcome("unknown")
        found = next(iter(ids))
        if not isinstance(found, str) or not TASK.fullmatch(found):
            raise BackendError("invalid_upstream_task_id")
        return self.poll(tag, found)

    def _record(self, tag, task_id):
        if not TASK.fullmatch(task_id):
            raise BackendError("invalid_upstream_task_id")
        history = self._json("GET", "/history/" + task_id)
        record = history.get(task_id) if isinstance(history, dict) else None
        if record is None:
            return None
        if not isinstance(record, dict) or not self._tagged(record.get("prompt"), tag):
            raise BackendError("comfy_task_identity_mismatch")
        return record

    def poll(self, tag, task_id):
        record = self._record(tag, task_id)
        if record is None:
            queue = self._json("GET", "/queue")
            if isinstance(queue, dict) and any(
                self._tagged(p, tag) and p[1] == task_id
                for k in ("queue_running", "queue_pending")
                for p in queue.get(k, [])
            ):
                return Outcome("running", task_id)
            if task_id in self._confirmed_dequeued:
                return Outcome("cancelled", task_id)
            return Outcome("unknown", task_id)
        status = record.get("status", {})
        if status.get("completed") is True and status.get("status_str") not in (
            "error",
            "failed",
        ):
            return Outcome("succeeded", task_id)
        if status.get("completed") is False and status.get("status_str") in (
            "error",
            "failed",
        ):
            return Outcome("failed", task_id)
        return Outcome("unknown", task_id)

    def cancel(self, tag, task_id):
        if not TASK.fullmatch(task_id):
            raise BackendError("invalid_upstream_task_id")
        from comfy_workflow import COMFY_COMMIT

        if self.comfy_revision != COMFY_COMMIT:
            queue = self._json("GET", "/queue")
            if not isinstance(queue, dict) or any(
                not isinstance(queue.get(k), list)
                for k in ("queue_running", "queue_pending")
            ):
                raise BackendError("comfy_cancel_unknown")
            if any(
                self._tagged(p, tag) and p[1] == task_id for p in queue["queue_running"]
            ):
                # Unverified versions never get a global interrupt or guessed route.
                return False
            if not any(
                self._tagged(p, tag) and p[1] == task_id for p in queue["queue_pending"]
            ):
                return False
            try:
                response = self._http().post("/queue", json={"delete": [task_id]})
                if response.status_code != 200:
                    raise BackendError("comfy_cancel_unknown")
            except httpx.HTTPError:
                raise BackendError("comfy_cancel_unknown") from None
            checked = self._json("GET", "/queue")
            if not isinstance(checked, dict) or any(
                not isinstance(checked.get(k), list)
                for k in ("queue_running", "queue_pending")
            ):
                raise BackendError("comfy_cancel_unknown")
            stopped = not any(
                isinstance(p, list) and len(p) > 1 and p[1] == task_id
                for k in ("queue_running", "queue_pending")
                for p in checked[k]
            )
            if stopped:
                self._confirmed_dequeued.add(task_id)
            return stopped
        result = self._json("POST", "/api/jobs/" + task_id + "/cancel")
        if not isinstance(result, dict) or type(result.get("cancelled")) is not bool:
            raise BackendError("comfy_cancel_unknown")
        # This proves dispatch/dequeue only. Runner still checks actual task status.
        return result["cancelled"]

    def _outputs(self, record, tag):
        graph = record["prompt"][2]
        found = {}
        for node_id, result in record.get("outputs", {}).items():
            node = graph.get(str(node_id), {})
            kind = {"SaveVideo": "video", "SaveAudioAdvanced": "audio"}.get(
                node.get("class_type")
            )
            if kind is None:
                continue
            prefix = tag + ("_audio" if kind == "audio" else "")
            if node.get("inputs", {}).get("filename_prefix") != "h3-studio/" + prefix:
                continue
            for label in (
                ("videos", "images", "gifs") if kind == "video" else ("audio", "audios")
            ):
                for entry in result.get(label, []):
                    filename = entry.get("filename", "")
                    extension = ".mp4" if kind == "video" else ".flac"
                    if (
                        entry.get("type") == "output"
                        and entry.get("subfolder") == "h3-studio"
                        and isinstance(filename, str)
                        and re.fullmatch(
                            re.escape(prefix) + r"_\d+_?" + re.escape(extension),
                            filename,
                        )
                    ):
                        found.setdefault(kind, entry)
        return found

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        record = self._record(tag, task_id)
        if not record or record.get("status", {}).get("completed") is not True:
            raise BackendError("comfy_output_not_complete")
        outputs = self._outputs(record, tag)
        required = ("video", "audio") if _shape(job)[3] else ("video",)
        if any(kind not in outputs for kind in required):
            raise BackendError("comfy_save_outputs_missing")
        paths = {}
        for kind in required:
            heartbeat()
            entry = outputs[kind]
            target = Path(target_dir) / ("raw.mp4" if kind == "video" else "raw.flac")
            query = {key: entry[key] for key in ("filename", "subfolder", "type")}
            count, started = 0, time.monotonic()
            try:
                with (
                    self._http().stream("GET", "/view", params=query) as response,
                    target.open("wb") as destination,
                ):
                    if response.status_code != 200:
                        raise BackendError("comfy_download_failed")
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if (
                            count > self.max_bytes
                            or time.monotonic() - started > self.max_transfer_seconds
                        ):
                            raise BackendError("comfy_download_limit")
                        destination.write(chunk)
            except httpx.HTTPError:
                raise BackendError("comfy_download_failed") from None
            paths[kind] = target
        return paths

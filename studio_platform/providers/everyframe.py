"""EveryFrame adapter for hosted video renders (#918, #931 step 5a).

The job's bound configuration names the hosted model (hosted_models.MODELS);
the scheduler picks it from the take's chain. Not wired to any queue yet.

The platform keeps the API key and the storage credentials. `prepare` copies the
job's validated reference objects to the vendor as attachments; `submit` starts
one render. The vendor charges when the render starts and refunds a failed one
itself, so a refused request (4xx) costs nothing and is never retried, and a
lost answer is never resubmitted: reconcile stays unknown for the recovery path.

Credentials: `runtime-secrets/everyframe.json` in the data volume, installed by
the host like runpod-enhance.json: `{"api_key": ...}`.
"""

import json
import math
import os
import re
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .hosted_models import BACKEND, CONFIGURATIONS, PROVIDER, vendor_request
from ..inference.protocol import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
)
from ..media import ffmpeg, probe
from ..protected_config import read_protected_json

API = "https://api.everyframe.studio/v1"
FILENAME = "everyframe.json"
MAX_INPUT_BYTES = 512 * 1024**2
MAX_OUTPUT_BYTES = 512 * 1024**2
MAX_ANSWER_BYTES = 1024**2
STATES = {
    "queued": "running",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
}
ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "video/mp4", "audio/wav"}
CONTENT_TYPES |= {"video/quicktime", "video/webm", "audio/mpeg", "audio/mp4"}
CONTENT_TYPES |= {"audio/aac", "audio/ogg", "image/avif"}


class ConfigError(ValueError):
    def __init__(self):
        super().__init__("everyframe_config_invalid")


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)


def load_config(path):
    return validate_config(read_protected_json(path, ConfigError))


def validate_config(value):
    if (
        type(value) is not dict
        or set(value) != {"api_key"}
        or not isinstance(value["api_key"], str)
        or not 1 <= len(value["api_key"]) <= 4096
        or not value["api_key"].isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in value["api_key"])
    ):
        raise ConfigError()
    return Config(**value)


def cost_microusd(render):
    value = render.get("costUsd")
    if type(value) not in (int, float) or not 0 <= value <= 1000:
        return None
    return math.ceil(value * 1_000_000)


@dataclass(frozen=True)
class Prepared:
    tag: str
    body: bytes = field(repr=False)


class EveryFrameBackend:
    kind = BACKEND

    def __init__(
        self, store, config_file, *, enabled=True, transport=None, slot_key=PROVIDER
    ):
        self.store, self.config_file, self.enabled = store, Path(config_file), enabled
        # One local render lock per slot; the vendor's limits are per account.
        self.slot_key = slot_key
        self._transport, self._client, self._config = transport, None, None
        self.cost_resolver = self._cost

    def __repr__(self):
        return f"EveryFrameBackend(enabled={self.enabled})"

    def __reduce_ex__(self, protocol):
        raise TypeError("Provider credentials must remain process-local")

    def config(self):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if self._config is None:
            try:
                self._config = load_config(self.config_file)
            except ConfigError:
                raise NotReady("everyframe_config_unavailable") from None
        return self._config

    def close(self):
        if self._client is not None:
            self._client.close()
        self._transport, self._client, self._config = None, None, None

    def _request(self, method, path, *, content=None, content_type=None):
        config = self.config()
        if self._client is None:
            # No retry (a lost submit is never resent), no environment proxy or
            # netrc, and no redirect (the key only ever goes to the API origin).
            self._client = httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=60,
            )
        headers = {"Authorization": "Bearer " + config.api_key}
        if content_type:
            headers["Content-Type"] = content_type
        return self._client.build_request(
            method, API + path, headers=headers, content=content
        )

    def _call(self, method, path, body=None, *, raw=None, content_type=None):
        """(status, JSON dict or None). The key and URLs never reach logs or errors."""
        request = self._request(
            method,
            path,
            content=raw
            if raw is not None
            else None
            if body is None
            else json.dumps(body).encode(),
            content_type=content_type
            or ("application/json" if body is not None else None),
        )
        try:
            with closing(self._client.send(request, stream=True)) as response:
                content = b""
                for chunk in response.iter_bytes():
                    content += chunk
                    if len(content) > MAX_ANSWER_BYTES:
                        raise ValueError
                data = json.loads(content) if content else None
                if data is not None and type(data) is not dict:
                    raise ValueError
                return response.status_code, data
        except Exception:
            raise BackendError("everyframe_response_unavailable") from None

    def _attach(self, item):
        obj = item["model"]
        content_type = obj.get("content_type")
        if content_type not in CONTENT_TYPES:
            raise BackendError("everyframe_input_type_unsupported")
        info = self.store.stat(obj["key"])
        if info.size_bytes != obj["size_bytes"] or not 0 < info.size_bytes <= (
            MAX_INPUT_BYTES
        ):
            raise BackendError("everyframe_input_changed")
        with self.store.open(obj["key"]) as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) != info.size_bytes:
            raise BackendError("everyframe_input_changed")
        status, answer = self._call(
            "POST", "/render/attachment", raw=data, content_type=content_type
        )
        attachment = (answer or {}).get("attachment") or answer or {}
        if (
            not 200 <= status < 300
            or not isinstance(attachment.get("id"), str)
            or not ID.fullmatch(attachment["id"])
        ):
            raise BackendError("everyframe_attachment_refused")
        return attachment["id"]

    def prepare(self, job, tag, store, heartbeat):
        self.config()
        compiled = job["request"]
        model_id = CONFIGURATIONS.get(job["execution_plan"].get("configuration_id"))
        mapped = None if model_id is None else vendor_request(compiled, model_id)
        if mapped is None:
            raise BackendError("everyframe_request_invalid")
        body, asset_ids = mapped
        attachments = []
        for asset_id in asset_ids:
            heartbeat()
            item = compiled.get("assets", {}).get(asset_id)
            if type(item) is not dict or type(item.get("model")) is not dict:
                raise BackendError("everyframe_input_missing")
            attachments.append(self._attach(item))
        if attachments:
            body["attachmentIds"] = attachments
        return Prepared(tag, json.dumps(body).encode())

    def submit(self, prepared, tag):
        if not isinstance(prepared, Prepared) or prepared.tag != tag:
            raise BackendError("everyframe_prepared_identity_mismatch")
        try:
            status, data = self._call("POST", "/render", json.loads(prepared.body))
        except BackendError:
            raise SubmissionUncertain("everyframe_render_unknown") from None
        if 400 <= status < 500:
            # Refused before a render exists (balance, limit, policy, rate):
            # the vendor charged nothing.
            raise SubmissionRejected("everyframe_render_refused")
        task_id = ((data or {}).get("render") or {}).get("id")
        if (
            not 200 <= status < 300
            or not isinstance(task_id, str)
            or not ID.fullmatch(task_id)
        ):
            raise SubmissionUncertain("everyframe_render_unknown")
        return task_id

    def _render(self, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise BackendError("everyframe_task_identity_invalid")
        status, data = self._call("GET", "/render/" + task_id)
        render = (data or {}).get("render")
        if status != 200 or type(render) is not dict or render.get("id") != task_id:
            raise BackendError("everyframe_status_unavailable")
        return render

    def reconcile(self, tag, task_id=None):
        return Outcome("unknown") if task_id is None else self.poll(tag, task_id)

    def poll(self, tag, task_id):
        try:
            render = self._render(task_id)
        except BackendError:
            return Outcome("unknown", task_id)
        state = STATES.get(render.get("status"), "unknown")
        if state == "succeeded" and not str(render.get("videoFullUrl", "")).startswith(
            "/render/"
        ):
            state = "failed"  # Succeeded without the download contract.
        cost = None
        if state == "succeeded":
            cost = cost_microusd(render)
        elif state == "failed":
            cost = 0  # The vendor refunds a failed render to the balance.
        # Upstream error text never crosses into the Outcome.
        return Outcome(state, task_id, actual_cost_microusd=cost)

    def _cost(self, job, task_id):
        return self.poll(None, task_id).actual_cost_microusd

    def cancel(self, tag, task_id):
        return False  # No cancel endpoint; a running render finishes and settles.

    def _download(self, url, target, heartbeat):
        request = self._request("GET", url)
        count = 0
        try:
            with closing(self._client.send(request, stream=True)) as response:
                if response.status_code != 200:
                    raise BackendError("everyframe_output_unavailable")
                with target.open("xb") as handle:
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > MAX_OUTPUT_BYTES:
                            raise BackendError("everyframe_output_too_large")
                        handle.write(chunk)
                        heartbeat()
                    handle.flush()
                    os.fsync(handle.fileno())
        except BackendError:
            raise
        except Exception:
            raise BackendError("everyframe_output_unavailable") from None
        if count == 0:
            raise BackendError("everyframe_output_empty")

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        target = Path(target_dir)
        if not target.is_absolute() or not target.is_dir() or target.is_symlink():
            raise BackendError("everyframe_output_directory_invalid")
        render = self._render(task_id)
        url = str(render.get("videoFullUrl", ""))
        if render.get("status") != "succeeded" or not url.startswith("/render/"):
            raise BackendError("everyframe_output_unavailable")
        raw = target / ("everyframe-" + os.urandom(8).hex() + ".mp4")
        heartbeat()
        try:
            self._download(url, raw, heartbeat)
            return self._normalize(job, raw, target)
        finally:
            if raw.exists():
                raw.unlink()

    def _normalize(self, job, raw, target):
        """Exact output size and at least the planned duration.

        The vendor picks its own canvas per aspect; scale and centre-crop to
        the compiled size, and hold the last frame if its clip is a frame short.
        """
        output = job["request"]["output_spec"]
        width, height = output["width"], output["height"]
        duration = output["actual_duration"]
        audio = bool(job["request"]["request"].get("generate_audio"))
        info = probe(raw)
        if audio and not any(
            s.get("codec_type") == "audio" for s in info.get("streams", [])
        ):
            raise BackendError("everyframe_output_audio_missing")
        video = target / "everyframe-video.mp4"
        ffmpeg(
            [
                "-y",
                "-i",
                raw,
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps=24,tpad=stop_mode=clone:stop_duration=1",
                *(["-af", "apad"] if audio else ["-an"]),
                "-t",
                f"{duration:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "16",
                "-pix_fmt",
                "yuv420p",
                *(["-c:a", "aac", "-b:a", "192k"] if audio else []),
                video,
            ],
            timeout=180,
        )
        paths = {"video": video}
        if audio:
            flac = target / "everyframe-audio.flac"
            ffmpeg(
                ["-y", "-i", video, "-map", "0:a:0", "-c:a", "flac", flac],
                timeout=60,
            )
            paths["audio"] = flac
        return paths

    def discard_output(self, tag):
        """Nothing is staged on our side before fetch."""


def create(store, settings, slot_key=PROVIDER):
    """The fleet's backend: credentials from the host-installed runtime secret."""
    path = Path(settings.data_dir) / "runtime-secrets" / FILENAME
    return EveryFrameBackend(store, path, slot_key=slot_key)

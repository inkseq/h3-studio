"""RunPod Serverless execution of enhance jobs (#795, docs/ENHANCE.md).

The platform keeps the RunPod key and the storage credentials. The RunPod worker
(deploy/enhance/handler.py) gets one presigned GET of the caller's validated
source and one presigned PUT for a temporary output object, nothing else.

One RunPod job per attempt: WorkerRunner commits the submission intent before
`/run`, and a /run whose answer is lost is never repeated. Without a job id,
reconcile stays unknown for the existing recovery path.

Credentials: `runtime-secrets/runpod-enhance.json` in the data volume, installed
by the host from SSM, root-owned 0440, like langfuse.json:
`{"api_key": ..., "endpoint_id": ..., "rate_microusd_per_s": 970}`.
"""

import json
import math
import os
import re
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .enhance_plans import BACKEND, CONFIGURATION, MAX_SOURCE_BYTES, RECIPE
from .inference.protocol import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
)
from .media import ffmpeg
from .protected_config import read_protected_json

API = "https://api.runpod.ai/v2/"
FILENAME = "runpod-enhance.json"
TEMP_PREFIX = "enhance-tmp/"  # R2 lifecycle rule expires this prefix after 2 days.
MAX_OUTPUT_BYTES = 512 * 1024**2
URL_SECONDS = 3600
# RunPod job policy: at most 15 minutes of execution and 1 hour in total.
POLICY = {"executionTimeout": 900_000, "ttl": 3_600_000}
STATES = {
    "IN_QUEUE": "running",
    "IN_PROGRESS": "running",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "TIMED_OUT": "failed",
    "CANCELLED": "cancelled",
}
ID = re.compile(r"[A-Za-z0-9-]{1,128}")


class ConfigError(ValueError):
    def __init__(self):
        super().__init__("runpod_enhance_config_invalid")


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    endpoint_id: str
    rate_microusd_per_s: int


def load_config(path):
    return validate_config(read_protected_json(path, ConfigError))


def validate_config(value):
    if (
        type(value) is not dict
        or set(value) != {"api_key", "endpoint_id", "rate_microusd_per_s"}
        or not isinstance(value["api_key"], str)
        or not 1 <= len(value["api_key"]) <= 4096
        or not value["api_key"].isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in value["api_key"])
        or not isinstance(value["endpoint_id"], str)
        or not re.fullmatch(r"[a-z0-9]{1,64}", value["endpoint_id"])
        or type(value["rate_microusd_per_s"]) is not int
        or not 1 <= value["rate_microusd_per_s"] <= 100_000
    ):
        raise ConfigError()
    return Config(**value)


def temp_key(tag):
    if not isinstance(tag, str) or not ID.fullmatch(tag):
        raise BackendError("enhance_attempt_identity_invalid")
    return TEMP_PREFIX + tag + ".mp4"


def cost_microusd(status, rate):
    """Billed worker time, conservatively including queue and cold start.

    Without executionTime the cost is unknown, never zero.
    """
    total = 0
    for name, default in (("delayTime", 0), ("executionTime", None)):
        value = status.get(name, default)
        if type(value) not in (int, float) or not 0 <= value <= 86_400_000:
            return None
        total += value
    return math.ceil(total / 1000 * rate)


@dataclass(frozen=True)
class Prepared:
    tag: str
    body: bytes = field(repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError("Presigned URLs must remain process-local")


class RunPodEnhanceBackend:
    kind = BACKEND

    def __init__(self, store, config_file, *, enabled=True, transport=None):
        self.store, self.config_file, self.enabled = store, Path(config_file), enabled
        self.slot_key = "runpod-" + CONFIGURATION
        self._transport, self._config = transport, None
        self.cost_resolver = self._cost

    def __repr__(self):
        return f"RunPodEnhanceBackend(enabled={self.enabled})"

    def __reduce_ex__(self, protocol):
        raise TypeError("Provider credentials must remain process-local")

    def config(self):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if self._config is None:
            try:
                self._config = load_config(self.config_file)
            except ConfigError:
                raise NotReady("runpod_enhance_config_unavailable") from None
        return self._config

    def close(self):
        if self._transport is not None:
            self._transport.close()
        self._transport, self._config = None, None

    def _call(self, method, path, body=None):
        """(status, JSON dict or None). The key and URLs never reach logs or errors."""
        config = self.config()
        if self._transport is None:
            self._transport = httpx.HTTPTransport(retries=0, trust_env=False)
        request = httpx.Request(
            method,
            API + config.endpoint_id + path,
            headers={
                "Authorization": "Bearer " + config.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            content=None if body is None else json.dumps(body).encode(),
            extensions={
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), 30)
            },
        )
        try:
            with closing(self._transport.handle_request(request)) as response:
                content = response.read()
                if len(content) > 1024**2:
                    raise ValueError
                data = (
                    json.loads(content) if 200 <= response.status_code < 300 else None
                )
                if data is not None and type(data) is not dict:
                    raise ValueError
                return response.status_code, data
        except Exception:
            raise BackendError("runpod_enhance_response_unavailable") from None

    def prepare(self, job, tag, store, heartbeat):
        self.config()
        compiled = job["request"]
        enhance = compiled.get("request", {}).get("enhance")
        sources = list(compiled.get("sources", {}).values())
        if (
            compiled.get("recipe_id") != RECIPE
            or type(enhance) is not dict
            or enhance.get("version") != 1
            or len(sources) != 1
            or sources[0].get("kind") != "video"
        ):
            raise BackendError("enhance_request_invalid")
        source = sources[0]["object"]
        info = self.store.stat(source["key"])
        if (
            info.size_bytes != source["size_bytes"]
            or not 0 < info.size_bytes <= MAX_SOURCE_BYTES
            or (source.get("sha256") and info.sha256 not in (None, source["sha256"]))
        ):
            raise BackendError("enhance_source_changed")
        heartbeat()
        try:
            video = self.store.presign_download(
                source["key"], expires_seconds=URL_SECONDS
            )
            output = self.store.presign_worker_upload(
                temp_key(tag), content_type="video/mp4", expires_seconds=URL_SECONDS
            )
        except Exception:
            raise BackendError("enhance_transfer_urls_unavailable") from None
        body = {
            "input": {
                "video_url": video.reveal(),
                **{k: enhance[k] for k in ("res", "steps", "sigma", "audio", "unet")},
                # The take's own prompt, as in the #795 quality runs; else the default.
                **({"prompt": enhance["prompt"]} if enhance.get("prompt") else {}),
                "output_put_url": output.reveal(),
            },
            "policy": POLICY,
        }
        return Prepared(tag, json.dumps(body).encode())

    def submit(self, prepared, tag):
        if not isinstance(prepared, Prepared) or prepared.tag != tag:
            raise BackendError("enhance_prepared_identity_mismatch")
        try:
            status, data = self._call("POST", "/run", json.loads(prepared.body))
        except BackendError:
            raise SubmissionUncertain("runpod_enhance_run_unknown") from None
        if status in (400, 401, 403, 404):
            # Refused before a job exists: nothing runs and nothing is billed.
            raise SubmissionRejected("runpod_enhance_run_refused")
        task_id = (data or {}).get("id")
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise SubmissionUncertain("runpod_enhance_run_unknown")
        return task_id

    def _status(self, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise BackendError("enhance_task_identity_invalid")
        status, data = self._call("GET", "/status/" + task_id)
        if status == 404:
            raise BackendError("runpod_enhance_status_expired")
        if data is None or data.get("id", task_id) != task_id:
            raise BackendError("runpod_enhance_status_unavailable")
        return data

    def _expired(self, tag, task_id):
        """RunPod keeps /status for about 30 min after a job ends. The uploaded
        output then proves success (a presigned PUT is all-or-nothing); the
        billed time is no longer readable, so the cost stays unknown."""
        if tag is not None:
            try:
                if self.store.stat(temp_key(tag)).size_bytes > 0:
                    return Outcome("succeeded", task_id)
            except Exception:
                pass
        return Outcome("unknown", task_id)

    def reconcile(self, tag, task_id=None):
        return Outcome("unknown") if task_id is None else self.poll(tag, task_id)

    def poll(self, tag, task_id):
        try:
            data = self._status(task_id)
        except BackendError as error:
            if error.args == ("runpod_enhance_status_expired",):
                return self._expired(tag, task_id)
            return Outcome("unknown", task_id)
        state = STATES.get(data.get("status"), "unknown")
        output = data.get("output")
        if state == "succeeded" and not (
            type(output) is dict and output.get("uploaded") is True
        ):
            state = "failed"  # Completed without the uploaded result contract.
        cost = None
        if state in ("succeeded", "failed", "cancelled"):
            cost = cost_microusd(data, self.config().rate_microusd_per_s)
        # Upstream error text never crosses into the Outcome.
        return Outcome(state, task_id, actual_cost_microusd=cost)

    def _cost(self, job, task_id):
        return self.poll(None, task_id).actual_cost_microusd

    def cancel(self, tag, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            return False
        status, _ = self._call("POST", "/cancel/" + task_id)
        return 200 <= status < 300

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        target = Path(target_dir)
        if not target.is_absolute() or not target.is_dir() or target.is_symlink():
            raise BackendError("enhance_output_directory_invalid")
        output = target / "enhance-raw.mp4"
        partial = target / (".enhance-" + os.urandom(8).hex() + ".part")
        count = 0
        heartbeat()
        try:
            with self.store.open(temp_key(tag)) as source, partial.open("xb") as handle:
                while chunk := source.read(1024**2):
                    count += len(chunk)
                    if count > MAX_OUTPUT_BYTES:
                        raise BackendError("enhance_output_too_large")
                    handle.write(chunk)
                    heartbeat()
                handle.flush()
                os.fsync(handle.fileno())
            if count == 0:
                raise BackendError("enhance_output_empty")
            partial.replace(output)
        except BackendError:
            raise
        except Exception:
            raise BackendError("enhance_output_unavailable") from None
        finally:
            if partial.exists():
                partial.unlink()
        # The MP4 carries the regenerated draft audio. Map only that stream so
        # the collector's FLAC step never decodes the 2K video (#873 review).
        audio = target / "enhance-audio.flac"
        try:
            ffmpeg(
                ["-y", "-i", output, "-map", "0:a:0", "-c:a", "flac", audio], timeout=60
            )
        except Exception:
            raise BackendError("enhance_output_audio_missing") from None
        return {"video": output, "audio": audio}


def create(store, settings):
    """The fleet's backend: credentials from the host-installed runtime secret."""
    path = Path(settings.data_dir) / "runtime-secrets" / FILENAME
    return RunPodEnhanceBackend(store, path)

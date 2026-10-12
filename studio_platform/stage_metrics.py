"""Optional bounded Grafana Cloud reads; never another execution authority.

Only detail views use this client. A separate logs:read mount is required; the
OTLP ingestion credential is deliberately incompatible. No raw Cloud response,
authorization or exception text leaves this module. Successful video attempts
come from the original scoped business ledger, not from telemetry counters.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import urlencode, urlsplit

MAX_ATTEMPTS = 32
MAX_ROWS = 256
MAX_WINDOW = 7 * 86400
MAX_RESPONSE = 512 * 1024
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
_STAGE = re.compile(r"[0-9a-f]{32}\Z")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9-]{0,199}\Z")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _empty(code="not_configured"):
    return {"average_generate_seconds": None, "generate_samples": 0,
        "generation_basis": "unavailable", "sample_window_start": None,
        "sample_window_end": None, "stage_observed_at": None,
        "stage_query_status": code}


@dataclass(frozen=True)
class StageQueryConfig:
    endpoint: str
    authorization: str = field(repr=False)
    scope: str = "logs:read"

    def __post_init__(self):
        try:
            parsed = urlsplit(self.endpoint)
            valid_endpoint = (parsed.scheme == "https" and parsed.port in (None, 443)
                and parsed.hostname is not None
                and re.fullmatch(r"logs-prod-[a-z0-9-]+\.grafana\.net", parsed.hostname)
                and parsed.path in ("", "/") and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
            auth = self.authorization
            valid_auth = (isinstance(auth, str) and auth.startswith("Basic ")
                and len(auth) <= 8192 and not any(c in auth for c in "\r\n\x00")
                and re.fullmatch(rb"[0-9]+:[!-~]+", base64.b64decode(auth[6:], validate=True)))
        except (TypeError, ValueError):
            raise ValueError("stage_query_config_invalid") from None
        if not valid_endpoint or not valid_auth or self.scope != "logs:read":
            raise ValueError("stage_query_config_invalid")


def read_config(filename):
    try:
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
                raise ValueError
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        value = json.loads(raw)
        if (type(value) is not dict
                or set(value) != {"schema_version", "enabled", "endpoint", "authorization", "scope"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["enabled"]) is not bool):
            raise ValueError
        if not value["enabled"]:
            return None
        return StageQueryConfig(value["endpoint"], value["authorization"], value["scope"])
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ValueError("stage_query_config_invalid") from None


def _http_read(config, path, timeout):
    """Single HTTPS request, no redirects or response/error body diagnostics."""
    connection = http.client.HTTPSConnection(urlsplit(config.endpoint).hostname, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Authorization": config.authorization,
            "Accept": "application/json", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            return None
        return json.loads(raw)
    except Exception:
        return None
    finally:
        connection.close()


class NullStageReader:
    def __init__(self, code="not_configured"):
        self.code = code

    def generate(self, **_kwargs):
        return _empty(self.code)


class CloudStageReader:
    def __init__(self, config, *, transport=_http_read, monotonic=time.monotonic):
        self.config, self.transport, self.monotonic = config, transport, monotonic
        self._lock, self._cache = threading.Lock(), OrderedDict()

    def generate(self, *, node_id, provider, profile_id, mode, attempts, now):
        """At most one Cloud request; closed arguments originate in the ledger."""
        if (not isinstance(node_id, str) or not _UUID.fullmatch(node_id)
                or provider not in {"vast", "runpod"} or mode not in {"fl", "ref"}
                or not isinstance(profile_id, str) or not _PROFILE.fullmatch(profile_id)
                or not _finite(now) or type(attempts) is not list):
            return _empty("identity_unavailable")
        eligible = []
        for row in attempts:
            if (type(row) is dict and isinstance(row.get("job_id"), str)
                    and _UUID.fullmatch(row["job_id"])
                    and isinstance(row.get("attempt_id"), str) and _UUID.fullmatch(row["attempt_id"])
                    and _finite(row.get("submission_started_at")) and _finite(row.get("completed_at"))
                    and now-MAX_WINDOW <= row["submission_started_at"] <= row["completed_at"] <= now):
                eligible.append(dict(row))
        eligible.sort(key=lambda row: (row["completed_at"], row["attempt_id"]), reverse=True)
        eligible = eligible[:MAX_ATTEMPTS]
        if not eligible:
            return _empty("no_recent_successful_videos")
        key = (node_id, provider, profile_id, mode, tuple((row["job_id"], row["attempt_id"],
            row["submission_started_at"], row["completed_at"]) for row in eligible))
        with self._lock:
            tick = self.monotonic()
            cached = self._cache.get(key)
            if cached is not None and 0 <= tick-cached[0] < 60:
                self._cache.move_to_end(key)
                return dict(cached[1])
            # Serialize the optional reads so simultaneous detail requests cannot
            # duplicate a query. A failure is cached too; do not hammer outages.
            value = self._read(node_id, provider, profile_id, mode, eligible, now)
            self._cache[key] = (tick, dict(value))
            self._cache.move_to_end(key)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return value

    def _read(self, node_id, provider, profile_id, mode, eligible, now):
        start = max(0, now-MAX_WINDOW, min(row["submission_started_at"] for row in eligible)-1)
        end = min(now, max(row["completed_at"] for row in eligible)+1)
        # UUIDs are parsed JSON filters, never indexed labels. Fixed service names
        # include the original acceptance/exporter path; ledger qualification
        # still excludes native-only P2 jobs that did not produce business media.
        ids = "|".join(row["attempt_id"] for row in eligible)
        query = ('{service_name=~"sixnine-worker|sixnine-acceptance"} | json | __error__="" '
            '| event="stage_finished" | stage="generate" | outcome="success" '
            '| simulation="false" | node_id='+json.dumps(node_id)
            +' | provider='+json.dumps(provider)+' | profile_id='+json.dumps(profile_id)
            +' | mode='+json.dumps(mode)+' | attempt_id=~'+json.dumps(ids))
        path = "/loki/api/v1/query_range?" + urlencode({"query": query,
            "start": str(int(start*1_000_000_000)), "end": str(int(end*1_000_000_000)),
            "limit": str(MAX_ROWS), "direction": "backward"})
        try:
            result = self.transport(self.config, path, 1.0)
            if (type(result) is not dict or result.get("status") != "success"
                    or type(result.get("data")) is not dict
                    or result["data"].get("resultType") != "streams"
                    or type(result["data"].get("result")) is not list):
                return _empty("query_unavailable")
            rows = []
            for stream in result["data"]["result"]:
                if type(stream) is not dict or type(stream.get("values")) is not list:
                    return _empty("query_unavailable")
                rows.extend(stream["values"])
                if len(rows) >= MAX_ROWS:
                    # A truncated log result cannot prove deduplication/coverage.
                    return _empty("query_truncated")
            value = project_generate(rows, node_id=node_id, provider=provider,
                profile_id=profile_id, mode=mode, attempts=eligible, start=start, end=end)
            value.update(sample_window_start=start, sample_window_end=end, stage_observed_at=now)
            return value
        except Exception:
            return _empty("query_unavailable")


def project_generate(rows, *, node_id, provider, profile_id, mode, attempts, start, end):
    """Recheck all untrusted Cloud identity fields and deduplicate safe finishes."""
    qualified = {(row["job_id"], row["attempt_id"]): row for row in attempts}
    stages, conflicts = {}, set()
    for item in rows:
        try:
            if type(item) is not list or len(item) < 2 or not isinstance(item[1], str) or len(item[1]) > 16384:
                continue
            stamp, value = int(item[0])/1_000_000_000, json.loads(item[1])
            if type(value) is not dict:
                continue
            original = qualified.get((value.get("job_id"), value.get("attempt_id")))
            stage_id, duration = value.get("stage_id"), value.get("duration_seconds")
            interval_start = value.get("interval_start_unix_ns")
            if (original is None or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                    or value.get("event") != "stage_finished" or value.get("stage") != "generate"
                    or value.get("outcome") != "success" or value.get("simulation") is not False
                    or value.get("node_id") != node_id or value.get("provider") != provider
                    or value.get("profile_id") != profile_id or value.get("mode") != mode
                    or value.get("timing_basis") != "durable_cpu_interval"
                    or not isinstance(stage_id, str) or not _STAGE.fullmatch(stage_id)
                    or not _finite(duration) or not 0 < duration <= MAX_WINDOW
                    or type(interval_start) is not int
                    or abs(interval_start/1_000_000_000-original["submission_started_at"]) > .01
                    or not start <= stamp <= end
                    or not original["submission_started_at"] <= stamp <= original["completed_at"]+1
                    or duration > original["completed_at"]-original["submission_started_at"]+1):
                continue
            projected = (value["job_id"], value["attempt_id"], float(duration))
            if stage_id in stages and stages[stage_id] != projected:
                conflicts.add(stage_id)
            stages.setdefault(stage_id, projected)
        except (ValueError, TypeError, OverflowError):
            continue
    by_attempt = {}
    for stage_id, (job_id, attempt_id, duration) in stages.items():
        if stage_id not in conflicts:
            by_attempt.setdefault((job_id, attempt_id), set()).add(duration)
    # Recovery may export a finish twice with a new stage ID. Identical original
    # interval measurements are one video; conflicting intervals remain unknown.
    durations = [next(iter(values)) for values in by_attempt.values() if len(values) == 1]
    if not durations:
        return _empty("no_matching_stage_evidence")
    return {**_empty("ok"), "average_generate_seconds": sum(durations)/len(durations),
        "generate_samples": len(durations),
        "generation_basis": "grafana_loki_submit_to_upstream_terminal"}


_configured_lock, _configured = threading.Lock(), OrderedDict()


def configured_stage_reader(filename):
    """Load an explicit optional read mount; no network or environment mutation."""
    if not filename:
        return NullStageReader()
    try:
        path = Path(filename)
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
        with _configured_lock:
            if key not in _configured:
                config = read_config(filename)
                _configured[key] = CloudStageReader(config) if config else NullStageReader("disabled")
                while len(_configured) > 4:
                    _configured.popitem(last=False)
            return _configured[key]
    except (OSError, TypeError, ValueError):
        return NullStageReader("config_unavailable")

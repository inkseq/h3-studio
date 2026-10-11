"""Read-only broker-consumer proof, separate from native GPU readiness.

Only the pinned SDK's public WorkersClient.list/get are used. A broker observation
does not claim a job, alter a lease, or prove an upstream inference stopped.
It observes public eligibility, not a guarantee of future stream/job delivery.
Imports/construction do not read credentials or contact the broker.
"""
from __future__ import annotations

from datetime import datetime
from importlib.metadata import version
import math
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from .hatchet_dispatch import ROUTE, create_client, worker_labels
from .inference.wangp_contract import EngineManifest
from .inference.wangp_factory import read_document
from .repository import request_hash
from .runtime_catalog import validate_manifest
from .worker_admission import BROKER_HEARTBEAT_SECONDS

SDK_VERSION = "1.42.1"
POLL_SECONDS = 15
HEARTBEAT_SECONDS = BROKER_HEARTBEAT_SECONDS
MAX_SLOTS = 128
MAX_RESPONSE_BYTES = 1024 * 1024
_HTTP_REASONS = {
    401: "hatchet_consumer_http_unauthorized",
    403: "hatchet_consumer_http_forbidden",
    404: "hatchet_consumer_http_not_found",
    429: "hatchet_consumer_http_rate_limited",
}
_SAFE_RESPONSE_REASONS = frozenset((*_HTTP_REASONS.values(),
    "hatchet_consumer_http_unavailable", "hatchet_consumer_http_redirect_rejected",
    "hatchet_consumer_http_rejected", "hatchet_consumer_response_too_large"))


class _ObservationError(ValueError):
    """Closed diagnostics only; never retain an HTTP body, URL or SDK error."""
    def __init__(self, reason_code):
        if reason_code not in _SAFE_RESPONSE_REASONS:
            raise ValueError("invalid_hatchet_observation_code")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _workers_client(broker_config):
    """Public list/exact-detail APIs with a bounded read-only SDK transport.

    1.42.1 WorkersClient.list does not expose request timeout/redirect options.
    Its public ApiClient extension therefore supplies those explicitly, while
    leaving the SDK's authentication, endpoint construction and schema parsing
    intact. Retry/log hooks are disabled for this observation client only.
    """
    if version("hatchet-sdk") != SDK_VERSION:
        raise ValueError("hatchet_readiness_sdk_unpinned")
    from hatchet_sdk.clients.rest.api_client import ApiClient
    from hatchet_sdk.clients.rest.rest import RESTResponse
    from hatchet_sdk.config import TenacityConfig
    from hatchet_sdk.features.workers import WorkersClient
    import urllib3

    sdk = create_client(broker_config)
    config = sdk.config.model_copy(deep=True)
    config.tenacity = TenacityConfig(_env_file=None, max_attempts=0)
    origin = urlsplit(config.server_url)
    list_path = "/api/v1/tenants/" + config.tenant_id + "/worker"

    class ReadOnlyClient(ApiClient):
        def __init__(self, configuration, expected_path):
            super().__init__(configuration)
            self.expected_path = expected_path

        def call_api(self, method, url, header_params=None, body=None,
                     post_params=None, _request_timeout=None):
            parts = urlsplit(url)
            if (method != "GET" or parts.scheme != origin.scheme or parts.netloc != origin.netloc
                    or parts.path != self.expected_path or parts.fragment or body or post_params):
                raise ValueError("hatchet_readiness_endpoint_forbidden")
            raw = self.rest_client.pool_manager.request("GET", url, headers=header_params,
                timeout=urllib3.Timeout(total=3), retries=False, redirect=False, preload_content=False)
            try:
                if raw.status != 200:
                    reason = _HTTP_REASONS.get(raw.status, "hatchet_consumer_http_rejected")
                    if type(raw.status) is int and 500 <= raw.status <= 599:
                        reason = "hatchet_consumer_http_unavailable"
                    elif type(raw.status) is int and 300 <= raw.status <= 399:
                        reason = "hatchet_consumer_http_redirect_rejected"
                    raise _ObservationError(reason)
                data = raw.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise _ObservationError("hatchet_consumer_response_too_large")
                response = RESTResponse(raw)
                response.data = data
                return response
            finally:
                raw.close()
                raw.release_conn()

    class BoundedWorkersClient(WorkersClient):
        def __init__(self, configuration, expected_path=list_path):
            super().__init__(configuration)
            self.expected_path = expected_path
            self.api_config.retries = False

        def client(self):
            return ReadOnlyClient(self.api_config, self.expected_path)

        def get(self, worker_id):
            identifier = _worker_uuid(worker_id)
            if identifier is None:
                raise ValueError("hatchet_readiness_worker_invalid")
            # Bind this SDK get to one selected UUID. The transport cannot read
            # another worker, a different origin, or any mutation endpoint.
            detail = BoundedWorkersClient(self.client_config, "/api/v1/workers/" + identifier)
            return WorkersClient.get(detail, identifier)

    result = BoundedWorkersClient(config)
    result.api_config.retries = False
    return result


def _namespace(config, name):
    namespace = config.namespace.lower()
    if not namespace.endswith("_"):
        namespace += "_"
    return name if name.startswith(namespace) else namespace + name


def _expectation(config, slot):
    spec = slot.spec
    if (slot.enabled is not True or spec.dispatch_backend != ROUTE or spec.backend != "wangp-worker"
            or len(spec.physical_gpu_ids) != 1):
        raise ValueError("hatchet_readiness_slot_invalid")
    document = read_document(slot.runtime_config_file, maximum=16384)
    if (document.get("version") != 1 or document.get("enabled") is not True
            or document.get("configuration_id") != spec.configuration_id):
        raise ValueError("hatchet_readiness_slot_invalid")
    manifest = EngineManifest.from_dict(read_document(document["manifest_file"]))
    profile = validate_manifest(manifest)
    if manifest.digest != spec.engine_manifest_digest or profile["model_id"] != spec.model_id:
        raise ValueError("hatchet_readiness_manifest_changed")
    value = {"pool": spec.pool, "configuration_id": spec.configuration_id,
        "engine_manifest_digest": manifest.digest,
        "deployment_profile_id": manifest.document["deployment_profile_id"],
        "mode": manifest.document["mode"], "backend": spec.backend}
    expected = worker_labels(value, spec.worker_id)
    name = _namespace(config, spec.worker_id)
    workflow = _namespace(config, "sixnine-generation-" + request_hash(value)[:24])
    return name, expected, workflow


def _field(value, key, default=None):
    return value.get(key, default) if type(value) is dict else getattr(value, key, default)


def _enum(value):
    return getattr(value, "value", value)


def _complete_page(rows, pagination):
    if pagination is None:
        return True
    current, pages, next_page = (_field(pagination, key)
        for key in ("current_page", "num_pages", "next_page"))
    if type(current) is not int or current != 1 or type(pages) is not int:
        return False
    if next_page is not None and type(next_page) is not int:
        return False
    # Pinned 0.110.5 uses next_page=current_page on its final page, and
    # reports (1, 0, 2) for an empty list. Neither is an unobserved next page.
    return (pages == 1 and next_page in (None, 1)
        or pages == 0 and not rows and next_page in (None, 2))


def _heartbeat(value, now):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    stamp = value.timestamp()
    return stamp if math.isfinite(stamp) and 0 <= now-stamp <= HEARTBEAT_SECONDS else None


def _worker_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if parsed.int != 0 and str(parsed) == value else None


def _row_reason(row, name, expected, now):
    if _field(row, "name") != name or _enum(_field(row, "status")) != "ACTIVE":
        return "hatchet_consumer_unconfirmed"
    if _enum(_field(row, "type")) != "SELFHOSTED":
        return "hatchet_consumer_type_mismatch"
    entries = _field(row, "labels")
    actual = {_field(label, "key"): _field(label, "value") for label in entries} if isinstance(entries, list) else {}
    if not isinstance(entries, list) or len(actual) != len(entries) or any(actual.get(k) != v for k,v in expected.items()):
        return "hatchet_consumer_labels_mismatch"
    if _heartbeat(_field(row, "last_heartbeat_at"), now) is None:
        return "hatchet_consumer_heartbeat_stale"
    return None


class BrokerReadiness:
    """A 15-second per-slot read budget; only a closed projection is retained.

    probe(slot) is the boolean admission callback. projection(slot) returns
    ready/worker_id/observed_at/heartbeat_at/reason_code only. Errors invalidate the new
    observation and never surface SDK response text, URLs, labels or secrets.
    """
    def __init__(self, broker_config, clock=time.time, client_factory=_workers_client):
        self.config, self.clock, self.client_factory = broker_config, clock, client_factory
        self._client, self._cache, self._lock = None, {}, threading.Lock()

    def probe(self, slot):
        return self.projection(slot)["ready"]

    def projection(self, slot):
        now = self.clock()
        worker_id = getattr(getattr(slot, "spec", None), "worker_id", None)
        worker_id = worker_id if isinstance(worker_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_id) else None
        result = {"ready": False, "worker_id": worker_id, "observed_at": now, "heartbeat_at": None,
                  "reason_code": "hatchet_consumer_unconfirmed"}
        try:
            if type(now) not in (int, float) or not math.isfinite(now):
                raise ValueError("invalid_clock")
            name, expected, workflow = _expectation(self.config, slot)
            key = request_hash({"worker": name, "labels": expected, "workflow": workflow})
        except Exception:
            result.update(observed_at=now if type(now) in (int, float) and math.isfinite(now) else None,
                          reason_code="hatchet_consumer_slot_invalid")
            return result
        with self._lock:
            prior = self._cache.get(key)
            if prior and now < prior["observed_at"]:
                result["reason_code"] = "hatchet_consumer_clock_unconfirmed"
                return result
            if prior and 0 <= now-prior["observed_at"] < POLL_SECONDS:
                cached = dict(prior)
                if cached["ready"] and not 0 <= now-cached["heartbeat_at"] <= HEARTBEAT_SECONDS:
                    cached.update(ready=False, reason_code="hatchet_consumer_heartbeat_stale")
                return cached
            if key not in self._cache and len(self._cache) >= MAX_SLOTS:
                self._cache = {k: v for k,v in self._cache.items()
                    if now-v["observed_at"] <= HEARTBEAT_SECONDS}
                if len(self._cache) >= MAX_SLOTS:
                    result["reason_code"] = "hatchet_consumer_observation_limit"
                    return result
            try:
                if self._client is None:
                    self._client = self.client_factory(self.config)
                response = self._client.list()
                observed = self.clock()
                if type(observed) not in (int, float) or not math.isfinite(observed) or observed < now:
                    raise ValueError("invalid_observed_clock")
                now = observed
                result["observed_at"] = now
                rows = _field(response, "rows")
                pagination = _field(response, "pagination")
                if (not isinstance(rows, list) or len(rows) > 1024
                        or not _complete_page(rows, pagination)):
                    raise ValueError("ambiguous_list")
                active = [row for row in rows if _field(row, "name") == name
                    and _enum(_field(row, "status")) == "ACTIVE"]
                if len(active) > 1:
                    result["reason_code"] = "hatchet_consumer_ambiguous"
                elif len(active) == 1:
                    row = active[0]
                    reason = _row_reason(row, name, expected, now)
                    identifier = _worker_uuid(_field(_field(row, "metadata"), "id"))
                    if reason is not None:
                        result["reason_code"] = reason
                    elif identifier is None:
                        result["reason_code"] = "hatchet_consumer_identity_unconfirmed"
                    else:
                        # Pinned 0.110.5 list omits registeredWorkflows; neither
                        # REST transformer populates lastListenerEstablished.
                        # Exact detail supplies the supported workflow proof.
                        detail = self._client.get(identifier)
                        observed = self.clock()
                        if type(observed) not in (int, float) or not math.isfinite(observed) or observed < now:
                            raise ValueError("invalid_observed_clock")
                        now = observed
                        result["observed_at"] = now
                        reason = _row_reason(detail, name, expected, now)
                        heartbeat = _heartbeat(_field(detail, "last_heartbeat_at"), now)
                        workflows = _field(detail, "registered_workflows")
                        if _worker_uuid(_field(_field(detail, "metadata"), "id")) != identifier:
                            result["reason_code"] = "hatchet_consumer_identity_unconfirmed"
                        elif reason is not None:
                            result["reason_code"] = reason
                        elif _worker_uuid(_field(detail, "dispatcher_id")) is None:
                            result["reason_code"] = "hatchet_consumer_listener_unconfirmed"
                        elif not isinstance(workflows, list) or not any(_field(item, "name") == workflow for item in workflows):
                            result["reason_code"] = "hatchet_consumer_workflow_unconfirmed"
                        else:
                            result.update(ready=True, heartbeat_at=heartbeat, reason_code="hatchet_consumer_ready")
            except _ObservationError as error:
                result.update(ready=False, heartbeat_at=None, reason_code=error.reason_code)
            except Exception:
                result.update(ready=False, heartbeat_at=None, reason_code="hatchet_consumer_observation_failed")
            self._cache[key] = dict(result)
            return result

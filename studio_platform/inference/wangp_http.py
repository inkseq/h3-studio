"""Authenticated private loopback transport, normally carried by an SSH tunnel.

No retries of generation POSTs, redirects, proxy environment or network on import.
The token is process configuration and is never part of a manifest or receipt.
"""

from dataclasses import asdict
from urllib.parse import urlsplit
import re
import time

import httpx

from .protocol import BackendError, SubmissionUncertain
from .wangp_contract import (
    HostReadiness,
    InputDescriptor,
    OperationReceipt,
    canonical_json,
)

_OP = re.compile(r"wangp-[A-Za-z0-9_-]{1,80}\Z")


def private_endpoint(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("wangp_loopback_tunnel_required")
    return value.rstrip("/")


def _operation(value):
    if not isinstance(value, str) or not _OP.fullmatch(value):
        raise ValueError("wangp_invalid_operation")
    return value


class HTTPWanGPTransport:
    def __init__(
        self,
        endpoint,
        token,
        *,
        transport=None,
        timeout=30,
        max_transfer_seconds=180,
        expected_incarnation=None,
    ):
        self.endpoint = private_endpoint(endpoint)
        if (
            not isinstance(token, str)
            or len(token) < 32
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ValueError("wangp_private_token_required")
        self._token, self._transport = token, transport
        if expected_incarnation is not None and (
            not isinstance(expected_incarnation, str)
            or not re.fullmatch(r"[0-9a-f]{32}", expected_incarnation)
        ):
            raise ValueError("wangp_invalid_runtime_incarnation")
        self.expected_incarnation = expected_incarnation
        self._client = None
        self.timeout = timeout
        if not 0 < max_transfer_seconds < float("inf"):
            raise ValueError("wangp_invalid_transfer_deadline")
        self.max_transfer_seconds = max_transfer_seconds

    def _http(self):
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.endpoint,
                transport=self._transport,
                headers={"Authorization": "Bearer " + self._token},
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    def _json(
        self,
        method,
        path,
        *,
        value=None,
        missing=False,
        submission=False,
        timeout_seconds=None,
    ):
        try:
            started = time.monotonic()
            headers = {"Content-Type": "application/json"}
            if (
                method == "POST"
                and path == "/v1/operations"
                and self.expected_incarnation is not None
            ):
                # A readiness GET is only an observation. The receiving host
                # must reject a replacement epoch before dispatching a POST.
                headers["X-Wangp-Incarnation"] = self.expected_incarnation
            with self._http().stream(
                method,
                path,
                content=canonical_json(value).encode() if value is not None else None,
                headers=headers,
                **({"timeout": timeout_seconds} if timeout_seconds is not None else {}),
            ) as response:
                if missing and response.status_code == 404:
                    return None
                # Even rejection of this POST cannot disprove a prior accepted
                # response that was lost. Reconcile the durable operation ID.
                if response.status_code != 200:
                    raise BackendError("wangp_private_request_failed")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024 or time.monotonic() - started > (
                        timeout_seconds or self.max_transfer_seconds
                    ):
                        raise BackendError("wangp_private_response_limit")
                import json

                return json.loads(data)
        except Exception:
            if submission:
                raise SubmissionUncertain("wangp_private_submission_unknown") from None
            raise BackendError("wangp_private_request_failed") from None

    def submit(self, prepared):
        try:
            return OperationReceipt.from_dict(
                self._json(
                    "POST", "/v1/operations", value=asdict(prepared), submission=True
                )
            )
        except SubmissionUncertain:
            raise
        except Exception:
            raise SubmissionUncertain("wangp_invalid_submit_receipt") from None

    def inspect(self, operation_id):
        value = self._json(
            "GET", "/v1/operations/" + _operation(operation_id), missing=True
        )
        return None if value is None else OperationReceipt.from_dict(value)

    def prepare_delivery(self, operation_id, request):
        return self._json(
            'POST',
            '/v1/operations/' + _operation(operation_id) + '/delivery/prepare',
            value=request,
            timeout_seconds=900,
        )

    def upload_delivery(self, operation_id, identity, fingerprint, targets):
        return self._json(
            'POST',
            '/v1/operations/' + _operation(operation_id) + '/delivery/upload',
            value={
                'identity': identity,
                'fingerprint': fingerprint,
                'targets': targets,
            },
            timeout_seconds=400,
        )

    def delivery_status(self, operation_id, identity):
        from urllib.parse import urlencode

        query = {k: v for k, v in identity.items() if k != 'operation_id'}
        return self._json(
            'GET',
            '/v1/operations/'
            + _operation(operation_id)
            + '/delivery?'
            + urlencode(query),
            missing=True,
        )

    def cancel(self, operation_id):
        value = self._json(
            "POST", "/v1/operations/" + _operation(operation_id) + "/cancel"
        )
        return isinstance(value, dict) and value.get("acknowledged") is True

    def readiness(self):
        value = self._json("GET", "/v1/readiness")
        return HostReadiness(**value)

    def read_artifact(self, operation_id, kind):
        if kind not in {"video", "audio"}:
            raise ValueError("wangp_invalid_artifact_kind")
        try:
            started = time.monotonic()
            with self._http().stream(
                "GET",
                "/v1/operations/" + _operation(operation_id) + "/artifacts/" + kind,
            ) as response:
                if response.status_code != 200:
                    raise BackendError("wangp_artifact_unavailable")
                for chunk in response.iter_bytes(1024 * 1024):
                    if time.monotonic() - started > self.max_transfer_seconds:
                        raise BackendError("wangp_artifact_transfer_deadline")
                    yield chunk
        except Exception:
            raise BackendError("wangp_artifact_unavailable") from None

    def stage_input(self, descriptor, source, heartbeat=lambda: None):
        if not isinstance(descriptor, InputDescriptor):
            raise ValueError("wangp_invalid_input")
        signed = getattr(source, "signed_url", None)
        url = signed() if signed is not None else None
        if url is not None and self._fetch_input(descriptor, url, heartbeat):
            return descriptor

        def chunks():
            started = time.monotonic()
            remaining = descriptor.size_bytes
            while remaining:
                if time.monotonic() - started > self.max_transfer_seconds:
                    raise BackendError("wangp_input_transfer_deadline")
                heartbeat()
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise BackendError("wangp_input_truncated")
                remaining -= len(chunk)
                yield chunk
            if source.read(1):
                raise BackendError("wangp_input_size_mismatch")

        try:
            started = time.monotonic()
            with self._http().stream(
                "PUT",
                "/v1/inputs/" + descriptor.handle,
                headers={
                    "X-Wangp-Input": canonical_json(asdict(descriptor)),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(descriptor.size_bytes),
                },
                content=chunks(),
            ) as response:
                if response.status_code != 200:
                    raise BackendError("wangp_input_stage_failed")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if (
                        len(body) > 16384
                        or time.monotonic() - started > self.max_transfer_seconds
                    ):
                        raise BackendError("wangp_input_stage_failed")
                import json

                if json.loads(body) != asdict(descriptor):
                    raise BackendError("wangp_input_stage_failed")
        except Exception:
            raise BackendError("wangp_input_stage_failed") from None
        return descriptor

    def _fetch_input(self, descriptor, url, heartbeat):
        """True once the host pulled and verified it; False for an older host."""
        heartbeat()
        try:
            response = self._http().post(
                "/v1/inputs/" + descriptor.handle + "/fetch",
                content=canonical_json(
                    {"input": asdict(descriptor), "url": url}
                ).encode(),
                headers={"Content-Type": "application/json"},
                timeout=self.max_transfer_seconds,
            )
            if response.status_code == 404:
                return False
            if response.status_code != 200 or response.json() != asdict(descriptor):
                raise BackendError("wangp_input_stage_failed")
        except BackendError:
            raise
        except Exception:
            raise BackendError("wangp_input_stage_failed") from None
        return True

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

"""Small streaming HTTP client shared by vendor execution adapters.

Adapters own fixed URLs, credentials, request encoding and outcome decisions.
This client never retries or follows redirects, including uncertain submissions.
"""

from contextlib import closing
import json

import httpx


class VendorHTTP:
    def __init__(self, *, timeout, transport=None):
        self.timeout, self.transport = timeout, transport
        self._client = None

    def session(self):
        if self._client is None:
            self._client = httpx.Client(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
            )
        return self._client

    def build_request(self, *args, **kwargs):
        return self.session().build_request(*args, **kwargs)

    def send(self, request):
        # Keep Client.send as the VCR replay boundary for hosted acceptance.
        return self.session().send(request, stream=True)

    def json(self, request, *, limit, parse_error_body=False, empty_body=False):
        with closing(self.send(request)) as response:
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("vendor_response_too_large")
            data = None
            if parse_error_body or 200 <= response.status_code < 300:
                data = json.loads(content) if content or not empty_body else None
            if data is not None and type(data) is not dict:
                raise ValueError("vendor_response_object_required")
            return response.status_code, data

    def close(self, *, close_unused_transport=False):
        if self._client is not None:
            self._client.close()
        elif close_unused_transport and self.transport is not None:
            self.transport.close()
        self._client, self.transport = None, None

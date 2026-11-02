"""Pinned dstack/provider read transport and single original create request."""

from __future__ import annotations

import logging

import json
import math
import re
import random
import time
from urllib.parse import urlsplit

import httpx

from .dstack_control_plane import provider_retry_after
from .capacity_memory import vast_ram_floor
from .providers.registry import VASTAI

from .dstack_inventory import (
    CLOUDRIFT_INSTANCES_URL,
    KEY_HEADER_URLS,
    LIUM_PODS_URL,
    RUNPOD_GRAPHQL_URL,
    InventoryUnconfirmed,
    read_ids,
)

from .dstack_request import (
    BACKENDS,
    VAST_INSTANCES_URL,
    PROVIDER_READ_ATTEMPTS,
    PROVIDER_READ_MAX_DELAY,
    DstackError,
    DstackReadRateLimited,
    _require,
    _uuid,
)

_logger = logging.getLogger(__name__)


class DstackClient:
    """Exact 0.22.5 REST requests, no automatic side-effect retry."""

    def __init__(
        self,
        endpoint,
        token,
        project,
        *,
        session=None,
        timeout=30,
        provider_session=None,
    ):
        parsed = urlsplit(endpoint)
        _require(
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"},
            "dstack_endpoint_invalid",
        )
        _require(
            not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"},
            "dstack_endpoint_invalid",
        )
        _require(
            isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", project),
            "dstack_project_invalid",
        )
        _require(
            isinstance(token, str)
            and 16 <= len(token) <= 4096
            and not any(c.isspace() for c in token),
            "dstack_token_invalid",
        )
        _require(
            type(timeout) in (int, float) and 0 < timeout <= 120,
            "dstack_timeout_invalid",
        )
        self.endpoint, self.project, self.timeout = (
            endpoint.rstrip("/"),
            project,
            timeout,
        )
        self._headers = {"Authorization": "Bearer " + token}
        self._session = session or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_session = session is None
        self._provider_session, self._owns_provider_session = (
            provider_session,
            provider_session is None,
        )

    def close(self):
        if self._owns_session:
            self._session.close()
        if self._owns_provider_session and self._provider_session is not None:
            self._provider_session.close()

    def _post(self, path, body, *, absent=False, timeout=None):
        try:
            if timeout is None:
                response = self._session.post(
                    self.endpoint + path,
                    json=body,
                    headers=self._headers,
                    timeout=self.timeout,
                )
            else:
                deadline = time.monotonic() + min(timeout, self.timeout)
                with self._session.stream(
                    "POST",
                    self.endpoint + path,
                    json=body,
                    headers=self._headers,
                    timeout=min(timeout, self.timeout),
                    follow_redirects=False,
                ) as incoming:
                    content = bytearray()
                    for chunk in incoming.iter_bytes(chunk_size=65536):
                        _require(
                            time.monotonic() < deadline
                            and len(content) + len(chunk) <= 8 * 1024**2,
                            "dstack_response_limit",
                        )
                        content.extend(chunk)
                    _require(time.monotonic() < deadline, "dstack_api_unavailable")
                    response = httpx.Response(
                        incoming.status_code, content=bytes(content)
                    )
            if absent and response.status_code == 404:
                return None
            _require(len(response.content) <= 8 * 1024**2, "dstack_response_limit")
            # Pinned 0.22.5 returns its structured NotExistsError as HTTP 400.
            # Only an exact optional read may interpret that sole error as
            # absence; never treat other errors or a rejected mutation so.
            if absent and response.status_code == 400:
                document = response.json()
                detail = document.get("detail") if isinstance(document, dict) else None
                if (
                    isinstance(detail, list)
                    and len(detail) == 1
                    and isinstance(detail[0], dict)
                    and detail[0].get("code") == "resource_not_exists"
                ):
                    return None
            _require(
                response.status_code < 300 and response.status_code >= 200,
                "dstack_api_rejected",
            )
            if not response.content:
                return {}
            return response.json()
        except DstackError:
            raise
        except Exception as _observation_error:
            _logger.warning(
                "dstack_client_post_failed error_type=%s",
                type(_observation_error).__name__,
            )
            raise DstackError("dstack_api_unavailable") from None

    def get(self, *, run_name=None, run_id=None):
        _require(bool(run_name) != bool(run_id), "dstack_exact_identity_required")
        if run_id:
            _uuid(run_id)
        else:
            _require(
                isinstance(run_name, str)
                and re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name),
                "dstack_run_name_invalid",
            )
        return self._post(
            f"/api/project/{self.project}/runs/get",
            {"id": run_id} if run_id else {"run_name": run_name},
            absent=True,
        )

    def _provider_document(self, method, url, key, *, timeout, body=None, params=None):
        _require(
            (method, url)
            in {
                ("GET", VAST_INSTANCES_URL),
                ("POST", RUNPOD_GRAPHQL_URL),
                ("GET", LIUM_PODS_URL),
                ("POST", CLOUDRIFT_INSTANCES_URL),
                ("POST", "https://console.vast.ai/api/v0/bundles/"),
                ("GET", "https://console.vast.ai/api/v0/charges/"),
            }
            or method == "GET"
            and re.fullmatch(
                r"https://console\.vast\.ai/api/v0/instances/[1-9][0-9]{0,17}/", url
            ),
            "dstack_provider_inventory_unconfirmed",
        )
        if self._provider_session is None:
            self._provider_session = httpx.Client(
                follow_redirects=False, trust_env=False
            )

        def unique_object(pairs):
            value = {}
            for name, item in pairs:
                _require(name not in value, "dstack_provider_inventory_unconfirmed")
                value[name] = item
            return value

        def invalid_constant(_):
            raise DstackError("dstack_provider_inventory_unconfirmed")

        try:
            deadline = time.monotonic() + timeout
            for attempt in range(PROVIDER_READ_ATTEMPTS):
                remaining = deadline - time.monotonic()
                _require(remaining > 0, "dstack_provider_inventory_unavailable")
                with self._provider_session.stream(
                    method,
                    url,
                    headers={"X-API-Key": key}
                    if url in KEY_HEADER_URLS
                    else {"Authorization": "Bearer " + key},
                    json=body,
                    params=params,
                    timeout=remaining,
                    follow_redirects=False,
                ) as response:
                    # These exact allowlisted endpoints are reads, including
                    # search/GraphQL POSTs. Mutations use _post and never retry.
                    if response.status_code == 429:
                        retry_after = provider_retry_after(
                            response.headers.get("Retry-After")
                        )
                    else:
                        _require(
                            response.status_code == 200,
                            "dstack_provider_inventory_rejected",
                        )
                        content = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            _require(
                                time.monotonic() < deadline
                                and len(content) + len(chunk) <= 4 * 1024**2,
                                "dstack_provider_inventory_response_limit",
                            )
                            content.extend(chunk)
                        _require(
                            time.monotonic() < deadline,
                            "dstack_provider_inventory_unavailable",
                        )
                        return json.loads(
                            content,
                            object_pairs_hook=unique_object,
                            parse_constant=invalid_constant,
                        )
                # Close the refused response before sleeping. A long server
                # delay is surfaced rather than retried earlier than requested.
                delay = retry_after if retry_after is not None else 2**attempt
                if (
                    attempt + 1 == PROVIDER_READ_ATTEMPTS
                    or delay > PROVIDER_READ_MAX_DELAY
                ):
                    raise DstackReadRateLimited(retry_after)
                delay += random.uniform(0, 0.25)
                if delay >= deadline - time.monotonic():
                    raise DstackReadRateLimited(retry_after)
                time.sleep(delay)
        except DstackError:
            raise
        except Exception as _observation_error:
            _logger.warning(
                "dstack_client_provider_document_failed error_type=%s",
                type(_observation_error).__name__,
            )
            raise DstackError("dstack_provider_inventory_unavailable") from None

    def _vast_key(self, *, timeout=None):
        # The exact existing dstack backend remains credential authority.
        # No registry/profile switch, key persistence or process env mutation.
        config = self._post(
            f"/api/project/{self.project}/backends/vastai/config_info",
            {},
            **({"timeout": timeout} if timeout is not None else {}),
        )
        creds = config.get("creds") if isinstance(config, dict) else None
        _require(
            isinstance(config, dict)
            and config.get("type") == VASTAI.id
            and isinstance(creds, dict)
            and creds.get("type") == "api_key",
            "dstack_host_account_unconfirmed",
        )
        key = creds.get("api_key")
        _require(
            isinstance(key, str)
            and 16 <= len(key) <= 4096
            and not any(c.isspace() for c in key),
            "dstack_host_account_unconfirmed",
        )
        return key

    def vast_offer_mapping(self, request, *, clock=time.time):
        """One search-only POST; mirror the installed gpuhunt 0.1.33 body.

        This is its direct filter JSON, not the distinct Vast CLI q wrapper.
        Unknown physical IDs are omitted; an offer ID is never a machine ID.
        Native planning still qualifies resources and its verified-first filter.
        """
        _require(request.backend == VASTAI.id, "dstack_host_backend_invalid")
        observed = clock()
        query = {
            "direct_port_count": {"gte": 1},
            "reliability2": {"gte": request.min_reliability},
            "inet_down": {"gte": 1000},
            "verified": {"eq": True},
            "cuda_max_good": {"gte": 13.0},
            "compute_cap": {"gte": 600},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "num_gpus": {"eq": 1},
            "cpu_cores": {"gte": request.cpu_count},
            "cpu_ram": {"gte": vast_ram_floor(request)},
            "gpu_ram": {"gte": request.gpu_memory_gib * 1024 * 0.93},
            "disk_space": {"gte": request.disk_gib},
            "dph_total": {"lte": request.max_price_microusd / 1_000_000},
            "order": [["dph_base", "asc"]],
            "limit": 1000,
        }
        # Exact gpuhunt aliases; the normal backend plan does final GPU matching.
        names = []
        for name in request.gpu_names:
            if re.fullmatch(r"RTX[0-9]{4}[A-Za-z]?", name):
                names.append("RTX " + name[3:])
        if names and len(names) == len(request.gpu_names):
            query["gpu_name"] = {"in": names}
        document = self._provider_document(
            "POST",
            "https://console.vast.ai/api/v0/bundles/",
            self._vast_key(),
            timeout=min(self.timeout, 25),
            body=query,
        )
        _require(
            isinstance(document, dict)
            and isinstance(document.get("offers"), list)
            and len(document["offers"]) <= 1000,
            "dstack_host_offers_unconfirmed",
        )
        found = {}
        for row in document["offers"]:
            if not isinstance(row, dict):
                raise DstackError("dstack_host_offers_unconfirmed")
            offer, machine = row.get("id"), row.get("machine_id")
            if (
                type(offer) is not int
                or offer <= 0
                or type(machine) is not int
                or machine <= 0
            ):
                continue
            bandwidth = row.get("inet_down")
            if not (
                type(bandwidth) in (int, float)
                and math.isfinite(bandwidth)
                and 1000 <= bandwidth <= 1_000_000_000
            ):
                continue
            # Vast verification has appeared as a string as well as boolean.
            # Missing is unobserved, never projected as verified=False. The
            # exact verified-first search/native plan remains authoritative.
            verified = row.get("verified")
            if verified is False or verified in ("unverified", "deverified"):
                continue
            # The native Resources/JPD model omits advertised network speed.
            # Retain only bounded facts from this exact, frozen offer instead.
            location = row.get("geolocation")
            country, region = row.get("country"), None
            if isinstance(location, str) and 0 < len(location) <= 200:
                parts = location.replace(", ", ",").split(",")
                if len(parts) == 2:
                    city, advertised_country = parts
                    country = country if country is not None else advertised_country
                    location = advertised_country + "-" + city
                normalized = location.lower().replace(" ", "")
                if re.fullmatch(r"[a-z0-9-]{1,100}", normalized):
                    region = normalized
            if not isinstance(country, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z .'-]{0,99}", country
            ):
                country = None
            item = {
                "offer_id": str(offer),
                "machine_id": str(machine),
                "country": country,
                "region": region,
                "advertised_download_mbps": bandwidth,
            }
            _require(
                str(offer) not in found or found[str(offer)] == item,
                "dstack_host_offer_identity_conflict",
            )
            found[str(offer)] = item
        ended = clock()
        _require(
            type(observed) in (int, float)
            and math.isfinite(observed)
            and observed > 0
            and type(ended) in (int, float)
            and math.isfinite(ended)
            and 0 <= ended - observed < 120,
            "dstack_host_clock_invalid",
        )
        return {"observed_at": observed, "offers": list(found.values())}

    def vast_charges(self, start, end):
        from .invoice_checks import CHARGES_URL, read_vast_charges

        # This inspection also runs during active work. Keep its total I/O
        # on2s/6s request budgets, inside the30s freshness window; no thread
        # or second poller. An incomplete read stays unknown until next check.
        key = self._vast_key(timeout=2)
        return read_vast_charges(
            lambda params, timeout: self._provider_document(
                "GET",
                CHARGES_URL,
                key,
                params=params,
                timeout=min(timeout, self.timeout, 3),
            ),
            start,
            end,
            timeout_budget=6,
        )

    def vast_instance_identity(self, identity):
        _require(
            isinstance(identity, str) and re.fullmatch(r"[1-9][0-9]{0,17}", identity),
            "dstack_host_instance_invalid",
        )
        document = self._provider_document(
            "GET",
            f"https://console.vast.ai/api/v0/instances/{identity}/",
            self._vast_key(),
            timeout=min(self.timeout, 15),
        )
        _require(isinstance(document, dict), "dstack_host_instance_unconfirmed")
        row = document.get("instances")
        if row is None:
            return None
        _require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and str(row["id"]) == identity
            and type(row.get("machine_id")) is int
            and row["machine_id"] > 0,
            "dstack_host_instance_unconfirmed",
        )
        reliability = row.get("reliability2")
        return {
            "provider_instance_id": identity,
            "machine_id": str(row["machine_id"]),
            "reliability": reliability
            if type(reliability) in (int, float)
            and math.isfinite(reliability)
            and 0 <= reliability <= 1
            else None,
        }

    def reusable_hosts_confirmed(self, approved):
        """Complete project fleet read before apply; no unknown reuse fallback.

        Native 0.22.5 skips type constraints if an existing instance has no JPD.
        Refuse every potentially reusable unknown instance; a known matching
        bundle additionally needs an exact same-account physical identity read.
        """
        fleets = self._post(
            f"/api/project/{self.project}/fleets/list", {"include_imported": True}
        )
        _require(
            isinstance(fleets, list) and len(fleets) < 1000,
            "dstack_host_reuse_unconfirmed",
        )
        count = 0
        for fleet in fleets:
            _require(
                isinstance(fleet, dict) and isinstance(fleet.get("instances"), list),
                "dstack_host_reuse_unconfirmed",
            )
            for instance in fleet["instances"]:
                count += 1
                _require(
                    count <= 4096 and isinstance(instance, dict),
                    "dstack_host_reuse_unconfirmed",
                )
                status = instance.get("status")
                _require(
                    status
                    in {
                        "pending",
                        "provisioning",
                        "idle",
                        "busy",
                        "terminating",
                        "terminated",
                    },
                    "dstack_host_reuse_unconfirmed",
                )
                if status in {"terminated", "terminating"}:
                    continue
                # Public Instance assigns instance_type only from JPD. Public
                # DTOs omit the JPD/provider ID, so do not guess those fields.
                # With no type witness native filtering skips the selector.
                data = instance.get("instance_type")
                _require(
                    instance.get("backend") in BACKENDS
                    and isinstance(data, dict)
                    and isinstance(data.get("name"), str)
                    and data["name"],
                    "dstack_host_reuse_identity_unknown",
                )
                # Known nonmatching types are rejected again by native locked
                # assignment. Matching types use the same exact bundle-ID
                # selector; final allocation still needs a same-account
                # machine-ID read before any native bootstrap/readiness.
        return True

    def backend_api_key(self, backend):
        """The account key dstack holds for this backend, for this call's memory only."""
        config = self._post(
            f"/api/project/{self.project}/backends/{backend}/config_info", {}
        )
        creds = config.get("creds") if isinstance(config, dict) else None
        _require(
            isinstance(config, dict)
            and config.get("type") == backend
            and isinstance(creds, dict)
            and creds.get("type") == "api_key",
            "dstack_provider_inventory_unconfirmed",
        )
        key = creds.get("api_key")
        _require(
            isinstance(key, str)
            and 16 <= len(key) <= 4096
            and not any(char.isspace() for char in key),
            "dstack_provider_inventory_unconfirmed",
        )
        return key

    def provider_inventory(self, backend, *, clock=None):
        """Transient complete account read using the SAME configured backend key.

        Pinned dstack's ProjectAdmin config_info read returns credentials only
        into this call's memory. They are never returned, stored, logged or sent
        to another origin. This is not stock, a rental command or reconciliation.
        Vast v1 pagination follows the current official show-instances contract;
        v0 list absence is deliberately unsupported. RunPod's public unpaginated
        User.pods query is distinct from its paginated podsConnection field.
        """
        _require(
            isinstance(backend, str) and backend in BACKENDS,
            "dstack_backend_unsupported",
        )
        clock = clock or time.time
        observed_at, started = clock(), time.monotonic()
        _require(
            type(observed_at) in (int, float)
            and math.isfinite(observed_at)
            and observed_at > 0,
            "dstack_capacity_clock_invalid",
        )
        try:
            key = self.backend_api_key(backend)

            def read(method, url, *, body=None, params=None):
                remaining = 30 - (time.monotonic() - started)
                _require(remaining > 0, "dstack_provider_inventory_unavailable")
                return self._provider_document(
                    method,
                    url,
                    key,
                    body=body,
                    params=params,
                    timeout=min(15, self.timeout, remaining),
                )

            ids, aliases, labels = [], {}, []
            if backend == VASTAI.id:
                params = {
                    "limit": 25,
                    "select_cols": '["id","label"]',
                    "order_by": '[{"col":"id","dir":"asc"}]',
                }
                cursors, total = set(), None
                for _ in range(32):
                    page = read("GET", VAST_INSTANCES_URL, params=params)
                    _require(
                        isinstance(page, dict)
                        and page.get("success") is True
                        and set(page)
                        <= {
                            "success",
                            "instances",
                            "instances_found",
                            "total_instances",
                            "next_token",
                            "label_counts",
                        },
                        "dstack_provider_inventory_unconfirmed",
                    )
                    rows, found, size = (
                        page.get("instances"),
                        page.get("instances_found"),
                        page.get("total_instances"),
                    )
                    _require(
                        isinstance(rows, list)
                        and type(found) is int
                        and found == len(rows)
                        and 0 <= found <= 25
                        and type(size) is int
                        and 0 <= size <= 800
                        and (total is None or total == size),
                        "dstack_provider_inventory_unconfirmed",
                    )
                    total = size
                    for row in rows:
                        _require(
                            isinstance(row, dict)
                            and type(row.get("id")) is int
                            and row["id"] > 0,
                            "dstack_provider_inventory_unconfirmed",
                        )
                        ids.append(str(row["id"]))
                        labels.append(row.get("label"))
                    _require(
                        len(set(ids)) == len(ids) and len(ids) <= total,
                        "dstack_provider_inventory_unconfirmed",
                    )
                    _require(
                        "next_token" in page, "dstack_provider_inventory_unconfirmed"
                    )
                    cursor = page["next_token"]
                    if cursor is None:
                        _require(
                            len(ids) == total, "dstack_provider_inventory_unconfirmed"
                        )
                        break
                    _require(
                        found > 0
                        and isinstance(cursor, str)
                        and re.fullmatch(r"[A-Za-z0-9+/=_-]{1,4096}", cursor)
                        and cursor not in cursors,
                        "dstack_provider_inventory_unconfirmed",
                    )
                    cursors.add(cursor)
                    params["after_token"] = cursor
                else:
                    raise DstackError("dstack_provider_inventory_unconfirmed")
            else:
                try:
                    ids, aliases, labels = read_ids(backend, read)
                except InventoryUnconfirmed:
                    raise DstackError("dstack_provider_inventory_unconfirmed") from None
            result = {
                "backend": backend,
                "complete": True,
                "observed_at": observed_at,
                "instance_ids": ids,
            }
            if aliases:
                result["aliases"] = aliases
            if labels is not None:  # dstack instance names, for #868 proofs
                result["labels"] = labels
            return result
        except DstackError:
            raise
        except Exception as _observation_error:
            _logger.warning(
                "dstack_client_provider_inventory_failed error_type=%s",
                type(_observation_error).__name__,
            )
            raise DstackError("dstack_provider_inventory_unavailable") from None

    def configured_backends(self):
        """Supported backends configured on this dstack project (names only)."""
        project = self._post(f"/api/projects/{self.project}/get", {})
        rows = project.get("backends") if isinstance(project, dict) else None
        _require(isinstance(rows, list), "dstack_provider_inventory_unconfirmed")
        names = {row.get("name") for row in rows if isinstance(row, dict)}
        return frozenset(names & BACKENDS)

    def plan(self, spec):
        # Use the same fleet selection as apply. Global stock browsing can
        # offer machines that the configured native fleet cannot place (#659).
        return self._post(
            f"/api/project/{self.project}/runs/get_plan",
            {"run_spec": spec, "max_offers": 100, "for_offers_only": False},
        )

    def apply(self, spec):
        # Null expected resource + force:false is dstack's create-only CAS.
        return self._post(
            f"/api/project/{self.project}/runs/apply",
            {"plan": {"run_spec": spec, "current_resource": None}, "force": False},
        )

    def stop(self, run_name):
        _require(
            re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name), "dstack_run_name_invalid"
        )
        return self._post(
            f"/api/project/{self.project}/runs/stop",
            {"runs_names": [run_name], "abort": False},
        )


# Preserve the original public import and pickle identity of moved definitions.
DstackClient.__module__ = "studio_platform.dstack_capacity"

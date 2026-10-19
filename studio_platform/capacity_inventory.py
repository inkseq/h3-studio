"""Read-only, allowlisted stock observations; never an authority to rent or run.

The two pinned endpoints currently return complete JSON lists. Unexpected
pagination or malformed rows fail the whole observation rather than turning an
incomplete response into evidence of no stock. Imports do not load credentials.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import math
import re
import time
import uuid

import httpx

from .lium_identity import BASE_URL, KEY_VARIABLE, PROFILE, SERVICE

TARGON_URL = "https://api.targon.com/tha/v3/inventory"
LIUM_URL = BASE_URL + "/executors?available=true"
MAX_RESPONSE_BYTES = 4 * 1024**2
MAX_ROWS = 4096
TIMEOUT_SECONDS = 15
MAX_SCAN_SECONDS = 30
_GPU_NAMES = {
    "RTX 5090": "RTX 5090",
    "NVIDIA GeForce RTX 5090": "RTX 5090",
    "RTX-PRO-6000B": "RTX PRO 6000 Blackwell",
    "RTX PRO 6000 Blackwell": "RTX PRO 6000 Blackwell",
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": "RTX PRO 6000 Blackwell Server Edition",
    "RTX PRO 6000 Blackwell Server Edition": "RTX PRO 6000 Blackwell Server Edition",
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": "RTX PRO 6000 Blackwell Workstation Edition",
    "RTX PRO 6000 Blackwell Workstation Edition": "RTX PRO 6000 Blackwell Workstation Edition",
}


def allocation_resources(row):
    """Constraint view of a whole allocation, preserving the raw stock quote.

    Lium executor telemetry describes total host RAM. Its rental allocation
    reserves the larger of 4 GiB or 1 percent for the host. Targon inventory RAM
    already describes the resource SKU and needs no inferred subtraction.
    """
    value = dict(row)
    ram = value.get("ram_gib")
    if value.get("provider") == "lium" and ram is not None:
        value["ram_gib"] = max(0, ram - max(4, ram * 0.01))
    return value


class _InventoryError(Exception):
    """Only our static codes may cross the adapter boundary."""


def _number(value, *, integer=False, minimum=0, maximum=10**15):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise _InventoryError("inventory_invalid_response")
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or not minimum <= number <= maximum
            or (integer and number != number.to_integral_value())
        ):
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise _InventoryError("inventory_invalid_response") from None


def _count(value, *, minimum=0):
    # Provider JSON counts are integers; strings and booleans are ambiguous.
    if type(value) is not int:
        raise _InventoryError("inventory_invalid_response")
    return int(_number(value, integer=True, minimum=minimum, maximum=65536))


def _mapping(value):
    if not isinstance(value, dict):
        raise _InventoryError("inventory_invalid_response")
    return value


def _text(value, *, identifier=False):
    pattern = (
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
        if identifier
        else r"[^\x00-\x1f\x7f]{1,128}"
    )
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise _InventoryError("inventory_invalid_response")
    return value


def _micro(value):
    return int(
        (_number(value, maximum=10**6) * 1_000_000).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_json_constant(_):
    raise ValueError


def _optional_number(value, field, missing, *, divisor=1):
    if value is None:
        missing.append(field)
        return None
    return float(_number(value) / divisor)


def _row(
    provider,
    identity,
    gpu,
    count,
    available,
    price,
    *,
    ram,
    disk,
    cpu,
    download=None,
    country=None,
    kind,
    unverified=None,
):
    missing = list(unverified or ())
    if gpu == "RTX PRO 6000 Blackwell":
        missing.append("gpu_edition")
    if country is None:
        missing.append("country")
    elif not isinstance(country, str) or re.fullmatch(r"[A-Z]{2}", country) is None:
        raise _InventoryError("inventory_invalid_response")
    value = {
        "offer_id": identity,
        "provider": provider,
        "gpu_type": gpu,
        "gpu_count": count,
        "available_count": available,
        "ram_gib": _optional_number(ram, "ram_gib", missing),
        "disk_gib": _optional_number(disk, "disk_gib", missing),
        "download_mbps": _optional_number(download, "download_mbps", missing),
        "country": country,
        "hourly_cost_microusd": price,
        "price_per_gpu_hour_microusd": int(
            (Decimal(price) / count).to_integral_value(rounding=ROUND_CEILING)
        ),
        "cpu_cores": _optional_number(cpu, "cpu_cores", missing),
        "kind": kind,
    }
    value["unverified_fields"] = sorted(set(missing))
    return value


def _targon(rows):
    offers, identities = [], set()
    for raw in rows:
        raw = _mapping(raw)
        identity = _text(raw.get("name"), identifier=True)
        if identity in identities:
            raise _InventoryError("inventory_duplicate_offer")
        identities.add(identity)
        available = _count(raw.get("available"))
        price = _micro(raw.get("cost_per_hour"))
        kind = _text(raw.get("type"), identifier=True)
        spec = _mapping(raw.get("spec"))
        if type(raw.get("gpu")) is not bool:
            raise _InventoryError("inventory_invalid_response")
        if not raw["gpu"]:
            continue
        count = _count(spec.get("gpu_count"), minimum=1)
        gpu = _GPU_NAMES.get(_text(spec.get("gpu_model")))
        # Unsupported hardware and confirmed zero stock are not offers.
        if gpu is None or available == 0:
            continue
        missing = []
        ram = _optional_number(spec.get("memory_mib"), "ram_gib", missing, divisor=1024)
        disk = _optional_number(
            spec.get("disk_size_mib"), "disk_gib", missing, divisor=1024
        )
        cpu = _optional_number(
            spec.get("cpu_millicores"), "cpu_cores", missing, divisor=1000
        )
        offers.append(
            _row(
                "targon",
                identity,
                gpu,
                count,
                available,
                price,
                ram=ram,
                disk=disk,
                cpu=cpu,
                kind=kind,
                unverified=missing,
            )
        )
    return offers


def _lium(rows):
    offers, identities = [], set()
    for raw in rows:
        raw = _mapping(raw)
        try:
            identity = str(uuid.UUID(raw.get("id")))
        except (ValueError, TypeError, AttributeError):
            raise _InventoryError("inventory_invalid_response") from None
        if identity in identities:
            raise _InventoryError("inventory_duplicate_offer")
        identities.add(identity)
        count = _count(raw.get("gpu_count"), minimum=1)
        free = _count(raw.get("available_gpu_count"))
        if free > count:
            raise _InventoryError("inventory_invalid_response")
        price_per_gpu = _number(raw.get("price_per_gpu"), maximum=10**6)
        spec = _mapping(raw.get("specs"))
        gpu_spec = _mapping(spec.get("gpu"))
        details = gpu_spec.get("details")
        if not isinstance(details, list) or len(details) != count:
            raise _InventoryError("inventory_invalid_response")
        names = {_text(_mapping(item).get("name")) for item in details}
        mapped = {_GPU_NAMES.get(name) for name in names}
        if len(mapped) != 1:
            raise _InventoryError("inventory_mixed_gpu_host")
        gpu = mapped.pop()
        # Preserve partially free hosts as allocation-unknown evidence. Dropping
        # them would falsely prove no stock when a requested slice might fit.
        if gpu is None or free == 0:
            continue
        for flag in ("is_whole_host_free", "has_no_pending_rental"):
            if raw.get(flag) is not None and type(raw[flag]) is not bool:
                raise _InventoryError("inventory_invalid_response")
        minimum = raw.get("min_gpu_count_for_rental")
        missing = []
        if minimum is not None and _count(minimum, minimum=1) > count:
            raise _InventoryError("inventory_invalid_response")
        # A whole-host quote needs no splitting permission. Keep an unknown
        # minimum for the projection to assess smaller requested allocations.
        full_host_available = (
            free == count
            and raw.get("is_whole_host_free") is not False
            and raw.get("has_no_pending_rental") is not False
        )
        if not full_host_available:
            missing.append("allocation")
        # Executor RAM and hard-disk telemetry are KiB, unlike Targon's MiB.
        ram = _optional_number(
            _mapping(spec.get("ram", {})).get("total"),
            "ram_gib",
            missing,
            divisor=1024**2,
        )
        disk = _optional_number(
            _mapping(spec.get("hard_disk", {})).get("free"),
            "disk_gib",
            missing,
            divisor=1024**2,
        )
        cpu = _mapping(spec.get("cpu", {})).get("count")
        country = _mapping(raw.get("location", {})).get("country_code")
        # Use the provider's effective telemetry; don't invent fallback precedence.
        offer = _row(
            "lium",
            identity,
            gpu,
            count,
            int(full_host_available),
            _micro(price_per_gpu * count),
            ram=ram,
            disk=disk,
            cpu=cpu,
            download=raw.get("effective_download_speed_mbps"),
            country=country,
            kind="executor",
            unverified=missing,
        )
        # Count/resources/price still describe the complete host, never a slice.
        offer.update(available_gpu_count=free, min_gpu_count_for_rental=minimum)
        offers.append(offer)
    return offers


def _fetch(client, url, headers):
    started = time.monotonic()
    with client.stream(
        "GET", url, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False
    ) as response:
        if response.status_code != 200:
            raise _InventoryError("inventory_http_error")
        # Raw chunks let us check the elapsed bound on every network read, even
        # for a slow response. Do not buffer 64 KiB or decompress untrusted data
        # before checking the limit. One final read is bounded by the 15 s timeout.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise _InventoryError("inventory_response_encoding")
        if re.search(
            r"rel\s*=\s*[\"']?next", response.headers.get("link", ""), re.I
        ) or any(
            response.headers.get(key)
            for key in ("x-next-page", "x-next-cursor", "content-range")
        ):
            raise _InventoryError("inventory_pagination_unverified")
        raw = bytearray()
        chunks = (
            response.iter_bytes()
            if response.is_stream_consumed
            else response.iter_raw()
        )
        for part in chunks:
            if time.monotonic() - started > MAX_SCAN_SECONDS:
                raise _InventoryError("inventory_timeout")
            raw.extend(part)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _InventoryError("inventory_response_too_large")
        try:
            value = json.loads(
                raw,
                parse_float=Decimal,
                object_pairs_hook=_json_object,
                parse_constant=_invalid_json_constant,
            )
        except (ValueError, UnicodeError):
            raise _InventoryError("inventory_invalid_response") from None
        if not isinstance(value, list) or len(value) > MAX_ROWS:
            raise _InventoryError("inventory_invalid_response")
        if response.headers.get("x-total-count") is not None:
            if int(_number(response.headers["x-total-count"], integer=True)) != len(
                value
            ):
                raise _InventoryError("inventory_pagination_unverified")
        return value


def _scan(provider, *, loader=None, transport=None, client=None, clock=time.time):
    observed = clock()
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
    ):
        raise ValueError("inventory_invalid_clock")
    owned = None
    try:
        if client is not None and transport is not None:
            raise _InventoryError("inventory_invalid_client")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if provider == "lium":
            if loader is None:
                from .central_registry import load_api

                loader = load_api
            try:
                config = loader(SERVICE, profile=PROFILE)
                if (
                    config.service != SERVICE
                    or config.profile != PROFILE
                    or config.base_url != BASE_URL
                    or config.primary_key_variable != KEY_VARIABLE
                    or not isinstance(config.api_key, str)
                    or not config.api_key.strip()
                    or any(c in config.api_key for c in "\r\n\x00")
                ):
                    raise ValueError
                headers["X-API-Key"] = config.api_key
            except Exception:
                raise _InventoryError("inventory_profile_unavailable") from None
        if client is None:
            owned = client = httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=TIMEOUT_SECONDS,
                transport=transport,
            )
        rows = _fetch(client, TARGON_URL if provider == "targon" else LIUM_URL, headers)
        offers = (_targon if provider == "targon" else _lium)(rows)
        status, reason = "ok", None
    except _InventoryError as error:
        offers, status, reason = [], "error", str(error)
    except httpx.TimeoutException:
        offers, status, reason = [], "error", "inventory_timeout"
    except Exception:
        offers, status, reason = [], "error", "inventory_request_failed"
    finally:
        if owned is not None:
            owned.close()
    return {
        "provider": provider,
        "status": status,
        "reason_code": reason,
        "observed_at": observed,
        "offers": offers,
    }


def scan_targon(*, transport=None, client=None, clock=time.time):
    """One bounded GET of public stock; no Targon credential is required."""
    return _scan("targon", transport=transport, client=client, clock=clock)


def scan_lium(loader=None, *, transport=None, client=None, clock=time.time):
    """One authenticated GET; use an injected approved loader on a service host."""
    return _scan("lium", loader=loader, transport=transport, client=client, clock=clock)

"""Explicit, fail-closed Lium adapter; importing it cannot rent or load a key.

This adapter is not wired into a CLI. The durable ScaleCoordinator must commit
the creation/deletion intent before calling it. No HTTP operation is retried.
Only a removed, exactly identified ledger statement proves removed capacity;
404, an empty list, VM failure and an accepted DELETE never do so by themselves.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import importlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

import httpx

from .repository import Conflict, money
from .scaler import CreationNotSubmitted, LaunchSpec, ProviderFact, _safe_id
from .lium_identity import BASE_URL, KEY_VARIABLE, PROFILE, SERVICE
from .rent_journal import RentJournal


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 4096


class LiumError(Conflict):
    """Only static, non-secret error codes leave the adapter."""


class LiumNotSubmitted(LiumError, CreationNotSubmitted):
    """This invocation failed before the sole rent POST; not a HTTP rejection."""


class LiumRentRejected(LiumError):
    """A pinned 4xx code proves allocation was refused; never a timeout/5xx."""


RENT_REFUSALS = {
    "node_not_found": {404},
    "template_not_found": {404},
    "node_unavailable": {400},
    "node_rent_in_progress": {400, 409},
    "node_paused_by_provider": {409},
    "provider_banned": {409},
    "gpu_count_invalid": {400},
    "gpu_split_not_allowed": {400},
    "node_not_verified": {400},
    "template_invalid": {400},
    "ttl_invalid": {400},
    "no_executor_matches_spec": {409},
}


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise LiumError("lium_invalid_identifier") from None


def _pod_name(tag):
    return "sixnine-" + _uuid(tag)


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise LiumError("lium_invalid_money")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal("9000000000"):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise LiumError("lium_invalid_money") from None


def _microusd(value):
    # Ledger decimal amounts can have finer precision than our micro-USD unit.
    # Round upward by <1 microUSD; never replace the ledger with an estimate.
    return money(
        int((_decimal(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    )


def _timestamp(value, *, assume_utc=False):
    if not isinstance(value, str) or len(value) > 80:
        raise LiumError("lium_invalid_statement_time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            if not assume_utc:
                raise ValueError
            result = result.replace(tzinfo=timezone.utc)
        stamp = result.timestamp()
        if not math.isfinite(stamp):
            raise ValueError
        return stamp
    except (ValueError, OverflowError):
        raise LiumError("lium_invalid_statement_time") from None


def _public_key(value):
    # Accept one public key, never a filename, private PEM or authorized_keys
    # options. The approved operator supplies public content, not private files.
    if (
        not isinstance(value, str)
        or len(value) > 16_384
        or value != value.strip()
        or any(c in value for c in "\r\n\x00")
    ):
        raise LiumError("lium_public_ssh_key_required")
    parts = value.split()
    if len(parts) < 2 or parts[0] not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
    }:
        raise LiumError("lium_public_ssh_key_required")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        length = int.from_bytes(raw[:4], "big")
        if (
            len(raw) < 20
            or length < 1
            or length > 80
            or raw[4 : 4 + length] != parts[0].encode("ascii")
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LiumError("lium_public_ssh_key_required") from None
    return value


@dataclass(frozen=True)
class LiumManifest:
    """Trusted operator input, never inferred from a job's user payload.

    image_id is a Lium template UUID, not a model ID or a Docker image string.
    A fresh GET price cannot enforce an atomic rent price cap. Explicitly accept
    that limitation before enabling a manifest; the default cannot submit rent.
    """

    configuration_id: str
    model_id: str
    executor_id: str
    template_id: str
    gpu_count: int
    max_price_per_gpu_hour_microusd: int
    termination_hours: int
    user_public_key: str = field(repr=False)
    approved_until: float
    region: str = ""
    allow_preflight_only_price_cap: bool = False
    execution_slots: int = 1
    compatible_gpu_names: tuple[str, ...] = ()
    minimum_vram_mib: int = 0
    allowed_countries: tuple[str, ...] = ()
    server_side_selection: bool = False
    minimum_ram_gib: int = 0
    minimum_disk_gib: int = 0
    require_docker_in_docker: bool = False
    min_download_mbps: float | None = None

    def __post_init__(self):
        _safe_id(self.configuration_id), _safe_id(self.model_id)
        if self.region:
            _safe_id(self.region)
        _uuid(self.template_id)
        if self.executor_id:
            _uuid(self.executor_id)
        elif self.executor_id != "" or not self.compatible_gpu_names:
            raise LiumError("lium_hardware_filter_required_without_executor")
        if type(self.gpu_count) is not int or not 1 <= self.gpu_count <= 128:
            raise LiumError("lium_invalid_gpu_count")
        if (
            type(self.execution_slots) is not int
            or not 1 <= self.execution_slots <= self.gpu_count
        ):
            raise LiumError("lium_invalid_execution_slot_count")
        money(self.max_price_per_gpu_hour_microusd)
        if (
            type(self.termination_hours) is not int
            or not 1 <= self.termination_hours <= 720
        ):
            raise LiumError("lium_invalid_ttl")
        if (
            type(self.allow_preflight_only_price_cap) is not bool
            or isinstance(self.approved_until, bool)
            or not isinstance(self.approved_until, (int, float))
            or not math.isfinite(self.approved_until)
        ):
            raise LiumError("lium_invalid_manifest")
        _public_key(self.user_public_key)
        for key in ("compatible_gpu_names", "allowed_countries"):
            value = getattr(self, key)
            if (
                not isinstance(value, (list, tuple))
                or len(value) > 32
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in value)
                or len(set(value)) != len(value)
            ):
                raise LiumError("lium_invalid_hardware_selector")
            object.__setattr__(self, key, tuple(value))
        if (
            type(self.minimum_vram_mib) is not int
            or not 0 <= self.minimum_vram_mib <= 1_048_576
            or bool(self.compatible_gpu_names) != bool(self.minimum_vram_mib)
            or any(
                len(v) != 2 or not v.isascii() or not v.isupper() or not v.isalpha()
                for v in self.allowed_countries
            )
        ):
            raise LiumError("lium_invalid_hardware_selector")
        if (
            type(self.server_side_selection) is not bool
            or type(self.require_docker_in_docker) is not bool
            or type(self.minimum_ram_gib) is not int
            or not 0 <= self.minimum_ram_gib <= 65536
            or type(self.minimum_disk_gib) is not int
            or not 0 <= self.minimum_disk_gib <= 1048576
            or self.server_side_selection
            and (
                self.executor_id
                or len(self.compatible_gpu_names) != 1
                or len(self.allowed_countries) > 1
            )
        ):
            raise LiumError("lium_invalid_server_selector")
        if self.min_download_mbps is not None:
            try:
                valid = (
                    type(self.min_download_mbps) in (int, float)
                    and math.isfinite(self.min_download_mbps)
                    and self.min_download_mbps > 0
                )
            except OverflowError:
                valid = False
            if not valid:
                raise LiumError("lium_invalid_download_floor")


@dataclass(frozen=True)
class InferenceIdleProof:
    """A trusted probe checked the exact instance's inference queue separately."""

    instance_id: str
    observed_at: float
    idle_since: float
    idle: bool

    def __post_init__(self):
        _uuid(self.instance_id)
        if type(self.idle) is not bool or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in (self.observed_at, self.idle_since)
        ):
            raise LiumError("lium_invalid_idle_proof")


def _central_loader(service, *, profile):
    """Import the existing central implementation lazily, without copying it."""
    configured = os.environ.get("AI_REGISTRY_ROOT", "")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise LiumError("lium_registry_path_invalid")
    elif os.name == "nt":
        root = Path(r"C:\Users\danmo\Desktop\AI-Registry")
    else:
        # This known WSL mount must really exist; an AWS host gets no fallback.
        root = Path("/mnt/c/Users/danmo/Desktop/AI-Registry")
    source = root / "api_registry.py"
    if not source.is_file():
        raise LiumError("lium_central_loader_unavailable")
    try:
        existing = sys.modules.get("api_registry")
        if (
            existing is not None
            and Path(existing.__file__).resolve() != source.resolve()
        ):
            raise ValueError
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = importlib.import_module("api_registry")
        if Path(module.__file__).resolve() != source.resolve():
            raise ValueError
        return module.load_api(service, profile=profile)
    except Exception:
        raise LiumError("lium_central_loader_unavailable") from None


class LiumProvider:
    provider_id = SERVICE

    def __init__(
        self,
        *,
        enabled=False,
        manifests=(),
        loader=None,
        transport=None,
        idle_probe=None,
        clock=time.time,
        timeout_s=20,
        ttl_margin_s=60,
        journal_dir=None,
    ):
        if (
            type(enabled) is not bool
            or isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 30
            or isinstance(ttl_margin_s, bool)
            or not math.isfinite(ttl_margin_s)
            or not 30 <= ttl_margin_s <= 300
        ):
            raise LiumError("lium_invalid_settings")
        manifest_list = tuple(manifests)
        if any(not isinstance(item, LiumManifest) for item in manifest_list) or len(
            {
                (item.configuration_id, item.executor_id, item.template_id)
                for item in manifest_list
            }
        ) != len(manifest_list):
            raise LiumError("lium_duplicate_or_invalid_manifest")
        if journal_dir is None and any(
            item.server_side_selection for item in manifest_list
        ):
            raise LiumError("lium_server_selector_requires_durable_journal")
        self.enabled = enabled
        self._manifests = {
            (item.configuration_id, item.executor_id, item.template_id): item
            for item in manifest_list
        }
        self._loader = loader or _central_loader
        self._transport, self._idle_probe, self.clock = transport, idle_probe, clock
        self._timeout, self._ttl_margin = timeout_s, ttl_margin_s
        self._client = None
        self._initialization_lock, self._create_lock = (
            threading.Lock(),
            threading.Lock(),
        )
        self._submitted_tags = set()
        self._destroy_submitted = set()
        self._availability_cache = {}
        self._journal = RentJournal(journal_dir) if journal_dir is not None else None

    def _journal_write(self, tag, phase, **fields):
        if self._journal is not None:
            try:
                self._journal.save(tag, phase, **fields)
            except Exception:
                raise LiumError("lium_rent_journal_unconfirmed") from None

    def _journal_read(self, tag):
        if self._journal is None:
            return None
        try:
            return self._journal.read(tag)
        except Exception:
            raise LiumError("lium_rent_journal_unconfirmed") from None

    def _ttl_observation(self, tag, instance_id, ttl):
        detail = self._request("GET", "pods/" + _uuid(instance_id))
        try:
            if (
                not isinstance(detail, dict)
                or _uuid(detail.get("id")) != instance_id
                or self._name(detail) != _pod_name(tag)
                or detail.get("status") not in ("PENDING", "RUNNING")
            ):
                raise ValueError
            created_raw, removed_raw = (
                detail["created_at"],
                detail.get("removal_scheduled_at"),
            )
            if not isinstance(created_raw, str) or len(created_raw) > 80:
                raise ValueError
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            naive = created.tzinfo is None
            created_at = _timestamp(created_raw, assume_utc=naive)
            if (
                abs(created_at - ttl["created_at"]) > 300
                or created_at > self.clock() + 300
            ):
                raise ValueError
            removed_at = None
            if removed_raw is not None:
                if not isinstance(removed_raw, str) or len(removed_raw) > 80:
                    raise ValueError
                parsed = datetime.fromisoformat(removed_raw.replace("Z", "+00:00"))
                if (parsed.tzinfo is None) != naive:
                    raise ValueError
                removed_at = _timestamp(removed_raw, assume_utc=naive)
                if removed_at <= created_at:
                    raise ValueError
            hours = detail.get("termination_hours")
            if not (
                type(hours) is int
                and hours == ttl["requested_hours"]
                or hours is None
                and removed_at is not None
            ):
                raise ValueError
            if ttl["instance_id"] is not None and (
                ttl["instance_id"] != instance_id
                or ttl["provider_created_at"] != created_at
            ):
                raise ValueError
            return detail, created_at, removed_at
        except (KeyError, TypeError, ValueError, LiumError, OverflowError):
            raise LiumError("lium_absolute_ttl_unconfirmed") from None

    def _ensure_absolute_ttl(self, tag, instance_id):
        """Bound new rents only; legacy markers never acquire inferred authority.

        First scheduling waits for RUNNING: the provider may overwrite a
        PENDING schedule during startup. Unknown POSTs remain GET-only. One
        historical acknowledged and verified pending schedule may still be
        shortened at RUNNING; a second overwrite remains an operator hold.
        """
        marker = self._journal_read(tag)
        if not marker or "absolute_ttl" not in marker:
            return None
        try:
            with self._journal.ttl_lock(tag):
                marker = self._journal_read(tag)
                if marker["phase"] not in {"post_started", "confirmed"}:
                    raise ValueError
                if marker.get("instance_id", instance_id) != instance_id:
                    raise ValueError
                ttl = marker["absolute_ttl"]
                detail, created, removed = self._ttl_observation(tag, instance_id, ttl)
                ttl["instance_id"], ttl["provider_created_at"] = instance_id, created
                if removed is not None:
                    ttl["effective_deadline"] = min(ttl["effective_deadline"], removed)
                if ttl["effective_deadline"] <= self.clock():
                    raise ValueError
                if detail["status"] == "PENDING":
                    # Retain the original identity and any earlier observed
                    # deadline, but do not spend a schedule attempt before the
                    # provider has finished initializing its relative timer.
                    self._journal.update_ttl(tag, ttl)
                    raise ValueError
                safe = removed is not None and removed <= ttl["effective_deadline"]
                if safe:
                    if ttl["attempts"]:
                        ttl["attempts"][-1]["confirmed"] = True
                    self._journal.update_ttl(tag, ttl)
                    return detail
                previous = ttl["attempts"][-1] if ttl["attempts"] else None
                if previous and (
                    len(ttl["attempts"]) != 1
                    or not previous["acknowledged"]
                    or not previous["confirmed"]
                    or previous["status"] != "PENDING"
                    or detail["status"] != "RUNNING"
                ):
                    raise ValueError
                # DB clocks can retain sub-microsecond precision. ISO datetime
                # rounds to microseconds; never round the approved ceiling up.
                target_time = datetime.fromtimestamp(
                    ttl["effective_deadline"], timezone.utc
                )
                if target_time.timestamp() > ttl["effective_deadline"]:
                    target_time -= timedelta(microseconds=1)
                ttl["effective_deadline"] = target_time.timestamp()
                if ttl["effective_deadline"] <= self.clock():
                    raise ValueError
                # The lock and fsynced intent precede the only allowed request.
                ttl["attempts"].append(
                    {
                        "target": ttl["effective_deadline"],
                        "started_at": self.clock(),
                        "status": detail["status"],
                        "acknowledged": False,
                        "confirmed": False,
                    }
                )
                self._journal.update_ttl(tag, ttl)
                self._request(
                    "POST",
                    "pods/" + instance_id + "/schedule-removal",
                    payload={"removal_scheduled_at": target_time.isoformat()},
                )
                ttl["attempts"][-1]["acknowledged"] = True
                self._journal.update_ttl(tag, ttl)
                detail, _, removed = self._ttl_observation(tag, instance_id, ttl)
                if removed is None or removed > ttl["effective_deadline"]:
                    raise ValueError
                ttl["effective_deadline"] = min(ttl["effective_deadline"], removed)
                ttl["attempts"][-1]["confirmed"] = True
                self._journal.update_ttl(tag, ttl)
                return detail
        except Exception:
            raise LiumError("lium_absolute_ttl_unconfirmed") from None

    def create_for_intent(self, tag, launch, *, hard_deadline, intent_created_at):
        """Optional coordinator hook; only original DB time can bind a new TTL."""
        return self.create(
            tag,
            launch,
            hard_deadline=hard_deadline,
            intent_created_at=intent_created_at if self._journal is not None else None,
        )

    def create_selected_for_intent(
        self, tag, launch, *, selected_offer, hard_deadline, intent_created_at
    ):
        return self.create(
            tag,
            launch,
            hard_deadline=hard_deadline,
            intent_created_at=intent_created_at,
            selected_offer=selected_offer,
        )

    def _select_exact_offer(self, manifest, expected):
        """Recheck the selected complete host; never fall through to another ID."""
        from .capacity_inventory import _GPU_NAMES, _lium, allocation_resources
        from .capacity_market import _constraints

        try:
            identity = _uuid(expected.get("offer_id"))
            if (
                expected.get("provider") != SERVICE
                or expected.get("gpu_count") != manifest.gpu_count
                or manifest.execution_slots != manifest.gpu_count
                or not manifest.compatible_gpu_names
                and identity != manifest.executor_id
            ):
                raise ValueError
            cap = money(expected.get("hourly_cost_microusd"))
            route = "executors?available=true"
            if manifest.min_download_mbps is not None:
                route += "&" + urlencode(
                    {"min_download_mbps": manifest.min_download_mbps}
                )
            matches = [row for row in self._rows(route) if row.get("id") == identity]
            if len(matches) != 1:
                raise ValueError
            raw = matches[0]
            normalized = _lium([raw])
            if len(normalized) != 1:
                raise ValueError
            row = normalized[0]
            filters = {
                "min_ram_gib": manifest.minimum_ram_gib,
                "min_disk_gib": manifest.minimum_disk_gib,
                "max_price_per_gpu_hour_microusd": manifest.max_price_per_gpu_hour_microusd,
                "allowed_countries": manifest.allowed_countries,
            }
            if manifest.min_download_mbps is not None:
                filters["min_download_mbps"] = manifest.min_download_mbps
            if (
                row["gpu_count"] != manifest.gpu_count
                or row["available_count"] != 1
                or row["hourly_cost_microusd"] > cap
                or _constraints(allocation_resources(row), filters)
                or row["gpu_type"] != expected.get("gpu_type")
            ):
                raise ValueError
            details = raw["specs"]["gpu"]["details"]
            # Inventory publishes canonical names; the provider may retain an
            # NVIDIA prefix. Use that same closed alias map on both sides, while
            # keeping every physical GPU's type and VRAM requirements intact.
            allowed_names = {
                _GPU_NAMES.get(name, name) for name in manifest.compatible_gpu_names
            }
            if manifest.compatible_gpu_names and any(
                _GPU_NAMES.get(item.get("name")) not in allowed_names
                or type(item.get("capacity")) is not int
                or item["capacity"] < manifest.minimum_vram_mib
                for item in details
            ):
                raise ValueError
            if (
                manifest.require_docker_in_docker
                and raw.get("docker_in_docker") is not True
            ):
                raise ValueError
            if (
                sum(
                    row.get("id") == manifest.template_id
                    for row in self._rows("templates")
                )
                != 1
            ):
                raise ValueError
            return identity
        except LiumError:
            raise
        except Exception:
            raise LiumError("lium_selected_offer_unavailable") from None

    def _select_offer(self, manifest):
        # Selection is opt-in operator policy. Model, template, count, TTL and
        # money reservation stay unchanged. Recheck inventory at actual create.
        if manifest.server_side_selection:
            try:
                response = self._request(
                    "POST",
                    "executors/rent-by-spec",
                    rental=True,
                    payload=self._spec_payload(
                        manifest, "sixnine-preflight", dry_run=True
                    ),
                )
            except LiumRentRejected:
                raise LiumError("lium_exact_offer_or_template_unavailable") from None
            self._validate_spec_response(response, manifest, dry_run=True)
            return _uuid(response["selected_executor"]["id"])
        route = (
            "executors?available=true" if manifest.compatible_gpu_names else "executors"
        )
        if manifest.min_download_mbps is not None:
            # Lium applies its trusted telemetry precedence and excludes missing
            # measurements. Do not infer speed from a differently shaped field.
            route += ("&" if "?" in route else "?") + urlencode(
                {"min_download_mbps": manifest.min_download_mbps}
            )
        rows = self._rows(route)
        candidates = []
        for row in rows:
            if (
                not manifest.compatible_gpu_names
                and row.get("id") != manifest.executor_id
            ):
                continue
            try:
                identity = _uuid(row.get("id"))
                price = _microusd(row.get("price_per_gpu"))
                count = row.get("gpu_count")
                if (
                    type(count) is not int
                    or count < manifest.gpu_count
                    or price > manifest.max_price_per_gpu_hour_microusd
                ):
                    continue
                free = row.get("available_gpu_count", count)
                if type(free) is not int or free < manifest.gpu_count:
                    continue
                # Direct selection cannot infer splitting permission. Atomic
                # rent-by-spec is required for a partial rental of a larger host.
                if count != manifest.gpu_count:
                    continue
                minimum = row.get("min_gpu_count_for_rental", 1)
                if type(minimum) is not int or minimum > manifest.gpu_count:
                    continue
                if (
                    manifest.allowed_countries
                    and row.get("location", {}).get("country_code")
                    not in manifest.allowed_countries
                ):
                    continue
                if manifest.compatible_gpu_names:
                    details = row["specs"]["gpu"]["details"]
                    if not isinstance(details, list) or not details:
                        continue
                    if any(
                        not isinstance(d, dict)
                        or d.get("name") not in manifest.compatible_gpu_names
                        or type(d.get("capacity")) is not int
                        or d["capacity"] < manifest.minimum_vram_mib
                        for d in details
                    ):
                        continue
                candidates.append((identity != manifest.executor_id, price, identity))
            except (LiumError, KeyError, TypeError, AttributeError):
                continue
        if not candidates:
            code = (
                "lium_offer_outside_approved_limits"
                if any(r.get("id") == manifest.executor_id for r in rows)
                else "lium_exact_offer_or_template_unavailable"
            )
            raise LiumError(code)
        selected = sorted(candidates)[0][2]
        if sum(r.get("id") == selected for r in rows) != 1:
            raise LiumError("lium_duplicate_offer_identity")
        templates = [
            r for r in self._rows("templates") if r.get("id") == manifest.template_id
        ]
        if len(templates) != 1:
            raise LiumError("lium_exact_offer_or_template_unavailable")
        return selected

    @staticmethod
    def _spec_payload(manifest, name, *, dry_run, hours=None):
        value = {
            "pod_name": name,
            "template_id": manifest.template_id,
            "gpu_count": manifest.gpu_count,
            "gpu_type": manifest.compatible_gpu_names[0],
            "min_vram_gb": manifest.minimum_vram_mib / 1024,
            "max_price_per_gpu_hour": manifest.max_price_per_gpu_hour_microusd
            / 1_000_000,
            "user_public_key": manifest.user_public_key,
            "dry_run": dry_run,
        }
        if hours is not None:
            value["termination_hours"] = hours
        if manifest.allowed_countries:
            value["country"] = manifest.allowed_countries[0]
        if manifest.minimum_ram_gib:
            value["min_ram_gb"] = manifest.minimum_ram_gib
        if manifest.minimum_disk_gib:
            value["min_disk_gb"] = manifest.minimum_disk_gib
        if manifest.require_docker_in_docker:
            value["docker_in_docker"] = True
        if manifest.min_download_mbps is not None:
            value["min_download_mbps"] = manifest.min_download_mbps
        return value

    @staticmethod
    def _validate_spec_response(response, manifest, *, dry_run):
        if (
            not isinstance(response, dict)
            or response.get("success") is not True
            or response.get("dry_run") is not dry_run
            or response.get("template_id") != manifest.template_id
            or not isinstance(response.get("selected_executor"), dict)
        ):
            raise LiumError("lium_spec_response_unconfirmed")
        _uuid(response["selected_executor"].get("id"))
        if (
            _microusd(response.get("price_per_hour"))
            > manifest.gpu_count * manifest.max_price_per_gpu_hour_microusd
            or dry_run
            and response.get("pod_id") is not None
        ):
            raise LiumError("lium_spec_response_outside_approved_limits")

    def preflight_availability(self, launch):
        """Read-only probe before ledger reservation; negatives retry after 60s.

        Never settle an existing intent from inventory absence. Cache only this
        read-only result, never rent responses or a selected offer for creation.
        """
        manifest = self._manifest(launch)
        now = self.clock()
        cached = self._availability_cache.get(manifest)
        if cached and 0 <= now - cached[0] < 60:
            return cached[1]
        try:
            self._select_offer(manifest)
            reason = None
        except LiumError as exc:
            reason = (
                "provider_inventory_unavailable"
                if str(exc)
                in {
                    "lium_exact_offer_or_template_unavailable",
                    "lium_offer_outside_approved_limits",
                }
                else "provider_inventory_unconfirmed"
            )
        self._availability_cache[manifest] = (now, reason)
        return reason

    def _http(self):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        with self._initialization_lock:
            if self._client is None:
                try:
                    config = self._loader(SERVICE, profile=PROFILE)
                    if (
                        config.service != SERVICE
                        or config.profile != PROFILE
                        or config.base_url != BASE_URL
                        or config.primary_key_variable != KEY_VARIABLE
                        or not isinstance(config.api_key, str)
                        or not config.api_key
                        or any(c in config.api_key for c in "\r\n\x00")
                    ):
                        raise ValueError
                    self._client = httpx.Client(
                        base_url=BASE_URL + "/",
                        trust_env=False,
                        follow_redirects=False,
                        timeout=self._timeout,
                        transport=self._transport,
                        headers={
                            "X-API-Key": config.api_key,
                            "Accept": "application/json",
                        },
                    )
                except Exception:
                    raise LiumError("lium_profile_unavailable_or_mismatched") from None
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()

    def _request(
        self,
        method,
        route,
        *,
        payload=None,
        missing_ok=False,
        rental=False,
        request_id=None,
    ):
        # All routes are generated internally from validated UUIDs. Disable
        # redirects, proxies and retries; never propagate response body/errors.
        try:
            headers = {"X-Request-Id": _uuid(request_id)} if request_id else None
            with self._http().stream(
                method, route, json=payload, headers=headers
            ) as response:
                if missing_ok and response.status_code == 404:
                    return None
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise LiumError("lium_response_too_large")
                # Preserve money's JSON decimal spelling; avoid binary floats.
                value = json.loads(raw, parse_float=Decimal)
                if response.status_code < 200 or response.status_code >= 300:
                    if (
                        rental
                        and isinstance(value, dict)
                        and value.get("success") is False
                        and all(
                            value.get(k) is None
                            for k in ("pod_id", "instance_id", "pod", "pods")
                        )
                    ):
                        code = (
                            (value.get("error") or {}).get("code")
                            if isinstance(value.get("error"), dict)
                            else value.get("code")
                        )
                        if response.status_code in RENT_REFUSALS.get(code, set()):
                            raise LiumRentRejected("lium_rent_rejected")
                    raise LiumError("lium_request_unconfirmed")
                return value
        except LiumError:
            raise
        except Exception:
            raise LiumError("lium_request_unconfirmed") from None

    def _rows(self, route):
        rows = self._request("GET", route)
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_ROWS
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise LiumError("lium_invalid_listing")
        return rows

    @staticmethod
    def _name(row):
        name = row.get("name", row.get("pod_name"))
        if "name" in row and "pod_name" in row and row["name"] != row["pod_name"]:
            raise LiumError("lium_pod_name_conflict")
        return name

    def _exact_pod(self, tag, instance_id=None):
        name = _pod_name(tag)
        known = _uuid(instance_id) if instance_id is not None else None
        # Read every visible row rather than SDK's first name/prefix match.
        matches = [row for row in self._rows("pods") if self._name(row) == name]
        if len(matches) > 1:
            raise LiumError("lium_duplicate_tag_requires_operator")
        if not matches:
            return None
        pod_id = _uuid(matches[0].get("id"))
        if known is not None and known != pod_id:
            raise LiumError("lium_instance_identity_conflict")
        return matches[0]

    def _running_fact(self, tag, pod):
        pod_id = _uuid(pod.get("id"))
        if self._name(pod) != _pod_name(tag):
            raise LiumError("lium_instance_identity_conflict")
        status = pod.get("status")
        if not isinstance(status, str):
            raise LiumError("lium_invalid_pod_status")
        if status.upper() != "RUNNING":
            # FAILED/STOPPED may still reserve a billed node; not removed.
            known = (
                status.upper()
                if status.upper() in {"PENDING", "FAILED", "STOPPED"}
                else None
            )
            phase = (
                "configuring_ssh"
                if pod.get("phase") == "configuring ssh"
                else "provider_preparing"
            )
            return ProviderFact(
                "starting",
                pod_id,
                provider_status=known,
                preparation_stage=phase if known else None,
            )
        proof = None
        if self._idle_probe is not None:
            try:
                proof = self._idle_probe(tag, pod_id)
            except Exception:
                pass
        now = self.clock()
        if (
            isinstance(proof, InferenceIdleProof)
            and proof.instance_id == pod_id
            and proof.idle
            and 0 <= now - proof.observed_at <= 30
            and proof.idle_since <= proof.observed_at
        ):
            return ProviderFact(
                "running", pod_id, idle_confirmed=True, idle_since=proof.idle_since
            )
        return ProviderFact("running", pod_id)

    def _removed_statement(self, tag, instance_id):
        pod_id = _uuid(instance_id)
        statement = self._request("GET", f"pods/{pod_id}/statement", missing_ok=True)
        if statement is None:
            return None
        if (
            not isinstance(statement, dict)
            or _uuid(statement.get("pod_id")) != pod_id
            or statement.get("pod_name") != _pod_name(tag)
        ):
            raise LiumError("lium_statement_identity_conflict")
        if statement.get("removed") is not True:
            return None
        # The live Lium statement uses paired timezone-less UTC ISO values,
        # just like the pod lifetime endpoint. Never interpret them in the
        # controller machine's local timezone or accept mixed formats.
        try:
            parsed = [
                datetime.fromisoformat(statement[k].replace("Z", "+00:00"))
                for k in ("created_at", "removed_at")
            ]
            if (parsed[0].tzinfo is None) != (parsed[1].tzinfo is None):
                raise ValueError
            naive = parsed[0].tzinfo is None
        except (KeyError, TypeError, AttributeError, ValueError):
            raise LiumError("lium_invalid_statement_time") from None
        removed_at = _timestamp(statement.get("removed_at"), assume_utc=naive)
        created_at = _timestamp(statement.get("created_at"), assume_utc=naive)
        if not created_at <= removed_at <= self.clock() + 30:
            raise LiumError("lium_invalid_statement_time")
        # Absence of final money does not prevent a verified physical removal.
        # Missing/inconsistent accounting remains pending for a later read.
        actual = None
        try:
            actual = _microusd(statement["total"])
            seconds = _decimal(statement.get("billed_seconds"))
            if seconds > Decimal(str(removed_at - created_at + 60)):
                actual = None
        except (KeyError, LiumError):
            actual = None
        return ProviderFact("destroyed", pod_id, actual_cost_microusd=actual)

    def _manifest(self, launch):
        manifest = self._manifests.get(
            (
                getattr(launch, "configuration_id", None),
                getattr(launch, "offer_id", None),
                getattr(launch, "image_id", None),
            )
        )
        if (
            manifest is None
            or not isinstance(launch, LaunchSpec)
            or launch.provider != SERVICE
            or launch.model_id != manifest.model_id
            or launch.region != manifest.region
            or launch.offer_id != manifest.executor_id
            or launch.image_id != manifest.template_id
            or not manifest.allow_preflight_only_price_cap
            or manifest.approved_until <= self.clock()
        ):
            raise LiumError("lium_launch_manifest_unapproved")
        return manifest

    def ssh_connection(self, tag, instance_id):
        """Read exact provider-owned SSH coordinates, never execute its command.

        The known API structure is executor.executor_ip_address and
        ports_mapping['22']. A public address and numeric port are required;
        arbitrary ssh command strings, URLs, secrets, and private targets are not
        returned or interpreted. This remains CPU-side operational metadata.
        """
        if not self.execution_allowed(tag, instance_id):
            raise LiumError("lium_rental_contract_requires_reconciliation")
        ttl_detail = self._ensure_absolute_ttl(_uuid(tag), _uuid(instance_id))
        pod = self._exact_pod(tag, instance_id)
        if pod is None or str(pod.get("status", "")).upper() != "RUNNING":
            raise LiumError("lium_pod_not_running_for_bootstrap")
        detail = (
            ttl_detail
            if ttl_detail is not None
            else self._request("GET", "pods/" + _uuid(instance_id))
        )
        try:
            if not isinstance(detail, dict) or _uuid(detail.get("id")) != _uuid(
                instance_id
            ):
                raise ValueError
            if self._name(detail) is not None and self._name(detail) != _pod_name(tag):
                raise ValueError
            host = str(ipaddress.ip_address(detail["executor"]["executor_ip_address"]))
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
            port_value = detail["ports_mapping"]["22"]
            if isinstance(port_value, bool) or not isinstance(port_value, (str, int)):
                raise ValueError
            port = int(port_value)
            if not 1 <= port <= 65535:
                raise ValueError
        except (KeyError, TypeError, ValueError, LiumError):
            raise LiumError("lium_ssh_coordinates_unverified") from None
        return {
            "instance_id": _uuid(instance_id),
            "host": host,
            "port": port,
            "username": "root",
        }

    def execution_allowed(self, tag, instance_id):
        marker = self._journal_read(_uuid(tag))
        if (
            marker
            and "instance_id" in marker
            and marker["instance_id"] != _uuid(instance_id)
        ):
            raise LiumError("lium_instance_identity_conflict")
        return not marker or marker["phase"] != "quarantined"

    def lifetime(self, tag, instance_id, *, local_created_at, maximum_hours=4):
        """A conservative actual-pod deadline, never an extension of approval.

        Lium currently returns timezone-less pod times. Interpret that format as
        UTC only after its creation timestamp agrees with our durable rent intent
        within five minutes; otherwise refuse an expiry claim. Return an earlier
        deadline with a ten-minute collection/clock margin. Caller may only
        shorten its existing ledger deadline using this fact.
        """
        if (
            type(maximum_hours) is not int
            or not 1 <= maximum_hours <= 4
            or type(local_created_at) not in (int, float)
            or not math.isfinite(local_created_at)
        ):
            raise LiumError("lium_invalid_lifetime_bound")
        marker = self._journal_read(_uuid(tag))
        ttl_detail = None
        if marker and "absolute_ttl" in marker:
            ttl = marker["absolute_ttl"]
            if (
                ttl["created_at"] != local_created_at
                or ttl["requested_hours"] > maximum_hours
            ):
                raise LiumError("lium_absolute_ttl_unconfirmed")
            ttl_detail = self._ensure_absolute_ttl(tag, _uuid(instance_id))
        if self._exact_pod(tag, instance_id) is None:
            raise LiumError("lium_lifetime_not_confirmed")
        detail = (
            ttl_detail
            if ttl_detail is not None
            else self._request("GET", "pods/" + _uuid(instance_id))
        )
        try:
            if (
                not isinstance(detail, dict)
                or _uuid(detail.get("id")) != _uuid(instance_id)
                or self._name(detail) != _pod_name(tag)
            ):
                raise ValueError
            parsed = [
                datetime.fromisoformat(detail[k].replace("Z", "+00:00"))
                for k in ("created_at", "removal_scheduled_at")
            ]
            if (parsed[0].tzinfo is None) != (parsed[1].tzinfo is None):
                raise ValueError
            assumed = parsed[0].tzinfo is None
            if assumed:
                parsed = [v.replace(tzinfo=timezone.utc) for v in parsed]
            created, removed = (v.timestamp() for v in parsed)
            if (
                abs(created - local_created_at) > 300
                or not 0 < removed - created <= maximum_hours * 3600 + 300
            ):
                raise ValueError
            safe = min(removed, created + maximum_hours * 3600) - 600
            if safe <= self.clock():
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise LiumError("lium_lifetime_not_confirmed") from None
        return {
            "instance_id": _uuid(instance_id),
            "provider_created_at": detail["created_at"],
            "provider_removal_scheduled_at": detail["removal_scheduled_at"],
            "safe_deadline": safe,
            "timezone_evidence": "naive_utc_crosschecked_against_local_intent"
            if assumed
            else "explicit_offset",
            "safety_margin_seconds": 600,
        }

    def validate_launch(
        self, launch, *, physical_gpus, slots, reserved_cost_microusd, hard_deadline
    ):
        """Pure pre-reservation check; no credentials/HTTP, no parallel budget.

        The coordinator's existing durable reservation must account for every
        approved physical GPU and the full approved TTL at the preflight cap.
        This remains a reservation, not a final invoice or an atomic price cap.
        """
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        manifest = self._manifest(launch)
        if (
            type(physical_gpus) is not int
            or physical_gpus != manifest.gpu_count
            or type(slots) is not int
            or slots != manifest.execution_slots
        ):
            raise LiumError("lium_capacity_manifest_mismatch")
        reservation = money(reserved_cost_microusd)
        cap = money(
            manifest.max_price_per_gpu_hour_microusd
            * manifest.gpu_count
            * manifest.termination_hours
        )
        if reservation < cap:
            raise LiumError("lium_ttl_budget_not_reserved")
        now = self.clock()
        if (
            isinstance(hard_deadline, bool)
            or not isinstance(hard_deadline, (float, int))
            or not math.isfinite(hard_deadline)
            or hard_deadline - now - self._ttl_margin < 3600
        ):
            raise LiumError("lium_insufficient_provider_ttl_window")
        return {
            "physical_gpus": manifest.gpu_count,
            "slots": manifest.execution_slots,
            "ttl_cap_reservation_microusd": cap,
        }

    def create(
        self,
        tag,
        launch: LaunchSpec,
        *,
        hard_deadline,
        intent_created_at=None,
        selected_offer=None,
    ):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        tag = _uuid(tag)
        name = _pod_name(tag)
        # Pure validation says nothing about a pre-existing resource bearing
        # this tag. An expired manifest must not create a zero-charge marker.
        manifest = self._manifest(launch)
        now = self.clock()
        if intent_created_at is not None and (
            self._journal is None
            or type(intent_created_at) not in (int, float)
            or not math.isfinite(intent_created_at)
            or not 0 <= intent_created_at <= now
        ):
            raise LiumError("lium_invalid_intent_creation_time")
        if (
            isinstance(hard_deadline, bool)
            or not isinstance(hard_deadline, (float, int))
            or not math.isfinite(hard_deadline)
        ):
            raise LiumError("lium_invalid_deadline")
        hours = min(
            manifest.termination_hours,
            math.floor((hard_deadline - now - self._ttl_margin) / 3600),
        )
        if hours < 1:
            raise LiumError("lium_insufficient_provider_ttl_window")
        with self._create_lock:
            if tag in self._submitted_tags or self._journal_read(tag) is not None:
                raise LiumError("lium_creation_already_submitted_reconcile_only")
            self._journal_write(tag, "checking")
            # Any pre-existing exact tag retains reconciliation even when its
            # payload is malformed. Never settle that resource at zero cost.
            existing = self._exact_pod(tag)
            if existing is not None:
                return self._running_fact(tag, existing)
            try:
                selected_executor = (
                    self._select_exact_offer(manifest, selected_offer)
                    if selected_offer is not None
                    else self._select_offer(manifest)
                )
            except LiumError as exc:
                # Only this pre-POST block provides absence proof. In particular
                # a rejected/timed-out POST must NEVER use this exception type.
                self._journal_write(tag, "not_submitted")
                raise LiumNotSubmitted(str(exc)) from None
            # Preflight may have consumed time. Recheck approval and absolute
            # deadline immediately before the single side-effecting request.
            now = self.clock()
            hours = min(
                hours, math.floor((hard_deadline - now - self._ttl_margin) / 3600)
            )
            if hours < 1 or manifest.approved_until <= now:
                self._journal_write(tag, "not_submitted")
                raise LiumNotSubmitted("lium_launch_approval_expired")
            server_select = manifest.server_side_selection and selected_offer is None
            post_executor = None if server_select else selected_executor
            ttl = None
            if intent_created_at is not None:
                deadline = min(intent_created_at + hours * 3600, hard_deadline)
                if deadline <= now + self._ttl_margin:
                    self._journal_write(tag, "not_submitted")
                    raise LiumNotSubmitted("lium_insufficient_original_ttl_window")
                ttl = {
                    "version": 1,
                    "created_at": intent_created_at,
                    "hard_deadline": hard_deadline,
                    "requested_hours": hours,
                    "deadline": deadline,
                    "effective_deadline": deadline,
                    "instance_id": None,
                    "provider_created_at": None,
                    "attempts": [],
                }
            self._journal_write(
                tag, "post_started", executor_id=post_executor, absolute_ttl=ttl
            )
            self._submitted_tags.add(tag)
            payload = {
                "pod_name": name,
                "template_id": manifest.template_id,
                "user_public_key": manifest.user_public_key,
                "gpu_count": manifest.gpu_count,
                "termination_hours": hours,
            }
            route = f"executors/{selected_executor}/rent"
            if server_select:
                route = "executors/rent-by-spec"
                payload = self._spec_payload(manifest, name, dry_run=False, hours=hours)
            try:
                response = self._request(
                    "POST", route, payload=payload, rental=True, request_id=tag
                )
            except LiumRentRejected:
                self._journal_write(tag, "rejected", executor_id=post_executor)
                return ProviderFact(
                    "not_created", actual_cost_microusd=0, absence_confirmed=True
                )
            if not isinstance(response, dict) or response.get("success") is not True:
                raise LiumError("lium_creation_response_unconfirmed")
            instance = _uuid(response.get("pod_id"))
            # Preserve a successful pod identity even when a later response
            # validation fails. Reconciliation must retain that paid resource.
            if server_select:
                try:
                    self._validate_spec_response(response, manifest, dry_run=False)
                except LiumError:
                    self._journal_write(
                        tag,
                        "quarantined",
                        executor_id=post_executor,
                        instance_id=instance,
                    )
                    raise
            self._journal_write(
                tag, "confirmed", executor_id=post_executor, instance_id=instance
            )
            try:
                self._ensure_absolute_ttl(tag, instance)
            except LiumError:
                pass  # Paid identity remains acknowledged; reconciliation retries GET, never rent.
            return ProviderFact("starting", instance)

    def reconcile(self, tag, instance_id=None):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        marker = self._journal_read(_uuid(tag))
        if (
            marker
            and marker["phase"] in {"not_submitted", "rejected"}
            and instance_id is None
        ):
            return ProviderFact(
                "not_created", actual_cost_microusd=0, absence_confirmed=True
            )
        if marker and marker["phase"] in {"confirmed", "quarantined"}:
            if instance_id is not None and _uuid(instance_id) != marker["instance_id"]:
                raise LiumError("lium_instance_identity_conflict")
            if instance_id is None:
                # Recover the acknowledged identity before doing network I/O;
                # subsequent reconcile/delete can work even if GET is down now.
                return ProviderFact("starting", marker["instance_id"])
            instance_id = marker["instance_id"]
        pod = self._exact_pod(tag, instance_id)
        if pod is not None:
            try:
                self._ensure_absolute_ttl(tag, _uuid(pod["id"]))
            except LiumError:
                pass  # Readiness/lifetime/SSH remain gated by the same durable bound.
            return self._running_fact(tag, pod)
        if instance_id is not None:
            statement = self._removed_statement(tag, instance_id)
            if statement is not None:
                return statement
        # Key visibility / eventual consistency is not authoritative absence.
        if marker and marker["phase"] in {"confirmed", "quarantined"}:
            return ProviderFact("starting", instance_id)
        return ProviderFact("unknown", _uuid(instance_id) if instance_id else None)

    def destroy(self, tag, instance_id):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        pod_id = _uuid(instance_id)
        removed = self._removed_statement(tag, pod_id)
        if removed is not None:
            return removed
        pod = self._exact_pod(tag, pod_id)
        if pod is None:
            return ProviderFact("unknown", pod_id)
        # The coordinator already persisted destroy_started_at. DELETE is
        # issued once. A 2xx payload alone has no pinned terminal response
        # schema, so read the durable removed statement before releasing.
        with self._create_lock:
            if pod_id in self._destroy_submitted:
                raise LiumError("lium_destruction_already_submitted_reconcile_only")
            self._destroy_submitted.add(pod_id)
        self._request("DELETE", f"pods/{pod_id}")
        removed = self._removed_statement(tag, pod_id)
        return removed or ProviderFact("unknown", pod_id)

    def billing(self, tag, instance_id=None):
        if not self.enabled:
            raise LiumError("lium_provider_disabled")
        if instance_id is None:
            return None
        statement = self._removed_statement(tag, instance_id)
        return statement.actual_cost_microusd if statement is not None else None

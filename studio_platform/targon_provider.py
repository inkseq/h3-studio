"""Targon VM lifecycle through the existing rental coordinator.

Registration and deployment each have a durable, single-attempt barrier. A
lost deploy response permits observation, never another deploy. Importing this
module loads no credentials and sends no requests. VM deadlines are enforced
by the controller: Targon's documented VM API has no automatic TTL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import ipaddress
import json
import math
import re
import threading
import time
import uuid
from urllib.parse import urlencode

import httpx

from .lium_provider import _central_loader
from .rent_journal import RentJournal
from .repository import Conflict, money, request_hash
from .scaler import CreationNotSubmitted, LaunchSpec, ProviderFact

SERVICE = "targon"
PROFILE = "targon--rig-root"
BASE_URL = "https://api.targon.com"
KEY_VARIABLE = "TARGON_API_KEY"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 4096


class TargonError(Conflict):
    """Only static codes leave the adapter, never upstream messages."""


class TargonNotSubmitted(TargonError, CreationNotSubmitted):
    """The invocation did not submit registration or deployment."""


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise TargonError("targon_invalid_identifier")
    return value


def _tag(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise TargonError("targon_invalid_tag") from None


def _name(tag):
    # Full UUID entropy fits the provider's 32-character workload-name limit.
    return uuid.UUID(_tag(tag)).hex


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _price(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return money(
            int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        )
    except (ValueError, InvalidOperation, OverflowError):
        raise TargonError("targon_invalid_price") from None


@dataclass(frozen=True)
class TargonManifest:
    configuration_id: str
    model_id: str
    org_slug: str
    resource_name: str
    image_name: str
    ssh_key_ids: tuple[str, ...]
    gpu_count: int
    hourly_cost_cap_microusd: int
    approved_until: float
    max_lifetime_seconds: int = 7200
    execution_slots: int = 1
    gpu_model: str = "RTX-PRO-6000B"
    minimum_ram_gib: int = 0
    minimum_disk_gib: int = 0
    allow_preflight_only_price_cap: bool = False
    allow_controller_lifetime: bool = False

    def __post_init__(self):
        for value in (
            self.configuration_id,
            self.model_id,
            self.org_slug,
            self.resource_name,
            self.image_name,
        ):
            _id(value)
        if (
            not isinstance(self.ssh_key_ids, (tuple, list))
            or not 1 <= len(self.ssh_key_ids) <= 16
            or len(set(self.ssh_key_ids)) != len(self.ssh_key_ids)
        ):
            raise TargonError("targon_ssh_key_ids_required")
        for key in self.ssh_key_ids:
            _id(key)
        object.__setattr__(self, "ssh_key_ids", tuple(self.ssh_key_ids))
        if (
            type(self.gpu_count) is not int
            or not 1 <= self.gpu_count <= 8
            or type(self.execution_slots) is not int
            or not 1 <= self.execution_slots <= self.gpu_count
            or type(self.max_lifetime_seconds) is not int
            or not 120 <= self.max_lifetime_seconds <= 14400
            or not _number(self.approved_until)
            or type(self.hourly_cost_cap_microusd) is not int
            or self.hourly_cost_cap_microusd <= 0
            or any(
                type(v) is not int or v < 0
                for v in (self.minimum_ram_gib, self.minimum_disk_gib)
            )
            or not isinstance(self.gpu_model, str)
            or not re.fullmatch(r"[A-Za-z0-9 ._-]{1,128}", self.gpu_model)
            or type(self.allow_preflight_only_price_cap) is not bool
            or type(self.allow_controller_lifetime) is not bool
        ):
            raise TargonError("targon_invalid_manifest")
        money(self.hourly_cost_cap_microusd)


@dataclass(frozen=True)
class TargonIdleProof:
    instance_id: str
    observed_at: float
    idle_since: float
    idle: bool

    def __post_init__(self):
        _id(self.instance_id)
        if (
            not _number(self.observed_at)
            or not _number(self.idle_since)
            or type(self.idle) is not bool
        ):
            raise TargonError("targon_invalid_idle_proof")


class _Journal(RentJournal):
    """Reuse the private, atomic file and process-lock machinery, not Lium phases."""

    @staticmethod
    def _validate(value, tag):
        fields = {
            "version",
            "tag",
            "phase",
            "configuration_id",
            "manifest_hash",
            "created_at",
            "deadline",
            "instance_id",
            "delete_acknowledged",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["version"] != 1
            or value["tag"] != tag
            or value["phase"]
            not in {
                "register_started",
                "registered",
                "deploy_started",
                "deployed",
                "delete_started",
                "deleted",
            }
            or not isinstance(value["manifest_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["manifest_hash"])
            or not _number(value["created_at"])
            or not _number(value["deadline"])
            or value["created_at"] >= value["deadline"]
            or type(value["delete_acknowledged"]) is not bool
        ):
            raise TargonError("targon_journal_invalid")
        _id(value["configuration_id"])
        if value["instance_id"] is not None:
            _id(value["instance_id"])
        elif value["phase"] != "register_started":
            raise TargonError("targon_journal_invalid")
        return value

    def begin(self, tag, manifest, created_at, deadline):
        with self.ttl_lock(tag):
            if self.read(tag) is not None:
                raise TargonError("targon_creation_already_submitted_reconcile_only")
            value = {
                "version": 1,
                "tag": tag,
                "phase": "register_started",
                "configuration_id": manifest.configuration_id,
                "manifest_hash": request_hash(asdict(manifest)),
                "created_at": created_at,
                "deadline": deadline,
                "instance_id": None,
                "delete_acknowledged": False,
            }
            self._validate(value, tag)
            self._write(self._path(tag), value, False)
            return value

    def transition(self, tag, phases, phase, **changes):
        with self.ttl_lock(tag):
            value = self.read(tag)
            if value is None or value["phase"] not in phases:
                raise TargonError("targon_journal_transition_refused")
            if set(changes) - {"instance_id", "delete_acknowledged"}:
                raise TargonError("targon_journal_transition_refused")
            if (
                value["instance_id"] is not None
                and changes.get("instance_id", value["instance_id"])
                != value["instance_id"]
            ):
                raise TargonError("targon_instance_identity_conflict")
            result = {**value, **changes, "phase": phase}
            self._validate(result, tag)
            self._write(self._path(tag), result, True)
            return result


class TargonProvider:
    provider_id = SERVICE

    def __init__(
        self,
        *,
        enabled=False,
        manifests=(),
        journal_dir=None,
        loader=None,
        transport=None,
        idle_probe=None,
        clock=time.time,
        timeout_s=20,
        cleanup_guard=None,
    ):
        values = tuple(manifests)
        if (
            type(enabled) is not bool
            or not _number(timeout_s)
            or not 0 < timeout_s <= 30
            or any(not isinstance(item, TargonManifest) for item in values)
            or len({item.configuration_id for item in values}) != len(values)
            or enabled
            and journal_dir is None
        ):
            raise TargonError("targon_invalid_settings")
        self.enabled, self.clock = enabled, clock
        self._manifests = {item.configuration_id: item for item in values}
        self._journal = _Journal(journal_dir) if journal_dir is not None else None
        self._loader, self._transport, self._idle_probe = (
            loader or _central_loader,
            transport,
            idle_probe,
        )
        self._cleanup_guard = cleanup_guard
        self._timeout, self._client = timeout_s, None
        self._initialization_lock = threading.Lock()

    def _http(self):
        if not self.enabled:
            raise TargonError("targon_provider_disabled")
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
                        base_url=BASE_URL + "/tha/v3/",
                        trust_env=False,
                        follow_redirects=False,
                        timeout=self._timeout,
                        transport=self._transport,
                        headers={
                            "Authorization": "Bearer " + config.api_key,
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                        },
                    )
                except Exception:
                    raise TargonError(
                        "targon_profile_unavailable_or_mismatched"
                    ) from None
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()

    def _request(self, method, route, *, payload=None, missing_ok=False):
        try:
            started = time.monotonic()
            with self._http().stream(method, route, json=payload) as response:
                if response.status_code == 404 and missing_ok:
                    return None
                if not 200 <= response.status_code < 300:
                    raise TargonError("targon_request_unconfirmed")
                if method == "DELETE" and response.status_code == 204:
                    return {"deleted_acknowledged": True}
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise TargonError("targon_response_encoding_unconfirmed")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() - started > 30:
                        raise TargonError("targon_response_bounds_exceeded")
                return json.loads(raw, parse_float=Decimal)
        except TargonError:
            raise
        except Exception:
            raise TargonError("targon_request_unconfirmed") from None

    def _manifest(self, launch):
        manifest = self._manifests.get(getattr(launch, "configuration_id", None))
        if (
            not isinstance(launch, LaunchSpec)
            or manifest is None
            or launch.provider != SERVICE
            or launch.model_id != manifest.model_id
            or launch.offer_id != manifest.resource_name
            or launch.image_id != manifest.image_name
            or launch.region
            or not manifest.allow_preflight_only_price_cap
            or not manifest.allow_controller_lifetime
        ):
            raise TargonError("targon_launch_manifest_unapproved")
        return manifest

    def _marker(self, tag, instance_id=None):
        if not self.enabled or self._journal is None:
            raise TargonError("targon_provider_disabled")
        try:
            marker = self._journal.read(_tag(tag))
        except TargonError:
            raise
        except Exception:
            raise TargonError("targon_journal_unconfirmed") from None
        if marker is None:
            raise TargonError("targon_creation_marker_required")
        if instance_id is not None and marker["instance_id"] not in (
            None,
            _id(instance_id),
        ):
            raise TargonError("targon_instance_identity_conflict")
        manifest = self._manifests.get(marker["configuration_id"])
        if (
            manifest is None
            or request_hash(asdict(manifest)) != marker["manifest_hash"]
        ):
            raise TargonError("targon_manifest_identity_conflict")
        return marker, manifest

    @staticmethod
    def _route(manifest, instance_id=None):
        route = "orgs/" + manifest.org_slug + "/workloads"
        return route + "/" + _id(instance_id) if instance_id else route

    def preflight_availability(self, launch):
        if not self.enabled:
            raise TargonError("targon_provider_disabled")
        manifest = self._manifest(launch)
        rows = self._request("GET", "inventory?type=vm&gpu=true")
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_ROWS
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise TargonError("targon_invalid_inventory")
        matches = [row for row in rows if row.get("name") == manifest.resource_name]
        if len(matches) != 1:
            raise TargonError("targon_resource_unavailable")
        row = matches[0]
        spec = row.get("spec", {})
        if (
            row.get("type") != "vm"
            or type(row.get("available")) is not int
            or row["available"] < 1
            or not isinstance(spec, dict)
            or spec.get("gpu_count") != manifest.gpu_count
            or spec.get("gpu_model", spec.get("gpu_type")) != manifest.gpu_model
            or _price(row.get("cost_per_hour")) > manifest.hourly_cost_cap_microusd
        ):
            raise TargonError("targon_resource_contract_mismatch")
        for field, floor in (
            ("memory_mib", manifest.minimum_ram_gib),
            ("disk_size_mib", manifest.minimum_disk_gib),
        ):
            if floor and (not _number(spec.get(field)) or spec[field] < floor * 1024):
                raise TargonError("targon_resource_contract_mismatch")
        return {
            "available_count": row["available"],
            "hourly_cost_microusd": _price(row["cost_per_hour"]),
        }

    def validate_launch(
        self, launch, *, physical_gpus, slots, reserved_cost_microusd, hard_deadline
    ):
        manifest = self._manifest(launch)
        if not self.enabled:
            raise TargonError("targon_provider_disabled")
        required = math.ceil(
            manifest.hourly_cost_cap_microusd * manifest.max_lifetime_seconds / 3600
        )
        if (
            type(physical_gpus) is not int
            or physical_gpus != manifest.gpu_count
            or type(slots) is not int
            or slots != manifest.execution_slots
            or money(reserved_cost_microusd) < required
        ):
            raise TargonError("targon_capacity_or_reservation_mismatch")
        if (
            not _number(hard_deadline)
            or min(hard_deadline, manifest.approved_until) <= self.clock() + 60
        ):
            raise TargonError("targon_launch_approval_expired")
        return {
            "physical_gpus": physical_gpus,
            "slots": slots,
            "ttl_cap_reservation_microusd": required,
        }

    def _identity(self, value, tag, manifest, instance_id=None):
        if (
            not isinstance(value, dict)
            or value.get("name") != _name(tag)
            or value.get("type") != "VM"
            or value.get("image") != manifest.image_name
            or not isinstance(value.get("resource"), dict)
            or value["resource"].get("name") != manifest.resource_name
            or value["resource"].get("gpu_count") != manifest.gpu_count
        ):
            raise TargonError("targon_workload_identity_unconfirmed")
        identity = _id(value.get("uid"))
        if instance_id is not None and identity != instance_id:
            raise TargonError("targon_instance_identity_conflict")
        return identity

    def _post_instance(self, value, tag, manifest, instance_id=None):
        """POST acknowledgements may omit identity fields; GET proves identity.

        Retain the returned UID before observing registration so a lost GET can
        recover the same workload even when listings lag. Supplied conflicting
        fields still fail closed; missing fields never count as identity proof.
        """
        if not isinstance(value, dict):
            raise TargonError("targon_workload_identity_unconfirmed")
        identity = _id(value.get("uid", instance_id))
        if instance_id is not None and identity != instance_id:
            raise TargonError("targon_instance_identity_conflict")
        expected = {"name": _name(tag), "type": "VM", "image": manifest.image_name}
        if any(
            key in value and value[key] != expected_value
            for key, expected_value in expected.items()
        ):
            raise TargonError("targon_workload_identity_unconfirmed")
        if "resource" in value:
            resource = value["resource"]
            if not isinstance(resource, dict) or any(
                key in resource and resource[key] != expected_value
                for key, expected_value in {
                    "name": manifest.resource_name,
                    "gpu_count": manifest.gpu_count,
                }.items()
            ):
                raise TargonError("targon_workload_identity_unconfirmed")
        return identity

    def _exact(self, tag, manifest, instance_id=None):
        if instance_id is not None:
            value = self._request(
                "GET", self._route(manifest, instance_id), missing_ok=True
            )
            if value is not None:
                self._identity(value, tag, manifest, instance_id)
            return value
        matches, cursor, seen = [], None, set()
        for _ in range(8):
            query = {"name": _name(tag), "type": "VM", "limit": 500}
            if cursor:
                query["cursor"] = cursor
            value = self._request("GET", self._route(manifest) + "?" + urlencode(query))
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("items"), list)
                or len(value["items"]) > 500
                or any(not isinstance(row, dict) for row in value["items"])
            ):
                raise TargonError("targon_invalid_listing")
            for row in value["items"]:
                if row.get("name") == _name(tag):
                    self._identity(row, tag, manifest)
                    matches.append(row)
            cursor = value.get("next_cursor")
            if not cursor:
                if len(matches) > 1:
                    raise TargonError("targon_duplicate_workload_identity")
                return matches[0] if matches else None
            _id(cursor)
            if cursor in seen:
                break
            seen.add(cursor)
        raise TargonError("targon_listing_incomplete")

    def _fact(self, tag, value):
        instance = _id(value["uid"])
        state = value.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("status"), str):
            raise TargonError("targon_workload_state_unconfirmed")
        status = state["status"].lower()
        if status == "deleted":
            return ProviderFact("destroyed", instance)
        marker, _ = self._marker(tag, instance)
        if marker["phase"] == "delete_started":
            # An ACK is not removal proof. A stale running/provisioning GET
            # must not advertise boot readiness or report preparation again.
            return ProviderFact("unknown", instance)
        if status != "running":
            return ProviderFact(
                "starting",
                instance,
                provider_status="FAILED" if status == "error" else "PENDING",
                preparation_stage="provider_preparing",
            )
        proof = None
        if self._idle_probe is not None:
            try:
                proof = self._idle_probe(tag, instance)
            except Exception:
                pass
        if (
            isinstance(proof, TargonIdleProof)
            and proof.instance_id == instance
            and proof.idle
            and 0 <= self.clock() - proof.observed_at <= 30
            and proof.idle_since <= proof.observed_at
        ):
            return ProviderFact(
                "running", instance, idle_confirmed=True, idle_since=proof.idle_since
            )
        return ProviderFact("running", instance)

    def create_for_intent(self, tag, launch, *, hard_deadline, intent_created_at):
        return self.create(
            tag,
            launch,
            hard_deadline=hard_deadline,
            intent_created_at=intent_created_at,
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

    def create(
        self, tag, launch, *, hard_deadline, intent_created_at=None, selected_offer=None
    ):
        tag, manifest = _tag(tag), self._manifest(launch)
        if not self.enabled or self._journal is None:
            raise TargonError("targon_provider_disabled")
        if self._journal.read(tag) is not None:
            raise TargonError("targon_creation_already_submitted_reconcile_only")
        created = self.clock() if intent_created_at is None else intent_created_at
        if not _number(created) or created > self.clock() or not _number(hard_deadline):
            raise TargonError("targon_invalid_deadline")
        deadline = min(
            hard_deadline,
            manifest.approved_until,
            created + manifest.max_lifetime_seconds,
        )
        if self._exact(tag, manifest) is not None:
            raise TargonError("targon_preexisting_workload_requires_reconciliation")
        # This pre-mutation block alone can establish non-submission.
        try:
            if deadline <= self.clock() + 60:
                raise TargonError("targon_launch_approval_expired")
            stock = self.preflight_availability(launch)
            if selected_offer is not None:
                if (
                    selected_offer.get("provider") != SERVICE
                    or selected_offer.get("offer_id") != manifest.resource_name
                    or selected_offer.get("gpu_count") != manifest.gpu_count
                    or not _number(selected_offer.get("hourly_cost_microusd"))
                    or stock["hourly_cost_microusd"]
                    > selected_offer["hourly_cost_microusd"]
                ):
                    raise TargonError("targon_selected_offer_unavailable")
        except TargonError as error:
            raise TargonNotSubmitted(str(error)) from None
        if deadline <= self.clock() + 60:
            raise TargonNotSubmitted("targon_launch_approval_expired")
        self._journal.begin(tag, manifest, created, deadline)
        value = self._request(
            "POST",
            self._route(manifest),
            payload={
                "type": "VM",
                "name": _name(tag),
                "image": manifest.image_name,
                "resource_name": manifest.resource_name,
                "ssh_keys": list(manifest.ssh_key_ids),
                "vm_config": {"hostname": _name(tag)},
            },
        )
        instance = self._post_instance(value, tag, manifest)
        self._journal.transition(
            tag, {"register_started"}, "register_started", instance_id=instance
        )
        value = self._exact(tag, manifest, instance)
        if value is None:
            raise TargonError("targon_workload_identity_unconfirmed")
        self._journal.transition(
            tag, {"register_started"}, "registered", instance_id=instance
        )
        return self._deploy(tag, manifest, value)

    def _deploy(self, tag, manifest, value):
        marker, _ = self._marker(tag, value["uid"])
        # No deploy can occur after its original approval/deadline, including
        # recovery of a registered workload after a lost registration response.
        if marker["deadline"] <= self.clock() + 60:
            return ProviderFact(
                "starting", marker["instance_id"], provider_status="STOPPED"
            )
        if value.get("state", {}).get("status") != "registered":
            return self._fact(tag, value)
        if self._cleanup_proof(marker, arm=True) is None:
            raise TargonError("targon_independent_cleanup_unconfirmed")
        self._journal.transition(tag, {"registered"}, "deploy_started")
        value = self._request(
            "POST", self._route(manifest, marker["instance_id"]) + "/deploy"
        )
        self._post_instance(value, tag, manifest, marker["instance_id"])
        value = self._exact(tag, manifest, marker["instance_id"])
        if value is None:
            raise TargonError("targon_workload_identity_unconfirmed")
        self._journal.transition(tag, {"deploy_started"}, "deployed")
        return self._fact(tag, value)

    def reconcile(self, tag, instance_id=None):
        marker, manifest = self._marker(tag, instance_id)
        instance = marker["instance_id"] or instance_id
        if marker["phase"] == "deleted":
            return ProviderFact("destroyed", instance)
        value = self._exact(tag, manifest, instance)
        if value is None:
            if (
                marker["phase"] == "delete_started"
                and marker["delete_acknowledged"]
                or self._cleanup_removed(marker)
            ):
                self._journal.transition(
                    tag,
                    {"registered", "deploy_started", "deployed", "delete_started"},
                    "deleted",
                )
                return ProviderFact("destroyed", instance)
            return ProviderFact("unknown", instance)
        if marker["phase"] == "register_started":
            self._journal.transition(
                tag, {"register_started"}, "registered", instance_id=value["uid"]
            )
            marker, manifest = self._marker(tag, value["uid"])
        if marker["phase"] == "registered":
            return self._deploy(tag, manifest, value)
        return self._fact(tag, value)

    def destroy(self, tag, instance_id):
        marker, manifest = self._marker(tag, instance_id)
        if marker["instance_id"] != instance_id:
            raise TargonError("targon_instance_identity_conflict")
        if marker["phase"] in {"delete_started", "deleted"}:
            return self.reconcile(tag, instance_id)
        value = self._exact(tag, manifest, instance_id)
        if value is None:
            return ProviderFact("unknown", instance_id)
        if value.get("state", {}).get("status") == "deleted":
            return ProviderFact("destroyed", instance_id)
        self._journal.transition(
            tag, {"registered", "deploy_started", "deployed"}, "delete_started"
        )
        value = self._request("DELETE", self._route(manifest, instance_id))
        if value != {"deleted_acknowledged": True}:
            raise TargonError("targon_delete_unconfirmed")
        self._journal.transition(
            tag, {"delete_started"}, "delete_started", delete_acknowledged=True
        )
        return self.reconcile(tag, instance_id)

    def billing(self, tag, instance_id=None):
        self._marker(tag, instance_id)
        # No documented VM invoice endpoint. Never substitute an hourly estimate
        # or a deletion acknowledgement for an actual final charge.
        return None

    def execution_allowed(self, tag, instance_id):
        marker, _ = self._marker(tag, instance_id)
        return (
            marker["instance_id"] == instance_id
            and marker["phase"] in {"deploy_started", "deployed"}
            and self.clock() < marker["deadline"]
            and self._cleanup_proof(marker) is not None
        )

    def _cleanup_proof(self, marker, *, arm=False):
        """Only the injected independent guardian can attest its own registration.

        arm must be idempotent for the exact workload/deadline. This adapter
        neither implements a timer nor labels controller liveness a TTL proof.
        """
        if self._cleanup_guard is None or marker["instance_id"] is None:
            return None
        try:
            if arm:
                self._cleanup_guard.arm(marker["instance_id"], marker["deadline"])
            proof = self._cleanup_guard.proof(marker["instance_id"])
            if (
                not isinstance(proof, dict)
                or proof.get("instance_id") != marker["instance_id"]
                or proof.get("deadline") != marker["deadline"]
                or proof.get("independent") is not True
                or proof.get("armed") is not True
                or self.clock() >= marker["deadline"]
            ):
                return None
            return proof
        except Exception:
            return None

    def _cleanup_removed(self, marker):
        if self._cleanup_guard is None or marker["instance_id"] is None:
            return False
        try:
            proof = self._cleanup_guard.removal_proof(marker["instance_id"])
            return (
                isinstance(proof, dict)
                and proof.get("instance_id") == marker["instance_id"]
                and proof.get("deadline") == marker["deadline"]
                and proof.get("independent") is True
                and proof.get("removed") is True
                and proof.get("evidence")
                in {"exact_uid_deleted", "exact_uid_404_after_delete_ack"}
            )
        except Exception:
            return False

    def ssh_connection(self, tag, instance_id):
        marker, manifest = self._marker(tag, instance_id)
        if not self.execution_allowed(tag, instance_id):
            raise TargonError("targon_execution_not_allowed")
        value = self._exact(tag, manifest, instance_id)
        try:
            state = value["state"]
            host = str(ipaddress.ip_address(state["public_ip"]))
            port = state["ssh_port"]
            if (
                state["status"] != "running"
                or not ipaddress.ip_address(host).is_global
                or type(port) is not int
                or not 1 <= port <= 65535
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise TargonError("targon_ssh_coordinates_unverified") from None
        return {
            "instance_id": instance_id,
            "host": host,
            "port": port,
            "username": "ubuntu",
        }

    def lifetime(self, tag, instance_id, *, local_created_at, maximum_hours=4):
        marker, manifest = self._marker(tag, instance_id)
        if (
            type(maximum_hours) is not int
            or not 1 <= maximum_hours <= 4
            or local_created_at != marker["created_at"]
        ):
            raise TargonError("targon_lifetime_identity_unconfirmed")
        value = self._exact(tag, manifest, instance_id)
        try:
            created = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
            if (
                created.tzinfo is None
                or abs(created.timestamp() - local_created_at) > 300
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TargonError("targon_lifetime_identity_unconfirmed") from None
        deadline = min(marker["deadline"], local_created_at + maximum_hours * 3600)
        guarded = self._cleanup_proof(marker) is not None
        if not guarded:
            raise TargonError("targon_independent_cleanup_unconfirmed")
        return {
            "instance_id": instance_id,
            "provider_created_at": value["created_at"],
            "provider_removal_scheduled_at": None,
            "safe_deadline": deadline,
            "enforcement": "independent_watchdog",
            "provider_ttl_confirmed": False,
            "execution_allowed": True,
            "independent_cleanup_deadline": marker["deadline"],
            "timezone_evidence": "explicit_offset",
            "safety_margin_seconds": 0,
        }

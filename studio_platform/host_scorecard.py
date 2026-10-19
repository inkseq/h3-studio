"""Owner-safe host scorecard and audited operator policy.

Original nodes, attempts, media and outbox remain the evidence. Policy lives in
the existing operator_policy table. No rental, balance or task mutation occurs.
"""

from __future__ import annotations

import copy
import math
import re
import statistics
import uuid

from sqlalchemy import case, cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB

from .host_policy import (
    HostPolicyStore,
    REASONS,
    bound_observation,
    failure,
    finite,
    numeric_id,
    original_nodes,
)
from .operator_capacity import operator_commands, operator_policy, require, safe_id
from .repository import artifacts, attempts, jobs, outbox, request_hash

RENTAL_STATES = {
    "reserved",
    "creating",
    "creation_unknown",
    "starting",
    "runtime_unconfirmed",
    "ready",
    "busy",
    "draining",
    "stopping",
    "stopped",
    "observation_unknown",
    "removal_unknown",
}
PHASES = {
    "provisioning",
    "image_pull",
    "model_download",
    "runtime_start",
    "model_load",
    "runtime_ready",
    "stopping",
    "stopped",
}
CONTROL_KEYS = (
    "width",
    "height",
    "steps",
    "duration",
    "frames",
    "fps",
    "image_refs",
    "video_refs",
    "audio_refs",
)


def _actor(tenant, owner):
    return "dstack:" + request_hash({"tenant_id": tenant, "owner_id": owner})


def _median(values):
    return statistics.median(values) if values else None


def _controls(row):
    delivery = row.get("delivery") if isinstance(row.get("delivery"), dict) else {}
    result = {}
    for key in CONTROL_KEYS:
        value = row.get(key)
        if key in {"width", "height", "fps"}:
            value = delivery.get(key, value)
        elif key == "frames":
            value = delivery.get("frame_count", value)
        result[key] = value if finite(value) else None
    return result


class OperatorHosts:
    def __init__(self, repo, settings, stage_reader=None):
        self.repo, self.settings, self.stage_reader = repo, settings, stage_reader
        self.store = HostPolicyStore(repo, settings.tenant_id)

    def _authorize(self, principal, *, admin=False):
        from .identity import require_admin

        owner = require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_capacity=True,
            legacy_code="operator_forbidden",
        )
        if admin:
            require_admin(
                principal,
                self.repo,
                self.settings,
                legacy_code="operator_admin_forbidden",
            )
        return owner

    def _page(self, limit, offset):
        require(
            type(limit) is int
            and 1 <= limit <= 100
            and type(offset) is int
            and 0 <= offset <= 100000,
            "host_page_invalid",
            422,
        )

    def _successful(self, connection, nodes, *, now):
        identities = {
            "dstack-" + node["intent_id"].replace("-", ""): node for node in nodes
        }
        if not identities:
            return {}
        effective = jobs.c.request["request"]
        fields = [
            jobs.c.id.label("job_id"),
            attempts.c.id.label("attempt_id"),
            attempts.c.worker_id,
            attempts.c.submission_started_at,
            jobs.c.updated_at.label("completed_at"),
            jobs.c.owner_id,
            jobs.c.request["deployment_profile_id"].as_string().label("profile_id"),
            effective["mode"].as_string().label("mode"),
            jobs.c.request["output_spec"].label("delivery"),
        ]
        for key in ("width", "height", "steps", "duration", "frames", "fps"):
            fields.append(effective[key].as_float().label(key))
        for key, field in (
            ("images", "image_refs"),
            ("videos", "video_refs"),
            ("audios", "audio_refs"),
        ):
            array = effective["inputs"][key]
            if connection.dialect.name == "postgresql":
                array = cast(array, JSONB)
                value = case(
                    (
                        func.jsonb_typeof(array) == "array",
                        func.jsonb_array_length(array),
                    ),
                    else_=None,
                )
            else:
                value = case(
                    (func.json_type(array) == "array", func.json_array_length(array)),
                    else_=None,
                )
            fields.append(value.label(field))
        # EXISTS gives one original attempt per video without DISTINCT on a
        # JSON column (PostgreSQL JSON has no equality operator).
        video = (
            select(artifacts.c.id)
            .where(
                artifacts.c.job_id == jobs.c.id,
                artifacts.c.attempt_id == attempts.c.id,
                artifacts.c.metadata["kind"].as_string() == "video",
                artifacts.c.metadata["validated"].as_boolean().is_(True),
            )
            .exists()
        )
        q = (
            select(*fields)
            .select_from(
                jobs.join(
                    attempts,
                    (attempts.c.id == jobs.c.current_attempt_id)
                    & (attempts.c.job_id == jobs.c.id),
                )
            )
            .where(
                jobs.c.tenant_id == self.settings.tenant_id,
                jobs.c.status == "succeeded",
                attempts.c.status == "succeeded",
                attempts.c.worker_id.in_(identities),
                video,
            )
        )
        rows = list(connection.execute(q.limit(4097)).mappings())
        require(len(rows) <= 4096, "host_sample_limit_exceeded")
        result = {}
        for item in rows:
            node = identities[item["worker_id"]]
            payload, binding = node["payload"], node["payload"]["dstack"]
            if (
                item["owner_id"] != payload["owner_id"]
                or item["profile_id"] != binding["profile_id"]
                or item["mode"] != binding["mode"]
                or not finite(item["submission_started_at"], minimum=1, maximum=now)
                or not finite(
                    item["completed_at"],
                    minimum=item["submission_started_at"],
                    maximum=now,
                )
            ):
                continue
            result.setdefault(node["intent_id"], []).append(
                {
                    **dict(item),
                    "node_id": node["intent_id"],
                    "provider": "vast",
                    "warmth": "unknown",
                    "controls": _controls(dict(item)),
                }
            )
        return result

    def _failed(self, connection, nodes, *, now):
        from .generation_diagnostics import WANGP_VALIDATION_CODES, COMMON_CODES
        from .inference.protocol import INFERENCE_FAILURE_CODES

        codes = WANGP_VALIDATION_CODES | COMMON_CODES | INFERENCE_FAILURE_CODES
        identities = {"dstack-" + n["intent_id"].replace("-", ""): n for n in nodes}
        if not identities:
            return {}
        rows = list(
            connection.execute(
                select(
                    jobs.c.owner_id,
                    jobs.c.request["deployment_profile_id"]
                    .as_string()
                    .label("profile_id"),
                    jobs.c.request["request"]["mode"].as_string().label("mode"),
                    attempts.c.worker_id,
                    attempts.c.error_code,
                    attempts.c.updated_at,
                )
                .select_from(
                    jobs.join(
                        attempts,
                        (attempts.c.job_id == jobs.c.id)
                        & (attempts.c.id == jobs.c.current_attempt_id),
                    )
                )
                .where(
                    jobs.c.tenant_id == self.settings.tenant_id,
                    jobs.c.status == "failed",
                    attempts.c.status == "failed",
                    attempts.c.submission_started_at.is_not(None),
                    attempts.c.worker_id.in_(identities),
                )
                .limit(4097)
            ).mappings()
        )
        require(len(rows) <= 4096, "host_sample_limit_exceeded")
        result = {}
        for row in rows:
            node = identities[row["worker_id"]]
            binding = node["payload"]["dstack"]
            if (
                row["owner_id"] == node["payload"]["owner_id"]
                and row["profile_id"] == binding["profile_id"]
                and row["mode"] == binding["mode"]
                and finite(row["updated_at"], minimum=1, maximum=now)
            ):
                result.setdefault(node["intent_id"], []).append(
                    {
                        "stage": "generation",
                        "at": row["updated_at"],
                        "reason_code": row["error_code"]
                        if row["error_code"] in codes
                        else "unknown_error",
                        "outcome": "failure",
                    }
                )
        return result

    def _project(
        self,
        connection,
        machine,
        nodes,
        samples,
        *,
        now,
        all_nodes=None,
        confirmed=False,
        failed=None,
    ):
        physical = [(node, bound_observation(node, now=now)) for node in nodes]
        physical = [
            (node, value)
            for node, value in physical
            if value and value["machine_id"] == machine
        ]
        latest = max(
            (value for _, value in physical),
            key=lambda v: v["observed_at"],
            default=None,
        )
        policy = self.store.read(connection, machine, nodes=all_nodes, now=now)[0]
        failures, diagnostics, failed_nodes = [], [], set()
        execution_failures = []
        from .dstack_preparation import DIAGNOSTICS, NORMAL

        for node, _ in physical:
            try:
                item = failure(node["payload"].get("host_failure"), now=now)
                failures.append({**item, "outcome": "failure"})
                failed_nodes.add(node["intent_id"])
            except ValueError:
                pass
            observed = (failed or {}).get(node["intent_id"], [])
            execution_failures.extend(observed)
            if observed:
                failed_nodes.add(node["intent_id"])
            binding = node["payload"]["dstack"]
            code = binding.get("reason_code")
            at = binding.get("observed_at")
            if (
                finite(at, minimum=1, maximum=now)
                and code in DIAGNOSTICS
                and code not in NORMAL
            ):
                diagnostics.append(
                    {
                        "stage": binding.get("preparation", {}).get("phase", "unknown"),
                        "reason_code": code,
                        "at": at,
                        "outcome": "unknown",
                    }
                )
            if finite(node["payload"].get("stop_requested_at"), minimum=1, maximum=now):
                diagnostics.append(
                    {
                        "stage": "stopping",
                        "reason_code": "operator_stop_requested",
                        "at": node["payload"]["stop_requested_at"],
                        "outcome": "cancelled",
                    }
                )
        rentals, groups = [], {}
        for node, value in physical:
            binding = node["payload"]["dstack"]
            phase = binding.get("preparation", {}).get("phase")
            rentals.append(
                {
                    "node_id": node["intent_id"],
                    "dstack_run_id": binding.get("run_id"),
                    "offer_id": value["offer_id"],
                    "provider_instance_id": value["provider_instance_id"],
                    "state": node["runtime_state"]
                    if node["runtime_state"] in RENTAL_STATES
                    else "unknown",
                    "stage": phase if phase in PHASES else None,
                    "observed_at": value["observed_at"],
                    "quoted_hourly_microusd": value["quoted_hourly_microusd"],
                    "region": value["region"],
                    "profile_id": binding["profile_id"],
                    "mode": binding["mode"],
                }
            )
            for sample in samples.get(node["intent_id"], []):
                controls = sample["controls"]
                key = (
                    sample["profile_id"],
                    sample["mode"],
                    sample["warmth"],
                    *(controls[k] for k in CONTROL_KEYS),
                )
                group = groups.setdefault(
                    key,
                    {
                        "profile_id": sample["profile_id"],
                        "mode": sample["mode"],
                        "resolution": (
                            f'{int(controls["width"])}x{int(controls["height"])}'
                            if controls["width"] and controls["height"]
                            else None
                        ),
                        "steps": controls["steps"],
                        "warmth": sample["warmth"],
                        "duration_s": controls["duration"],
                        "width": controls["width"],
                        "height": controls["height"],
                        "frames": controls["frames"],
                        "fps": controls["fps"],
                        "image_refs": controls["image_refs"],
                        "video_refs": controls["video_refs"],
                        "audio_refs": controls["audio_refs"],
                        "samples": 0,
                        "cold_start_samples": 0,
                        "generation_samples": 0,
                        "median_cold_start_s": None,
                        "median_generation_s": None,
                        "cost_per_video_microusd": None,
                        "cost_basis": "quote_estimate",
                        "download_mb_s": None,
                        "cold_start_basis": "bootstrap_to_native_worker_ready",
                        "download_basis": "network_transfer_unobserved",
                        "_generation": [],
                        "_cold": {},
                        "_cost": [],
                    },
                )
                group["samples"] += 1
                duration = sample.get("generation_seconds")
                if finite(duration, minimum=0.000001):
                    group["_generation"].append(duration)
                start, ready = (
                    binding.get("apply_started_at"),
                    binding.get("first_runtime_ready_at"),
                )
                if (
                    finite(start, minimum=1)
                    and finite(ready, minimum=start, maximum=now)
                    and ready > start
                ):
                    cold = ready - start
                    group["_cold"][node["intent_id"]] = cold
                    if finite(duration, minimum=0.000001):
                        group["_cost"].append(
                            value["quoted_hourly_microusd"]
                            * (duration + cold / len(samples[node["intent_id"]]))
                            / 3600
                        )
        for group in groups.values():
            spans = group.pop("_generation")
            group["generation_samples"] = len(spans)
            group["median_generation_s"] = _median(spans)
            cold = list(group.pop("_cold").values())
            group["cold_start_samples"] = len(cold)
            group["median_cold_start_s"] = _median(cold)
            cost = _median(group.pop("_cost"))
            group["cost_per_video_microusd"] = (
                math.ceil(cost) if cost is not None else None
            )
        actual_failures = failures + execution_failures
        diagnostics.extend(actual_failures)
        latest_fields = (
            "observed_at",
            "region",
            "gpu",
            "gpu_count",
            "allocated_ram_mib",
            "allocated_vram_mib",
            "cpu",
            "reliability",
            "quoted_hourly_microusd",
        )
        return {
            "provider": "vastai",
            "machine_id": machine,
            "identity_confirmed": bool(latest or confirmed),
            "physical_id_source": "vast_machine_id"
            if latest or confirmed
            else "operator_supplied_numeric",
            "policy": policy,
            "runs": len(physical),
            "failure_count": len(failed_nodes),
            "qualified_start_failure_count": len(failures),
            "execution_failure_count": len(execution_failures),
            "failure_rate": len(failed_nodes) / len(physical) if physical else None,
            "last_failure": (
                {
                    key: max(actual_failures, key=lambda f: f["at"])[key]
                    for key in ("stage", "reason_code", "at", "outcome")
                }
                if actual_failures
                else None
            ),
            "last_diagnostic": (
                {
                    key: max(diagnostics, key=lambda f: f["at"])[key]
                    for key in ("stage", "reason_code", "at", "outcome")
                }
                if diagnostics
                else None
            ),
            "last_observed_at": latest["observed_at"] if latest else None,
            "latest_observation": {key: latest[key] for key in latest_fields}
            if latest
            else None,
            "rentals": sorted(
                rentals,
                key=lambda row: (row["observed_at"], row["node_id"]),
                reverse=True,
            )[:10],
            "groups": list(groups.values()),
        }

    def list(self, principal, *, limit=100, offset=0, admin=False, machine_id=None):
        owner = self._authorize(principal, admin=admin)
        self._page(limit, offset)
        if machine_id is not None:
            numeric_id(machine_id)
        now = self.repo.clock()
        with self.repo._budget_read() as connection:
            all_nodes = original_nodes(connection, self.settings.tenant_id)
            nodes = (
                all_nodes
                if admin
                else [n for n in all_nodes if n["payload"]["owner_id"] == owner]
            )
            found = {
                value["machine_id"]
                for node in nodes
                if (value := bound_observation(node, now=now))
            }
            if admin:
                found |= self.store.known_policy_ids(connection)
                if machine_id is not None:
                    found.add(machine_id)
            elif machine_id is not None:
                require(machine_id in found, "host_not_found", 404)
            if machine_id is not None:
                found &= {machine_id}
            ids = sorted(found, key=int)
            page = ids[offset : offset + limit]
            selected_nodes = [
                n
                for n in nodes
                if (value := bound_observation(n, now=now))
                and value["machine_id"] in page
            ]
            samples = self._successful(connection, selected_nodes, now=now)
            failed = self._failed(connection, selected_nodes, now=now)
            self._stage_samples(samples, now)
            hosts = [
                self._project(
                    connection,
                    machine,
                    selected_nodes,
                    samples,
                    now=now,
                    all_nodes=all_nodes,
                    failed=failed,
                )
                for machine in page
            ]
        return {
            "account": owner,
            "admin": admin,
            "as_of": now,
            "provider": "vastai",
            "hosts": hosts,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(ids),
        }

    def _stage_samples(self, samples, now):
        reader = getattr(self.stage_reader, "host_samples", None)
        if not callable(reader):
            return
        eligible = sorted(
            [item for values in samples.values() for item in values],
            key=lambda x: x["completed_at"],
            reverse=True,
        )[:32]
        try:
            observed = reader(attempts=eligible, now=now)
            matched = {(row["job_id"], row["attempt_id"]): row for row in observed}
            for values in samples.values():
                for sample in values:
                    row = matched.get((sample["job_id"], sample["attempt_id"]))
                    if row:
                        sample["generation_seconds"] = row["duration_seconds"]
                        sample["warmth"] = row["warmth"]
        except Exception:
            pass

    def histories(
        self, connection, owner, offers, *, profile_id, observed_at, expires_at
    ):
        now = self.repo.clock()
        all_nodes = original_nodes(connection, self.settings.tenant_id)
        nodes = [n for n in all_nodes if n["payload"]["owner_id"] == owner]
        selected = {numeric_id(row["machine_id"]) for row in offers}
        nodes = [
            n
            for n in nodes
            if (value := bound_observation(n, now=now))
            and value["machine_id"] in selected
        ]
        samples = self._successful(connection, nodes, now=now)
        failed = self._failed(connection, nodes, now=now)
        self._stage_samples(samples, now)
        summaries = {
            machine: self._project(
                connection,
                machine,
                nodes,
                samples,
                now=now,
                all_nodes=all_nodes,
                confirmed=True,
                failed=failed,
            )
            for machine in selected
        }
        return {
            numeric_id(row["offer_id"]): [
                {
                    **copy.deepcopy(summaries[row["machine_id"]]),
                    "offer_id": row["offer_id"],
                    "capacity_profile_id": profile_id,
                    "offer_observed_at": observed_at,
                    "selection_expires_at": expires_at,
                }
            ]
            for row in offers
        }

    def policy(self, principal, body, key):
        owner = self._authorize(principal, admin=True)
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "provider",
                "machine_id",
                "policy",
                "reason_code",
                "expected_revision",
                "expected_snapshot",
            }
            and body["provider"] == "vastai"
            and isinstance(body["policy"], str)
            and body["policy"] in REASONS
            and body["reason_code"] == REASONS[body["policy"]]
            and type(body["expected_revision"]) is int
            and body["expected_revision"] >= 0
            and isinstance(body["expected_snapshot"], str)
            and re.fullmatch(r"[0-9a-f]{64}", body["expected_snapshot"])
            and safe_id(key),
            "host_policy_request_invalid",
            422,
        )
        machine = numeric_id(body["machine_id"])
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "host_policy", "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            old_command = self.repo._locked(
                connection,
                select(operator_commands).where(
                    operator_commands.c.actor == actor,
                    operator_commands.c.idempotency_key == key,
                ),
            )
            if old_command:
                require(
                    old_command["kind"] == "host_policy"
                    and old_command["request_hash"] == digest,
                    "operator_idempotency_conflict",
                )
                return copy.deepcopy(old_command["payload"]["result"])
            old, value = self.store.read(connection, machine)
            require(
                old["revision"] == body["expected_revision"]
                and old["snapshot"] == body["expected_snapshot"],
                "host_policy_conflict",
            )
            changed = old["manual"] != body["policy"]
            now, command_id, event_id = self.repo.clock(), str(uuid.uuid4()), None
            if changed:
                value.update(manual=body["policy"], reason_code=body["reason_code"])
                row = {
                    "version": old["revision"] + 1,
                    "payload": value,
                    "actor": principal.actor_id,
                    "updated_at": now,
                }
                if old["revision"]:
                    connection.execute(
                        update(operator_policy)
                        .where(operator_policy.c.id == self.store.key(machine))
                        .values(**row)
                    )
                else:
                    connection.execute(
                        insert(operator_policy).values(
                            id=self.store.key(machine), **row
                        )
                    )
                event_id = str(uuid.uuid4())
            new = self.store.read(connection, machine)[0]
            receipt = {
                "state": "completed",
                "changed": changed,
                "host": {"provider": "vastai", "machine_id": machine},
                "policy": new,
                "audit_event_id": event_id,
                "command_id": command_id,
            }
            connection.execute(
                insert(operator_commands).values(
                    id=command_id,
                    actor=actor,
                    idempotency_key=key,
                    request_hash=digest,
                    kind="host_policy",
                    state="completed",
                    payload={"result": receipt},
                    reason_code=body["reason_code"],
                    created_at=now,
                    updated_at=now,
                )
            )
            if changed:
                fields = ("manual", "revision", "snapshot")
                connection.execute(
                    insert(outbox).values(
                        id=event_id,
                        event_type="operator.host_policy_updated",
                        aggregate_id=machine,
                        payload={
                            "tenant_id": self.settings.tenant_id,
                            "provider": "vastai",
                            "machine_id": machine,
                            "actor_id": principal.actor_id,
                            "actor_owner": owner,
                            "reason_code": body["reason_code"],
                            "old": {k: old[k] for k in fields},
                            "new": {k: new[k] for k in fields},
                            "command_id": command_id,
                        },
                        created_at=now,
                    )
                )
            return receipt

    def audit(self, principal, *, limit=100, offset=0):
        owner = self._authorize(principal, admin=True)
        self._page(limit, offset)
        with self.repo._budget_read() as connection:
            rows = list(
                connection.execute(
                    select(outbox)
                    .where(
                        outbox.c.event_type == "operator.host_policy_updated",
                        outbox.c.payload["tenant_id"].as_string()
                        == self.settings.tenant_id,
                    )
                    .order_by(outbox.c.created_at.desc(), outbox.c.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                ).mappings()
            )
            fields = (
                "provider",
                "machine_id",
                "actor_id",
                "actor_owner",
                "reason_code",
                "old",
                "new",
                "command_id",
            )
            events = [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    **{k: copy.deepcopy(row["payload"][k]) for k in fields},
                }
                for row in rows[:limit]
            ]
        return {
            "account": owner,
            "admin": True,
            "as_of": self.repo.clock(),
            "events": events,
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }

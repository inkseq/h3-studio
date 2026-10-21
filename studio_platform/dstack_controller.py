"""CPU reconciliation and one guarded demand decision; never replays a GPU POST.

The original database owns leases, budgets, jobs and drain decisions. dstack
owns capacity. Native readiness owns the private model endpoint. PostgreSQL
notifications wake original-ledger consumers. All dependencies are injected; imports have no I/O.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import argparse
import signal
import threading

from sqlalchemy import or_, select

from .control import WorkerControl, worker_spec_payload
from .dstack_capacity import idle_action, _owns, _terminal_no_allocation_shape
from .demand_wakeup import IDLE_SAFETY_SECONDS
from .dstack_preparation import download_timebox_due
from .dstack_operator import (
    BACKEND_MARKER,
    _request,
    _cold_unadmitted_runtime,
    _window_open,
)
from .operator_capacity import operator_commands, operator_nodes
from .repository import (
    BudgetExceeded,
    attempts,
    instance_intents,
    jobs,
    registered_workers,
    request_hash,
    scaler_actions,
)
from .worker import _slot_lock
from .telemetry import NullTelemetry, configured_telemetry

TERMINAL = {"succeeded", "failed", "cancelled"}


def _terminal_review_candidate(run, binding, project):
    """Scheduling evidence only; an unknown allocator result stays unknown."""
    try:
        from .dstack_allocation_proof import ATTEMPT_FIELDS, PREFIX, _unique_object

        _owns(run, binding, project)
        if (
            run.get("id") != binding.get("run_id")
            or not _terminal_no_allocation_shape(run, binding)
            or run["jobs"][0].get("job_connection_info") is not None
        ):
            return False
        message = run["latest_job_submission"].get("termination_reason_message")
        if (
            not isinstance(message, str)
            or len(message.encode()) > 8192
            or not message.startswith(PREFIX)
        ):
            return False
        marker = json.loads(message[len(PREFIX) :], object_pairs_hook=_unique_object)
        selected = {item["offer_id"] for item in binding["host_selection"]["offers"]}
        return (
            isinstance(marker, dict)
            and set(marker) == {"version", "outcome", "attempted_count", "attempts"}
            and type(marker["version"]) is int
            and marker["version"] == 1
            and marker["outcome"] == "unknown"
            and isinstance(marker["attempts"], list)
            and 1 <= len(marker["attempts"]) <= 25
            and type(marker["attempted_count"]) is int
            and marker["attempted_count"] == len(marker["attempts"])
            and all(
                isinstance(item, dict)
                and set(item) == ATTEMPT_FIELDS
                and item["ask_id"] in selected
                and item["contract_id"] is None
                for item in marker["attempts"]
            )
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class DstackController:
    def __init__(self, service, runtime, *, worker_process=None, telemetry=None):
        if service.capacity is None:
            raise ValueError("dstack_capacity_disabled")
        self.service, self.repo, self.runtime = service, service.repo, runtime
        self.capacity = service.capacity
        self.capacity.readiness = runtime.readiness
        self.control = WorkerControl(
            self.repo, capacity_usage=service.registration_capacity_usage
        )
        self.worker_process = worker_process
        # The existing original-row writer publishes bounded allocation facts.
        # Import/construction stays offline; only the explicit entry point loads
        # the already mounted Cloud configuration. Export never owns state.
        self.telemetry = telemetry if telemetry is not None else NullTelemetry()
        self.capacity.store.telemetry = self.telemetry
        self._cleanup_pending = set()
        # Scheduling stamps only. They never authorize admission, retirement,
        # settlement or a claim that the original PUT allocated nothing.
        self._passive_unknown = {}
        self._removal_checked_at = {}
        self._invoice_checked_at = None

    def _business_demand(self, connection):
        if connection.execute(
            select(jobs.c.id)
            .where(
                jobs.c.tenant_id == self.service.settings.tenant_id,
                jobs.c.execution_plan["dispatch_backend"].as_string() == "hatchet-v1",
                jobs.c.status.in_(
                    (
                        "waiting_capacity",
                        "queued",
                        "claimed",
                        "submitting",
                        "running",
                        "collecting",
                        "submission_unknown",
                        "cancel_requested",
                        "recovery_hold",
                    )
                ),
            )
            .limit(1)
        ).first():
            return True
        return (
            connection.execute(
                select(attempts.c.id)
                .select_from(attempts.join(jobs, attempts.c.job_id == jobs.c.id))
                .where(
                    jobs.c.tenant_id == self.service.settings.tenant_id,
                    jobs.c.execution_plan["dispatch_backend"].as_string()
                    == "hatchet-v1",
                    or_(
                        attempts.c.submission_started_at.is_not(None),
                        attempts.c.upstream_task_id.is_not(None),
                    ),
                    or_(
                        attempts.c.upstream_stopped != 1,
                        attempts.c.upstream_stopped.is_(None),
                    ),
                )
                .limit(1)
            ).first()
            is not None
        )

    def _passive_fingerprint(self, node):
        binding = node["payload"].get("dstack", {})
        if (
            node["desired_state"] != "stopped"
            or node["runtime_state"] != "creation_unknown"
            or binding.get("reason_code") != "dstack_allocation_requires_reconciliation"
            or binding.get("apply_started") is not True
            or binding.get("stop_started")
            or not binding.get("run_id")
            or binding.get("registration_observation")
            or binding.get("broker_observation")
            or binding.get("consumer_observation")
            or binding.get("host_observation")
            or node["payload"].get("host_observation")
            or any(
                binding.get(key) is not None
                for key in (
                    "provider_instance_id",
                    "runtime_incarnation",
                    "first_runtime_ready_at",
                )
            )
            or any(
                binding.get(key) not in (None, False)
                for key in (
                    "bootstrap_started",
                    "bootstrap_launch_started",
                    "runtime_launch_started",
                )
            )
        ):
            return None
        with self.repo.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
            current = (
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == node["intent_id"]
                    )
                )
                .mappings()
                .one()
            )
            intent = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == node["intent_id"]
                    )
                )
                .mappings()
                .one()
            )
            # A stopped uncertain start can already be in the original
            # destroying phase. Cadence evidence never settles that obligation.
            if (
                dict(current) != node
                or intent["state"] not in {"creation_unknown", "destroying"}
                or intent["provider_instance_id"] is not None
            ):
                return None
            worker_id = "dstack-" + node["intent_id"].replace("-", "")
            if (
                connection.execute(
                    select(registered_workers.c.id).where(
                        registered_workers.c.id == worker_id
                    )
                ).first()
                or connection.execute(
                    select(attempts.c.id).where(attempts.c.worker_id == worker_id)
                ).first()
                or connection.execute(
                    select(jobs.c.id).where(jobs.c.lease_worker_id == worker_id)
                ).first()
            ):
                return None
            commands = [
                dict(row)
                for row in connection.execute(
                    select(operator_commands)
                    .where(
                        or_(
                            operator_commands.c.id == node["command_id"],
                            operator_commands.c.payload["node_id"].as_string()
                            == node["intent_id"],
                        )
                    )
                    .order_by(operator_commands.c.id)
                ).mappings()
            ]
            action = (
                connection.execute(
                    select(scaler_actions).where(
                        scaler_actions.c.intent_id == node["intent_id"]
                    )
                )
                .mappings()
                .one()
            )
            return request_hash(
                {
                    "node": node,
                    "intent": dict(intent),
                    "commands": commands,
                    "action": dict(action),
                }
            )

    def _passive_matches(self, node, *, allow_due=False):
        witness = self._passive_unknown.get(node["intent_id"])
        if witness is None:
            return False
        try:
            age = self.repo.clock() - witness["observed_at"]
            matches = (
                age >= 0
                and (allow_due or age < IDLE_SAFETY_SECONDS)
                and witness["fingerprint"] == self._passive_fingerprint(node)
            )
        except Exception:
            matches = False
        if not matches:
            self._passive_unknown.pop(node["intent_id"], None)
        return matches

    def _empty_inventory(self, inventory, backend):
        observed = inventory.get("observed_at") if isinstance(inventory, dict) else None
        return (
            isinstance(inventory, dict)
            and inventory.get("backend") == backend
            and inventory.get("complete") is True
            and inventory.get("instance_ids") == []
            and type(observed) in (int, float)
            and math.isfinite(observed)
            and 0 <= self.repo.clock() - observed <= 30
        )

    def _defer_terminal_review(self, node, run):
        try:
            if self._cleanup_pending or getattr(self.worker_process, "children", None):
                return False
            with self.repo.engine.connect() as connection:
                if self._business_demand(connection):
                    self._passive_unknown.clear()
                    return False
            fingerprint = self._passive_fingerprint(node)
            binding = node["payload"].get("dstack", {})
            if not fingerprint or not _terminal_review_candidate(
                run, binding, self.capacity.client.project
            ):
                return False
            inventory = self.capacity.client.provider_inventory(binding["backend"])
            if not self._empty_inventory(
                inventory, binding["backend"]
            ) or fingerprint != self._passive_fingerprint(node):
                return False
            self._passive_unknown[node["intent_id"]] = {
                "fingerprint": fingerprint,
                "observed_at": self.repo.clock(),
            }
            return True
        except Exception:
            self._passive_unknown.pop(node["intent_id"], None)
            return False

    def has_demand(self):
        """Historical stopped rows/invoices alone do not require fast polling."""
        if self._cleanup_pending or getattr(self.worker_process, "children", None):
            self._passive_unknown.clear()
            return True
        with self.repo.engine.connect() as connection:
            if self._business_demand(connection):
                self._passive_unknown.clear()
                return True
            nodes = [
                dict(row)
                for row in connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.payload["capacity_backend"].as_string()
                        == BACKEND_MARKER,
                        operator_nodes.c.payload["tenant_id"].as_string()
                        == self.service.settings.tenant_id,
                        operator_nodes.c.runtime_state != "stopped",
                    )
                ).mappings()
            ]
        # Due stamps remain passive until the existing safety callback refreshes
        # them. Direct tick refuses an expired stamp; no second polling actor.
        return any(not self._passive_matches(node, allow_due=True) for node in nodes)

    def _invoice_check(self):
        now = self.repo.clock()
        if (
            self._invoice_checked_at is not None
            and 0 <= now - self._invoice_checked_at < IDLE_SAFETY_SECONDS
        ):
            return {"state": "not_due", "ledger_writes": 0, "provider_mutations": 0}
        self._invoice_checked_at = now
        from .invoice_checks import inspect_pending_invoices

        try:
            return inspect_pending_invoices(
                self.repo, self.service.settings.tenant_id, self.capacity.client
            )
        except Exception:
            return {
                "state": "check_unavailable",
                "ledger_writes": 0,
                "provider_mutations": 0,
            }

    def safety_sweep(self):
        """Rare complete-account observation; never stops or settles a resource."""
        heartbeat_cpu = getattr(self.worker_process, "heartbeat_cpu", None)
        if callable(heartbeat_cpu):
            heartbeat_cpu()
        invoice_check = self._invoice_check()
        try:
            client = self.capacity.client
            rows = client._post(
                "/api/runs/list",
                {
                    "project_name": client.project,
                    "only_active": True,
                    "include_jobs": True,
                    "job_submissions_limit": 1,
                    "limit": 100,
                },
            )
            if not isinstance(rows, list) or len(rows) >= 100:
                raise ValueError("incomplete_active_runs")
            providers = {
                name: client.provider_inventory(name) for name in ("vastai", "runpod")
            }
            if not all(value["complete"] is True for value in providers.values()):
                raise ValueError("incomplete_provider_inventory")
            known = {name: set() for name in providers}
            known_runs = set()
            nodes = self._nodes()
            for node in nodes:
                binding = node["payload"].get("dstack", {})
                if binding.get("backend") in known and binding.get(
                    "provider_instance_id"
                ):
                    known[binding["backend"]].add(binding["provider_instance_id"])
                if binding.get("run_id"):
                    known_runs.add(binding["run_id"])
            with self.repo.engine.connect() as connection:
                busy = self._business_demand(connection)
            if (
                busy
                or self._cleanup_pending
                or getattr(self.worker_process, "children", None)
            ):
                self._passive_unknown.clear()
            present = {node["intent_id"] for node in nodes}
            for intent_id in list(self._passive_unknown):
                if intent_id not in present:
                    self._passive_unknown.pop(intent_id, None)
            for node in nodes:
                if not self._passive_matches(node, allow_due=True):
                    continue
                witness = self._passive_unknown[node["intent_id"]]
                binding = node["payload"]["dstack"]
                if not self._empty_inventory(
                    providers.get(binding["backend"]), binding["backend"]
                ) or any(row["id"] == binding["run_id"] for row in rows):
                    self._passive_unknown.pop(node["intent_id"], None)
                    continue
                if self.repo.clock() - witness["observed_at"] >= IDLE_SAFETY_SECONDS:
                    run = client.get(run_id=binding["run_id"])
                    if not _terminal_review_candidate(
                        run, binding, client.project
                    ) or witness["fingerprint"] != self._passive_fingerprint(node):
                        self._passive_unknown.pop(node["intent_id"], None)
                        continue
                    witness["observed_at"] = self.repo.clock()
            return {
                "state": "idle_safety_observed",
                "active_runs": len(rows),
                "invoice_check": invoice_check,
                "provider_allocations": {
                    name: len(value["instance_ids"])
                    for name, value in providers.items()
                },
                "untracked_active_runs": sum(
                    row["id"] not in known_runs for row in rows
                ),
                "untracked_provider_allocations": {
                    name: len(set(value["instance_ids"]) - known[name])
                    for name, value in providers.items()
                },
                "provider_observed_at": {
                    name: value["observed_at"] for name, value in providers.items()
                },
                "business_writes": 0,
                "provider_mutations": 0,
            }
        except Exception:
            self._passive_unknown.clear()
            return {
                "state": "idle_safety_unconfirmed",
                "invoice_check": invoice_check,
                "business_writes": 0,
                "provider_mutations": 0,
            }

    def _nodes(self):
        with self.repo.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(select(operator_nodes)).mappings()
                if row["payload"].get("capacity_backend") == BACKEND_MARKER
                and row["payload"].get("tenant_id") == self.service.settings.tenant_id
            ]

    def _obligations(self, intent):
        from .worker_admission import matching_dstack_queued_jobs

        with self.repo.engine.connect() as connection:
            workers = list(
                connection.execute(
                    select(registered_workers).where(
                        registered_workers.c.provider == intent["provider"],
                        registered_workers.c.instance_id
                        == intent["provider_instance_id"],
                    )
                ).mappings()
            )
            ids = [worker["id"] for worker in workers]
            linked = (
                list(
                    connection.execute(
                        select(jobs).where(jobs.c.lease_worker_id.in_(ids))
                    ).mappings()
                )
                if ids
                else []
            )
            related = (
                list(
                    connection.execute(
                        select(attempts).where(attempts.c.worker_id.in_(ids))
                    ).mappings()
                )
                if ids
                else []
            )
            by_id = {job["id"]: job for job in linked}
            for attempt in related:
                if attempt["job_id"] not in by_id:
                    by_id[attempt["job_id"]] = self.repo._job(
                        connection, attempt["job_id"]
                    )
            node = (
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent["id"]
                    )
                )
                .mappings()
                .one()
            )
            for job in matching_dstack_queued_jobs(connection, intent, node):
                by_id.setdefault(job["id"], job)
            # Expired, fenced, never-submitted queue history is not work on
            # this stopped host. Keep it queued for another matching worker.
            active = sum(
                job["status"] not in TERMINAL
                and not (
                    node["desired_state"] == "stopped"
                    and self.control._proven_unsubmitted_queue(
                        connection, job, expired_lease_only=True
                    )
                )
                for job in by_id.values()
            )
        unsafe = sum(
            (
                attempt["submission_started_at"] is not None
                or attempt["upstream_task_id"] is not None
            )
            and attempt["upstream_stopped"] != 1
            for attempt in related
        )
        last = max(
            [intent["created_at"], *[job["updated_at"] for job in by_id.values()]]
        )
        return workers, active, unsafe, last

    def _request_stop(self, node, *, reason_code="dstack_idle_expired"):
        # Idle expiry is an internal original-ledger command, never an HTTP
        # principal impersonation. Preserve the original deadline/reservation.
        from sqlalchemy import update

        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            current = self.repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == node["intent_id"]
                ),
            )
            if current["desired_state"] == "running":
                payload = dict(current["payload"])
                if reason_code == "dstack_idle_expired":
                    from .worker_admission import matching_dstack_queued_jobs

                    intent = self.repo._locked(
                        connection,
                        select(instance_intents).where(
                            instance_intents.c.id == node["intent_id"]
                        ),
                    )
                    if matching_dstack_queued_jobs(connection, intent, current):
                        return False
                elif reason_code == "dstack_download_timebox_exceeded":
                    intent = self.repo._locked(
                        connection,
                        select(instance_intents).where(
                            instance_intents.c.id == node["intent_id"]
                        ),
                    )
                    now = self.repo.clock()
                    binding = payload.get("dstack", {})
                    worker_id = "dstack-" + node["intent_id"].replace("-", "")
                    workers = list(
                        connection.execute(
                            select(registered_workers).where(
                                or_(
                                    registered_workers.c.id == worker_id,
                                    (
                                        registered_workers.c.provider
                                        == intent["provider"]
                                    )
                                    & (
                                        registered_workers.c.instance_id
                                        == intent["provider_instance_id"]
                                    ),
                                )
                            )
                        ).mappings()
                    )
                    ids = [worker_id, *[worker["id"] for worker in workers]]
                    record = getattr(self.service, "record_download_timebox", None)
                    if (
                        not download_timebox_due(
                            binding,
                            now,
                            fresh_seconds=self.service.policy[
                                "observation_fresh_seconds"
                            ],
                        )
                        or not _cold_unadmitted_runtime(
                            binding,
                            workers,
                            intent["provider_instance_id"],
                            now,
                            self.service.policy["observation_fresh_seconds"],
                        )
                        or connection.execute(
                            select(attempts.c.id)
                            .where(attempts.c.worker_id.in_(ids))
                            .limit(1)
                        ).first()
                        or connection.execute(
                            select(jobs.c.id)
                            .where(jobs.c.lease_worker_id.in_(ids))
                            .limit(1)
                        ).first()
                        or not callable(record)
                    ):
                        return False
                    payload = record(connection, current, intent, now)
                    if not isinstance(payload, dict):
                        return False
                payload["stop_reason_code"] = reason_code
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node["intent_id"])
                    .values(
                        desired_state="stopped",
                        runtime_state="draining",
                        payload=payload,
                        updated_at=self.repo.clock(),
                    )
                )
                self.repo._emit(
                    connection,
                    "dstack.idle_drain_requested",
                    node["intent_id"],
                    {"intent_id": node["intent_id"], "reason_code": reason_code},
                )
                return True
            return current["desired_state"] == "stopped"

    def _tick_node(self, node):
        intent_id = node["intent_id"]
        # A node cached by tick() may predate an atomic reviewed recovery.
        # Read the committed fence before any provider observation/bootstrap.
        with self.repo.engine.connect() as connection:
            current = dict(
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    )
                )
                .mappings()
                .one()
            )
            intent = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, intent, current):
                return {"node_id": intent_id, "state": "stopped"}
        binding = self.capacity.store.load(intent_id)
        funded = True
        if (
            node["payload"].get("lifecycle", {}).get("version") == 2
            and node["desired_state"] == "running"
        ):
            with self.repo.engine.connect() as connection:
                prior = (
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == intent_id
                        )
                    )
                    .mappings()
                    .one()
                )
            if binding.get("apply_started") and prior["state"] in {
                "draining",
                "destroying",
                "destroyed",
            }:
                # Upstream-only terminal evidence can end a stale projection;
                # it cannot bootstrap, reopen admission, or fund stopped work.
                # Any nonterminal/unknown read still meets the original funding
                # refusal below before native activity can occur.
                terminal = self.capacity.observe_terminal(intent_id)
                if terminal is not None:
                    return {"node_id": intent_id, "state": terminal["state"]}
            try:
                # Funding precedes native bootstrap as well as admission. Time
                # spent downloading/holding/uncertain still consumes runway.
                with self.repo.transaction() as connection:
                    self.repo._lock_capacity(connection)
                    current = self.repo._locked(
                        connection,
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == intent_id
                        ),
                    )
                    intent = self.repo._locked(
                        connection,
                        select(instance_intents).where(
                            instance_intents.c.id == intent_id
                        ),
                    )
                    if current["desired_state"] == "running":
                        self.service.fund_window(
                            connection, intent, current, self.repo.clock()
                        )
            except BudgetExceeded as error:
                # The failed transaction rolled back all funding mutations.
                # Preserve any running task/result and request normal drain.
                funded = False
                reason = (
                    error.args[0]
                    if error.args
                    and error.args[0]
                    in {
                        "dstack_test_daily_limit_reached",
                        "dstack_test_task_limit_reached",
                        "dstack_test_runtime_limit_reached",
                        "dstack_test_budget_unconfirmed",
                        "dstack_test_guard_unconfirmed",
                    }
                    else "dstack_spend_limit_reached"
                )
                self._request_stop(node, reason_code=reason)
            with self.repo.engine.connect() as connection:
                node = dict(
                    connection.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == intent_id
                        )
                    )
                    .mappings()
                    .one()
                )
        if not binding.get("apply_started"):
            with self.repo.engine.connect() as connection:
                intent = (
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == intent_id
                        )
                    )
                    .mappings()
                    .one()
                )
                action = (
                    connection.execute(
                        select(scaler_actions).where(
                            scaler_actions.c.intent_id == intent_id
                        )
                    )
                    .mappings()
                    .one()
                )
            # BOTH original journals must prove that no request was sent.
            # begin_apply repeats these checks atomically before any mutation.
            if intent["state"] != "reserved" or action["create_started_at"] is not None:
                return {
                    "node_id": intent_id,
                    "state": "reconciliation_required",
                    "code": "dstack_apply_journal_mismatch",
                }
            if node["desired_state"] == "stopped" or not _window_open(
                intent, node, self.repo.clock()
            ):
                self._request_stop(node)
                cancelled = self.capacity.store.cancel_unapplied(intent_id)
                return {
                    "node_id": intent_id,
                    "state": "never_applied"
                    if cancelled
                    else "reconciliation_required",
                }
            observation = self.capacity.start(
                _request(node["payload"]["request"]),
                self.service.config["ssh_public_key"],
            )
            return {"node_id": intent_id, "state": observation["state"]}
        if binding.get("stop_started"):
            # Exact read + bounded journaled retry belongs to capacity.stop.
            # stop_started alone cannot prove that its HTTP request arrived.
            observation = self.capacity.stop(intent_id)
            return {"node_id": intent_id, "state": observation["state"]}
        observed = []
        observation = (
            self.capacity.observe(intent_id, _on_observed=observed.append)
            if node["desired_state"] == "stopped"
            else self.capacity.observe(intent_id)
        )
        binding = self.capacity.store.load(intent_id)
        with self.repo.engine.connect() as connection:
            current = dict(
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    )
                )
                .mappings()
                .one()
            )
            intent = dict(
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
        workers, active, unsafe, last = self._obligations(intent)
        if (
            observed
            and not workers
            and not active
            and not unsafe
            and self._defer_terminal_review(current, observed[0])
        ):
            return {
                "node_id": intent_id,
                "state": observation["state"],
                "code": "dstack_terminal_review_waiting_safety",
            }
        payload = current["payload"]
        now = self.repo.clock()
        if (
            current["desired_state"] == "running"
            and download_timebox_due(
                binding,
                now,
                fresh_seconds=self.service.policy["observation_fresh_seconds"],
            )
            and self._request_stop(
                current, reason_code="dstack_download_timebox_exceeded"
            )
        ):
            # Fence this exact cold host first. The next tick uses the original
            # begin_stop checks; no worker is launched and the queued job stays.
            return {
                "node_id": intent_id,
                "state": "draining",
                "code": "dstack_download_timebox_exceeded",
            }
        observed = binding.get("observed_at")
        fresh = (
            type(observed) in (int, float)
            and 0 <= now - observed <= self.service.policy["observation_fresh_seconds"]
        )
        last = max(last, binding.get("first_runtime_ready_at", now))
        lifecycle = payload.get("lifecycle", {})
        idle_seconds = lifecycle.get(
            "idle_shutdown_seconds", self.service.policy["idle_shutdown_seconds"]
        )
        if lifecycle.get("test_gpu_guard"):
            from .spending_limits import ADMIN_OWNER, read_settings

            with self.repo.engine.connect() as connection:
                limits = read_settings(connection, payload["tenant_id"], ADMIN_OWNER)
            if limits["version"]:
                idle_seconds = min(
                    idle_seconds, limits["value"]["idle_shutdown_seconds"]
                )
        action = idle_action(
            now=now,
            hard_deadline=payload["hard_deadline"],
            hold_until=payload["hold_until"],
            last_business_activity=last,
            idle_seconds=idle_seconds,
            active_jobs=active,
            unsafe_attempts=unsafe,
            collection_holds=int(binding.get("state") == "busy"),
            observations_fresh=bool(fresh),
            lifecycle_version=lifecycle.get("version", 1),
            keep_on=lifecycle.get("keep_on", "idle"),
            budget_available=funded,
        )
        if action in {"stop", "drain"}:
            if not self._request_stop(current):
                action = "retain"
        stopping = current["desired_state"] == "stopped" or action in {"stop", "drain"}
        if stopping:
            if (
                (active or unsafe)
                and binding.get("state") in {"ready", "busy"}
                and fresh
                and self.worker_process
            ):
                # Restore the same CPU consumer for reconciliation/collection;
                # its drained registration cannot acquire new generation work.
                self.worker_process.ensure(intent_id, self.runtime.slot(intent_id))
            for worker in workers:
                if worker["state"] != "retired":
                    self.control.drain(worker["id"])
            # Retiring while a result is uncollected would lose its recovery
            # binding. Keep its CPU worker alive until the original job ends.
            if binding.get("state") == "ready" and fresh and not active and not unsafe:
                for worker in workers:
                    if worker["state"] != "retired":
                        self.control.retire(worker["id"], upstream_idle_confirmed=True)
                if self.worker_process:
                    self.worker_process.stop(intent_id)
                result = self.capacity.stop(intent_id)
                return {"node_id": intent_id, "state": result["state"]}
            # Cold model preparation has never admitted execution. It need not
            # finish downloading/loading merely to become eligible for stop.
            # begin_stop rechecks the same evidence under the capacity lock.
            cold_unadmitted = _cold_unadmitted_runtime(
                binding,
                workers,
                intent.get("provider_instance_id"),
                now,
                self.service.policy["observation_fresh_seconds"],
            )
            if (not active and not unsafe and cold_unadmitted) or (
                not binding.get(
                    "bootstrap_launch_started", binding.get("bootstrap_started")
                )
                and not workers
                and binding.get("runtime_incarnation") is None
                and binding.get("first_runtime_ready_at") is None
            ):
                result = self.capacity.stop(intent_id)
                return {"node_id": intent_id, "state": result["state"]}
            return {"node_id": intent_id, "state": "draining"}
        if binding.get("state") == "ready" and fresh:
            slot = self.runtime.slot(intent_id)
            try:
                registered = self.control.register(slot.spec)
            except BudgetExceeded as error:
                if error.args == ("global_capacity_exceeded",):
                    self.capacity.store.record_registration(
                        intent_id, slot.spec.worker_id, "global_capacity_exceeded"
                    )
                raise
            self.capacity.store.record_registration(intent_id, slot.spec.worker_id)
            if (
                (
                    registered["state"] != "ready"
                    or registered["expires_at"] <= self.repo.clock()
                )
                and not registered["current_job_id"]
                and not registered["drain_requested"]
            ):
                self.control.mark_ready(
                    slot.spec.worker_id, upstream_idle_confirmed=True
                )
            if self.worker_process:
                self.worker_process.ensure(intent_id, slot)
        elif binding.get("state") == "busy" and fresh and workers:
            # Running original inference must not age out its live consumer's
            # proof. Refresh transport eligibility without marking it idle.
            slot = self.runtime.slot(intent_id)
            if self.worker_process:
                self.worker_process.ensure(intent_id, slot)
        return {"node_id": intent_id, "state": observation["state"]}

    def _refresh_pending_removal(self, node, intent):
        """Bounded exact terminal read; no native boot, invoice or invented proof."""
        binding = node["payload"].get("dstack", {})
        now = self.repo.clock()
        if (
            node["desired_state"] != "stopped"
            or binding.get("state") != "stopped"
            or binding.get("dstack_status")
            not in {"done", "failed", "terminated", "aborted"}
            or not binding.get("run_id")
            or not intent.get("provider_instance_id")
            or binding.get("provider_instance_id") != intent["provider_instance_id"]
            or binding.get("provider_removal_proof")
        ):
            self._removal_checked_at.pop(node["intent_id"], None)
            return
        last = self._removal_checked_at.get(
            node["intent_id"], binding.get("observed_at")
        )
        if type(last) not in (int, float) or not math.isfinite(last) or now - last < 30:
            return
        # Stamp before I/O: even a failed/unknown GET cannot become a hot loop.
        self._removal_checked_at[node["intent_id"]] = now
        self.capacity.observe_terminal(node["intent_id"])

    def tick(self):
        heartbeat_cpu = getattr(self.worker_process, "heartbeat_cpu", None)
        if callable(heartbeat_cpu):
            heartbeat_cpu()
        results = []
        with self.repo.engine.connect() as connection:
            busy = self._business_demand(connection)
        if (
            busy
            or self._cleanup_pending
            or getattr(self.worker_process, "children", None)
        ):
            self._passive_unknown.clear()
        for node in self._nodes():
            try:
                if self._passive_matches(node):
                    continue
                # A task terminal is not an invoice or provider destruction proof.
                if node["runtime_state"] == "stopped":
                    with self.repo.engine.connect() as connection:
                        intent = dict(
                            connection.execute(
                                select(instance_intents).where(
                                    instance_intents.c.id == node["intent_id"]
                                )
                            )
                            .mappings()
                            .one()
                        )
                    _, active, unsafe, _ = self._obligations(intent)
                    self._refresh_pending_removal(node, intent)
                    if not active and not unsafe:
                        if self.worker_process:
                            self.worker_process.stop(node["intent_id"])
                        self.runtime.close(node["intent_id"])
                        self._cleanup_pending.discard(node["intent_id"])
                    continue
                result = self._tick_node(node)
                results.append(result)
                if result["state"] == "stopped":
                    self._cleanup_pending.add(node["intent_id"])
            except Exception as error:
                code = (
                    "global_capacity_exceeded"
                    if isinstance(error, BudgetExceeded)
                    and error.args == ("global_capacity_exceeded",)
                    else "dstack_controller_observation_failed"
                )
                results.append(
                    {
                        "node_id": node["intent_id"],
                        "state": "reconciliation_required",
                        "code": code,
                    }
                )
        return {
            "state": "running",
            "nodes": results,
            "demand_start": self._demand_start(),
            "invoice_check": self._invoice_check(),
        }

    def _demand_start(self):
        """One decision through the original service, after existing idle cleanup.

        The service owns current eligibility, the runtime kill switch, original
        command identity and the atomic manual/automatic capacity guard. The
        broad business-demand predicate above is only a wake/recovery hint.
        An unknown result is observed on the next tick, never retried here.
        """
        trigger = getattr(self.service, "demand_start_once", None)
        if not callable(trigger):
            return {"state": "disabled", "reason_code": "demand_start_unavailable"}
        try:
            result = trigger()
            if not isinstance(result, dict) or result.get("state") not in {
                "disabled",
                "idle",
                "blocked",
                "accepted",
                "observing",
            }:
                raise ValueError("dstack_demand_start_result_invalid")
            fields = ("state", "reason_code", "command_id", "node_id")
            value = {key: result[key] for key in fields if result.get(key) is not None}
            if any(
                not isinstance(item, str) or not 1 <= len(item) <= 200
                for item in value.values()
            ):
                raise ValueError("dstack_demand_start_result_invalid")
            return value
        except Exception:
            # A failed response may follow an already committed original start.
            # Do not issue a second command, choose an offer, or expose errors.
            return {
                "state": "blocked",
                "reason_code": "dstack_demand_start_observation_failed",
            }


class SlotProcesses:
    """Only CPU worker processes. No arbitrary command or GPU process signal."""

    def __init__(self, work_dir, *, popen=subprocess.Popen):
        self.work_dir = Path(work_dir)
        if not self.work_dir.is_absolute():
            raise ValueError("absolute_dstack_worker_paths_required")
        self.popen, self.children = popen, {}
        self._supervisor_lock = None

    def __enter__(self):
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._supervisor_lock = _slot_lock(self.work_dir, "dstack-cpu-supervisor")
        if not self._supervisor_lock.__enter__():
            self._supervisor_lock.__exit__(None, None, None)
            self._supervisor_lock = None
            raise ValueError("dstack_cpu_supervisor_already_active")
        return self

    def ensure(self, intent_id, slot):
        if self._supervisor_lock is None:
            raise ValueError("dstack_cpu_supervisor_lock_required")
        prior = self.children.get(intent_id)
        if prior and prior.poll() is None:
            return
        if prior:
            prior.wait()  # Reap an exited child before replacing this exact slot.
        directory = self.work_dir / intent_id
        directory.mkdir(parents=True, exist_ok=True)
        item = asdict(slot)
        item.update(worker_spec_payload(slot.spec))
        item.pop("spec")
        value = {
            "version": 2,
            "work_dir": str(self.work_dir),
            "enabled": True,
            "max_children": 1,
            "shutdown_grace_s": 210,
            "slots": [item],
        }
        path = directory / "fleet.json"
        temporary = directory / "fleet.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        self.children[intent_id] = self.popen(
            [
                sys.executable,
                "-m",
                "studio_platform.fleet",
                "--config",
                str(path),
                "--slot",
                slot.spec.worker_id,
            ],
            stdin=subprocess.DEVNULL,
        )

    def stop(self, intent_id):
        process = self.children.get(intent_id)
        if process and process.poll() is None:
            process.terminate()  # Original job is already terminal and native idle.
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()  # Our CPU child only; GPU job/tag remains durable.
                process.wait(timeout=5)
        elif process:
            process.wait()
        self.children.pop(intent_id, None)

    def close(self):
        failure = None
        try:
            for intent_id in list(self.children):
                try:
                    self.stop(intent_id)
                except Exception as error:
                    # Still reap the other children if one refuses shutdown.
                    failure = failure or error
        finally:
            if self._supervisor_lock is not None:
                self._supervisor_lock.__exit__(None, None, None)
                self._supervisor_lock = None
        if failure is not None:
            raise failure

    def __exit__(self, *_):
        self.close()


def main(argv=None):
    """CPU-only supervisor; construction never creates schemas or budgets."""
    parser = argparse.ArgumentParser(
        description="Original-ledger dstack reconciliation and native slot supervisor"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix":
        raise ValueError("dstack_controller_requires_linux")
    from .settings import Settings
    from .repository import Repository
    from .dstack_operator import from_environment
    from .dstack_factory import create_runtime

    settings = Settings.from_environment()
    repo = Repository(settings.database_url)
    service = None
    runtime = None
    processes = None
    telemetry = None
    durable_executor = None
    try:
        service = from_environment(repo, settings)
        if service.capacity is None:
            raise ValueError("dstack_capacity_disabled")
        runtime, work_dir = create_runtime(repo, service.capacity.store)
        if settings.generation_queue == "dbos":
            from .dbos_transport import DurableExecutor
            from .api import create_app
            from .film_workflows import register_film_workflows
            from .film_runs import VERSION as FILM_VERSION

            film_app = create_app(settings, repository=repo, dstack_operator=service)
            film_service = film_app.state.film_runs

            def film_setup(DBOS):
                repo.generation_transport.client.register_queue(
                    FILM_VERSION, polling_interval_sec=10
                )

                def principal_for(tenant, owner, project_id, run_id):
                    grant = film_service.execution_context(
                        tenant, owner, project_id, run_id
                    )
                    return film_app.state.auth.workflow_principal(owner, grant)

                register_film_workflows(DBOS, film_service, principal_for)

            durable_executor = DurableExecutor(
                repo,
                settings,
                work_dir,
                film_setup=film_setup,
                account_services=(
                    film_app.state.account_sessions,
                    film_app.state.account_deletion,
                ),
            ).__enter__()
            processes = durable_executor.slots
            durable_executor.import_pending()
        else:
            processes = SlotProcesses(work_dir).__enter__()
        telemetry = configured_telemetry()
        controller = DstackController(
            service, runtime, worker_process=processes, telemetry=telemetry
        )
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
        if args.once:
            print(json.dumps(controller.tick(), sort_keys=True), flush=True)
            return 0
        from .demand_wakeup import DemandWakeup, run_demand_loop

        wakeup = DemandWakeup(repo, stop)
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, lambda *_: (stop.set(), wakeup.interrupt()))
            run_demand_loop(
                stop,
                wakeup,
                controller.has_demand,
                controller.tick,
                active_seconds=15,
                safety=controller.safety_sweep,
                report=lambda value: print(
                    json.dumps(value, sort_keys=True), flush=True
                ),
            )
        finally:
            wakeup.close()
    finally:
        # Closing local tunnels is safe; durable original attempts remain
        # recoverable. Do not translate process shutdown into paid-host stop.
        try:
            if durable_executor:
                durable_executor.close()
            elif processes:
                processes.close()
        finally:
            try:
                if runtime:
                    runtime.close()
            finally:
                try:
                    if service and service.capacity:
                        service.capacity.client.close()
                finally:
                    try:
                        repo.close()
                    finally:
                        if telemetry is not None:
                            try:
                                telemetry.force_flush(timeout_millis=1000)
                            except Exception:
                                pass
                            try:
                                telemetry.close()
                            except Exception:
                                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(
            "dstack_controller_failed; inspect protected service diagnostics"
        ) from None

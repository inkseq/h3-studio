"""Original owner-scoped native start, state and command operations."""

from __future__ import annotations
from dataclasses import asdict, replace
import copy
import math
import uuid
from sqlalchemy import insert, select, update
from .dstack_capacity import (
    request_from_document,
    DstackError,
    control_plane,
)
from .capacity_policy import window_open
from .capacity_memory import require_preview_ram
from .operator_capacity import (
    advance_command_revision,
    command_revision,
    operator_commands,
    operator_nodes,
    operator_previews,
    require,
    safe_id,
)
from .repository import (
    Scope,
    budget_accounts,
    instance_intents,
    jobs,
    registered_workers,
    request_hash,
    scaler_actions,
)
from .providers.registry import VASTAI

from .dstack_operator_contract import (
    BACKEND_MARKER,
    PROVIDERS,
    REGISTRATION_REASON_CODES,
    _actor,
    _integer,
    _finite,
)


class DstackCommands:
    def _existing_command(self, connection, actor, key, digest):
        require(safe_id(key), "operator_idempotency_key_invalid", 422)
        command = self.repo._locked(
            connection,
            select(operator_commands).where(
                operator_commands.c.actor == actor,
                operator_commands.c.idempotency_key == key,
            ),
        )
        if command:
            require(command["request_hash"] == digest, "operator_idempotency_conflict")
        return dict(command) if command else None

    def start(self, principal, body, key):
        owner = self.authorize(principal)
        return self._start_for_owner(owner, principal.actor_id, body, key)

    def _start_for_owner(self, owner, actor_id, body, key, *, demand_job_id=None):
        require(
            isinstance(body, dict)
            and set(body) == {"preview_id"}
            and safe_id(body["preview_id"]),
            "dstack_start_invalid",
            422,
        )
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_start", "body": body})
        # Provider reads first, with no lock or connection held (#1148). The
        # locked transaction below uses them only while the committed facts
        # they depend on are unchanged; otherwise it reads live under the lock
        # as before, so a concurrent start still sees the same refusal.
        preread = self._start_preread(actor, body["preview_id"])
        with self.repo.transaction() as connection:
            if demand_job_id is not None:
                from .identity import lock_account_gate

                lock_account_gate(connection, self.settings.tenant_id)
            self.repo._lock_capacity(connection)
            from .spending_limits import ADMIN_OWNER, lock_owner

            lock_owner(connection, self.settings.tenant_id, ADMIN_OWNER)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {
                    "command_id": existing["id"],
                    "node_id": existing["payload"]["node_id"],
                    "state": existing["state"],
                }
            value, demand, profile = self._validate_start_preview(
                connection, owner, actor, body["preview_id"], demand_job_id
            )
            now, capacity_snapshot, usage = self._admit_start_capacity(
                connection, owner, value, profile, preread=preread["capacity"]
            )
            original = require_preview_ram(value["request"], profile)
            host_selection = self._validate_start_hosts(
                connection,
                profile,
                original,
                now,
                demand_job_id,
                hosts=preread["hosts"],
            )
            intent, continuing = self._reserve_start_intent(
                connection,
                owner,
                actor_id,
                body["preview_id"],
                value,
                profile,
                original,
                usage,
                capacity_snapshot,
            )
            request, command_id = self._record_start_request(
                connection,
                owner,
                actor,
                key,
                digest,
                body["preview_id"],
                value,
                demand,
                profile,
                original,
                now,
                intent,
                host_selection,
                capacity_snapshot,
                continuing,
            )
        return self._apply_reserved_start(intent, request, command_id)

    def _validate_start_preview(
        self, connection, owner, actor, preview_id, demand_job_id
    ):
        """Validate the original preview and single-use fence under the start locks."""
        self._enabled()
        preview = self.repo._locked(
            connection,
            select(operator_previews).where(
                operator_previews.c.id == preview_id,
                operator_previews.c.actor == actor,
            ),
        )
        require(
            preview is not None and self.repo.clock() < preview["expires_at"],
            "dstack_preview_expired",
        )
        value = preview["payload"]
        require(
            value.get("config_fingerprint") == self.fingerprint
            and value.get("capacity_backend") == BACKEND_MARKER,
            "dstack_preview_configuration_changed",
        )
        require(
            value.get("admin_limits_snapshot")
            == request_hash(self._admin_limits(connection)),
            "spending_settings_changed",
        )
        from .operator_prices import OperatorPrices

        trusted = self.profiles.get(value["profile_id"])
        require(
            trusted is not None and trusted["owner_id"] == owner,
            "dstack_profile_not_found",
            404,
        )
        demand = value.get("demand_start")
        if demand_job_id is not None:
            from .dstack_demand import validate_start

            require(
                demand
                == validate_start(self, connection, demand_job_id, trusted["id"]),
                "dstack_demand_changed",
            )
        else:
            require(demand is None, "dstack_demand_start_internal_only")
        price, _, price_version = OperatorPrices(self.repo, self.settings).read(
            connection, trusted
        )
        require(
            price["snapshot"] == value["price_policy_snapshot"]
            if "price_policy_snapshot" in value
            else price_version == 0
            and value["profile"]["max_price_microusd"] == price["max_price_microusd"],
            "dstack_price_policy_changed",
        )
        # A preview is single-use even when another idempotency key is supplied.
        previous = connection.execute(
            select(operator_commands.c.id).where(
                operator_commands.c.actor == actor,
                operator_commands.c.kind == "dstack_start",
                operator_commands.c.payload["preview_id"].as_string() == preview_id,
            )
        ).first()
        require(previous is None, "dstack_preview_consumed")
        profile = value["profile"]
        return value, demand, profile

    def _admit_start_capacity(self, connection, owner, value, profile, preread=None):
        from .capacity_lookahead_runtime import admit_capacity

        return admit_capacity(self, connection, owner, value, profile, preread)

    def _start_preread(self, actor, preview_id):
        """Provider reads one start needs, taken before its capacity lock (#1148).

        Grants nothing: the locked transaction still validates the preview,
        and uses a read only for the same request and unchanged node facts.
        A provider error is kept and raised at its original place under the
        lock, so a refused start keeps its original reason.
        """
        hosts = None
        with self.repo.engine.connect() as connection:
            preview = (
                connection.execute(
                    select(operator_previews.c.payload).where(
                        operator_previews.c.id == preview_id,
                        operator_previews.c.actor == actor,
                    )
                )
                .scalars()
                .first()
            )
        try:
            original = preview["request"]
            if (
                preview["profile"]["backend"] == VASTAI.id
                and original.get("host_selection") is not None
            ):
                approved = {
                    r["offer_id"]: r["machine_id"]
                    for r in original["host_selection"]["offers"]
                }
                request = request_from_document(original)
                hosts = {"request": request_hash(original), "approved": approved}
                client = self.capacity.client
                try:
                    fresh = client.vast_offer_mapping(request, clock=self.repo.clock)
                    hosts["fresh"] = (fresh, None)
                except Exception as error:  # Raised again under the lock.
                    hosts["fresh"] = (None, error)
                try:
                    hosts["reusable"] = (
                        client.reusable_hosts_confirmed(approved),
                        None,
                    )
                except Exception as error:  # Raised again under the lock.
                    hosts["reusable"] = (None, error)
        except (KeyError, TypeError, AttributeError, ValueError):
            hosts = None  # Not a usable Vast preview: the lock path decides.
        return {"capacity": self.capacity_preread(), "hosts": hosts}

    @staticmethod
    def _preread_host(hosts, original, name):
        """The unlocked read for this exact request, re-raising its error."""
        if hosts is None or hosts["request"] != request_hash(original):
            return False, None
        value, error = hosts[name]
        if error is not None:
            raise error
        return True, value

    def _validate_start_hosts(
        self, connection, profile, original, now, demand_job_id, hosts=None
    ):
        """Re-read exact Vast offer identities before reserving the original intent."""
        host_selection = original.get("host_selection")
        if profile["backend"] == VASTAI.id:
            from .host_policy import HostPolicyStore, selection

            require(host_selection is not None, "host_selection_required")
            host_selection = selection(host_selection, now=now)
            require(
                HostPolicyStore(self.repo, self.settings.tenant_id).admission_snapshot(
                    connection, host_selection["offers"], now=now
                )
                == host_selection["policy_snapshot"],
                "host_policy_changed",
            )
            # Re-read exact offer identities before any reservation. Stock
            # presence isn't allocation proof; a missing/changed mapping
            # refuses this new intent, without changing old paid runs.
            known, fresh = self._preread_host(hosts, original, "fresh")
            if not known:
                fresh = self.capacity.client.vast_offer_mapping(
                    request_from_document(original), clock=self.repo.clock
                )
            current = {
                r['offer_id']: r['machine_id']
                for r in fresh['offers']
                if type(r.get('advertised_download_mbps')) in (int, float)
                and math.isfinite(r['advertised_download_mbps'])
                and r['advertised_download_mbps'] >= 1000
            }
            excluded = set()
            if demand_job_id is not None:
                from .dstack_demand_start import excluded_download_machines

                excluded = excluded_download_machines(
                    connection, self.settings.tenant_id, demand_job_id
                )
            require(
                all(
                    current.get(r["offer_id"]) == r["machine_id"]
                    and r['machine_id'] not in excluded
                    for r in host_selection["offers"]
                ),
                "host_offer_identity_changed",
            )
            approved = {
                r["offer_id"]: r["machine_id"] for r in host_selection["offers"]
            }
            if not (
                hosts is not None
                and hosts["approved"] == approved
                and self._preread_host(hosts, original, "reusable")[0]
            ):
                self.capacity.client.reusable_hosts_confirmed(approved)
            selection(host_selection, now=self.repo.clock())
        elif host_selection is not None:
            # A Lium selector only excludes denied executors (#900); dstack
            # applies it, and the policy must not have changed since preview.
            from .host_policy import HostPolicyStore, selection, selection_provider

            host_selection = selection(host_selection, now=now)
            require(
                selection_provider(host_selection) == profile["backend"]
                and HostPolicyStore(
                    self.repo, self.settings.tenant_id
                ).admission_snapshot(
                    connection,
                    host_selection["offers"],
                    now=now,
                    provider=profile["backend"],
                )
                == host_selection["policy_snapshot"],
                "host_policy_changed",
            )
        return host_selection

    def _reserve_start_intent(
        self,
        connection,
        owner,
        actor_id,
        preview_id,
        value,
        profile,
        original,
        usage,
        capacity_snapshot,
    ):
        """Preserve the scoped spend cap and reserve one immutable instance intent."""
        continuing = original.get("lifecycle_version", 1) == 2
        if continuing:
            # A tenant/global account alone cannot authorize unlimited
            # keep-on for a person. Require their original scoped cap.
            accounts = list(
                connection.execute(
                    select(budget_accounts).where(
                        budget_accounts.c.id.in_(profile["budget_account_ids"])
                    )
                ).mappings()
            )
            require(
                any(
                    account["tenant_id"] == self.settings.tenant_id
                    and account["owner_id"] == owner
                    and account["project_id"] in (None, profile["project_id"])
                    for account in accounts
                ),
                "dstack_owner_spend_limit_required",
            )
            require(
                self._expected_runtime(profile)
                == value["lifecycle"]["expected_runtime_s"],
                "dstack_execution_policy_changed",
            )
        scope = Scope(
            self.settings.tenant_id,
            owner,
            profile["project_id"],
            actor_id,
        )
        intent = self.repo.reserve_instance_intent(
            scope,
            profile["pool"],
            "dstack:" + preview_id,
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=value["reservation_microusd"],
            hard_deadline=original["hard_deadline"],
            budget_account_ids=profile["budget_account_ids"],
            dry_run=False,
            provider=PROVIDERS[profile["backend"]],
            connection=connection,
            continuing=continuing,
            capacity_usage=usage,
            active_instance_ids=usage["active_instance_ids"],
            capacity_policy_snapshot=capacity_snapshot if continuing else None,
            capacity_provenance={
                "capacity_backend": BACKEND_MARKER,
                "lifecycle_version": 2,
                "tenant_id": scope.tenant_id,
                "owner_id": scope.owner_id,
                "project_id": scope.project_id,
                "pool": profile["pool"],
                "provider": PROVIDERS[profile["backend"]],
            }
            if continuing and capacity_snapshot is not None
            else None,
        )
        return intent, continuing

    def _record_start_request(
        self,
        connection,
        owner,
        actor,
        key,
        digest,
        preview_id,
        value,
        demand,
        profile,
        original,
        now,
        intent,
        host_selection,
        capacity_snapshot,
        continuing,
    ):
        """Persist request, command, node and outbox in the original transaction."""
        request = self._capacity_request(
            profile,
            intent["id"],
            now,
            original["hard_deadline"],
            original["max_duration_s"],
            lifecycle_version=original.get("lifecycle_version", 1),
            test_max_duration=original.get("test_max_duration_s"),
            test_task_id=original.get("test_task_id"),
        )
        if host_selection is not None:
            request = replace(request, host_selection=copy.deepcopy(host_selection))
        binding = request.binding(self.config["ssh_public_key"])
        # Which dstack server owns this run (local or Sky); see live_capacity_usage.
        binding["control_plane"] = control_plane(self.capacity.client)
        command_id = str(uuid.uuid4())
        command_payload = {
            "preview_id": preview_id,
            "node_id": intent["id"],
            "capacity_backend": BACKEND_MARKER,
        }
        connection.execute(
            insert(operator_commands).values(
                id=command_id,
                actor=actor,
                idempotency_key=key,
                request_hash=digest,
                kind="dstack_start",
                state="accepted",
                payload=command_payload,
                created_at=now,
                updated_at=now,
            )
        )
        payload = {
            "capacity_backend": BACKEND_MARKER,
            "command_revision": 0,
            "tenant_id": self.settings.tenant_id,
            "owner_id": owner,
            "project_id": profile["project_id"],
            "provider": PROVIDERS[profile["backend"]],
            "configuration_id": profile["configuration_id"],
            "hard_deadline": request.hard_deadline,
            "group_id": value.get("group_id"),
            "observation_fresh_seconds": self.policy["observation_fresh_seconds"],
            "hourly_cost_microusd": profile["max_price_microusd"],
            "hold_until": 0,
            "selection": {
                "runtime_profile_id": profile["runtime_profile_id"],
                "mode": profile["mode"],
                "gpu_type": profile["gpu_names"][0],
                "node_count": 1,
                "gpu_count": 1,
                "ttl_seconds": original["max_duration_s"],
                "provider": PROVIDERS[profile["backend"]],
            },
            "request": asdict(request),
            "dstack": binding,
        }
        if demand is not None:
            payload["demand_start"] = copy.deepcopy(demand)
            # Whose job asked for this machine, against whose profile pays for
            # it. A job owner without a demand profile of their own falls back
            # to another owner's profile and GPU account (#1140 Q2): record it.
            job_owner = connection.execute(
                select(jobs.c.owner_id).where(jobs.c.id == demand["job_id"])
            ).scalar_one_or_none()
            if job_owner is not None:
                payload["demand_job_owner_id"] = job_owner
                payload["cross_owner_fallback"] = job_owner != owner
        if capacity_snapshot is not None:
            payload["capacity_policy_snapshot"] = copy.deepcopy(capacity_snapshot)
        if continuing:
            payload["lifecycle"] = copy.deepcopy(value["lifecycle"])
            payload["hold_until"] = payload["lifecycle"]["hold_until"]
            payload["extra_reservation_microusd"] = profile[
                "extra_reservation_microusd"
            ]
        connection.execute(
            insert(operator_nodes).values(
                intent_id=intent["id"],
                command_id=command_id,
                ordinal=0,
                binding_id=profile["id"],
                binding_hash=request_hash(binding),
                payload=payload,
                desired_state="running",
                runtime_state="reserved",
                updated_at=now,
            )
        )
        if value.get("group_id") is not None:
            from .dstack_workspace import advance_group_version

            advance_group_version(
                connection, self.settings, owner, value["group_id"], now
            )
        connection.execute(
            insert(scaler_actions).values(
                intent_id=intent["id"],
                pool=profile["pool"],
                launch_spec={
                    "capacity_backend": BACKEND_MARKER,
                    "request": asdict(request),
                },
            )
        )
        self.repo._emit(
            connection,
            "dstack.start_requested",
            intent["id"],
            {"intent_id": intent["id"], "command_id": command_id},
        )
        return request, command_id

    def _apply_reserved_start(self, intent, request, command_id):
        """Apply once after commit and preserve the reviewed-settlement read-back fence."""
        # The reservation, immutable request and command survive any crash before
        # or after HTTP. Controller restart resumes this exact intent only.
        try:
            observation = self.capacity.start(request, self.config["ssh_public_key"])
            state = "unknown" if "unknown" in observation["state"] else "running"
        except DstackError:
            state = "waiting"
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            current = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == intent["id"]
                ),
            )
            current_intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, current_intent, node):
                state = current["state"]
            else:
                connection.execute(
                    update(operator_commands)
                    .where(operator_commands.c.id == command_id)
                    .values(state=state, updated_at=self.repo.clock())
                )
        return {"command_id": command_id, "node_id": intent["id"], "state": state}

    def _owned_node(self, connection, principal, node_id, *, allow_admin=False):
        owner = self.authorize(principal)
        node = self.repo._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
        )
        require(
            node is not None
            and node["payload"].get("capacity_backend") == BACKEND_MARKER
            and node["payload"].get("tenant_id") == self.settings.tenant_id
            and (
                node["payload"].get("owner_id") == owner
                or allow_admin
                and self._is_admin(principal)
            ),
            "dstack_node_not_found",
            404,
        )
        return dict(node)

    def state(self, principal):
        owner = self.authorize(principal)
        return self._state_for_owner(owner)

    def _is_admin(self, principal):
        from .identity import is_admin

        return is_admin(principal, self.repo, self.settings)

    def _state_for_owner(self, owner):
        # Internal read projection: caller must authorize the original actor.
        now = self.repo.clock()
        result = []
        with self.repo.engine.connect() as connection:
            for node in connection.execute(
                select(operator_nodes).order_by(operator_nodes.c.updated_at.desc())
            ).mappings():
                payload = node["payload"]
                if (
                    payload.get("capacity_backend") != BACKEND_MARKER
                    or payload.get("tenant_id") != self.settings.tenant_id
                    or payload.get("owner_id") != owner
                ):
                    continue
                binding = payload["dstack"]
                observed = binding.get("observed_at")
                fresh = _finite(observed) and 0 <= now - observed <= self.policy.get(
                    "observation_fresh_seconds", 30
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
                native_ready = bool(
                    fresh
                    and binding.get("ready") is True
                    and node["desired_state"] == "running"
                    and window_open(intent, node, now)
                )
                workers = (
                    list(
                        connection.execute(
                            select(registered_workers).where(
                                registered_workers.c.provider == intent["provider"],
                                registered_workers.c.instance_id
                                == intent["provider_instance_id"],
                            )
                        ).mappings()
                    )
                    if intent["provider_instance_id"]
                    else []
                )
                from .worker_admission import worker_window_reason

                expected = payload.get("lifecycle", {}).get("expected_runtime_s")
                eligible = [
                    worker
                    for worker in workers
                    if worker["state"] == "ready"
                    and not worker["drain_requested"]
                    and not worker["current_job_id"]
                    and worker["expires_at"] > now
                    and worker_window_reason(
                        connection,
                        worker,
                        now,
                        deployment_profile_id=payload["selection"][
                            "runtime_profile_id"
                        ],
                        expected_runtime_s=expected,
                    )
                    is None
                ]
                ready = bool(native_ready and eligible)
                dispatch_reason = None
                registration = binding.get("registration_observation", {})
                registration_time = registration.get("observed_at")
                registration_reason = (
                    registration.get("reason_code")
                    if _finite(registration_time)
                    and 0
                    <= now - registration_time
                    <= self.policy.get("observation_fresh_seconds", 30)
                    and registration.get("reason_code") in REGISTRATION_REASON_CODES
                    else None
                )
                if native_ready and not ready:
                    dispatch_reason = registration_reason or next(
                        (
                            reason
                            for worker in workers
                            if (
                                reason := worker_window_reason(
                                    connection,
                                    worker,
                                    now,
                                    deployment_profile_id=payload["selection"][
                                        "runtime_profile_id"
                                    ],
                                    expected_runtime_s=expected,
                                )
                            )
                        ),
                        "matching_worker_unavailable",
                    )
                # Presentation only: unresolved stopped creates still retain their
                # original ledger/fences until allocation evidence is reconciled.
                review_required = (
                    node["desired_state"] == "stopped"
                    and node["runtime_state"] == "creation_unknown"
                )
                result.append(
                    {
                        "node_id": node["intent_id"],
                        "provider": payload["provider"],
                        "runtime_profile_id": payload["selection"][
                            "runtime_profile_id"
                        ],
                        "mode": payload["selection"]["mode"],
                        "desired_state": node["desired_state"],
                        "state": node["runtime_state"],
                        "record_group": "pending_review"
                        if review_required
                        else "current",
                        "review_required": review_required,
                        "ready": ready,
                        "native_ready": native_ready,
                        "slots_ready": len(eligible) if native_ready else 0,
                        "observation_fresh": bool(fresh),
                        "observed_at": observed,
                        "hard_deadline": payload["hard_deadline"],
                        "hold_until": payload["hold_until"],
                        "hourly_cost_microusd": payload["hourly_cost_microusd"],
                        "hourly_cost_basis": "approved_ceiling",
                        **(
                            {"lifecycle": copy.deepcopy(payload["lifecycle"])}
                            if "lifecycle" in payload
                            else {}
                        ),
                        "billing_state": binding.get("billing_state", "unsettled"),
                        "reason_code": payload.get("stop_reason_code")
                        or binding.get("reason_code")
                        or dispatch_reason,
                        "dispatch_reason_code": dispatch_reason,
                        "registration_reason_code": registration_reason,
                        "version": command_revision(payload),
                    }
                )
        return {
            "capacity_backend": BACKEND_MARKER,
            "enabled": self.policy.get("enabled") is True,
            "operator": {"account": owner},
            "nodes": result,
            "summary": {
                "ready": sum(node["ready"] for node in result),
                "starting": sum(
                    node["state"]
                    in {"reserved", "creating", "starting", "runtime_unconfirmed"}
                    for node in result
                ),
                "unknown": sum(
                    "unknown" in node["state"] or not node["observation_fresh"]
                    for node in result
                ),
            },
        }

    def node_command(self, principal, node_id, body, key, kind="stop"):
        require(
            kind == "stop"
            and isinstance(body, dict)
            and not set(body) - {"expected_version", "acknowledge_capacity_loss"}
            and ("expected_version" not in body or _finite(body["expected_version"]))
            and (
                "acknowledge_capacity_loss" not in body
                or isinstance(body["acknowledge_capacity_loss"], str)
            ),
            "dstack_node_command_invalid",
            422,
        )
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_stop", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id, allow_admin=True)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {
                    "command_id": existing["id"],
                    "node_id": node_id,
                    "state": existing["state"],
                }
            require(
                "expected_version" not in body
                or body["expected_version"] == command_revision(node["payload"]),
                "operator_node_version_conflict",
            )
            now = self.repo.clock()
            from .dstack_workspace import CapacityLoss, stop_impact

            impact = stop_impact(connection, self.settings, [node_id], now)
            if (
                impact["warnings"]
                and body.get("acknowledge_capacity_loss") != impact["fingerprint"]
            ):
                raise CapacityLoss(impact)
            payload = advance_command_revision(copy.deepcopy(node["payload"]))
            payload.update(stop_requested_at=now, hold_until=0)
            if payload.get("dstack_observation"):
                payload["dstack_observation"]["ready"] = False
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    desired_state="stopped",
                    runtime_state="draining",
                    payload=payload,
                    updated_at=now,
                )
            )
            instance = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == node_id)
                )
                .mappings()
                .one()
            )
            if instance.get("provider_instance_id"):
                connection.execute(
                    update(registered_workers)
                    .where(
                        registered_workers.c.provider == instance["provider"],
                        registered_workers.c.instance_id
                        == instance["provider_instance_id"],
                        registered_workers.c.state != "retired",
                    )
                    .values(drain_requested=1, state="draining", updated_at=now)
                )
            command_id = str(uuid.uuid4())
            connection.execute(
                insert(operator_commands).values(
                    id=command_id,
                    actor=actor,
                    idempotency_key=key,
                    request_hash=digest,
                    kind="dstack_stop",
                    state="accepted",
                    payload={"node_id": node_id},
                    created_at=now,
                    updated_at=now,
                )
            )
            self.repo._emit(
                connection,
                "dstack.stop_requested",
                node_id,
                {"intent_id": node_id, "command_id": command_id},
            )
        return {"command_id": command_id, "node_id": node_id, "state": "accepted"}

    def set_hold(self, principal, node_id, body, key):
        require(
            isinstance(body, dict)
            and set(body)
            <= {"hold_seconds", "expected_version", "keep_on", "idle_shutdown_seconds"}
            and ("expected_version" not in body or _finite(body["expected_version"]))
            and any(
                field in body
                for field in ("hold_seconds", "keep_on", "idle_shutdown_seconds")
            ),
            "dstack_hold_invalid",
            422,
        )
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_hold", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return copy.deepcopy(existing["payload"])
            require(
                "expected_version" not in body
                or body["expected_version"] == command_revision(node["payload"]),
                "operator_node_version_conflict",
            )
            now = self.repo.clock()
            payload = advance_command_revision(copy.deepcopy(node["payload"]))
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node["desired_state"] == "running" and window_open(intent, node, now),
                "dstack_hold_window_expired",
            )
            if payload.get("lifecycle", {}).get("version") == 2:
                original_policy = payload["lifecycle"]
                options = {
                    "idle_shutdown_seconds": original_policy["idle_shutdown_seconds"],
                    **body,
                }
                # Alter only the shutdown preference; frozen accounts and the
                # protected execution quote are not client-editable.
                candidate = self._lifecycle(
                    options,
                    now,
                    original_policy["expected_runtime_s"],
                    original_guard=original_policy.get("test_gpu_guard"),
                )
                if "keep_on" not in body and "hold_seconds" not in body:
                    candidate["keep_on"] = original_policy["keep_on"]
                    candidate["hold_until"] = original_policy["hold_until"]
                for field in ("reservation_seconds", "budget_account_ids"):
                    candidate[field] = copy.deepcopy(original_policy[field])
                if "test_gpu_guard" in original_policy:
                    candidate["test_gpu_guard"] = copy.deepcopy(
                        original_policy["test_gpu_guard"]
                    )
                payload.update(lifecycle=candidate, hold_until=candidate["hold_until"])
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now)
                )
                funded = self.fund_window(
                    connection, intent, {**node, "payload": payload}, now
                )
                result = {
                    "node_id": node_id,
                    "hold_until": candidate["hold_until"],
                    "hard_deadline": None,
                    "lifecycle": copy.deepcopy(candidate),
                    "reserved_cost_microusd": funded,
                    "additional_reservation_microusd": funded
                    - intent["reserved_cost_microusd"],
                    "scope": "continuing_idle_policy_with_original_spend_cap",
                }
            else:
                require(
                    not set(body) - {"hold_seconds", "expected_version"}
                    and _integer(body.get("hold_seconds"), 0, 86400),
                    "dstack_hold_invalid",
                    422,
                )
                until = now + body["hold_seconds"] if body["hold_seconds"] else 0
                require(
                    until <= payload["hard_deadline"], "dstack_hold_deadline_exceeded"
                )
                payload["hold_until"] = until
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now)
                )
                result = {
                    "node_id": node_id,
                    "hold_until": until,
                    "hard_deadline": payload["hard_deadline"],
                    "additional_reservation_microusd": 0,
                    "scope": "idle_hold_within_original_deadline",
                }
            connection.execute(
                insert(operator_commands).values(
                    id=str(uuid.uuid4()),
                    actor=actor,
                    idempotency_key=key,
                    request_hash=digest,
                    kind="dstack_hold",
                    state="completed",
                    payload=result,
                    created_at=self.repo.clock(),
                    updated_at=self.repo.clock(),
                )
            )
        return result

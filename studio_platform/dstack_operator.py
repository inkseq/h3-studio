"""dstack application commands backed exclusively by the original capacity ledger.

Constructing the service does not contact providers, initialize budgets or change
capacity ceilings. HTTP callers select trusted, pinned deployments, not images,
credentials or arbitrary resources. The existing CPU controller supplies native
readiness; a dstack RUNNING task alone never authorizes generation.
"""

from __future__ import annotations
import copy
import json
import math
import os
import uuid
from sqlalchemy import select
from .dstack_capacity import (
    live_capacity_usage,
    CapacityRequest,
    DstackCapacity,
    DstackClient,
)
from .capacity_policy import window_open
from .capacity_interface import CapacityService
from .capacity_memory import configured_ram
from .operator_capacity import (
    operator_nodes,
    OperatorError,
    require,
    safe_id,
)
from .repository import (
    BudgetExceeded,
    jobs,
    request_hash,
    scaler_actions,
)
from .dstack_inventory import OPTIONAL_PROFILE_FIELDS as OPTIONAL, fallbacks_valid
from .dstack_inventory import request_options
from .runtime_catalog import engine_manifest, get_profile

from .dstack_operator_contract import (
    _window_open as _window_open,
    _cold_unadmitted_runtime as _cold_unadmitted_runtime,
    _request as _request,
    BACKEND_MARKER as BACKEND_MARKER,
    MAX_HOST_CANDIDATES as MAX_HOST_CANDIDATES,
    PREFERRED_DOWNLOAD_COUNTRIES as PREFERRED_DOWNLOAD_COUNTRIES,
    preferred_download_offer as preferred_download_offer,
    PROVIDERS as PROVIDERS,
    SCHEDULER_GPU_ALIASES as SCHEDULER_GPU_ALIASES,
    TERMINAL_JOBS as TERMINAL_JOBS,
    OBSERVATION_KEYS as OBSERVATION_KEYS,
    IDENTITY_KEYS as IDENTITY_KEYS,
    REGISTRATION_REASON_CODES as REGISTRATION_REASON_CODES,
    _actor as _actor,
    _integer as _integer,
    _finite as _finite,
    plan_offers as plan_offers,
    _protected_read as _protected_read,
)

from .dstack_ledger import LedgerDstackStore as LedgerDstackStore
from .dstack_preview import DstackPreview
from .dstack_registration import DstackRegistration
from .dstack_commands import DstackCommands


class DstackOperator(DstackPreview, DstackRegistration, DstackCommands):
    """Small owner-scoped application service; mount alongside existing operator API."""

    def _live_capacity_usage(self, connection):
        # Legacy invoices/intents remain in the ledger, but are not current
        # dstack capacity. Only exact owned new-path allocations count here.
        nodes = [
            dict(node)
            for node in connection.execute(select(operator_nodes)).mappings()
            if node["payload"].get("capacity_backend") == BACKEND_MARKER
        ]
        return live_capacity_usage(
            self.capacity.client,
            nodes,
            provider_inventory=self.provider_inventory,
            clock=self.repo.clock,
            fresh_seconds=self.policy["observation_fresh_seconds"],
        )

    def _observed_registration_usage(self, nodes):
        # Retain only this call's exact read in memory. The existing helper
        # owns immutable-run/identity validation and account absence; its
        # conservative count of one is not proof of a one-GPU allocation.
        observed_runs = {}
        client = self.capacity.client

        class ObservedClient:
            project, endpoint = client.project, getattr(client, "endpoint", None)

            def get(self, **identity):
                run = client.get(**identity)
                observed_runs[(identity.get("run_id"), identity.get("run_name"))] = run
                return run

        usage = live_capacity_usage(
            ObservedClient(),
            nodes,
            provider_inventory=self.provider_inventory,
            clock=self.repo.clock,
            fresh_seconds=self.policy["observation_fresh_seconds"],
        )
        return usage, observed_runs

    def __init__(
        self,
        repo,
        settings,
        capacity: CapacityService | None = None,
        profile_config=None,
        *,
        provider_inventory=None,
    ):
        self.repo, self.settings, self.capacity = repo, settings, capacity
        self.provider_inventory = provider_inventory or getattr(
            getattr(capacity, "client", None), "provider_inventory", None
        )
        self.config = copy.deepcopy(
            profile_config
            or {"version": 1, "policy": {"enabled": False}, "profiles": []}
        )
        require(self.config.get("version") == 1, "dstack_config_version_invalid", 422)
        self.policy = self.config.get("policy", {})
        self.profiles = {}
        if self.policy.get("enabled"):
            require(
                capacity is not None and type(self.policy["enabled"]) is bool,
                "dstack_adapter_unavailable",
            )
            for name, maximum in (
                ("max_instances", 128),
                ("max_physical_gpus", 128),
                ("max_hourly_cost_microusd", 9_000_000_000_000),
                ("max_ttl_seconds", 86400),
                ("idle_shutdown_seconds", 86400),
                ("observation_fresh_seconds", 120),
            ):
                require(
                    _integer(self.policy.get(name), maximum=maximum),
                    "dstack_policy_limits_invalid",
                    422,
                )
            require(
                "expires_at" in self.policy
                and (
                    _finite(self.policy["expires_at"])
                    or self.policy["expires_at"] is None
                    and self.policy.get("lifecycle_version") == 2
                ),
                "dstack_policy_window_invalid",
                422,
            )
            require(
                type(self.policy.get("lifecycle_version", 1)) is int
                and self.policy.get("lifecycle_version", 1) in {1, 2},
                "dstack_lifecycle_version_invalid",
                422,
            )
            if self.policy.get("lifecycle_version") == 2:
                require(
                    _integer(self.policy.get("reservation_seconds"), 120, 86400),
                    "dstack_reservation_window_invalid",
                    422,
                )
        for value in self.config.get("profiles", []):
            profile = self._validate_profile(value)
            require(profile["id"] not in self.profiles, "dstack_profile_duplicate", 422)
            self.profiles[profile["id"]] = profile
        if "test_gpu_guard" in self.policy:
            from .dstack_test_budget import ACCOUNT, POOLS, PROJECTS, validate_guard

            try:
                validate_guard(self.policy["test_gpu_guard"])
            except ValueError:
                raise OperatorError("dstack_test_guard_invalid", 422) from None
            require(
                getattr(settings, "public_origin", None) == "https://staging.inkseq.com"
                # Local server project, or the dstack Sky staging project.
                and self.config.get("project") in {"sixnine-staging", "inkseq-staging"}
                and self.policy.get("lifecycle_version") == 2
                and _integer(self.policy.get("max_instances"), 1, 10)
                and _integer(self.policy.get("max_physical_gpus"), 1, 10)
                and self.policy["max_instances"] == self.policy["max_physical_gpus"]
                and 1 <= self.policy["idle_shutdown_seconds"] <= 86400
                and all(
                    profile["pool"] in POOLS
                    and profile["project_id"] in PROJECTS
                    and ACCOUNT in profile["budget_account_ids"]
                    and profile["extra_reservation_microusd"] > 0
                    for profile in self.profiles.values()
                ),
                "dstack_test_guard_binding_invalid",
                422,
            )
        self.fingerprint = request_hash(self.config)

    def _validate_profile(self, value):
        require(isinstance(value, dict), "dstack_profile_invalid", 422)
        profile = copy.deepcopy(value)
        required = {
            "id",
            "owner_id",
            "project_id",
            "pool",
            "backend",
            "runtime_profile_id",
            "mode",
            "configuration_id",
            "image",
            "gpu_names",
            "memory_gib",
            "disk_gib",
            "gpu_memory_gib",
            "cpu_count",
            "max_price_microusd",
            "max_ttl_seconds",
            "extra_reservation_microusd",
            "budget_account_ids",
        }
        require(
            required <= set(profile) and not set(profile) - required - OPTIONAL,
            "dstack_profile_fields_invalid",
            422,
        )
        require(
            all(
                safe_id(profile[key])
                for key in ("id", "owner_id", "project_id", "pool", "configuration_id")
            )
            and self._capacity_owner_known(profile["owner_id"])
            and isinstance(profile["budget_account_ids"], list)
            and profile["budget_account_ids"]
            and len(profile["budget_account_ids"])
            == len(set(profile["budget_account_ids"]))
            and all(safe_id(item) for item in profile["budget_account_ids"]),
            "dstack_profile_scope_invalid",
            422,
        )
        try:
            native = get_profile(profile["runtime_profile_id"])
            manifest = engine_manifest(profile["runtime_profile_id"], profile["mode"])
        except (KeyError, ValueError):
            raise OperatorError("dstack_native_profile_invalid", 422) from None
        floors = {
            "memory_gib": max(
                native.get("minimum_ram_bytes", 0),
                native["runtime"].get("minimum_available_ram_bytes", 0),
                native["runtime"].get("minimum_total_ram_bytes", 0),
            ),
            "disk_gib": native.get("minimum_disk_bytes", 0),
            "gpu_memory_gib": max(
                native.get("minimum_free_vram_bytes", 0),
                native["runtime"].get("minimum_free_vram_bytes", 0),
                native.get("hardware_admission", {}).get("minimum_total_vram_bytes", 0),
            ),
        }
        require(
            all(
                _integer(profile.get(key)) and profile[key] * 2**30 >= floor
                for key, floor in floors.items()
            ),
            "dstack_native_resource_floor_invalid",
            422,
        )
        normalize = lambda name: "".join(
            char.lower() for char in name if char.isalnum()
        )
        allowed = {
            normalize(name)
            for name in native.get("hardware_admission", {}).get(
                "gpu_models", native["gpu_models"]
            )
        }
        require(
            isinstance(profile["gpu_names"], list)
            and profile["gpu_names"]
            and all(
                isinstance(name, str)
                and normalize(SCHEDULER_GPU_ALIASES.get(name, name)) in allowed
                for name in profile["gpu_names"]
            ),
            "dstack_native_gpu_binding_invalid",
            422,
        )
        require(
            fallbacks_valid(profile, PROVIDERS), "dstack_profile_fields_invalid", 422
        )
        require(
            _integer(profile["extra_reservation_microusd"], 0)
            and _integer(profile["max_ttl_seconds"], 120, 86400),
            "dstack_profile_limits_invalid",
            422,
        )
        profile.update(
            model_id=native["model_id"],
            manifest_digest=manifest.digest,
            recipe_id=manifest.document["generation_recipe_id"],
        )
        # Exercise exact transport validation without constructing any provider request.
        self._capacity_request(profile, str(uuid.uuid4()), 1.0, 121.0, 120)
        return profile

    def authorize(self, principal):
        from .identity import require_admin

        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_capacity=True,
            legacy_code="operator_forbidden",
        )

    def _capacity_owner_known(self, owner):
        from .identity import capacity_owner_known

        return capacity_owner_known(self.repo, self.settings, owner)

    def authorize_admin(self, principal):
        from .identity import require_admin

        self.authorize(principal)
        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_code="operator_admin_forbidden",
        )

    def _enabled(self):
        require(
            self.policy.get("enabled") is True and self.capacity is not None,
            "dstack_capacity_disabled",
        )
        require(
            self.policy["expires_at"] is None
            or self.repo.clock() < self.policy["expires_at"],
            "dstack_authority_expired",
        )

    def demand_start_once(self):
        from .dstack_demand_start import start_once

        return start_once(self)

    def demand_start_summary(self, connection, now):
        from .dstack_demand import summary

        return summary(self, connection, now)

    def record_download_timebox(
        self, connection, current, intent, now, *, reason_code=None
    ):
        """Journal an owned cold stop on the original job and host history.

        The controller rechecks its cold/no-execution fence under this same
        capacity lock. This callback never starts/stops a provider or settles
        money; the original stop path owns those operations. A measured slow
        download is stopped even without a bound demand job or physical host;
        with a bound Vast machine it is also denied until an admin restores it.
        """
        from .dstack_demand_start import DOWNLOAD_TIMEBOX, DOWNLOAD_TOO_SLOW

        reason_code = reason_code or DOWNLOAD_TIMEBOX
        require(
            reason_code in {DOWNLOAD_TIMEBOX, DOWNLOAD_TOO_SLOW},
            "dstack_download_stop_reason_invalid",
        )
        payload = self._journal_download_stop(
            connection, current, intent, now, reason_code
        )
        if reason_code != DOWNLOAD_TOO_SLOW:
            return payload
        from .host_policy import HostPolicyStore, bound_observation

        physical = bound_observation(current, now=now)
        if (
            physical is not None
            and physical['provider_instance_id'] == intent['provider_instance_id']
        ):
            HostPolicyStore(self.repo, self.settings.tenant_id).deny_slow_download(
                connection,
                physical['machine_id'],
                node_id=current['intent_id'],
                now=now,
            )
        return payload if payload is not None else copy.deepcopy(current['payload'])

    def _journal_download_stop(self, connection, current, intent, now, reason_code):
        from .dstack_demand import profile_hash
        from .dstack_demand_start import (
            MAX_DOWNLOAD_REPLACEMENTS,
            _start_command,
            _start_key,
            _write,
            download_journal,
        )
        from .host_policy import bound_observation, failure

        payload = copy.deepcopy(current['payload'])
        demand = payload.get('demand_start')
        if not isinstance(demand, dict) or not isinstance(demand.get('job_id'), str):
            return None
        profile = self.profiles.get(demand.get('profile_id'))
        physical = bound_observation(current, now=now)
        if (
            profile is None
            or physical is None
            or physical['provider_instance_id'] != intent['provider_instance_id']
        ):
            return None
        job = (
            connection.execute(
                select(jobs).where(
                    jobs.c.id == demand['job_id'],
                    jobs.c.tenant_id == self.settings.tenant_id,
                )
            )
            .mappings()
            .first()
        )
        if (
            job is None
            or job['status'] != 'queued'
            or any(
                (
                    demand.get('plan_id') != job['plan_id'],
                    demand.get('request_hash') != job['request_hash'],
                    demand.get('execution_hash') != request_hash(job['execution_plan']),
                    demand.get('profile_id') != profile['id'],
                    demand.get('profile_hash') != profile_hash(self.config, profile),
                )
            )
        ):
            return None
        row = download_journal(connection, self.settings.tenant_id, job['id'])
        if row is None:
            return None
        identity = request_hash(
            {
                'tenant': self.settings.tenant_id,
                'job_id': job['id'],
                'profile_hash': demand['profile_hash'],
            }
        )
        journal = copy.deepcopy(row['payload'])
        if row['request_hash'] != identity or type(journal.get('cycle')) is not int:
            return None
        original = _start_command(
            connection, self, profile, _start_key(identity, journal['cycle'])
        )
        if original is None or original['payload']['node_id'] != current['intent_id']:
            return None
        stops = journal.get('download_stops', [])
        existing = next(
            (s for s in stops if s['node_id'] == current['intent_id']), None
        )
        if existing is None:
            if len(stops) >= MAX_DOWNLOAD_REPLACEMENTS + 1:
                return None
            stops.append(
                {
                    'node_id': current['intent_id'],
                    'machine_id': physical['machine_id'],
                    'at': now,
                }
            )
            journal['download_stops'] = stops
            _write(connection, row, journal, now)
        payload['host_failure'] = failure(
            {
                'version': 1,
                'stage': 'model_download',
                'reason_code': reason_code,
                'at': existing['at'] if existing else now,
                'qualified_start_failure': False,
            },
            now=now,
        )
        payload['download_timebox_event'] = {
            'version': 1,
            'job_id': job['id'],
            'machine_id': physical['machine_id'],
            'at': existing['at'] if existing else now,
            'reason_code': reason_code,
        }
        self.repo._emit(
            connection,
            'dstack.download_timebox_stop_requested',
            current['intent_id'],
            {
                'job_id': job['id'],
                'machine_id': physical['machine_id'],
                'reason_code': reason_code,
            },
        )
        return payload

    def _capacity_request(
        self,
        profile,
        intent_id,
        created_at,
        deadline,
        duration,
        *,
        lifecycle_version=1,
        test_max_duration=None,
        test_task_id=None,
    ):
        return CapacityRequest(
            intent_id=intent_id,
            backend=profile["backend"],
            profile_id=profile["runtime_profile_id"],
            mode=profile["mode"],
            model_id=profile["model_id"],
            configuration_id=profile["configuration_id"],
            manifest_digest=profile["manifest_digest"],
            image=profile["image"],
            gpu_names=tuple(profile["gpu_names"]),
            memory_gib=configured_ram(profile),
            disk_gib=profile["disk_gib"],
            gpu_memory_gib=profile["gpu_memory_gib"],
            cpu_count=profile["cpu_count"],
            max_price_microusd=profile["max_price_microusd"],
            created_at=created_at,
            hard_deadline=deadline,
            max_duration_s=duration,
            **request_options(profile, self),
            lifecycle_version=lifecycle_version,
            test_task_id=test_task_id,
            test_max_duration_s=(
                test_max_duration
                or self.policy.get("test_gpu_guard", {}).get("max_runtime_seconds")
                if lifecycle_version == 2
                else None
            ),
        )

    def _execution_check(self, profile, now=None):
        """One read-only policy check shared by catalog and original admission."""
        from .execution_profiles import selected_policy
        from .execution_policy import authority_current, reservation_for_duration

        try:
            policy = selected_policy(
                self.settings,
                profile_id=profile["runtime_profile_id"],
                recipe_id=profile["recipe_id"],
            )
            now = self.repo.clock() if now is None else now
            if policy is None:
                return None, "execution_policy_missing"
            if policy["enabled"] is not True:
                return None, "execution_policy_disabled"
            if not (
                policy["backend"] == "wangp-worker"
                and policy.get("dispatch_backend") == "hatchet-v1"
                and policy["pool"] == profile["pool"]
                and policy["configuration_id"] == profile["configuration_id"]
                and policy["model_id"] == profile["model_id"]
                and policy["engine_manifest_digest"] == profile["manifest_digest"]
            ):
                return None, "execution_policy_binding_mismatch"
            if policy["qualification"]["status"] not in {
                "accepted",
                "runtime_required",
            }:
                return None, "execution_policy_unqualified"
            if not authority_current(policy, now):
                return None, (
                    "execution_policy_not_yet_valid"
                    if policy["qualification"]["verified_at"] > now
                    else "execution_policy_expired"
                )
            duration = reservation_for_duration(
                policy, policy["envelope"]["max_duration_seconds"]
            )["expected_runtime_s"]
            if not (_finite(duration) and 0 < duration <= 86400):
                return None, "execution_runtime_unconfirmed"
            return duration, None
        except (KeyError, TypeError, ValueError):
            return None, "execution_policy_invalid"

    def _expected_runtime(self, profile):
        """Use the original protected execution quote, never an HTTP estimate."""
        duration, reason = self._execution_check(profile)
        require(
            reason is None,
            "dstack_execution_runtime_unconfirmed"
            if reason == "execution_runtime_unconfirmed"
            else "dstack_execution_policy_unconfirmed",
        )
        return duration

    def _admin_limits(self, connection=None):
        from .spending_limits import admin_defaults
        from .capacity_settings import read_policy

        if connection is None:
            with self.repo.engine.connect() as connection:
                return self._admin_limits(connection)
        return read_policy(
            connection,
            self.settings.tenant_id,
            admin_defaults=admin_defaults(self),
        )

    def _lifecycle(
        self, body, now, expected_runtime, *, limits=None, original_guard=None
    ):
        limits = limits or self._admin_limits()["value"]
        keep = body.get("keep_on", "until" if body.get("hold_seconds", 0) else "idle")
        hold = body.get("hold_seconds", 0)
        idle = body.get("idle_shutdown_seconds", limits["idle_shutdown_seconds"])
        require(
            isinstance(keep, str)
            and keep in {"idle", "until", "until_stopped"}
            and _integer(hold, 0, 86400)
            and _integer(idle, 1, 86400)
            and (keep == "until" and hold > 0 or keep != "until" and hold == 0),
            "dstack_lifecycle_policy_invalid",
            422,
        )
        guard = self.policy.get("test_gpu_guard")
        require(
            guard is None or keep == "idle" and idle <= limits["idle_shutdown_seconds"],
            "dstack_test_idle_policy_required",
            422,
        )
        result = {
            "version": 2,
            "keep_on": keep,
            "hold_until": now + hold if hold else 0,
            "idle_shutdown_seconds": idle,
            "reservation_seconds": self.policy["reservation_seconds"],
            "expected_runtime_s": expected_runtime,
        }
        if guard is not None:
            from .dstack_test_budget import task_guard, valid_task_id, validate_guard

            require(
                original_guard is not None or valid_task_id(body.get("test_task_id")),
                "dstack_test_task_required",
                422,
            )
            require(
                limits["max_runtime_seconds"]
                > expected_runtime + guard["stop_margin_seconds"],
                "dstack_test_runtime_limit_reached",
                422,
            )
            result["test_gpu_guard"] = (
                copy.deepcopy(validate_guard(original_guard))
                if original_guard is not None
                else task_guard(
                    guard,
                    body["test_task_id"],
                    limit=limits["test_task_limit_microusd"],
                    max_runtime=limits["max_runtime_seconds"],
                )
            )
        else:
            require("test_task_id" not in body, "dstack_test_task_invalid", 422)
        return result

    def fund_window(self, connection, intent, node, now):
        """Fund preparation, elapsed paid time and a full safe execution runway."""
        from .spending_limits import ADMIN_OWNER, lock_owner

        self.repo._lock_capacity(connection)
        lock_owner(connection, self.settings.tenant_id, ADMIN_OWNER)
        from .worker_admission import COMPLETION_MARGIN_SECONDS

        payload, lifecycle = node["payload"], node["payload"]["lifecycle"]
        from .dstack_test_budget import budget_reason

        reason = budget_reason(connection, intent, node, now)
        if reason:
            raise BudgetExceeded(reason)
        require(window_open(intent, node, now), "dstack_continuing_policy_unconfirmed")
        started = connection.execute(
            select(scaler_actions.c.create_started_at).where(
                scaler_actions.c.intent_id == intent["id"]
            )
        ).scalar_one_or_none()
        started = intent["created_at"] if started is None else started
        expected = lifecycle.get("expected_runtime_s")
        hourly, extra = (
            payload.get("hourly_cost_microusd"),
            payload.get("extra_reservation_microusd", 0),
        )
        require(
            _finite(started)
            and 0 <= now - started
            and _finite(expected)
            and 0 < expected <= 86400
            and _integer(hourly)
            and _integer(extra, 0),
            "dstack_funding_basis_unconfirmed",
        )
        chunk = lifecycle["reservation_seconds"]
        paid = (
            now
            - started
            + expected
            + max(lifecycle["idle_shutdown_seconds"], COMPLETION_MARGIN_SECONDS)
        )
        # Admission requires strictly more funding than the complete runway.
        seconds = math.ceil((paid + 1) / chunk) * chunk + 300
        minimum = math.ceil(hourly * seconds / 3600) + extra
        reason = budget_reason(connection, intent, node, now, minimum_reserved=minimum)
        if reason:
            raise BudgetExceeded(reason)
        return self.repo.fund_instance_window(
            connection, intent["id"], minimum, lifecycle["budget_account_ids"]
        )


def from_environment(repo, settings, *, readiness=None):
    """Protected explicit config only; no provider I/O, DDL or budget widening."""
    filename = os.environ.get("DSTACK_OPERATOR_CONFIG", "")
    if not filename:
        return DstackOperator(repo, settings)
    try:
        config = json.loads(_protected_read(filename))
        require(
            isinstance(config, dict) and config.get("version") == 1,
            "dstack_config_invalid",
            422,
        )
        if config.get("policy", {}).get("enabled") is not True:
            return DstackOperator(repo, settings)
        token = _protected_read(config.pop("token_file"), maximum=4096).strip()
        ssh = _protected_read(config.pop("ssh_public_key_file"), maximum=16384).strip()
        client = DstackClient(config["endpoint"], token, config["project"])
        lanes = []
        if "serverless_file" in config:
            # RunPod Serverless dial-out jobs beside dstack (#859).
            from .runpod_serverless import RunPodServerlessClient

            serverless = json.loads(
                _protected_read(config.pop("serverless_file"), maximum=16384)
            )
            lanes.append(RunPodServerlessClient(serverless, config["project"]))
        if "modal_dialout_file" in config:
            # Modal sandboxes running the same dial-out worker (#859).
            from .modal_dialout import ModalDialoutClient

            modal = json.loads(
                _protected_read(config.pop("modal_dialout_file"), maximum=16384)
            )
            lanes.append(ModalDialoutClient(modal, config["project"]))
        if lanes:
            from .dialout_capacity import RoutedClient

            client = RoutedClient(client, *lanes)
        config["ssh_public_key"] = ssh
        store = LedgerDstackStore(
            repo,
            settings.tenant_id,
            observation_fresh_seconds=config["policy"]["observation_fresh_seconds"],
        )
        capacity = DstackCapacity(client, store, clock=repo.clock, readiness=readiness)
        return DstackOperator(repo, settings, capacity, config)
    except (KeyError, TypeError, json.JSONDecodeError):
        raise OperatorError("dstack_config_invalid", 422) from None


def create_capacity():
    """No-argument factory for the pinned capacity CLI; use original runtime DB."""
    from .repository import Repository
    from .settings import Settings

    settings = Settings.from_environment()
    repo = Repository(settings.database_url)
    service = from_environment(repo, settings)
    require(service.capacity is not None, "dstack_capacity_disabled")
    return service.capacity

"""Current native host usage and original registration limits."""

from __future__ import annotations
import copy
from sqlalchemy import select
from .operator_capacity import (
    operator_nodes,
    require,
)
from .repository import (
    BudgetExceeded,
    instance_intents,
    manually_reviewed_inactive,
    registered_devices,
)
from .dstack_inventory import profile_backends

from .dstack_operator_contract import (
    BACKEND_MARKER,
    PROVIDERS,
)


class DstackRegistration:
    def machine_limits(self, connection, *, for_demand=False):
        from .capacity_settings import LEGACY_DEMAND_MAX_MACHINES

        settings = self._admin_limits(connection)
        if settings["adopted"]:
            maximum = settings["value"]["max_machines"]
            # A production policy adoption does not widen the independently
            # qualified staging test guard or its explicitly protected allowance.
            if "test_gpu_guard" in self.policy:
                return (
                    min(maximum, self.policy["max_instances"]),
                    min(maximum, self.policy["max_physical_gpus"]),
                )
            return maximum, maximum
        # Old environments are not adopted by deploying a reader. Preserve the
        # old protected configuration until the explicit audited settings write.
        maximum = (
            LEGACY_DEMAND_MAX_MACHINES
            if for_demand or self.policy.get("demand_start")
            else 128
        )
        return (
            min(maximum, self.policy["max_instances"]),
            min(maximum, self.policy["max_physical_gpus"]),
        )

    def prelaunch_capacity_limits(
        self,
        connection,
        *,
        profile=None,
        request=None,
        capacity_snapshot=None,
        for_demand=False,
    ):
        from .capacity_settings import admission_snapshot
        from .control import effective_registration_limits
        from .repository import capacity_gate, pool_limits

        global_limits = (
            connection.execute(
                select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            .mappings()
            .first()
        )
        stored = self._admin_limits(connection)
        current_snapshot = admission_snapshot(stored)
        selected = (
            self.profiles.get(profile.get("id")) if isinstance(profile, dict) else None
        )
        bound_profile = (
            selected is not None
            and all(
                profile.get(key) == selected.get(key)
                for key in (
                    "id",
                    "owner_id",
                    "project_id",
                    "pool",
                    "runtime_profile_id",
                    "mode",
                    "configuration_id",
                )
            )
            and profile.get("backend") in profile_backends(selected, PROVIDERS)
        )
        pool = None
        if bound_profile:
            pool = (
                connection.execute(
                    select(pool_limits).where(pool_limits.c.pool == selected["pool"])
                )
                .mappings()
                .first()
            )
            if pool is None:
                raise BudgetExceeded("pool_disabled")
        admitted = None
        if (
            current_snapshot is not None
            and bound_profile
            and type(self.policy.get("lifecycle_version")) is int
            and self.policy["lifecycle_version"] == 2
        ):
            if for_demand:
                # Eligible demand already selects this exact configured v2 profile;
                # the normal start revalidates and freezes the current snapshot.
                admitted = current_snapshot
            elif capacity_snapshot == current_snapshot and isinstance(request, dict):
                expected = {
                    "backend": request.get("backend")
                    if request.get("backend") in profile_backends(selected, PROVIDERS)
                    else selected["backend"],
                    "profile_id": selected["runtime_profile_id"],
                    "mode": selected["mode"],
                    "model_id": selected["model_id"],
                    "configuration_id": selected["configuration_id"],
                    "manifest_digest": selected["manifest_digest"],
                    "hard_deadline": None,
                    "gpu_count": 1,
                    "lifecycle_version": 2,
                }
                if (
                    all(
                        key in request and request[key] == value
                        for key, value in expected.items()
                    )
                    and type(request["gpu_count"]) is int
                    and type(request["lifecycle_version"]) is int
                ):
                    admitted = current_snapshot
        limits = effective_registration_limits(global_limits, pool, snapshot=admitted)
        maximum, gpus = self.machine_limits(connection, for_demand=for_demand)
        return {
            "max_instances": min(maximum, limits["max_instances"]),
            "max_physical_gpus": min(gpus, limits["max_physical_gpus"]),
        }

    def _limits(
        self,
        connection,
        hourly,
        *,
        profile=None,
        request=None,
        capacity_snapshot=None,
        preread=None,
    ):
        from .control import (
            registration_capacity_projection,
            check_registration_capacity,
        )

        usage = self.prelaunch_capacity_usage(connection, preread=preread, reread=True)
        limits = self.prelaunch_capacity_limits(
            connection,
            profile=profile,
            request=request,
            capacity_snapshot=capacity_snapshot,
        )
        check_registration_capacity(registration_capacity_projection(usage), limits)
        require(
            usage["hourly_cost_microusd"] + hourly
            <= self._admin_limits(connection)["value"]["fleet_hourly_limit_microusd"],
            "dstack_hourly_limit_exceeded",
        )
        return usage

    def prelaunch_capacity_usage(self, connection, preread=None, *, reread=False):
        """Use the same global overlay as registration under the capacity lock.

        ``preread`` comes from ``capacity_preread`` before the lock; then only
        committed facts are rechecked here and no provider is called (#1148).
        """
        from .native_capacity_inventory import prelaunch_usage

        return prelaunch_usage(self, connection, preread=preread, reread=reread)

    def capacity_preread(self):
        """Provider reads for ``prelaunch_capacity_usage``; call without a lock."""
        from .native_capacity_inventory import capacity_preread

        return capacity_preread(self)

    def _registration_resource_overlay(
        self, connection, nodes, intents, usage, observed_runs
    ):
        """Read-only global registration inventory; no release/settlement writes."""
        by_id = {node["intent_id"]: node for node in nodes}
        excluded = set()
        absent_workers = {}
        for observation in usage["observations"]:
            if observation["counted"]:
                continue
            node, intent = (
                by_id[observation["intent_id"]],
                intents.get(observation["intent_id"]),
            )
            original = node["payload"]["dstack"]
            if (
                intent is None
                or PROVIDERS.get(original.get("backend")) != intent["provider"]
            ):
                continue
            if (
                observation["reason_code"]
                in {"dstack_terminal_provider_absent", "dstack_provider_removal_proven"}
                and original.get("provider_instance_id")
                == intent["provider_instance_id"]
            ):
                # The original provider identity may never have been journaled
                # before provisioning failed. live_capacity_usage still proved
                # this exact owned allocation terminal and its complete fresh
                # account absence. Two null journals are consistent with that
                # proof; conflicting identities remain conservative.
                excluded.add(intent["id"])
                if intent["provider_instance_id"]:
                    key = (intent["provider"], intent["provider_instance_id"])
                    absent_workers.setdefault(key, set()).add(
                        "dstack-" + intent["id"].replace("-", "")
                    )
            elif (
                observation["reason_code"]
                in {
                    "dstack_terminal_no_allocation",
                    "dstack_terminal_provider_accounted",
                }
                and intent["provider_instance_id"] is None
                and original.get("provider_instance_id") is None
            ):
                excluded.add(intent["id"])
            elif (
                observation["reason_code"] == "dstack_never_applied"
                and intent["state"] == "destroyed"
            ):
                excluded.add(intent["id"])
        # The same fresh owned native GET may know a provider identity before
        # observe() journals it. Normalize only a non-conflicting active identity;
        # otherwise an intent key and its known allocation would count twice.
        active_keys, unconfirmed_allocations = {}, []
        for observation in usage["observations"]:
            if observation["reason_code"] != "dstack_capacity_active":
                continue
            node = by_id[observation["intent_id"]]
            original = node["payload"]["dstack"]
            intent = intents.get(node["intent_id"])
            run = observed_runs.get(
                (original["run_id"], None)
                if original.get("run_id")
                else (None, original["run_name"])
            )
            jpd = (run.get("latest_job_submission") or {}).get(
                "job_provisioning_data"
            ) or {}
            instance = jpd.get("instance_id")
            if (
                intent is not None
                and isinstance(instance, str)
                and instance
                and jpd.get("backend") == original.get("backend")
                and PROVIDERS.get(jpd.get("backend")) == intent["provider"]
                and intent["provider_instance_id"] in (None, instance)
                and original.get("provider_instance_id") in (None, instance)
                and original["backend"] + ":" + instance in usage["resources"]
            ):
                active_keys[intent["id"]] = (intent["provider"], instance)
            gpus = (jpd.get("instance_type") or {}).get("resources", {}).get("gpus")
            if (
                node["intent_id"] not in active_keys
                or not isinstance(gpus, list)
                or not 1 <= len(gpus) <= 128
            ):
                unconfirmed_allocations.append(node["intent_id"])
        resources, active_intents = {}, []
        for intent in intents.values():
            if (
                intent["state"] == "destroyed"
                or intent["id"] in excluded
                or manually_reviewed_inactive(connection, intent)
            ):
                continue
            active_intents.append(intent["id"])
            key = active_keys.get(
                intent["id"],
                (
                    intent["provider"],
                    intent["provider_instance_id"] or "intent:" + intent["id"],
                ),
            )
            resources[key] = resources.get(key, 0) + intent["physical_gpus"]
        # A second included binding for the same physical identity defeats the
        # absence exclusion. Devices owned by unrelated/legacy workers remain.
        live_keys = {}
        for resource_key, count in usage["resources"].items():
            for backend, provider in PROVIDERS.items():
                if resource_key.startswith(backend + ":"):
                    live_keys[(provider, resource_key[len(backend) + 1 :])] = count
                    break
        device_counts = {}
        for device in connection.execute(
            select(registered_devices).where(
                registered_devices.c.state != "released",
                registered_devices.c.provider != "mock",
            )
        ).mappings():
            key = (device["provider"], device["instance_id"])
            if (
                device["worker_id"] in absent_workers.get(key, ())
                and key not in resources
                and key not in live_keys
            ):
                continue
            device_counts[key] = device_counts.get(key, 0) + 1
        for key, count in device_counts.items():
            resources[key] = max(resources.get(key, 0), count)
        # Positive live allocations may exceed the original intent's requested
        # count. Retain them globally even when a different node is registering;
        # unknown intent-key fallbacks never replace original ledger identities.
        for key, count in live_keys.items():
            resources[key] = max(resources.get(key, 0), count)
        return {
            "resources": resources,
            "instances": len(resources),
            "physical_gpus": sum(resources.values()),
            "all_resource_intent_ids": tuple(active_intents),
            "native_resources": tuple(live_keys),
            "unconfirmed_allocation_ids": tuple(unconfirmed_allocations),
        }

    def registration_capacity_usage(self, connection, spec, preread=None):
        """Overlay proven dstack inactivity on ALL original global resources.

        WorkerControl holds the original capacity lock while this read runs.
        Terminal owned runs plus complete, fresh same-account absence are the
        existing live_capacity_usage evidence; they are NOT destruction or
        settlement. Unaccounted provider identities, legacy intents and
        unrelated physical devices keep counting. No row, reservation or
        provider is mutated.
        """
        nodes = [
            dict(node)
            for node in connection.execute(select(operator_nodes)).mappings()
            if node["payload"].get("capacity_backend") == BACKEND_MARKER
        ]
        intents = {
            row["id"]: dict(row)
            for row in connection.execute(select(instance_intents)).mappings()
        }
        matching = [
            node
            for node in nodes
            if (intent := intents.get(node["intent_id"])) is not None
            and intent["provider"] == spec.provider
            and intent["provider_instance_id"] == spec.instance_id
            and intent["pool"] == spec.pool
            and node["payload"].get("tenant_id") == self.settings.tenant_id
            and node["desired_state"] == "running"
            and intent["state"] in {"starting", "ready", "busy"}
        ]
        require(
            len(matching) == 1
            and spec.backend == "wangp-worker"
            and spec.dispatch_backend == "hatchet-v1",
            "dstack_registration_binding_invalid",
        )
        binding = matching[0]["payload"]["dstack"]
        require(
            binding.get("provider_instance_id") == spec.instance_id
            and PROVIDERS.get(binding.get("backend")) == spec.provider
            and binding.get("model_id") == spec.model_id
            and binding.get("configuration_id") == spec.configuration_id
            and binding.get("manifest_digest") == spec.engine_manifest_digest
            and spec.recipe_ids
            == (
                {"fl": ("h3-base-fl2va-v1",), "ref": ("h3-base-ref2va-v1",)}.get(
                    binding.get("mode")
                )
            )
            and len(spec.physical_gpu_ids) == 1,
            "dstack_registration_binding_invalid",
        )
        from .native_capacity_inventory import observed_usage

        usage, observed_runs = observed_usage(self, nodes, preread, reread=True)
        current = next(
            observation
            for observation in usage["observations"]
            if observation["intent_id"] == matching[0]["intent_id"]
        )
        require(
            current["counted"]
            and current["dstack_status"] == "running"
            and current["reason_code"] == "dstack_capacity_active",
            "dstack_registration_run_unconfirmed",
        )
        resource_key = binding["backend"] + ":" + spec.instance_id
        run = (
            observed_runs.get((binding["run_id"], None))
            if binding.get("run_id")
            else observed_runs.get((None, binding["run_name"]))
        )
        gpus = run["latest_job_submission"]["job_provisioning_data"]["instance_type"][
            "resources"
        ].get("gpus")
        require(
            isinstance(gpus, list)
            and len(gpus) == 1
            and usage["resources"].get(resource_key) == 1,
            "dstack_registration_allocation_unconfirmed",
        )
        from .native_capacity_inventory import resource_overlay

        result = resource_overlay(
            self, connection, nodes, intents, usage, observed_runs
        )
        resources = result["resources"]
        original = matching[0]["payload"]
        snapshot = original.get("capacity_policy_snapshot")
        if (
            snapshot is not None
            and original.get("request", {}).get("lifecycle_version") == 2
            and binding.get("lifecycle_version") == 2
        ):
            from .capacity_settings import validate_snapshot

            validate_snapshot(snapshot)
            intent = intents[matching[0]["intent_id"]]
            require(
                type(original["request"]["lifecycle_version"]) is int
                and type(binding["lifecycle_version"]) is int
                and type(original.get("lifecycle", {}).get("version")) is int
                and original["lifecycle"]["version"] == 2
                and original.get("hard_deadline") is None
                and intent["hard_deadline"] is None
                and intent["physical_gpus"] == intent["slots"] == 1
                and spec.worker_id
                == "dstack-" + matching[0]["intent_id"].replace("-", "")
                and resources.get((spec.provider, spec.instance_id)) == 1,
                "dstack_registration_admission_invalid",
            )
            # This fulfills an original one-GPU admission, not a newly paid
            # capacity grant under today's possibly lower settings.
            result["capacity_admission"] = {
                "capacity_backend": BACKEND_MARKER,
                "lifecycle_version": 2,
                "intent_id": intent["id"],
                "worker_id": spec.worker_id,
                "provider": spec.provider,
                "instance_id": spec.instance_id,
                "pool": spec.pool,
                "capacity_policy_snapshot": copy.deepcopy(snapshot),
            }
        return result

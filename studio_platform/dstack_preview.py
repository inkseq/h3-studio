"""Native offer previews and existing price projections."""

from __future__ import annotations
from dataclasses import asdict, replace
import copy
import math
import uuid
from sqlalchemy import insert
from .capacity_memory import catalog_resources
from .operator_capacity import (
    OperatorError,
    operator_previews,
    require,
    safe_id,
)
from .repository import (
    request_hash,
)
from .providers.registry import VASTAI

from .dstack_operator_contract import (
    BACKEND_MARKER,
    MAX_HOST_CANDIDATES,
    preferred_download_offer,
    _actor,
    _integer,
    plan_offers,
)


class DstackPreview:
    def catalog(self, principal):
        owner = self.authorize(principal)
        fields = (
            "id",
            "runtime_profile_id",
            "configuration_id",
            "recipe_id",
            "model_id",
            "mode",
            "pool",
            "backend",
            "gpu_names",
            "memory_gib",
            "disk_gib",
            "gpu_memory_gib",
            "cpu_count",
            "max_price_microusd",
            "max_ttl_seconds",
        )
        from .operator_prices import OperatorPrices

        with self.repo.engine.connect() as connection:
            prices = OperatorPrices(self.repo, self.settings)
            profiles = []
            observed_at = self.repo.clock()
            for profile in self.profiles.values():
                if profile["owner_id"] != owner:
                    continue
                price, _, _ = prices.read(connection, profile)
                reason = None
                if self.policy.get("lifecycle_version") == 2:
                    _, reason = self._execution_check(profile, observed_at)
                profiles.append(
                    {
                        **catalog_resources(profile, fields),
                        "max_price_microusd": price["max_price_microusd"],
                        "price_ceiling_microusd": price["price_ceiling_microusd"],
                        "price_policy_snapshot": price["snapshot"],
                        "execution_eligibility": {
                            "status": "confirmed" if reason is None else "blocked",
                            "reason_code": reason,
                            "observed_at": observed_at,
                        },
                    }
                )
        # New starts follow the configured authoring default; retained profiles
        # remain explicit choices and existing nodes are never rebound.
        default = getattr(self.settings, "default_deployment_profile_id", None)
        profiles.sort(key=lambda item: item["runtime_profile_id"] != default)
        return {
            "capacity_backend": BACKEND_MARKER,
            "enabled": self.policy.get("enabled") is True,
            "lifecycle_version": self.policy.get("lifecycle_version", 1),
            "test_task_id_required": self.policy.get("test_gpu_guard") is not None,
            "operator": {"account": owner},
            "profiles": profiles,
        }

    def admin_prices(self, principal):
        owner = self.authorize_admin(principal)
        from .operator_prices import OperatorPrices

        prices = OperatorPrices(self.repo, self.settings)
        with self.repo.engine.connect() as connection:
            return {
                "operator": {"account": owner, "admin": True},
                "expected_settings_version": self._admin_limits(connection)["version"],
                "profiles": [
                    prices.read(connection, profile)[0]
                    for profile in self.profiles.values()
                ],
                "audits": prices.audits(connection),
            }

    def update_price(self, principal, profile_id, body):
        self.authorize_admin(principal)
        profile = self.profiles.get(profile_id)
        require(profile is not None, "dstack_profile_not_found", 404)
        from .operator_prices import OperatorPrices
        from .spending_limits import admin_defaults

        return OperatorPrices(
            self.repo, self.settings, admin_defaults=admin_defaults(self)
        ).update(profile, body, principal)

    def preview_price(self, principal, profile_id, body):
        self.authorize_admin(principal)
        profile = self.profiles.get(profile_id)
        require(profile is not None, "dstack_profile_not_found", 404)
        from .operator_prices import OperatorPrices
        from .spending_limits import admin_defaults

        return OperatorPrices(
            self.repo, self.settings, admin_defaults=admin_defaults(self)
        ).preview(profile, body)

    def preview(self, principal, body):
        owner = self.authorize(principal)
        return self._preview_for_owner(owner, body)

    def _vast_host_selection(self, request, offers, demand_job_id):
        """Filter planned Vast offers by host policy/download speed; freeze the selector."""
        from .host_policy import (
            FRESH_SECONDS,
            HostPolicyStore,
            denied_suppliers,
            selection,
        )

        mapper = getattr(self.capacity.client, "vast_offer_mapping", None)
        require(callable(mapper), "host_identity_unconfirmed")
        mapping = mapper(request, clock=self.repo.clock)
        require(
            isinstance(mapping, dict) and isinstance(mapping.get("offers"), list),
            "host_identity_unconfirmed",
        )
        by_offer = {r["offer_id"]: r for r in mapping["offers"]}
        store = HostPolicyStore(self.repo, self.settings.tenant_id)
        with self.repo._budget_read() as connection:
            from .host_policy import original_nodes

            nodes = original_nodes(connection, self.settings.tenant_id)
            excluded = set()
            if demand_job_id is not None:
                from .dstack_demand_start import excluded_download_machines

                excluded = excluded_download_machines(
                    connection, self.settings.tenant_id, demand_job_id
                )
            bad_suppliers = denied_suppliers(
                store, connection, nodes, mapping["offers"]
            )

            def supplier_allowed(offer):
                # A machine an admin chose to prefer stays usable.
                return (
                    offer.get("host_id") not in bad_suppliers
                    or store.read(connection, offer["machine_id"], nodes=nodes)[0][
                        "effective"
                    ]
                    == "prefer"
                )

            # Vast only from these countries; the next lane covers the rest (owner, 2026-11-06).
            allowed = [
                r
                for r in offers
                if r.get("offer_id") in by_offer
                and preferred_download_offer(by_offer[r['offer_id']])
                and supplier_allowed(by_offer[r['offer_id']])
                and by_offer[r['offer_id']]['machine_id'] not in excluded
                and type(by_offer[r['offer_id']].get('advertised_download_mbps'))
                in (int, float)
                and math.isfinite(by_offer[r['offer_id']]['advertised_download_mbps'])
                and by_offer[r['offer_id']]['advertised_download_mbps'] >= 1000
                and store.read(
                    connection, by_offer[r["offer_id"]]["machine_id"], nodes=nodes
                )[0]["effective"]
                != "deny"
            ]
            # instance_types freezes membership; the reviewed native Vast
            # patch also preserves the explicit preferred-first order.
            offers = sorted(
                allowed,
                key=lambda r: (
                    store.read(
                        connection,
                        by_offer[r["offer_id"]]["machine_id"],
                        nodes=nodes,
                    )[0]["effective"]
                    != "prefer"
                ),
            )[:MAX_HOST_CANDIDATES]
            require(offers, "dstack_no_compatible_host_offers")
            approved = [copy.deepcopy(by_offer[r["offer_id"]]) for r in offers]
            # Plan/native assignment uses bundle IDs, not a display-only
            # filtered offers list. The same selector is frozen below.
            selected = selection(
                {
                    "version": 1,
                    "observed_at": mapping["observed_at"],
                    "expires_at": mapping["observed_at"] + FRESH_SECONDS,
                    "policy_snapshot": store.admission_snapshot(connection, approved),
                    "offers": approved,
                },
                now=self.repo.clock(),
            )
        return replace(request, host_selection=selected), offers, store

    def _preview_for_owner(self, owner, body, *, demand_job_id=None):
        self._enabled()
        continuing = self.policy.get("lifecycle_version") == 2
        allowed = (
            {
                "profile_id",
                "ttl_seconds",
                "keep_on",
                "hold_seconds",
                "idle_shutdown_seconds",
                "group_id",
            }
            if continuing
            else {"profile_id", "ttl_seconds", "group_id"}
        )
        allowed.add("max_price_microusd")
        if self.policy.get("test_gpu_guard") is not None:
            allowed.add("test_task_id")
        require(
            isinstance(body, dict)
            and "profile_id" in body
            and not set(body) - allowed
            and (continuing or {"profile_id", "ttl_seconds"} <= set(body)),
            "dstack_preview_invalid",
            422,
        )
        require(safe_id(body["profile_id"]), "dstack_preview_invalid", 422)
        profile = self.profiles.get(body["profile_id"])
        require(
            profile is not None and profile["owner_id"] == owner,
            "dstack_profile_not_found",
            404,
        )
        from .operator_prices import (
            OperatorPrices,
            mode_price_ceiling,
            enforce_price_limit,
        )

        prices = OperatorPrices(self.repo, self.settings)
        with self.repo.engine.connect() as connection:
            price, _, _ = prices.read(connection, profile)
            admin_limits = self._admin_limits(connection)
        chosen_price = body.get("max_price_microusd", price["max_price_microusd"])
        require(
            _integer(chosen_price),
            "dstack_price_ceiling_exceeded",
            422,
        )
        enforce_price_limit(
            chosen_price,
            price["price_ceiling_microusd"],
            "profile",
            "dstack_price_ceiling_exceeded",
        )
        if admin_limits["adopted"] or self.policy.get("demand_start"):
            enforce_price_limit(
                chosen_price,
                mode_price_ceiling(profile, admin_limits),
                "mode",
                "dstack_price_ceiling_exceeded",
            )
        enforce_price_limit(
            chosen_price,
            admin_limits["value"]["max_hourly_price_microusd"],
            "admin",
            "admin_hourly_price_limit_reached",
        )
        profile = copy.deepcopy(profile)
        profile["max_price_microusd"] = chosen_price
        ttl = body.get("ttl_seconds", self.policy.get("reservation_seconds"))
        require(
            _integer(
                ttl,
                120,
                86400
                if continuing
                else min(profile["max_ttl_seconds"], self.policy["max_ttl_seconds"]),
            ),
            "dstack_runtime_limit_invalid",
            422,
        )
        now = self.repo.clock()
        if not continuing:
            require(
                self.policy["expires_at"] is None
                or now + ttl <= self.policy["expires_at"],
                "dstack_authority_window_exceeded",
            )
        lifecycle = (
            self._lifecycle(
                body, now, self._expected_runtime(profile), limits=admin_limits["value"]
            )
            if continuing
            else None
        )
        if continuing:
            lifecycle["budget_account_ids"] = copy.deepcopy(
                profile["budget_account_ids"]
            )
        preview_id = str(uuid.uuid4())
        request = self._capacity_request(
            profile,
            preview_id,
            now,
            None if continuing else now + ttl,
            None if continuing else ttl,
            lifecycle_version=2 if continuing else 1,
            test_max_duration=lifecycle.get("test_gpu_guard", {}).get(
                "max_runtime_seconds"
            )
            if lifecycle
            else None,
            test_task_id=lifecycle.get("test_gpu_guard", {}).get("task_id")
            if lifecycle
            else None,
        )
        # One intent always rents from exactly one backend. Each backend in
        # order (primary, then configured fallbacks) is planned and fully
        # filtered; the first with eligible offers binds the preview and start.
        from .scheduling.health import cooling_backends

        with self.repo.engine.connect() as connection:
            cooling = cooling_backends(connection, self.settings.tenant_id, now)
        # A lane whose recent starts failed cools down alone (#931 step 2).
        backends = [
            backend
            for backend in (request.backend, *profile.get("fallback_backends", []))
            if backend not in cooling
        ]
        require(bool(backends), "dstack_lane_backoff")
        store, offers = None, []
        for index, backend in enumerate(backends):
            candidate = (
                request
                if backend == request.backend
                else replace(request, backend=backend, regions=())
            )
            offers = plan_offers(
                self.capacity.plan(candidate, self.config["ssh_public_key"]),
                candidate,
            )
            if offers and backend == VASTAI.id:
                try:
                    candidate, offers, store = self._vast_host_selection(
                        candidate, offers, demand_job_id
                    )
                except OperatorError as error:
                    if (
                        error.code != "dstack_no_compatible_host_offers"
                        or index == len(backends) - 1
                    ):
                        raise
                    offers = []
            if offers:
                request = candidate
                if backend != profile["backend"]:
                    profile["backend"] = backend
                    profile.pop("regions", None)
                break
        require(bool(offers), "dstack_no_compatible_offers")
        # Reserve preparation and a complete job from the first paid request,
        # plus shutdown grace; continuing funding has no absolute lifetime.
        initial_seconds = lifecycle["reservation_seconds"] if continuing else ttl
        if continuing:
            from .worker_admission import COMPLETION_MARGIN_SECONDS

            initial_seconds = (
                math.ceil(
                    (
                        lifecycle["expected_runtime_s"]
                        + max(
                            lifecycle["idle_shutdown_seconds"],
                            COMPLETION_MARGIN_SECONDS,
                        )
                        + 1
                    )
                    / initial_seconds
                )
                * initial_seconds
            )
        cost = (
            math.ceil(profile["max_price_microusd"] * (initial_seconds + 300) / 3600)
            + profile["extra_reservation_microusd"]
        )
        payload = {
            "capacity_backend": BACKEND_MARKER,
            "tenant_id": self.settings.tenant_id,
            "owner_id": owner,
            "profile_id": profile["id"],
            "profile": profile,
            "config_fingerprint": self.fingerprint,
            "admin_limits_snapshot": request_hash(admin_limits),
            "request": asdict(request),
            "reservation_microusd": cost,
            "price_policy_snapshot": price["snapshot"],
        }
        if continuing:
            payload["lifecycle"] = lifecycle
        expires = min(
            bound
            for bound in (now + 120, self.policy["expires_at"], request.hard_deadline)
            if bound is not None
        )
        if request.host_selection is not None:
            expires = min(expires, request.host_selection["expires_at"])
        with self.repo.transaction() as connection:
            if demand_job_id is not None:
                from .identity import lock_account_gate

                lock_account_gate(connection, self.settings.tenant_id)
            self.repo._lock_capacity(connection)
            if demand_job_id is not None:
                from .dstack_demand import validate_start

                payload["demand_start"] = validate_start(
                    self, connection, demand_job_id, profile["id"]
                )
            require(
                prices.read(connection, self.profiles[profile["id"]])[0]["snapshot"]
                == price["snapshot"],
                "dstack_price_policy_changed",
            )
            if request.host_selection is not None:
                from .host_policy import selection

                require(
                    store.admission_snapshot(
                        connection, request.host_selection["offers"]
                    )
                    == request.host_selection["policy_snapshot"],
                    "host_policy_changed",
                )
                from .host_scorecard import OperatorHosts
                from .stage_metrics import configured_stage_reader

                stage_reader = configured_stage_reader(
                    getattr(self.settings, "stage_query_config_file", None)
                )
                histories = OperatorHosts(
                    self.repo, self.settings, stage_reader
                ).histories(
                    connection,
                    owner,
                    request.host_selection["offers"],
                    profile_id=profile["id"],
                    observed_at=request.host_selection["observed_at"],
                    expires_at=expires,
                )
                for offer in offers:
                    offer["host_history"] = histories[offer["offer_id"]]
                selection(request.host_selection, now=self.repo.clock())
            from .dstack_workspace import owned_group

            group_id = body.get("group_id")
            if group_id is not None:
                owned_group(connection, self.settings, owner, group_id)
            payload["group_id"] = group_id
            connection.execute(
                insert(operator_previews).values(
                    id=preview_id,
                    actor=_actor(self.settings.tenant_id, owner),
                    payload=payload,
                    created_at=now,
                    expires_at=expires,
                )
            )
        return {
            "preview_id": preview_id,
            "expires_at": expires,
            "hard_deadline": request.hard_deadline,
            "reservation_microusd": cost,
            "hourly_cost_microusd": profile["max_price_microusd"],
            "price_basis": "approved_ceiling",
            "max_price_microusd": profile["max_price_microusd"],
            "offer_status": "advisory",
            "offers": offers,
            "can_start": True,
            "capacity_backend": BACKEND_MARKER,
            **(
                {
                    "lifecycle": copy.deepcopy(lifecycle),
                    "initial_reservation_seconds": initial_seconds,
                }
                if continuing
                else {}
            ),
        }

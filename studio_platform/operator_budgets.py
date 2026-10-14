"""Human operator views of the original cumulative budget ledger.

Optional configured account IDs come from the caller's trusted execution
configuration, never the request. Authentication precedes every ledger read
or change. No provider, capacity, reservation or account creation occurs here.
"""
from __future__ import annotations

from .auth import Principal
from .operator_capacity import OperatorError
from .repository import identifier


class OperatorBudgets:
    def __init__(self, repository, settings):
        self.repo = repository
        self.settings = settings

    @staticmethod
    def _human(principal):
        if not isinstance(principal, Principal) or principal.machine is not False:
            raise OperatorError("operator_budgets_human_required", 403)
        identifier(principal.owner)
        identifier(principal.actor_id)

    def _admin(self, principal):
        self._human(principal)
        if principal.owner not in self.settings.operator_admin_owners:
            raise OperatorError("operator_admin_required", 403)

    def owned(self, principal, allowed_budget_ids=None, *, limit=100, after_id=None):
        self._human(principal)
        result = self.repo.list_budget_projections(self.settings.tenant_id,
            owner_id=principal.owner, account_ids=allowed_budget_ids,
            limit=limit, after_id=after_id)
        return {"account": principal.owner, "basis": "cumulative", "currency": "USD",
                "timezone": "UTC", **result}

    def admin(self, principal, *, limit=100, after_id=None):
        self._admin(principal)
        result = self.repo.list_budget_projections(self.settings.tenant_id,
            admin=True, limit=limit, after_id=after_id)
        return {"account": principal.owner, "admin": True, "basis": "cumulative",
                "currency": "USD", "timezone": "UTC", **result}

    def update(self, principal, account_id, body):
        self._admin(principal)
        if not isinstance(body, dict) or set(body) != {"expected_snapshot", "limit_microusd"}:
            raise OperatorError("operator_budget_update_invalid", 422)
        return self.repo.compare_set_budget_limit(account_id,
            tenant_id=self.settings.tenant_id, expected_snapshot=body["expected_snapshot"],
            limit_microusd=body["limit_microusd"], actor_id=principal.actor_id,
            actor_owner=principal.owner)

    def audit(self, principal, *, limit=100, before=None):
        self._admin(principal)
        return self.repo.list_budget_audit(self.settings.tenant_id, limit=limit, before=before)

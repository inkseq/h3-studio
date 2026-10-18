"""Human-reviewed original settlement and additive full-reference waivers.

No provider calls or inferred invoices. Preview/action receipts reuse documents;
the original reservation ledger retains supplier actuals and admission exposure.
"""
import base64
import json
import math
import re
import uuid

from sqlalchemy import and_, insert, literal, or_, select, union_all, update

from .operator_budgets import OperatorBudgets
from .repository import (Scope, NotFound, attempts, billing_adjustments, budget_accounts,
    budget_reservations, documents, identifier, instance_intents, jobs, money,
    outbox, registered_workers, request_hash, spending_origins)
from .settlement_recovery import closed_attempt

PROJECT = "__billing_control"
PREVIEW_SECONDS = 900
TERMINAL = {"succeeded", "failed", "cancelled"}
SHA = re.compile(r"[0-9a-f]{64}")


class BillingRefused(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def need(value, code="billing_request_invalid", status=422):
    if not value:
        raise BillingRefused(code, status)


def text(value, maximum):
    need(isinstance(value, str) and value == value.strip() and 1 <= len(value) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    return value


def _doc(conn, tenant, owner, kind, ident):
    return conn.execute(select(documents).where(documents.c.tenant_id == tenant,
        documents.c.owner_id == owner, documents.c.project_id == PROJECT,
        documents.c.kind == kind, documents.c.document_id == ident)).mappings().first()


def _save(conn, tenant, owner, kind, ident, value, now):
    conn.execute(insert(documents).values(tenant_id=tenant, owner_id=owner, project_id=PROJECT,
        kind=kind, document_id=ident, version=1, payload=value, updated_at=now))


def _waiver(conn, tenant, owner, kind, ident):
    return conn.execute(select(billing_adjustments).where(billing_adjustments.c.tenant_id == tenant,
        billing_adjustments.c.owner_id == owner, billing_adjustments.c.reference_type == kind,
        billing_adjustments.c.reference_id == ident, billing_adjustments.c.kind == "full_waiver")).mappings().first()


def _reference(repo, conn, tenant, owner, kind, ident, *, lock=False):
    need(kind in {"job", "instance"})
    identifier(ident)
    table = jobs if kind == "job" else instance_intents
    query = select(table).where(table.c.id == ident)
    row = conn.execute(query.with_for_update() if lock else query).mappings().first()
    if row is None:
        raise NotFound("billing_reference_not_found")
    reservations_query = select(budget_reservations).where(budget_reservations.c.reference_type == kind,
        budget_reservations.c.reference_id == ident).order_by(budget_reservations.c.account_id)
    reservations = [dict(r) for r in conn.execute(
        reservations_query.with_for_update() if lock else reservations_query).mappings()]
    if not reservations:
        raise NotFound("billing_reference_not_found")
    accounts = [dict(r) for r in conn.execute(select(budget_accounts).where(
        budget_accounts.c.id.in_([r["account_id"] for r in reservations])).order_by(budget_accounts.c.id)).mappings()]
    origin = conn.execute(select(spending_origins).where(spending_origins.c.reference_type == kind,
        spending_origins.c.reference_id == ident)).mappings().first()
    owners = {r["owner_id"] for r in accounts if r["owner_id"] is not None}
    if kind == "job":
        need(row["tenant_id"] == tenant, "billing_ownership_unconfirmed", 409)
        owners.add(row["owner_id"])
    if origin:
        need(origin["tenant_id"] == tenant, "billing_ownership_unconfirmed", 409)
        owners.add(origin["owner_id"])
    if owners != {owner} or any(r["tenant_id"] != tenant for r in accounts):
        raise NotFound("billing_reference_not_found")
    history, workers = [], []
    if kind == "job":
        history_query = select(attempts).where(attempts.c.job_id == ident).order_by(attempts.c.number)
        history = [dict(r) for r in conn.execute(history_query.with_for_update() if lock else history_query).mappings()]
        workers = [dict(r) for r in conn.execute(select(registered_workers).where(
            registered_workers.c.id.in_([a["worker_id"] for a in history])).order_by(registered_workers.c.id)).mappings()]
    else:
        workers = [dict(r) for r in conn.execute(select(registered_workers).where(
            registered_workers.c.provider == row["provider"],
            registered_workers.c.instance_id == row["provider_instance_id"]).order_by(registered_workers.c.id)).mappings()]
        history = [dict(r) for r in conn.execute(select(attempts).where(
            attempts.c.worker_id.in_([w["id"] for w in workers])).order_by(attempts.c.id)).mappings()]
    waiver = _waiver(conn, tenant, owner, kind, ident)
    value = {"reference_type": kind, "reference": dict(row), "reservations": reservations,
        "accounts": accounts, "origin": dict(origin) if origin else None, "attempts": history,
        "workers": workers, "waiver": dict(waiver) if waiver else None}
    value["snapshot"] = request_hash(value)
    return value


def _view(value, owner):
    kind, row, reservations = value["reference_type"], value["reference"], value["reservations"]
    states = {r["state"] for r in reservations}
    actuals = {r["actual_cost_microusd"] for r in reservations}
    consistent = len(states) == 1 and len(actuals) == 1
    state = next(iter(states)) if len(states) == 1 else "mixed"
    actual = next(iter(actuals)) if consistent and state in {"settled", "released"} else None
    estimate = max(money(r["amount_microusd"]) for r in reservations)
    gross = money(actual) if actual is not None else estimate
    reason = None
    result = (row.get("result") or {}) if kind == "job" else {}
    if (not consistent or state not in {"reserved", "invoice_pending", "settled", "released"}
            or state in {"reserved", "invoice_pending"} and actuals != {None}
            or state in {"settled", "released"} and None in actuals
            or result.get("billing_status") == "settled" and (
                actual is None or result.get("actual_cost_microusd") != actual)):
        reason = "billing_ledger_unconfirmed"
    elif kind == "job" and (row["status"] not in TERMINAL or row["lease_worker_id"] or row["lease_expires_at"]):
        reason = "billing_reference_active"
    elif kind == "instance" and row["state"] != "destroyed":
        reason = "billing_reference_active"
    elif any(not closed_attempt(a) for a in value["attempts"]) or any(
            w["current_job_id"] and (kind == "instance" or w["current_job_id"] == row["id"]) for w in value["workers"]):
        reason = "billing_upstream_unconfirmed"
    actions = [] if reason else (["settle"] if actual is None else []) + ([] if value["waiver"] else ["waive"])
    allocation = result.get("rental_allocation")
    return {"owner_id": owner, "reference_type": kind, "reference_id": row["id"],
        "snapshot": value["snapshot"], "state": state, "created_at": row["created_at"],
        "provider": row.get("provider"), "provider_instance_id": row.get("provider_instance_id"),
        "reserved_estimate_microusd": estimate,
        "provider_actual_cost_microusd": actual if kind == "instance" else None,
        "allocated_cost_microusd": actual if allocation else None,
        "user_charge_microusd": 0 if value["waiver"] else gross,
        "user_charge_basis": "waived" if value["waiver"] else "recorded" if actual is not None else "estimate",
        "waived": bool(value["waiver"]),
        "settlement_basis": "rental_allocation" if allocation else "recorded_provider_actual" if kind == "instance" and actual is not None else None,
        "eligible_actions": actions, "block_reason": reason,
        "waiver": {k: value["waiver"][k] for k in ("reason", "created_at", "action_id")} if value["waiver"] else None}


def _request(body):
    need(isinstance(body, dict) and set(body) == {"owner_id", "action", "reason", "evidence_sha256", "evidence_note", "items"})
    identifier(body["owner_id"])
    need(body["action"] in {"settle", "waive"})
    text(body["reason"], 1000); text(body["evidence_note"], 2000)
    need(isinstance(body["evidence_sha256"], str) and SHA.fullmatch(body["evidence_sha256"]))
    need(isinstance(body["items"], list) and 1 <= len(body["items"]) <= 200)
    seen = set()
    for item in body["items"]:
        need(isinstance(item, dict) and item.get("reference_type") in {"job", "instance"})
        fields = {"reference_type", "reference_id", "expected_snapshot"}
        if body["action"] == "settle":
            fields.add("actual_cost_microusd")
            if item["reference_type"] == "job":
                fields.add("allocation_instance_id")
                identifier(item.get("allocation_instance_id"))
            money(item.get("actual_cost_microusd"))
        need(set(item) == fields)
        identifier(item["reference_id"])
        need(isinstance(item["expected_snapshot"], str) and SHA.fullmatch(item["expected_snapshot"]))
        key = (item["reference_type"], item["reference_id"])
        need(key not in seen, "billing_duplicate_reference")
        seen.add(key)
    return json.loads(json.dumps(body))


def _allocations(repo, conn, tenant, owner, body, values):
    """Prove a complete, conserved allocation of actual rental cost, not a job invoice."""
    selected = {item["reference_id"]: item for item in body["items"] if item["reference_type"] == "job"}
    instance_items = {item["reference_id"]: item for item in body["items"] if item["reference_type"] == "instance"}
    for instance_id in sorted({item["allocation_instance_id"] for item in selected.values()}):
        instance = values.get(("instance", instance_id)) or _reference(repo, conn, tenant, owner, "instance", instance_id)
        row = instance["reference"]
        need(row["state"] == "destroyed" and row["provider"] in {"vast", "runpod"}
            and row["provider_instance_id"], "billing_allocation_unconfirmed", 409)
        source = _view(instance, owner)
        cost = instance_items[instance_id]["actual_cost_microusd"] if instance_id in instance_items else source["provider_actual_cost_microusd"]
        need(cost is not None, "billing_allocation_unconfirmed", 409)
        worker_ids = [w["id"] for w in instance["workers"]]
        submitted = list(conn.execute(select(attempts, jobs.c.owner_id.label("job_owner"),
            jobs.c.tenant_id.label("job_tenant"), jobs.c.result.label("job_result"), jobs.c.status.label("job_status"))
            .join(jobs, jobs.c.id == attempts.c.job_id).where(attempts.c.worker_id.in_(worker_ids),
                or_(attempts.c.submission_started_at.is_not(None), attempts.c.upstream_task_id.is_not(None)))).mappings())
        need(submitted and all(a["job_owner"] == owner and a["job_tenant"] == tenant
            and a["job_status"] in TERMINAL and closed_attempt(a) for a in submitted), "billing_allocation_unconfirmed", 409)
        linked = {a["job_id"]: a for a in submitted}
        allocated = 0
        for job_id, attempt in linked.items():
            if job_id in selected:
                item = selected[job_id]
                need(item["allocation_instance_id"] == instance_id, "billing_allocation_unconfirmed", 409)
                job = values[("job", job_id)]
                workers = {w["id"]: w for w in job["workers"]}
                actual_attempts = [a for a in job["attempts"] if a["submission_started_at"] is not None or a["upstream_task_id"] is not None]
                identities = {(workers[a["worker_id"]]["provider"], workers[a["worker_id"]]["instance_id"])
                    for a in actual_attempts if a["worker_id"] in workers}
                need(len(actual_attempts) > 0 and all(a["worker_id"] in workers for a in actual_attempts)
                    and identities == {(row["provider"], row["provider_instance_id"])}, "billing_allocation_unconfirmed", 409)
                allocated += item["actual_cost_microusd"]
            else:
                prior = (attempt["job_result"] or {}).get("rental_allocation") or {}
                need(prior.get("instance_intent_id") == instance_id
                    and type(prior.get("allocated_cost_microusd")) is int, "billing_allocation_incomplete", 409)
                prior_value = _reference(repo, conn, tenant, owner, "job", job_id)
                prior_cost = money(prior["allocated_cost_microusd"])
                need(all(r["state"] in {"settled", "released"} and r["actual_cost_microusd"] == prior_cost
                    for r in prior_value["reservations"]), "billing_allocation_unconfirmed", 409)
                allocated += prior_cost
        need(all(item["reference_id"] in linked for item in selected.values()
            if item["allocation_instance_id"] == instance_id), "billing_allocation_unconfirmed", 409)
        need(allocated == cost, "billing_allocation_total_mismatch", 409)


class OperatorBilling(OperatorBudgets):
    def references(self, principal, owner, *, limit=50, after=None, created_after=None, created_before=None):
        self._admin(principal); identifier(owner)
        need(type(limit) is int and 1 <= limit <= 100)
        for value in (created_after, created_before):
            need(value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0)
        need(created_after is None or created_before is None or created_after < created_before)
        origins = select(budget_reservations.c.reference_id).join(budget_accounts,
            budget_accounts.c.id == budget_reservations.c.account_id).outerjoin(spending_origins,
            and_(spending_origins.c.reference_type == budget_reservations.c.reference_type,
                spending_origins.c.reference_id == budget_reservations.c.reference_id)).where(
            budget_accounts.c.tenant_id == self.settings.tenant_id,
            budget_reservations.c.reference_type == "instance", or_(budget_accounts.c.owner_id == owner,
                and_(spending_origins.c.tenant_id == self.settings.tenant_id, spending_origins.c.owner_id == owner)))
        table = union_all(select(literal("job").label("kind"), jobs.c.id, jobs.c.created_at).where(
            jobs.c.tenant_id == self.settings.tenant_id, jobs.c.owner_id == owner,
            jobs.c.id.in_(select(budget_reservations.c.reference_id).where(budget_reservations.c.reference_type == "job"))),
            select(literal("instance").label("kind"), instance_intents.c.id, instance_intents.c.created_at)
            .where(instance_intents.c.id.in_(origins))).subquery()
        query = select(table)
        if created_after is not None: query = query.where(table.c.created_at >= created_after)
        if created_before is not None: query = query.where(table.c.created_at < created_before)
        if after is not None:
            try:
                need(isinstance(after, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,512}", after))
                at, kind, ident = json.loads(base64.urlsafe_b64decode(after+"="*(-len(after)%4)))
                need(type(at) in (int, float) and math.isfinite(at) and kind in {"job", "instance"})
                identifier(ident)
            except (ValueError, TypeError, json.JSONDecodeError):
                raise BillingRefused("billing_cursor_invalid", 422) from None
            query = query.where(or_(table.c.created_at < at, and_(table.c.created_at == at,
                or_(table.c.kind > kind, and_(table.c.kind == kind, table.c.id > ident)))))
        with self.repo.engine.connect() as conn:
            rows = list(conn.execute(query.order_by(table.c.created_at.desc(), table.c.kind, table.c.id).limit(limit+1)).mappings())
            items = [_view(_reference(self.repo, conn, self.settings.tenant_id, owner, r["kind"], r["id"]), owner) for r in rows[:limit]]
        cursor = base64.urlsafe_b64encode(json.dumps([rows[limit-1]["created_at"], rows[limit-1]["kind"], rows[limit-1]["id"]]).encode()).decode().rstrip("=") if len(rows) > limit else None
        return {"owner_id": owner, "items": items, "next_cursor": cursor}

    def _checked(self, conn, body, *, lock=False):
        values = {}
        for item in sorted(body["items"], key=lambda item: (item["reference_type"], item["reference_id"])):
            value = _reference(self.repo, conn, self.settings.tenant_id, body["owner_id"], item["reference_type"], item["reference_id"], lock=lock)
            need(value["snapshot"] == item["expected_snapshot"], "billing_snapshot_changed", 409)
            view = _view(value, body["owner_id"])
            need(body["action"] in view["eligible_actions"], view["block_reason"] or "billing_already_"+("waived" if body["action"] == "waive" else "settled"), 409)
            values[(item["reference_type"], item["reference_id"])] = value
        if body["action"] == "settle": _allocations(self.repo, conn, self.settings.tenant_id, body["owner_id"], body, values)
        return values

    def _summary(self, body, values):
        views = [_view(values[(i["reference_type"], i["reference_id"])], body["owner_id"]) for i in body["items"]]
        totals = {"reference_count": len(views), "instance_actual_microusd": 0, "job_allocated_microusd": 0,
            "waived_reference_count": len(views) if body["action"] == "waive" else 0,
            "waived_exposure_microusd": sum(v["user_charge_microusd"] for v in views) if body["action"] == "waive" else 0}
        if body["action"] == "settle":
            for item in body["items"]:
                totals["job_allocated_microusd" if item["reference_type"] == "job" else "instance_actual_microusd"] += item["actual_cost_microusd"]
        return views, totals

    def preview(self, principal, body):
        self._admin(principal); body = _request(body)
        with self.repo.transaction() as conn:
            values = self._checked(conn, body)
            items, totals = self._summary(body, values)
            now = self.repo.clock(); need(type(now) in (int, float) and math.isfinite(now), "billing_clock_unconfirmed", 409)
            ident = str(uuid.uuid4())
            value = {"preview_id": ident, "request": body, "request_hash": request_hash(body),
                "actor_id": principal.actor_id, "owner_id": body["owner_id"], "action": body["action"],
                "items": items, "totals": totals, "expires_at": now+PREVIEW_SECONDS}
            value["snapshot"] = request_hash(value)
            _save(conn, self.settings.tenant_id, principal.owner, "billing_preview", ident, value, now)
            return value

    def _operation_key(self, principal, key):
        text(key, 200)
        return request_hash({"actor_id": principal.actor_id, "key": key})

    def by_key(self, principal, key):
        self._admin(principal)
        with self.repo.engine.connect() as conn:
            row = _doc(conn, self.settings.tenant_id, principal.owner, "billing_action_key", self._operation_key(principal, key))
            if row is None: raise BillingRefused("billing_action_not_found", 404)
            return row["payload"]["result"]

    def action(self, principal, ident):
        self._admin(principal); identifier(ident)
        with self.repo.engine.connect() as conn:
            row = _doc(conn, self.settings.tenant_id, principal.owner, "billing_action", ident)
            if row is None or row["payload"]["actor_id"] != principal.actor_id: raise NotFound("billing_action_not_found")
            return row["payload"]

    def apply(self, principal, body, key):
        self._admin(principal)
        need(isinstance(body, dict) and set(body) == {"preview_id", "expected_snapshot", "confirmed"}
            and body["confirmed"] is True)
        identifier(body["preview_id"])
        need(isinstance(body["expected_snapshot"], str) and SHA.fullmatch(body["expected_snapshot"]))
        receipt_key = self._operation_key(principal, key); fingerprint = request_hash(body)
        with self.repo.transaction() as conn:
            self.repo._lock_capacity(conn)
            prior = _doc(conn, self.settings.tenant_id, principal.owner, "billing_action_key", receipt_key)
            if prior:
                need(prior["payload"]["fingerprint"] == fingerprint, "billing_idempotency_conflict", 409)
                return prior["payload"]["result"]
            preview = _doc(conn, self.settings.tenant_id, principal.owner, "billing_preview", body["preview_id"])
            if preview is None: raise NotFound("billing_preview_not_found")
            preview = preview["payload"]
            need(preview["actor_id"] == principal.actor_id, "billing_preview_actor_changed", 409)
            need(preview["snapshot"] == body["expected_snapshot"], "billing_snapshot_changed", 409)
            clock = self.repo.clock()
            need(type(clock) in (int, float) and math.isfinite(clock), "billing_clock_unconfirmed", 409)
            need(clock < preview["expires_at"], "billing_preview_expired", 409)
            request = _request(preview["request"])
            values = self._checked(conn, request, lock=True)
            account_ids = sorted({a["id"] for v in values.values() for a in v["accounts"]})
            for ident in account_ids:
                self.repo._locked(conn, select(budget_accounts).where(budget_accounts.c.id == ident))
            # Include counter changes after waiting for original account locks.
            values = self._checked(conn, request, lock=True)
            items, totals = self._summary(request, values)
            action_id, audit_id, now = str(uuid.uuid4()), str(uuid.uuid4()), self.repo.clock()
            for item in request["items"]:
                kind, ident = item["reference_type"], item["reference_id"]
                value = values[(kind, ident)]
                if request["action"] == "waive":
                    conn.execute(insert(billing_adjustments).values(id=str(uuid.uuid4()), tenant_id=self.settings.tenant_id,
                        owner_id=request["owner_id"], reference_type=kind, reference_id=ident, kind="full_waiver",
                        action_id=action_id, actor_id=principal.actor_id, reason=request["reason"],
                        evidence_sha256=request["evidence_sha256"], evidence_note=request["evidence_note"],
                        basis_exposure_microusd=_view(value, request["owner_id"])["user_charge_microusd"], created_at=now))
                elif kind == "instance":
                    self.repo.settle_instance_cost(ident, actual_cost_microusd=item["actual_cost_microusd"], connection=conn)
                else:
                    row = value["reference"]
                    settled = self.repo.settle_completed_job(Scope(self.settings.tenant_id, request["owner_id"], row["project_id"]), ident,
                        actual_cost_microusd=item["actual_cost_microusd"], connection=conn)
                    result = dict(settled["result"])
                    result["rental_allocation"] = {"instance_intent_id": item["allocation_instance_id"],
                        "allocated_cost_microusd": item["actual_cost_microusd"], "evidence_sha256": request["evidence_sha256"],
                        "basis": "human_reviewed_full_rental_allocation", "provider_per_job_invoice": False}
                    conn.execute(update(jobs).where(jobs.c.id == ident).values(result=result))
            after_items = [_view(_reference(self.repo, conn, self.settings.tenant_id, request["owner_id"],
                item["reference_type"], item["reference_id"]), request["owner_id"]) for item in request["items"]]
            result = {"action_id": action_id, "state": "completed", "actor_id": principal.actor_id,
                "owner_id": request["owner_id"], "action": request["action"], "preview_id": body["preview_id"],
                "request_hash": preview["request_hash"], "items": after_items, "totals": totals, "audit_event_id": audit_id,
                "reason": request["reason"], "evidence_sha256": request["evidence_sha256"], "evidence_note": request["evidence_note"],
                "completed_at": now}
            conn.execute(insert(outbox).values(id=audit_id, event_type="operator.billing_applied", aggregate_id=action_id,
                payload={"tenant_id": self.settings.tenant_id, "actor_owner": principal.owner, "request": request,
                    "preview_snapshot": preview["snapshot"], "before": values_to_audit(values), "result": result}, created_at=now))
            _save(conn, self.settings.tenant_id, principal.owner, "billing_action", action_id, result, now)
            _save(conn, self.settings.tenant_id, principal.owner, "billing_action_key", receipt_key,
                {"fingerprint": fingerprint, "result": result}, now)
            return result


def values_to_audit(values):
    return [{"reference_type": kind, "reference_id": ident, "snapshot": value["snapshot"],
        "reservations": value["reservations"], "waiver": value["waiver"]}
        for (kind, ident), value in sorted(values.items())]

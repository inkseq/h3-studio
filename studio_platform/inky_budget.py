"""Inky Terra reservations in existing documents. Never invokes a provider."""

from datetime import datetime, timezone

from sqlalchemy import and_, case, func, insert, select, text, update

from .repository import Conflict, Scope, documents, request_hash

ACCOUNT_DAILY = 30
GLOBAL_MONTHLY_MICROS = 20_000_000
SCOPE = Scope("__inky_global", "__system", "__terra_usage")
KIND = "inky_terra_call"
LOCK = 57167243348761258


class InkyBudget:
    """Reserve a verified worst-case price before a one-shot provider invocation.

    Unknown outcomes retain their reservation. Only a newly inserted receipt
    permits calling; replay never does. Periods use UTC at admission time.
    A caller must bound tokens/provider price to the reservation before enabling
    real calls. This ledger does not manufacture a price ceiling from an average.
    """

    def __init__(self, repo):
        self.repo = repo

    def where(self, ident=None):
        return (
            self.repo._scope(documents, SCOPE),
            documents.c.kind == KIND,
            *(() if ident is None else (documents.c.document_id == ident,)),
        )

    @staticmethod
    def lock(conn):
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
        # Repository.transaction uses BEGIN IMMEDIATE for SQLite.

    def reserve(self, *, tenant, owner, project, source_id, max_cost_micros):
        for value in (tenant, owner, project, source_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 200:
                raise ValueError("inky_budget_binding_invalid")
        if (
            type(max_cost_micros) is not int
            or not 0 < max_cost_micros <= GLOBAL_MONTHLY_MICROS
        ):
            raise ValueError("inky_budget_ceiling_invalid")
        binding = {
            "tenant": tenant,
            "owner": owner,
            "project": project,
            "source_id": source_id,
        }
        ident = request_hash(binding)
        now = self.repo.clock()
        date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        month = date[:7]
        with self.repo.transaction() as conn:
            self.lock(conn)
            original = (
                conn.execute(select(documents).where(*self.where(ident)))
                .mappings()
                .first()
            )
            if original:
                return {
                    "call_id": ident,
                    "allowed": False,
                    "reason": "inky_example_replay",
                }
            # Aggregate in SQL; never deserialize the month's entire call history.
            data = documents.c.payload
            daily, used, overrun = conn.execute(
                select(
                    func.count().filter(
                        and_(
                            data["day"].as_string() == date,
                            data["tenant"].as_string() == tenant,
                            data["owner"].as_string() == owner,
                        )
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    data["status"].as_string() == "settled",
                                    data["actual_micros"].as_integer(),
                                ),
                                else_=data["reserved_micros"].as_integer(),
                            )
                        ),
                        0,
                    ),
                    func.count().filter(data["overrun"].as_boolean().is_(True)),
                ).where(*self.where(), data["month"].as_string() == month)
            ).one()
            if (
                daily >= ACCOUNT_DAILY
                or used + max_cost_micros > GLOBAL_MONTHLY_MICROS
                or overrun
            ):
                return {
                    "call_id": ident,
                    "allowed": False,
                    "reason": "inky_example_budget",
                }
            value = {
                **binding,
                "day": date,
                "month": month,
                "status": "reserved",
                "reserved_micros": max_cost_micros,
                "actual_micros": None,
                "overrun": False,
            }
            conn.execute(
                insert(documents).values(
                    tenant_id=SCOPE.tenant_id,
                    owner_id=SCOPE.owner_id,
                    project_id=SCOPE.project_id,
                    kind=KIND,
                    document_id=ident,
                    version=1,
                    payload=value,
                    updated_at=now,
                )
            )
            return {
                "call_id": ident,
                "allowed": True,
                "reserved_micros": max_cost_micros,
            }

    def settle(self, call_id, actual_micros):
        """Known usage only. An overrun is recorded honestly and freezes the month."""
        if type(actual_micros) is not int or actual_micros < 0:
            raise ValueError("inky_usage_invalid")
        with self.repo.transaction() as conn:
            self.lock(conn)
            row = (
                conn.execute(select(documents).where(*self.where(call_id)))
                .mappings()
                .first()
            )
            if not row:
                raise Conflict("inky_reservation_missing")
            value = dict(row["payload"])
            if value["status"] == "settled":
                if value["actual_micros"] != actual_micros:
                    raise Conflict("inky_usage_conflict")
                return {
                    "call_id": call_id,
                    "actual_micros": actual_micros,
                    "overrun": value["overrun"],
                }
            value.update(
                status="settled",
                actual_micros=actual_micros,
                overrun=actual_micros > value["reserved_micros"],
            )
            conn.execute(
                update(documents)
                .where(*self.where(call_id))
                .values(
                    payload=value,
                    version=row["version"] + 1,
                    updated_at=self.repo.clock(),
                )
            )
            return {
                "call_id": call_id,
                "actual_micros": actual_micros,
                "overrun": value["overrun"],
            }

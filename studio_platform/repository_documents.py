"""Original documents ledger methods on the composed Repository transaction owner."""

from __future__ import annotations
import math
import uuid
from sqlalchemy import (
    and_,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .repository_schema import (
    NotFound,
    Conflict,
    canonical,
    request_hash,
    money,
    identifier,
    documents,
    plans,
    _allowed_ids,
    _pagination,
)


class DocumentLedger:
    def put_document(self, scope, kind, document_id, payload, *, expected_version=None):
        kind, document_id, payload = (
            identifier(kind),
            identifier(document_id),
            canonical(payload),
        )
        where = and_(
            self._scope(documents, scope),
            documents.c.kind == kind,
            documents.c.document_id == document_id,
        )
        try:
            with self.transaction() as connection:
                self._content_write_guard(
                    connection,
                    scope,
                    payload,
                    project_id=document_id
                    if scope.project_id == "__projects" and kind == "project"
                    else scope.project_id,
                )
                row = self._locked(connection, select(documents).where(where))
                if row is None:
                    if expected_version not in (None, 0):
                        raise Conflict("document_version_conflict")
                    connection.execute(
                        insert(documents).values(
                            tenant_id=scope.tenant_id,
                            owner_id=scope.owner_id,
                            project_id=scope.project_id,
                            kind=kind,
                            document_id=document_id,
                            payload=payload,
                            version=1,
                            updated_at=self.clock(),
                        )
                    )
                else:
                    # Blind overwrites of existing documents are intentionally disallowed.
                    if expected_version != row["version"]:
                        raise Conflict("document_version_conflict")
                    connection.execute(
                        update(documents)
                        .where(where)
                        .values(
                            payload=payload,
                            version=row["version"] + 1,
                            updated_at=self.clock(),
                        )
                    )
                return dict(
                    connection.execute(select(documents).where(where)).mappings().one()
                )
        except IntegrityError:
            raise Conflict("document_version_conflict") from None

    def get_document(self, scope, kind, document_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(documents).where(
                        self._scope(documents, scope),
                        documents.c.kind == kind,
                        documents.c.document_id == document_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("document_not_found")
            return dict(row)

    def list_documents(
        self,
        scope,
        kind,
        *,
        limit=100,
        offset=0,
        allowed_ids=None,
        summary=False,
        exclude_managed=False,
        story_chat=False,
    ):
        """Project catalogs can read metadata without materializing every draft.

        Summary rows preserve the document envelope and expose only payload.title.
        Full documents remain available through get_document/default list usage.
        """
        _pagination(limit, offset)
        if type(summary) is not bool or (summary and kind != "project"):
            raise ValueError("invalid_document_summary")
        allowed = _allowed_ids(allowed_ids)
        if allowed == ():
            return []
        columns = [column for column in documents.c if column.name != "payload"]
        statement = (
            select(*columns, documents.c.payload["title"].as_string().label("_title"))
            if summary
            else select(documents)
        )
        statement = statement.where(
            self._scope(documents, scope), documents.c.kind == kind
        )
        if exclude_managed:
            # Purges retain document identities for audit/history even when an
            # older trash marker is absent. Filter the tombstone before paging.
            statement = statement.where(
                documents.c.payload["content_deleted"].as_boolean().is_not(True)
            )
            trash = documents.alias("project_trash")
            statement = statement.where(
                ~documents.c.document_id.in_(
                    select(trash.c.payload["target"]["id"].as_string()).where(
                        trash.c.tenant_id == scope.tenant_id,
                        trash.c.owner_id == scope.owner_id,
                        trash.c.project_id == "__trash",
                        trash.c.kind == "trash_item",
                        trash.c.payload["target"]["kind"].as_string() == "project",
                        trash.c.payload["state"]
                        .as_string()
                        .in_(("trashed", "purging", "purged")),
                    )
                )
            )
            marker = documents.c.payload["integration_kind"].as_string()
            statement = statement.where(or_(marker.is_(None), marker != "quick_chat"))
        if allowed is not None:
            statement = statement.where(documents.c.document_id.in_(allowed))
        order = (documents.c.document_id,)
        if story_chat:
            # Story chats (#422): projects carrying journey.storyChat, newest first.
            if kind != "project":
                raise ValueError("invalid_document_summary")
            marker = documents.c.payload[("journey", "storyChat", "version")]
            # Typed, cast-free predicates: a malformed legacy marker (string, object,
            # bool) is simply not a story chat and must never raise in SQL.
            if self.engine.dialect.name == "postgresql":
                typed = and_(
                    func.json_typeof(marker) == "number", marker.as_string() == "1"
                )
            else:
                typed = and_(
                    func.json_type(documents.c.payload, "$.journey.storyChat.version")
                    == "integer",
                    marker.as_integer() == 1,
                )
            # A chat removed from Quick Create (hidden: true) keeps its story in Works.
            hidden = documents.c.payload[("journey", "storyChat", "hidden")]
            if self.engine.dialect.name == "postgresql":
                kind_of = func.json_typeof(hidden)
                shown = or_(
                    kind_of.is_(None),
                    kind_of != "boolean",
                    hidden.as_string() != "true",
                )
            else:
                kind_of = func.json_type(
                    documents.c.payload, "$.journey.storyChat.hidden"
                )
                shown = or_(kind_of.is_(None), kind_of != "true")
            statement = statement.where(typed, shown)
            order = (documents.c.updated_at.desc(), documents.c.document_id)
        with self.engine.connect() as connection:
            values = [
                dict(r)
                for r in connection.execute(
                    statement.order_by(*order).limit(limit).offset(offset)
                ).mappings()
            ]
        if summary:
            for value in values:
                value["payload"] = {"title": value.pop("_title")}
        return values

    def create_plan(
        self,
        scope,
        request,
        execution_plan,
        *,
        expires_at,
        estimated_cost_microusd=0,
        plan_id=None,
    ):
        if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise ValueError("invalid_plan_expiry")
        if expires_at <= self.clock():
            raise Conflict("plan_expired")
        request, execution_plan = canonical(request), canonical(execution_plan)
        if execution_plan.get("dispatch_backend", "legacy") not in {
            "legacy",
            "hatchet-v1",
        }:
            raise ValueError("invalid_dispatch_backend")
        row = dict(
            id=plan_id or str(uuid.uuid4()),
            tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
            project_id=scope.project_id,
            request=request,
            request_hash=request_hash(request),
            execution_plan=execution_plan,
            plan_hash=request_hash(execution_plan),
            estimated_cost_microusd=money(estimated_cost_microusd),
            created_at=self.clock(),
            expires_at=float(expires_at),
        )
        with self.transaction() as connection:
            self._content_write_guard(connection, scope, request)
            if plan_id is not None:
                previous = (
                    connection.execute(select(plans).where(plans.c.id == plan_id))
                    .mappings()
                    .first()
                )
                if previous:
                    if any(
                        previous[k] != row[k]
                        for k in ("tenant_id", "owner_id", "project_id", "request_hash")
                    ):
                        raise Conflict("plan_idempotency_conflict")
                    return dict(previous)
            connection.execute(insert(plans).values(**row))
        return row

    def get_plan(self, scope, plan_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(plans).where(
                        plans.c.id == plan_id, self._scope(plans, scope)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("plan_not_found")
            return dict(row)

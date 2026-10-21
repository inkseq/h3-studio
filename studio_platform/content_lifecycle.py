"""Original-owner reference fencing and irreversible content tombstones.

These helpers use the caller's transaction. They never open storage, acquire an
asset OS lock, initialize a schema, or settle a financial reservation.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from sqlalchemy import inspect, insert, select

from .repository import Conflict, documents


def owner_reference_gate(connection, tenant, owner):
    if connection.dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(
                ("creation-content-v1\0" + tenant + "\0" + owner).encode()
            ).digest()[:8],
            "big",
        ) & ((1 << 63) - 1)
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (key,))
    elif connection.dialect.name == "sqlite":
        # Storage journals use engine.begin(), whereas Repository already uses
        # BEGIN IMMEDIATE. Do not start a second physical transaction.
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    from .account_deletion import assert_writable

    # The marker is checked INSIDE the existing owner fence: an earlier
    # authenticated request cannot resume a content write after confirmation.
    assert_writable(connection, tenant, owner)


def _objects(connection):
    if not inspect(connection).has_table("platform_quick_chat_objects"):
        return None
    from .quick_chat import objects

    return objects


def token(kind, value, binding=None):
    return (
        kind
        + ":"
        + hashlib.sha256((str(binding or "") + "\0" + value).encode()).hexdigest()
    )


def history_cleanup_activation_id(tenant):
    return "history-cleanup-" + hashlib.sha256(tenant.encode()).hexdigest()[:40]


def history_cleanup_activated(connection, tenant):
    table = _objects(connection)
    if table is None:
        return False
    row = (
        connection.execute(
            select(table).where(
                table.c.id == history_cleanup_activation_id(tenant),
                table.c.tenant == tenant,
                table.c.owner == "__system__",
                table.c.session_id == "__history_system",
                table.c.kind == "history_cleanup_activation",
                table.c.business_key == "v1",
            )
        )
        .mappings()
        .first()
    )
    payload = row["payload"] if row else {}
    return bool(
        row
        and row["version"] == 1
        and payload.get("version") == 1
        and payload.get("enabled") is True
        and isinstance(payload.get("activated_release"), str)
        and re.fullmatch(r"[0-9a-f]{40}", payload["activated_release"])
    )


def assert_history_cleanup_activated(connection, tenant):
    if not history_cleanup_activated(connection, tenant):
        raise Conflict("history_cleanup_not_enabled")


def _deleted(connection, tenant, owner, tokens):
    table = _objects(connection)
    if table is None or not tokens:
        return False
    return (
        connection.execute(
            select(table.c.id)
            .where(
                table.c.tenant == tenant,
                table.c.owner == owner,
                table.c.kind == "content_tombstone",
                table.c.business_key.in_(tokens),
            )
            .limit(1)
        ).first()
        is not None
    )


def content_is_deleted(
    connection,
    tenant,
    owner,
    *,
    asset_id=None,
    artifact_id=None,
    object_key=None,
    storage_binding=None,
):
    tokens = [
        token(kind, value)
        for kind, value in (("asset", asset_id), ("artifact", artifact_id))
        if value
    ]
    if object_key:
        if storage_binding is not None:
            tokens.append(token("key", object_key, storage_binding))
        else:
            table = _objects(connection)
            if (
                table is not None
                and connection.execute(
                    select(table.c.id)
                    .where(
                        table.c.tenant == tenant,
                        table.c.owner == owner,
                        table.c.kind == "content_tombstone",
                        table.c.payload["object_key"].as_string() == object_key,
                    )
                    .limit(1)
                ).first()
            ):
                return True
    return _deleted(connection, tenant, owner, tokens)


def assert_content_available(
    connection,
    tenant,
    owner,
    *,
    asset_ids=(),
    artifact_ids=(),
    object_keys=(),
    storage_binding=None,
):
    from .universal_trash import where

    selected = (
        connection.execute(
            select(documents.c.payload).where(
                *where(tenant, owner, "trash_item"),
                documents.c.payload["state"]
                .as_string()
                .in_(("trashed", "purging", "purged")),
            )
        ).scalars()
        if inspect(connection).has_table(documents.name)
        else ()
    )
    for payload in selected:
        target = payload["target"]
        if (
            target["kind"] == "asset"
            and target["id"] in asset_ids
            or target["kind"] == "artifact"
            and target["id"] in artifact_ids
        ):
            raise Conflict("content_in_trash")
    tokens = [token("asset", value) for value in asset_ids]
    tokens += [token("artifact", value) for value in artifact_ids]
    if storage_binding is None:
        if any(
            content_is_deleted(connection, tenant, owner, object_key=value)
            for value in object_keys
        ):
            raise Conflict("content_purging_or_purged")
    else:
        tokens += [token("key", value, storage_binding) for value in object_keys]
    if _deleted(connection, tenant, owner, tokens):
        raise Conflict("content_purging_or_purged")


def assert_project_writable(connection, tenant, owner, project_id):
    _assert_project_available(connection, tenant, owner, project_id, ("active",))


def assert_project_renditions_available(connection, tenant, owner, project_id):
    """Display-only receipt updates may retain readable trashed chat history.

    This does not restore authoring authority or permit a project/content purge.
    The caller must separately validate the immutable original receipt and CAS.
    """
    _assert_project_available(
        connection, tenant, owner, project_id, ("active", "trashed")
    )


def _assert_project_available(connection, tenant, owner, project_id, history_states):
    from .universal_trash import active_target

    if inspect(connection).has_table(documents.name) and active_target(
        connection,
        tenant,
        owner,
        {"kind": "project", "project_id": project_id, "id": project_id},
    ):
        raise Conflict("project_in_trash")
    table = _objects(connection)
    if table is None:
        return
    session = connection.execute(
        select(table.c.payload).where(
            table.c.tenant == tenant,
            table.c.owner == owner,
            table.c.kind == "session",
            table.c.payload["project_id"].as_string() == project_id,
        )
    ).scalar_one_or_none()
    if session is not None:
        if (
            session.get("history", {}).get("trash_state", "active")
            not in history_states
        ):
            raise Conflict("quick_chat_session_not_active")
        return
    if not inspect(connection).has_table(documents.name):
        return
    project = connection.execute(
        select(documents.c.payload).where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == owner,
            documents.c.project_id == "__projects",
            documents.c.kind == "project",
            documents.c.document_id == project_id,
        )
    ).scalar_one_or_none()
    if project and (
        project.get("integration_kind") == "quick_chat"
        or project.get("journey", {}).get("integration_kind") == "quick_chat"
    ):
        raise Conflict("quick_chat_session_binding_unconfirmed")


def reference_ids(value):
    """Closed identity extraction; unknown equal-ID strings retain content too."""
    assets, artifacts = set(), set()

    def visit(item):
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            candidate = item
            # This is an identity census, never URL fetching. Retaining a
            # closed API media path on any HTTPS origin is conservative, and
            # also protects legacy absolute same-site URLs lacking an ID field.
            if candidate.startswith(("https://", "http://", "/v1/")):
                try:
                    path = urlsplit(candidate).path
                except ValueError:
                    path = ""
                match = re.fullmatch(
                    r"/v1/(assets|artifacts)/([0-9a-fA-F-]+)(?:/[^?#]*)?", path
                )
                if match:
                    kind, ident = match.groups()
                    if kind == "assets" and re.fullmatch(r"[0-9a-f]{32}", ident):
                        assets.add(ident)
                    elif kind == "artifacts" and re.fullmatch(
                        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", ident
                    ):
                        artifacts.add(ident)
                    return
            for prefix, target in (
                ("cloud_asset_", assets),
                ("/v1/assets/", assets),
                ("cloud_artifact_", artifacts),
                ("/v1/artifacts/", artifacts),
            ):
                if candidate.startswith(prefix):
                    target.add(candidate[len(prefix) :].split("/", 1)[0])
                    return
            if re.fullmatch(r"[0-9a-f]{32}", candidate):
                assets.add(candidate)
            elif re.fullmatch(r"[0-9a-fA-F-]{36}", candidate):
                artifacts.add(candidate)

    visit(value)
    return assets, artifacts


def assert_reference_payload(connection, tenant, owner, value):
    from .universal_trash import assert_authoring_available

    assert_authoring_available(connection, tenant, owner, value)
    assets, artifacts = reference_ids(value)
    assert_content_available(
        connection, tenant, owner, asset_ids=assets, artifact_ids=artifacts
    )
    # Whole-document saves must not introduce a new cross-owner reference that
    # evades the owner's gate. Existing inaccessible aliases are retained by
    # the purge census; newly attached identities still require their owner.
    from .assets import asset_table
    from .repository import artifacts as artifact_table, jobs

    if assets and inspect(connection).has_table(asset_table.name):
        if connection.execute(
            select(asset_table.c.id)
            .where(
                asset_table.c.id.in_(assets),
                (asset_table.c.tenant != tenant) | (asset_table.c.owner != owner),
            )
            .limit(1)
        ).first():
            raise Conflict("foreign_content_reference")
    if artifacts and inspect(connection).has_table(artifact_table.name):
        if connection.execute(
            select(artifact_table.c.id)
            .join(jobs, jobs.c.id == artifact_table.c.job_id)
            .where(
                artifact_table.c.id.in_(artifacts),
                (jobs.c.tenant_id != tenant) | (jobs.c.owner_id != owner),
            )
            .limit(1)
        ).first():
            raise Conflict("foreign_content_reference")


def put_tombstone(
    connection, tenant, owner, *, kind, value, action_id, now, storage_binding=None
):
    table = _objects(connection)
    if table is None or kind not in {"asset", "artifact", "key"}:
        raise Conflict("content_tombstone_unavailable")
    business = token(kind, value, storage_binding)
    prior = (
        connection.execute(
            select(table).where(
                table.c.tenant == tenant,
                table.c.owner == owner,
                table.c.kind == "content_tombstone",
                table.c.business_key == business,
            )
        )
        .mappings()
        .first()
    )
    if prior:
        return dict(prior)
    payload = {
        "kind": kind,
        "identity": value,
        "action_id": action_id,
        "storage_binding": storage_binding,
        "state": "pending",
    }
    if kind == "key":
        payload["object_key"] = value
    row = dict(
        id="content-"
        + hashlib.sha256(
            (tenant + "\0" + owner + "\0" + business).encode()
        ).hexdigest(),
        tenant=tenant,
        owner=owner,
        session_id=action_id,
        kind="content_tombstone",
        parent_id=None,
        business_key=business,
        version=1,
        payload=payload,
        created_at=now,
        updated_at=now,
    )
    connection.execute(insert(table).values(**row))
    return row

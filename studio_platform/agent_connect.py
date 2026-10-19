"""Short-lived browser authorization for a client-held, ordinary personal key.

No raw PAT is accepted, returned or stored by this protocol.  The client saves
its own high-entropy token before exchanging its digest.  A verifier recovers
the same result after a lost response; it can never issue another key.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import uuid
from urllib.parse import urlsplit

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .auth import (
    API_SCOPES,
    USERS,
    Auth,
    AuthenticationError,
    Principal,
    accounts,
    digest,
    personal_keys,
    sessions,
)

CODE_TTL_SECONDS = 600
RECOVERY_TTL_SECONDS = 300
KEY_LIFETIME_DAYS = 90
PROFILE_ID = "creator-full"
PROFILE_VERSION = 1
# Deliberately explicit: adding an Auth capability does not expand a grant.
CONNECT_SCOPES = tuple(
    sorted(
        {
            "projects:read",
            "projects:create",
            "projects:write",
            "assets:read",
            "assets:write",
            "jobs:read",
            "jobs:write",
            "assistant:run",
        }
    )
)
CODE = re.compile(r"^sxc_[A-Za-z0-9_-]{43}$")
VERIFIER = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
PREFIX = re.compile(r"^sxp_[A-Za-z0-9_-]{8}$")
CONNECTION_ID = re.compile(r"^connection-[0-9a-f]{32}$")
metadata = MetaData()
connections = Table(
    "platform_agent_connections",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("id", String(80), primary_key=True),
    Column("owner", String(80), nullable=False),
    Column("name", String(80), nullable=False),
    Column("code_hash", String(64), nullable=False, unique=True),
    Column("session_hash", String(64), nullable=False),
    Column("auth_mode", String(20), nullable=False),
    Column("password_version", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("claimed_at", Float),
    Column("recovery_until", Float),
    Column("revoked_at", Float),
    Column("client_challenge", String(64)),
    Column("key_id", String(80)),
    Column("token_hash", String(64)),
    Column("authorization", Text, nullable=False),
    Column("authorization_fingerprint", String(64), nullable=False),
    Column("idempotency_key", String(160), nullable=False),
    Column("request_hash", String(64), nullable=False),
    UniqueConstraint(
        "tenant", "owner", "idempotency_key", name="uq_agent_connect_issue"
    ),
)
exchange_limits = Table(
    "platform_agent_exchange_limits",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("source_hash", String(64), primary_key=True),
    Column("window_start", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
)
audit = Table(
    "platform_agent_connection_audit",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("id", String(80), primary_key=True),
    Column("connection_id", String(80), nullable=False),
    Column("owner", String(80), nullable=False),
    Column("event", String(40), nullable=False),
    Column("created_at", Float, nullable=False),
)


class ConnectError(Exception):
    """Static error codes only; never embed a credential or SQL parameter."""

    def __init__(self, code="connection_invalid", status_code=400, retry_after=None):
        self.code, self.status_code, self.retry_after = code, status_code, retry_after
        super().__init__(code)


from .agent_management import AgentManagementMixin


class AgentConnect(AgentManagementMixin):
    def __init__(self, auth: Auth):
        self.auth, self.engine, self.tenant, self.clock = (
            auth,
            auth.engine,
            auth.tenant,
            auth.clock,
        )
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(685939796868749722)")
            metadata.create_all(conn)
            from .repository import documents

            documents.create(conn, checkfirst=True)

    def _begin(self, conn):
        if self.engine.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def _emit(self, conn, row, event):
        conn.execute(
            insert(audit).values(
                tenant=self.tenant,
                id="audit-" + uuid.uuid4().hex,
                connection_id=row["id"],
                owner=row["owner"],
                event=event,
                created_at=self.clock(),
            )
        )

    def _account(self, conn, owner, password_version, *, lock=False):
        from .identity import account_identity

        identity = account_identity(conn, self.tenant, owner)
        if identity is not None and identity["status"] != "active":
            raise ConnectError("connection_authority_changed", 410)
        statement = select(accounts).where(
            accounts.c.tenant == self.tenant, accounts.c.username == owner
        )
        if lock:
            statement = statement.with_for_update()
        account = conn.execute(statement).mappings().first()
        if self.auth.mode == "password" and (
            not account
            or account["disabled"]
            or account["updated"] != password_version
            or not self.auth.ready(conn)
        ):
            raise ConnectError("connection_authority_changed", 410)
        return account

    def _browser(self, conn, principal):
        if (
            not isinstance(principal, Principal)
            or principal.machine
            or principal.owner not in USERS
            or principal.auth_mode != self.auth.mode
            or principal.password_version is None
            or not principal.session_hash
            or self.auth.mode not in {"password", "local-test"}
        ):
            raise AuthenticationError("请使用网站账户登录后连接Agent")
        self._account(conn, principal.owner, principal.password_version, lock=True)
        row = conn.execute(
            select(sessions.c.token_hash)
            .where(
                sessions.c.tenant == self.tenant,
                sessions.c.token_hash == principal.session_hash,
                sessions.c.username == principal.owner,
                sessions.c.auth_mode == self.auth.mode,
                sessions.c.password_version == principal.password_version,
                sessions.c.expires > self.clock(),
            )
            .with_for_update()
        ).first()
        if not row:
            raise AuthenticationError("登录状态已改变，请重新登录")

    @staticmethod
    def origin(value):
        if not isinstance(value, str):
            raise ConnectError("connection_origin_invalid", 422)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.scheme == "http"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ConnectError("connection_origin_invalid", 422)
        try:
            parsed.port
        except ValueError:
            raise ConnectError("connection_origin_invalid", 422) from None
        return f"{parsed.scheme}://{parsed.netloc.lower()}"

    def profile(self, policy=None):
        from .agent_management import DEFAULTS

        policy = policy or DEFAULTS
        return {
            "id": PROFILE_ID,
            "version": PROFILE_VERSION,
            "scopes": list(CONNECT_SCOPES),
            "all_projects": True,
            "project_ids": [],
            "key_lifetime_days": policy["key_lifetime_days"],
            "code_ttl_seconds": policy["code_ttl_seconds"],
            "recovery_ttl_seconds": policy["recovery_ttl_seconds"],
            "max_pending_connections": policy["max_pending_connections"],
            "description": "本人现有和未来项目的创作、上传、视频生成及文本助手调用；不含账户、预算或基础设施管理",
        }

    def issue(
        self,
        principal: Principal,
        *,
        name="Codex",
        authorization_profile_id,
        authorization_profile_version,
        idempotency_key,
        origin,
        daily_limit_microusd=0,
        scope=None,
        permission=None,
    ):
        from .repository import money

        try:
            money(daily_limit_microusd)
        except ValueError:
            raise ConnectError("connection_daily_limit_invalid", 422) from None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ConnectError("connection_name_invalid", 422)
        if (
            authorization_profile_id != PROFILE_ID
            or type(authorization_profile_version) is not int
            or authorization_profile_version != PROFILE_VERSION
        ):
            raise ConnectError("connection_profile_changed", 409)
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,160}", idempotency_key
        ):
            raise ConnectError("connection_idempotency_required", 422)
        origin = self.origin(origin)
        from . import auth as auth_module

        if not set(CONNECT_SCOPES).issubset(auth_module.API_SCOPES):
            raise ConnectError("connection_profile_unavailable", 503)
        request_value = {
            "name": name.strip(),
            "profile_id": authorization_profile_id,
            "profile_version": authorization_profile_version,
            "origin": origin,
        }
        if daily_limit_microusd:
            request_value["daily_limit_microusd"] = daily_limit_microusd
        if scope is not None or permission is not None:
            request_value.update(scope=scope, permission=permission)
        request_hash = digest(
            json.dumps(request_value, sort_keys=True, separators=(",", ":"))
        )
        code, now = "sxc_" + secrets.token_urlsafe(32), self.clock()
        with self.engine.begin() as conn:
            self._begin(conn)
            self._browser(conn, principal)
            prior = (
                conn.execute(
                    select(connections).where(
                        connections.c.tenant == self.tenant,
                        connections.c.owner == principal.owner,
                        connections.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ConnectError("idempotency_conflict", 409)
                # Only the first successful response contains the raw code.
                # Lost issuance responses cannot be reconstructed from hashes.
                return {
                    "connection": self._public_current(conn, prior),
                    "code": None,
                    "code_available": False,
                    "replayed": True,
                }
            from .spending_limits import lock_owner

            lock_owner(conn, self.tenant, principal.owner)
            policy = self._settings(conn, principal.owner)['value']
            grant = (
                self._grant_scope(conn, principal, scope, permission)
                if scope is not None or permission is not None
                else None
            )
            active = conn.execute(
                select(connections.c.id).where(
                    connections.c.tenant == self.tenant,
                    connections.c.owner == principal.owner,
                    connections.c.revoked_at.is_(None),
                    connections.c.claimed_at.is_(None),
                    connections.c.expires_at > now,
                )
            ).all()
            if len(active) >= policy['max_pending_connections']:
                raise ConnectError("too_many_pending_connections", 429)
            connection_id = "connection-" + uuid.uuid4().hex
            authorization = {
                "origin": origin,
                "owner": principal.owner,
                "tenant": self.tenant,
                "connection_id": connection_id,
                "profile_id": PROFILE_ID,
                "profile_version": PROFILE_VERSION,
                "scopes": list(CONNECT_SCOPES),
                "all_projects": True,
                "project_ids": [],
                "key_lifetime_days": policy['key_lifetime_days']
                if grant
                else KEY_LIFETIME_DAYS,
                "key_expires_at": now
                + (policy['key_lifetime_days'] if grant else KEY_LIFETIME_DAYS) * 86400,
            }
            if grant:
                authorization.update(
                    grant, recovery_ttl_seconds=policy['recovery_ttl_seconds']
                )
            if daily_limit_microusd:
                authorization["daily_limit_microusd"] = daily_limit_microusd
            encoded = json.dumps(authorization, sort_keys=True, separators=(",", ":"))
            row = dict(
                tenant=self.tenant,
                id="connection-" + uuid.uuid4().hex,
                owner=principal.owner,
                name=name.strip(),
                code_hash=digest(code),
                session_hash=principal.session_hash,
                auth_mode=self.auth.mode,
                password_version=principal.password_version,
                created_at=now,
                expires_at=now + policy["code_ttl_seconds"],
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                authorization=encoded,
                authorization_fingerprint=digest(encoded),
            )
            row["id"] = connection_id
            conn.execute(insert(connections).values(**row))
            self._emit(conn, row, "issued")
        return {
            "connection": self._public(row),
            "code": code,
            "code_available": True,
            "replayed": False,
        }

    def _public(self, row, key=None, *, unavailable=False):
        if row.get("revoked_at") is not None or key and key["revoked_at"] is not None:
            status = "revoked"
        elif unavailable:
            status = "unavailable"
        elif row.get("claimed_at") is not None:
            status = (
                "expired" if key and key["expires_at"] <= self.clock() else "connected"
            )
        else:
            status = "expired" if row["expires_at"] <= self.clock() else "pending"
        result = {
            k: row.get(k)
            for k in (
                "id",
                "name",
                "created_at",
                "expires_at",
                "claimed_at",
                "revoked_at",
                "key_id",
            )
        }
        authorization = json.loads(row["authorization"])
        result.update(
            status=status,
            scopes=authorization["scopes"],
            project_ids=authorization["project_ids"],
            all_projects=authorization["all_projects"],
            key_lifetime_days=authorization["key_lifetime_days"],
            authorization=authorization,
            authorization_fingerprint=row["authorization_fingerprint"],
        )
        result.update(
            scope=authorization.get('scope', {'kind': 'all'}),
            permission=authorization.get('permission', 'generate'),
            daily_limit_microusd=authorization.get('daily_limit_microusd', 0),
            paused=False,
            state_version=0,
        )
        if key:
            result["key"] = self.auth.public_key(key)
        return result

    def _public_current(self, conn, row):
        key = (
            conn.execute(
                select(personal_keys).where(
                    personal_keys.c.tenant == self.tenant,
                    personal_keys.c.owner == row["owner"],
                    personal_keys.c.id == row.get("key_id"),
                )
            )
            .mappings()
            .first()
            if row.get("key_id")
            else None
        )
        unavailable = row["auth_mode"] != self.auth.mode
        try:
            self._account(conn, row["owner"], row["password_version"])
        except ConnectError:
            unavailable = True
        if row.get("claimed_at") is not None and not key:
            unavailable = True
        if (
            row.get("claimed_at") is None
            and not conn.execute(
                select(sessions.c.token_hash).where(
                    sessions.c.tenant == self.tenant,
                    sessions.c.token_hash == row["session_hash"],
                    sessions.c.expires > self.clock(),
                )
            ).first()
        ):
            unavailable = True
        return self._management_public(
            conn,
            row,
            self._public(
                dict(row), dict(key) if key else None, unavailable=unavailable
            ),
        )

    def list(self, principal, *, limit=50, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise ConnectError("connection_pagination_invalid", 422)
        with self.engine.begin() as conn:
            self._browser(conn, principal)
            rows = (
                conn.execute(
                    select(connections)
                    .where(
                        connections.c.tenant == self.tenant,
                        connections.c.owner == principal.owner,
                    )
                    .order_by(connections.c.created_at.desc(), connections.c.id)
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
            values = [self._public_current(conn, row) for row in rows]
            return {
                "connections": values,
                "authorization_profile": self.profile(
                    self._settings(conn, principal.owner)["value"]
                ),
                "limit": limit,
                "offset": offset,
                "has_more": len(values) == limit,
                "next_offset": offset + limit if len(values) == limit else None,
            }

    def get(self, principal, connection_id):
        with self.engine.begin() as conn:
            self._browser(conn, principal)
            row = (
                conn.execute(
                    select(connections).where(
                        connections.c.tenant == self.tenant,
                        connections.c.owner == principal.owner,
                        connections.c.id == connection_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise ConnectError("connection_not_found", 404)
            return self._public_current(conn, row)

    def reserve_exchange(self, source, *, code=None):
        if not isinstance(source, str) or not 1 <= len(source) <= 512:
            source = "unknown"
        self._reserve_limit(digest("agent-connect:" + source), maximum=10)
        # Unknown codes do not create attacker-controlled per-code rows.
        if isinstance(code, str) and CODE.fullmatch(code):
            with self.engine.connect() as conn:
                connection_id = conn.execute(
                    select(connections.c.id).where(
                        connections.c.tenant == self.tenant,
                        connections.c.code_hash == digest(code),
                    )
                ).scalar_one_or_none()
            if connection_id:
                self._reserve_limit(digest("connection:" + connection_id), maximum=20)

    def _reserve_limit(self, key, *, maximum):
        now = self.clock()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(exchange_limits).values(
                        tenant=self.tenant,
                        source_hash=key,
                        window_start=now,
                        attempts=0,
                    )
                )
        except IntegrityError:
            pass
        with self.engine.begin() as conn:
            conn.execute(
                update(exchange_limits)
                .where(
                    exchange_limits.c.tenant == self.tenant,
                    exchange_limits.c.source_hash == key,
                    exchange_limits.c.window_start <= now - 300,
                )
                .values(window_start=now, attempts=0)
            )
            changed = conn.execute(
                update(exchange_limits)
                .where(
                    exchange_limits.c.tenant == self.tenant,
                    exchange_limits.c.source_hash == key,
                    exchange_limits.c.attempts < maximum,
                )
                .values(attempts=exchange_limits.c.attempts + 1)
            )
            if changed.rowcount != 1:
                start = conn.execute(
                    select(exchange_limits.c.window_start).where(
                        exchange_limits.c.tenant == self.tenant,
                        exchange_limits.c.source_hash == key,
                    )
                ).scalar_one()
                raise ConnectError(
                    "connection_exchange_limited", 429, max(1, int(start + 300 - now))
                )

    def exchange(
        self,
        *,
        code,
        client_challenge,
        token_hash,
        key_prefix,
        source,
        expected_authorization_fingerprint,
        origin,
        recovery_verifier=None,
    ):
        self.reserve_exchange(source, code=code)
        origin = self.origin(origin)
        if (
            not isinstance(code, str)
            or not CODE.fullmatch(code)
            or not isinstance(client_challenge, str)
            or not HEX.fullmatch(client_challenge)
            or not isinstance(token_hash, str)
            or not HEX.fullmatch(token_hash)
            or not isinstance(key_prefix, str)
            or not PREFIX.fullmatch(key_prefix)
            or not isinstance(expected_authorization_fingerprint, str)
            or not HEX.fullmatch(expected_authorization_fingerprint)
            or recovery_verifier is not None
            and (
                not isinstance(recovery_verifier, str)
                or not VERIFIER.fullmatch(recovery_verifier)
            )
        ):
            raise ConnectError()
        try:
            with self.engine.begin() as conn:
                self._begin(conn)
                initial = conn.execute(
                    select(connections.c.owner, connections.c.password_version).where(
                        connections.c.tenant == self.tenant,
                        connections.c.code_hash == digest(code),
                    )
                ).first()
                if not initial:
                    raise ConnectError()
                # Same lock order as Auth.set_password: account before key rows.
                self._account(conn, initial[0], initial[1], lock=True)
                row = (
                    conn.execute(
                        select(connections)
                        .where(
                            connections.c.tenant == self.tenant,
                            connections.c.code_hash == digest(code),
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                authorization = json.loads(row["authorization"])
                if (
                    not hmac.compare_digest(
                        row["authorization_fingerprint"],
                        expected_authorization_fingerprint,
                    )
                    or authorization["origin"] != origin
                ):
                    raise ConnectError("connection_authorization_mismatch", 409)
                if row["revoked_at"] is not None or row["auth_mode"] != self.auth.mode:
                    raise ConnectError("connection_unavailable", 410)
                now = self.clock()
                if row["claimed_at"] is not None:
                    if (
                        recovery_verifier is None
                        or row["recovery_until"] <= now
                        or not hmac.compare_digest(
                            digest(recovery_verifier), row["client_challenge"]
                        )
                        or not hmac.compare_digest(
                            client_challenge, row["client_challenge"]
                        )
                        or not hmac.compare_digest(token_hash, row["token_hash"])
                    ):
                        raise ConnectError("connection_consumed", 409)
                    key = (
                        conn.execute(
                            select(personal_keys)
                            .where(
                                personal_keys.c.tenant == self.tenant,
                                personal_keys.c.id == row["key_id"],
                                personal_keys.c.owner == row["owner"],
                                personal_keys.c.revoked_at.is_(None),
                                personal_keys.c.expires_at > now,
                            )
                            .with_for_update()
                        )
                        .mappings()
                        .first()
                    )
                    if (
                        not key
                        or key["prefix"] != key_prefix
                        or key["token_hash"] != token_hash
                    ):
                        raise ConnectError("connection_unavailable", 410)
                    self._emit(conn, row, "recovered")
                    return {
                        "connection": self._public(dict(row), dict(key)),
                        "key": self.auth.public_key(key),
                        "recovered": True,
                    }
                if recovery_verifier is not None:
                    # A timed-out exchange may not have reached us.  Recovery
                    # never mints a key; the caller can retry its original claim.
                    raise ConnectError("connection_not_exchanged", 409)
                if row["expires_at"] <= now:
                    raise ConnectError("connection_expired", 410)
                active_session = conn.execute(
                    select(sessions.c.token_hash)
                    .where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == row["session_hash"],
                        sessions.c.username == row["owner"],
                        sessions.c.auth_mode == self.auth.mode,
                        sessions.c.password_version == row["password_version"],
                        sessions.c.expires > now,
                    )
                    .with_for_update()
                ).first()
                if not active_session:
                    raise ConnectError("connection_authority_changed", 410)
                active_keys = conn.execute(
                    select(personal_keys.c.id).where(
                        personal_keys.c.tenant == self.tenant,
                        personal_keys.c.owner == row["owner"],
                        personal_keys.c.revoked_at.is_(None),
                        personal_keys.c.expires_at > now,
                    )
                ).all()
                if len(active_keys) >= 50:
                    raise ConnectError("too_many_active_keys", 429)
                key = dict(
                    tenant=self.tenant,
                    id="key-" + uuid.uuid4().hex,
                    owner=row["owner"],
                    name=row["name"],
                    token_hash=token_hash,
                    prefix=key_prefix,
                    projects=json.dumps(authorization["project_ids"]),
                    scopes=json.dumps(authorization["scopes"]),
                    all_projects=int(authorization["all_projects"]),
                    auth_mode=self.auth.mode,
                    password_version=row["password_version"],
                    created_at=now,
                    expires_at=authorization["key_expires_at"],
                    last_used_at=None,
                    revoked_at=None,
                )
                claimed = dict(
                    claimed_at=now,
                    recovery_until=now
                    + authorization.get("recovery_ttl_seconds", RECOVERY_TTL_SECONDS),
                    client_challenge=client_challenge,
                    token_hash=token_hash,
                    key_id=key["id"],
                )
                changed = conn.execute(
                    update(connections)
                    .where(
                        connections.c.tenant == self.tenant,
                        connections.c.id == row["id"],
                        connections.c.claimed_at.is_(None),
                        connections.c.revoked_at.is_(None),
                        connections.c.expires_at > now,
                    )
                    .values(**claimed)
                )
                if changed.rowcount != 1:
                    raise ConnectError("connection_consumed", 409)
                conn.execute(insert(personal_keys).values(**key))
                from .spending_limits import lock_owner, read_settings
                from .repository import spending_settings

                cap = authorization.get("daily_limit_microusd", 0)
                if cap:
                    lock_owner(conn, self.tenant, row["owner"])
                    config = read_settings(conn, self.tenant, row["owner"])
                    limits = {
                        **config["value"]["connection_limits"],
                        "key:" + key["id"]: cap,
                    }
                    stored = {
                        "value": {**config["value"], "connection_limits": limits},
                        "version": config["version"] + 1,
                        "actor_id": "browser:" + row["owner"],
                        "updated_at": now,
                    }
                    if config["version"]:
                        conn.execute(
                            update(spending_settings)
                            .where(
                                spending_settings.c.tenant_id == self.tenant,
                                spending_settings.c.owner_id == row["owner"],
                            )
                            .values(**stored)
                        )
                    else:
                        conn.execute(
                            insert(spending_settings).values(
                                tenant_id=self.tenant, owner_id=row["owner"], **stored
                            )
                        )
                row = dict(row) | claimed
                self._emit(conn, row, "exchanged")
                return {
                    "connection": self._public(row, key),
                    "key": self.auth.public_key(key),
                    "recovered": False,
                }
        except IntegrityError:
            # Unique token hashes are global across tenants, just like Auth.
            # Rollback also restores the unconsumed grant; no second PAT exists.
            raise ConnectError("connection_key_conflict", 409) from None

    def revoke(self, principal, connection_id):
        with self.engine.begin() as conn:
            self._begin(conn)
            self._browser(conn, principal)
            from .spending_limits import lock_owner

            lock_owner(conn, self.tenant, principal.owner)
            row = (
                conn.execute(
                    select(connections)
                    .where(
                        connections.c.tenant == self.tenant,
                        connections.c.owner == principal.owner,
                        connections.c.id == connection_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not row:
                raise ConnectError("connection_not_found", 404)
            if row["revoked_at"] is None:
                now = self.clock()
                conn.execute(
                    update(connections)
                    .where(
                        connections.c.tenant == self.tenant,
                        connections.c.id == row["id"],
                    )
                    .values(revoked_at=now)
                )
                if row["key_id"]:
                    conn.execute(
                        update(personal_keys)
                        .where(
                            personal_keys.c.tenant == self.tenant,
                            personal_keys.c.owner == principal.owner,
                            personal_keys.c.id == row["key_id"],
                            personal_keys.c.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
                self._emit(conn, row, "revoked")
            return {"id": connection_id, "revoked": True}

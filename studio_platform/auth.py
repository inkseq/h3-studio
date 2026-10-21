"""Shared SQL authentication, scoped machine clients, and revocable sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time
import uuid

import bcrypt
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
accounts = Table(
    "platform_accounts",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("username", String(80), primary_key=True),
    Column("password_hash", Text, nullable=False),
    Column("disabled", Integer, nullable=False, default=0),
    Column("updated", Float, nullable=False),
)
sessions = Table(
    "platform_sessions",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("token_hash", String(64), primary_key=True),
    Column("username", String(80), nullable=False),
    Column("auth_mode", String(20), nullable=False),
    Column("created", Float, nullable=False),
    Column("expires", Float, nullable=False),
    Column("password_version", Float, nullable=False),
)
login_limits = Table(
    "platform_login_limits",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("source_hash", String(64), primary_key=True),
    Column("window_start", Float, nullable=False),
    Column("attempts", Integer, nullable=False),
)
clients = Table(
    "platform_service_clients",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("id", String(80), primary_key=True),
    Column("token_hash", String(64), unique=True, nullable=False),
    Column("owner", String(80), nullable=False),
    Column("projects", Text, nullable=False),
    Column("scopes", Text, nullable=False),
    Column("disabled", Integer, nullable=False, default=0),
)

personal_keys = Table(
    "platform_personal_api_keys",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("id", String(80), primary_key=True),
    Column("token_hash", String(64), unique=True, nullable=False),
    Column("prefix", String(20), nullable=False),
    Column("owner", String(80), nullable=False),
    Column("name", String(80), nullable=False),
    Column("projects", Text, nullable=False),
    Column("scopes", Text, nullable=False),
    Column("all_projects", Integer, nullable=False),
    Column("auth_mode", String(20), nullable=False),
    Column("password_version", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("last_used_at", Float),
    Column("revoked_at", Float),
)
API_SCOPES = frozenset(
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

USERS = frozenset({"superdan", "supervan"})
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
PASSWORD_HASH = re.compile(r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$")
WORKOS_PRIVATE_OWNER = "__authkit"
WORKOS_PRIVATE_PROJECT = "__authkit"
WORKOS_SESSION_KIND = "authkit_session"
WORKOS_SESSION_FIELDS = {"sid", "subject", "client_id", "access_expires_at"}
_DUMMY = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt(rounds=12))
_PROVIDER_CHECK = object()


class AuthenticationError(Exception):
    pass


class SessionUnavailable(AuthenticationError):
    """A bounded provider check failed; no provider exception data is exposed."""


class LoginLimited(AuthenticationError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("登录尝试过多，请稍后重试")


@dataclass(frozen=True)
class Principal:
    owner: str
    actor_id: str
    machine: bool = False
    project_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    all_projects: bool = False
    # Browser authentication evidence is carried to credential-issuing writes.
    # Defaults keep operator machine principals compatible; they cannot mint keys.
    auth_mode: str | None = None
    password_version: float | None = None
    session_hash: str | None = field(default=None, repr=False)
    user_id: str | None = None
    role: str = "user"
    status: str = "active"
    identity_source: str = "legacy"

    def allows(self, project_id: str, scope: str) -> bool:
        return not self.machine or (
            (self.all_projects or project_id in self.project_ids)
            and scope in self.scopes
            and (scope != "projects:write" or "projects:read" in self.scopes)
        )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workos_session_shape(value):
    """Minimal logout metadata shape; this is never authentication authority."""
    return (
        type(value) is dict
        and set(value) == WORKOS_SESSION_FIELDS
        and isinstance(value["sid"], str)
        and re.fullmatch(r"session_[A-Za-z0-9]{10,80}", value["sid"]) is not None
        and isinstance(value["subject"], str)
        and re.fullmatch(r"user_[A-Za-z0-9]{10,80}", value["subject"]) is not None
        and isinstance(value["client_id"], str)
        and re.fullmatch(r"client_[A-Za-z0-9]{12,80}", value["client_id"]) is not None
        and type(value["access_expires_at"]) is int
        and value["access_expires_at"] > 0
    )


class Auth:
    def __init__(
        self,
        engine,
        *,
        tenant="sixnine",
        mode="password",
        session_seconds=7 * 86400,
        clock=time.time,
        staging_qa=False,
    ):
        self.engine = engine
        self.tenant = tenant
        self.mode = mode
        if type(session_seconds) is not int or not 1 <= session_seconds <= 365 * 86400:
            raise ValueError("Invalid browser session duration")
        self.session_seconds = session_seconds
        self.clock = clock
        self.staging_qa = staging_qa is True and mode == "password"
        self.session_checker = None
        with engine.begin() as connection:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(685939796868749721)"
                )
            metadata.create_all(connection)
            from .repository import documents

            documents.create(connection, checkfirst=True)

    def ready(self, connection=None):
        if self.mode == "local-test":
            return True
        if connection is None:
            with self.engine.connect() as conn:
                return self.ready(conn)
        rows = connection.execute(
            select(
                accounts.c.username, accounts.c.password_hash, accounts.c.disabled
            ).where(accounts.c.tenant == self.tenant)
        ).all()
        present = {
            name for name, encoded, _ in rows if PASSWORD_HASH.fullmatch(encoded)
        }
        # Disabling one account must not log out the other configured account.
        return USERS.issubset(present) and any(not disabled for _, _, disabled in rows)

    def set_password(self, username: str, password: str, *, expected_version=None):
        if username not in USERS:
            raise ValueError("Unsupported account")
        if (
            not isinstance(password, str)
            or len(password) < 12
            or len(password.encode("utf-8")) > 72
        ):
            raise ValueError(
                "Password must have at least 12 characters and at most 72 UTF-8 bytes"
            )
        encoded = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("ascii")
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(accounts.c.updated)
                .where(
                    accounts.c.tenant == self.tenant, accounts.c.username == username
                )
                .with_for_update()
            ).first()
            if expected_version is not None and (
                not existing or existing[0] != expected_version
            ):
                raise AuthenticationError("账户状态已改变，请重新登录")
            changed_at = (
                max(self.clock(), existing[0] + 0.000001) if existing else self.clock()
            )
            values = {"password_hash": encoded, "disabled": 0, "updated": changed_at}
            if existing:
                conn.execute(
                    update(accounts)
                    .where(
                        accounts.c.tenant == self.tenant,
                        accounts.c.username == username,
                    )
                    .values(**values)
                )
            else:
                conn.execute(
                    insert(accounts).values(
                        tenant=self.tenant, username=username, **values
                    )
                )
            conn.execute(
                delete(sessions).where(
                    sessions.c.tenant == self.tenant, sessions.c.username == username
                )
            )
            conn.execute(
                update(personal_keys)
                .where(
                    personal_keys.c.tenant == self.tenant,
                    personal_keys.c.owner == username,
                    personal_keys.c.revoked_at.is_(None),
                )
                .values(revoked_at=changed_at)
            )

    def change_password(self, username, old_password, new_password):
        if username not in USERS:
            raise ValueError("请在账户登录方式中管理 WorkOS 密码")
        if self.mode != "password":
            raise ValueError("当前测试登录模式不支持修改密码")
        self.reserve_login("password-change:" + username)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(accounts).where(
                        accounts.c.tenant == self.tenant,
                        accounts.c.username == username,
                    )
                )
                .mappings()
                .first()
            )
            eligible = row is not None and not row["disabled"] and self.ready(conn)
        valid = (
            isinstance(old_password, str)
            and 0 < len(old_password.encode("utf-8")) <= 72
        )
        hashed = row["password_hash"].encode("ascii") if eligible else _DUMMY
        matched = bcrypt.checkpw(
            old_password.encode("utf-8") if valid else b"invalid", hashed
        )
        if not valid or not eligible or not matched:
            raise AuthenticationError("原密码不正确")
        self.set_password(username, new_password, expected_version=row["updated"])

    def reserve_login(self, source: str):
        self._reserve_login(source, 5)

    def reserve_workos_start(self, source: str):
        # Opening the hosted chooser is not a password attempt. Keep its
        # separate existing IP namespace and leave credential attempts strict.
        self._reserve_login("workos-start:" + source, 20)

    def _reserve_login(self, source: str, limit: int):
        key, now = digest(source), self.clock()
        # A unique row followed by a conditional UPDATE also serializes writers
        # when two API processes see the first attempt at the same time.
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(login_limits).values(
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
                update(login_limits)
                .where(
                    login_limits.c.tenant == self.tenant,
                    login_limits.c.source_hash == key,
                    login_limits.c.window_start <= now - 300,
                )
                .values(window_start=now, attempts=0)
            )
            changed = conn.execute(
                update(login_limits)
                .where(
                    login_limits.c.tenant == self.tenant,
                    login_limits.c.source_hash == key,
                    login_limits.c.attempts < limit,
                )
                .values(attempts=login_limits.c.attempts + 1)
            )
            if changed.rowcount != 1:
                start = conn.execute(
                    select(login_limits.c.window_start).where(
                        login_limits.c.tenant == self.tenant,
                        login_limits.c.source_hash == key,
                    )
                ).scalar_one()
                raise LoginLimited(max(1, int(start + 300 - now)))

    def login(self, username, password, source="local"):
        captured_epoch = self.capture_epoch()
        self.reserve_login(source)
        if self.mode == "local-test":
            if username not in USERS:
                raise AuthenticationError("用户名或密码不正确")
            version = 0.0
        else:
            with self.engine.connect() as conn:
                row = (
                    conn.execute(
                        select(accounts).where(
                            accounts.c.tenant == self.tenant,
                            accounts.c.username
                            == (username if isinstance(username, str) else ""),
                        )
                    )
                    .mappings()
                    .first()
                )
            valid = (
                isinstance(password, str) and 0 < len(password.encode("utf-8")) <= 72
            )
            eligible = (
                self._password_owner(username)
                and row is not None
                and not row["disabled"]
                and self.ready()
            )
            hashed = row["password_hash"].encode("ascii") if eligible else _DUMMY
            matched = bcrypt.checkpw(
                password.encode("utf-8") if valid else b"invalid", hashed
            )
            if not valid or not eligible or not matched:
                raise AuthenticationError("用户名或密码不正确")
            version = row["updated"]
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            from .identity import account_identity, lock_account_gate
            from .session_authority import assert_epoch

            lock_account_gate(conn)
            assert_epoch(conn, self.tenant, username, captured_epoch)

            identity = account_identity(conn, self.tenant, username)
            if identity is not None and identity["status"] != "active":
                raise AuthenticationError(
                    "账户已暂停，请联系管理员"
                    if identity["status"] == "suspended"
                    else "账户身份不可用，请联系管理员"
                )
            if self.mode == "password":
                current = conn.execute(
                    select(accounts.c.updated, accounts.c.disabled)
                    .where(
                        accounts.c.tenant == self.tenant,
                        accounts.c.username == username,
                    )
                    .with_for_update()
                ).first()
                if not current or current[1] or current[0] != version:
                    raise AuthenticationError("账户状态已改变，请重新登录")
            conn.execute(
                insert(sessions).values(
                    tenant=self.tenant,
                    token_hash=digest(token),
                    username=username,
                    auth_mode=self.mode,
                    created=now,
                    expires=now + self.session_seconds,
                    password_version=version,
                )
            )
        return token

    def source_supported(self, source):
        """Own issued WorkOS grants do not depend on provider SDK/config health."""
        return source == "workos" or source == self.mode

    def _password_owner(self, owner):
        return (
            owner in USERS
            or self.staging_qa
            and owner in {"qa-admin", "qa-user", "qa-credit"}
        )

    def _grant_account(
        self, connection, owner, source, version, *, lock=False, check_identity=True
    ):
        """Revalidate the original source without synthesizing password rows."""
        from .identity import account_identity

        if (
            not isinstance(owner, str)
            or not 1 <= len(owner) <= 80
            or not self.source_supported(source)
        ):
            return None
        if source == "workos":
            if type(version) not in (int, float) or version != 0.0:
                return None
            from .identity_schema import users

            identity = account_identity(connection, self.tenant, owner)
            if identity is None or identity.get("status") != "active":
                return None
            statement = select(users).where(
                users.c.tenant == self.tenant, users.c.owner == owner
            )
            row = (
                connection.execute(statement.with_for_update() if lock else statement)
                .mappings()
                .first()
            )
            if (
                row is None
                or row["status"] != "active"
                or not isinstance(row["workos_user_id"], str)
                or not row["workos_user_id"]
            ):
                return None
            return row
        if (
            not (source == "password" and self._password_owner(owner))
            and owner not in USERS
        ):
            return None
        if check_identity:
            identity = account_identity(connection, self.tenant, owner)
            if identity is not None and identity.get("status") != "active":
                return None
        # Explicit local-test grants have no password backing row. Keep their
        # original fixed-query path; hydrate still enforces canonical status.
        if source == "local-test":
            return {"owner": owner} if version == 0.0 else None
        statement = select(accounts).where(
            accounts.c.tenant == self.tenant, accounts.c.username == owner
        )
        row = (
            connection.execute(statement.with_for_update() if lock else statement)
            .mappings()
            .first()
        )
        if source == "password":
            return (
                row
                if row is not None
                and not row["disabled"]
                and row["updated"] == version
                and self.ready(connection)
                else None
            )
        return None

    def _workos_document(self, owner, token_hash):
        from .repository import documents

        return (
            documents.c.tenant_id == self.tenant,
            documents.c.owner_id == WORKOS_PRIVATE_OWNER,
            documents.c.project_id == WORKOS_PRIVATE_PROJECT,
            documents.c.kind == WORKOS_SESSION_KIND,
            documents.c.document_id == digest(owner + "\0" + token_hash),
        )

    def capture_epoch(self):
        from .session_authority import epoch

        with self.engine.connect() as connection:
            return epoch(connection, self.tenant)

    def issue_workos_session(
        self, owner, workos_user_id, *, provider_session=None, captured_epoch=None
    ):
        """Trusted verified-SDK callback only, after its canonical upsert commits."""
        if not isinstance(workos_user_id, str) or not 1 <= len(workos_user_id) <= 200:
            raise AuthenticationError("WorkOS 登录身份不可用")
        token, now = secrets.token_urlsafe(32), self.clock()
        if provider_session is not None and (
            not _workos_session_shape(provider_session)
            or provider_session["subject"] != workos_user_id
            or provider_session["access_expires_at"] <= now
        ):
            raise AuthenticationError("WorkOS 登录身份不可用")
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            from .identity import lock_account_gate
            from .session_authority import assert_epoch

            lock_account_gate(connection)
            assert_epoch(
                connection,
                self.tenant,
                owner,
                captured_epoch if captured_epoch is not None else 0,
            )
            row = self._grant_account(connection, owner, "workos", 0.0, lock=True)
            if row is None or row["workos_user_id"] != workos_user_id:
                raise AuthenticationError("WorkOS 登录身份已改变，请重新登录")
            connection.execute(
                insert(sessions).values(
                    tenant=self.tenant,
                    token_hash=digest(token),
                    username=owner,
                    auth_mode="workos",
                    password_version=0.0,
                    created=now,
                    expires=now + self.session_seconds,
                )
            )
            if provider_session is not None:
                from .repository import documents

                connection.execute(
                    insert(documents).values(
                        tenant_id=self.tenant,
                        owner_id=WORKOS_PRIVATE_OWNER,
                        project_id=WORKOS_PRIVATE_PROJECT,
                        kind=WORKOS_SESSION_KIND,
                        document_id=digest(owner + "\0" + digest(token)),
                        version=1,
                        payload={
                            **provider_session,
                            "owner": owner,
                            "session_hash": digest(token),
                            "session_created_at": now,
                        },
                        updated_at=now,
                    )
                )
        return token

    def session(self, token, *, verify_provider=True):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        return self._session_hash(
            digest(token), renew=True, verify_provider=verify_provider
        )

    def _session_hash(self, token_hash, *, renew=False, verify_provider=True):
        principal = self._local_session_hash(
            token_hash,
            renew=renew,
            require_provider=verify_provider and self.session_checker is not None,
        )
        if principal is _PROVIDER_CHECK:
            # The first local read has CLOSED its connection before checking
            # AuthKit. Password sessions keep their original one-read path.
            if not self.session_checker(token_hash):
                return None
            return self._local_session_hash(token_hash, renew=renew)
        return principal

    def _local_session_hash(self, token_hash, *, renew=False, require_provider=False):
        now = self.clock()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == token_hash,
                        sessions.c.expires > now,
                        sessions.c.auth_mode.in_((self.mode, "workos")),
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            if require_provider and row["auth_mode"] == "workos":
                return _PROVIDER_CHECK
            if (
                self._grant_account(
                    conn,
                    row["username"],
                    row["auth_mode"],
                    row["password_version"],
                    check_identity=False,  # Fresh hydrate below checks legacy status/role.
                )
                is None
            ):
                return None
            from .identity import hydrate

            principal = hydrate(
                Principal(
                    row["username"],
                    "browser:" + row["username"],
                    auth_mode=row["auth_mode"],
                    password_version=row["password_version"],
                    session_hash=row["token_hash"],
                ),
                conn,
                self.tenant,
            )
            if principal is None:
                return None
            # Slide at most hourly (half the TTL for short explicit test/config
            # durations). Polling and media reads do not write on every request.
            interval = min(3600, self.session_seconds / 2)
            if renew and row["expires"] <= now + self.session_seconds - interval:
                statement = update(sessions).where(
                    sessions.c.tenant == self.tenant,
                    sessions.c.token_hash == row["token_hash"],
                    sessions.c.expires > now,
                    sessions.c.auth_mode == row["auth_mode"],
                    sessions.c.password_version == row["password_version"],
                )
                if row["auth_mode"] == "password":
                    statement = statement.where(
                        select(accounts.c.username)
                        .where(
                            accounts.c.tenant == self.tenant,
                            accounts.c.username == row["username"],
                            accounts.c.disabled == 0,
                            accounts.c.updated == row["password_version"],
                        )
                        .exists()
                    )
                # Update-only: a concurrent logout/password change cannot
                # recreate a deleted session. Never shorten a concurrent renewal.
                conn.execute(
                    statement.where(
                        sessions.c.expires < now + self.session_seconds
                    ).values(expires=now + self.session_seconds)
                )
        return principal

    def _consume_workos_logout(self, conn, row, token_hash):
        from .repository import documents

        where = self._workos_document(row["username"], token_hash)
        record = conn.execute(
            select(documents.c.payload).where(*where).with_for_update()
        ).first()
        # Consume this exact binding even when stale/malformed. Access-token
        # expiry only gated issuance; the sliding local session owns this SID.
        conn.execute(delete(documents).where(*where))
        value = record[0] if record is not None else None
        if (
            type(value) is dict
            and set(value)
            == WORKOS_SESSION_FIELDS | {"owner", "session_hash", "session_created_at"}
            and value["owner"] == row["username"]
            and value["session_hash"] == token_hash
            and type(value["session_created_at"]) in (int, float)
            and value["session_created_at"] == row["created"]
            and row["expires"] > self.clock()
        ):
            candidate = {key: value[key] for key in WORKOS_SESSION_FIELDS}
            if _workos_session_shape(candidate):
                account = self._grant_account(conn, row["username"], "workos", 0.0)
                if (
                    account is not None
                    and account["workos_user_id"] == candidate["subject"]
                ):
                    return candidate
        return None

    def logout(self, token):
        provider_session = None
        if isinstance(token, str) and TOKEN.fullmatch(token):
            with self.engine.begin() as conn:
                if self.engine.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                token_hash = digest(token)
                row = (
                    conn.execute(
                        select(sessions)
                        .where(
                            sessions.c.tenant == self.tenant,
                            sessions.c.token_hash == token_hash,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if row is not None and row["auth_mode"] == "workos":
                    try:
                        with conn.begin_nested():
                            provider_session = self._consume_workos_logout(
                                conn, row, token_hash
                            )
                    except Exception:
                        # A failed metadata read/delete must not roll back local
                        # revocation. Any orphan is inert without its session.
                        provider_session = None
                conn.execute(
                    delete(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == token_hash,
                    )
                )
        return provider_session

    def register_client(self, client_id, token, owner, project_ids, scopes):
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", client_id)
            or not isinstance(owner, str)
            or not 1 <= len(owner) <= 80
        ):
            raise ValueError("Invalid client identity")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("Client token must be a high entropy URL-safe value")
        allowed = {
            "projects:read",
            "assets:read",
            "assets:write",
            "jobs:read",
            "jobs:write",
        }
        if (
            not isinstance(project_ids, (list, tuple))
            or not 1 <= len(project_ids) <= 4096
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", x)
                for x in project_ids
            )
            or not isinstance(scopes, (list, tuple))
            or any(not isinstance(s, str) or s not in allowed for s in scopes)
        ):
            raise ValueError("Client must have explicit projects and scopes")
        with self.engine.begin() as conn:
            if (
                owner not in USERS
                and self._grant_account(conn, owner, "workos", 0.0) is None
            ):
                raise ValueError("Invalid client identity")
            # Rotation changes the hash atomically and invalidates the old token.
            values = dict(
                token_hash=digest(token),
                owner=owner,
                projects=json.dumps(project_ids),
                scopes=json.dumps(scopes),
                disabled=0,
            )
            if conn.execute(
                select(clients.c.id).where(
                    clients.c.tenant == self.tenant, clients.c.id == client_id
                )
            ).first():
                conn.execute(
                    update(clients)
                    .where(clients.c.tenant == self.tenant, clients.c.id == client_id)
                    .values(**values)
                )
            else:
                conn.execute(
                    insert(clients).values(tenant=self.tenant, id=client_id, **values)
                )

    @staticmethod
    def public_key(row):
        return {
            k: row[k]
            for k in (
                "id",
                "name",
                "prefix",
                "created_at",
                "expires_at",
                "last_used_at",
                "revoked_at",
            )
        } | {
            "scopes": json.loads(row["scopes"]),
            "project_ids": json.loads(row["projects"]),
            "all_projects": bool(row["all_projects"]),
        }

    def list_keys(self, owner):
        with self.engine.connect() as conn:
            return [
                self.public_key(r)
                for r in conn.execute(
                    select(personal_keys)
                    .where(
                        personal_keys.c.tenant == self.tenant,
                        personal_keys.c.owner == owner,
                    )
                    .order_by(personal_keys.c.created_at.desc(), personal_keys.c.id)
                ).mappings()
            ]

    def create_key(
        self,
        owner,
        *,
        authenticated_session: Principal,
        name,
        scopes,
        project_ids=(),
        all_projects=False,
        expires_in_days=90,
    ):
        if (
            not isinstance(owner, str)
            or not 1 <= len(owner) <= 80
            or not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 80
        ):
            raise ValueError("API key名称须为1至80字符")
        if (
            not isinstance(scopes, (list, tuple))
            or not scopes
            or len(scopes) > len(API_SCOPES)
            or any(not isinstance(s, str) or s not in API_SCOPES for s in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            raise ValueError("API key权限无效")
        if "projects:write" in scopes and "projects:read" not in scopes:
            raise ValueError(
                "创作修改权限同时需要projects:read，因为编辑响应包含完整故事"
            )
        if (
            type(all_projects) is not bool
            or not isinstance(project_ids, (list, tuple))
            or len(project_ids) > 4096
            or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", p)
                for p in project_ids
            )
            or len(set(project_ids)) != len(project_ids)
            or (all_projects and project_ids)
            or (not all_projects and not project_ids)
        ):
            raise ValueError("明确选择全部本人项目或现有项目列表")
        if "projects:create" in scopes and not all_projects:
            raise ValueError("创建故事权限需要选择全部本人项目（包含未来项目）")
        if type(expires_in_days) is not int or not 1 <= expires_in_days <= 365:
            raise ValueError("有效期须为1至365天")
        if (
            not isinstance(authenticated_session, Principal)
            or authenticated_session.machine
            or authenticated_session.owner != owner
            or not self.source_supported(authenticated_session.auth_mode)
            or authenticated_session.password_version is None
            or not authenticated_session.session_hash
        ):
            raise AuthenticationError("登录状态已改变，请重新登录")
        token, now = "sxp_" + secrets.token_urlsafe(32), self.clock()
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE; take its write reservation before
                # reading authority, including local-test accounts without rows.
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            # Same lock order as password rotation: account, then session/key.
            # A request authenticated before rotation/logout must not mint a key
            # stamped with the replacement password's newer authority.
            account = self._grant_account(
                conn,
                owner,
                authenticated_session.auth_mode,
                authenticated_session.password_version,
                lock=True,
            )
            if account is None:
                raise AuthenticationError("账户状态已改变，请重新登录")
            session = conn.execute(
                select(sessions.c.token_hash)
                .where(
                    sessions.c.tenant == self.tenant,
                    sessions.c.token_hash == authenticated_session.session_hash,
                    sessions.c.username == owner,
                    sessions.c.auth_mode == authenticated_session.auth_mode,
                    sessions.c.expires > self.clock(),
                    sessions.c.password_version
                    == authenticated_session.password_version,
                )
                .with_for_update()
            ).first()
            if not session:
                raise AuthenticationError("账户状态已改变，请重新登录")
            active = conn.execute(
                select(personal_keys.c.id).where(
                    personal_keys.c.tenant == self.tenant,
                    personal_keys.c.owner == owner,
                    personal_keys.c.revoked_at.is_(None),
                    personal_keys.c.expires_at > now,
                )
            ).all()
            if len(active) >= 50:
                raise ValueError("每个账户最多50个有效API key，请先撤销不再使用的key")
            row = dict(
                tenant=self.tenant,
                id="key-" + uuid.uuid4().hex,
                token_hash=digest(token),
                prefix=token[:12],
                owner=owner,
                name=name.strip(),
                scopes=json.dumps(scopes),
                projects=json.dumps(project_ids),
                all_projects=int(all_projects),
                auth_mode=authenticated_session.auth_mode,
                password_version=authenticated_session.password_version,
                created_at=now,
                expires_at=now + expires_in_days * 86400,
                last_used_at=None,
                revoked_at=None,
            )
            conn.execute(insert(personal_keys).values(**row))
        return {**self.public_key(row), "api_key": token}

    def revoke_key(self, owner, key_id):
        with self.engine.begin() as conn:
            from .spending_limits import lock_owner

            lock_owner(conn, self.tenant, owner)
            row = (
                conn.execute(
                    select(personal_keys).where(
                        personal_keys.c.tenant == self.tenant,
                        personal_keys.c.owner == owner,
                        personal_keys.c.id == key_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            if row["revoked_at"] is None:
                conn.execute(
                    update(personal_keys)
                    .where(
                        personal_keys.c.tenant == self.tenant,
                        personal_keys.c.owner == owner,
                        personal_keys.c.id == key_id,
                    )
                    .values(revoked_at=self.clock())
                )
            return {"id": key_id, "revoked": True}

    def bearer(self, token):
        # Operator-registered legacy tokens had no reserved prefix. Keep their
        # original identity even if one happens to begin with the new prefix.
        legacy = self.static_bearer(token)
        if legacy:
            return legacy
        return (
            self.personal_bearer(token)
            if isinstance(token, str) and token.startswith("sxp_")
            else None
        )

    def personal_bearer(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        return self._personal_grant(personal_keys.c.token_hash == digest(token))

    def _personal_grant(self, predicate):
        now = self.clock()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(personal_keys).where(
                        personal_keys.c.tenant == self.tenant,
                        predicate,
                        personal_keys.c.revoked_at.is_(None),
                        personal_keys.c.expires_at > now,
                        personal_keys.c.auth_mode.in_((self.mode, "workos")),
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            if (
                self._grant_account(
                    conn,
                    row["owner"],
                    row["auth_mode"],
                    row["password_version"],
                    check_identity=False,  # Fresh hydrate below checks legacy status/role.
                )
                is None
            ):
                return None
            from .identity import hydrate

            principal = hydrate(
                Principal(
                    row["owner"],
                    "key:" + row["id"],
                    True,
                    tuple(json.loads(row["projects"])),
                    tuple(json.loads(row["scopes"])),
                    bool(row["all_projects"]),
                    auth_mode=row["auth_mode"],
                    password_version=row["password_version"],
                ),
                conn,
                self.tenant,
            )
            if principal is None:
                return None
            from .agent_management import paused, doc_where
            from .repository import documents

            if paused(conn, self.tenant, row['owner'], row['id']):
                return None
            pause_exists = (
                select(documents.c.document_id)
                .where(
                    *doc_where(
                        self.tenant, row['owner'], 'connection_key_state', row['id']
                    ),
                    documents.c.payload['paused'].as_boolean() == True,
                )
                .exists()
            )
            # Conditional update makes revocation racing authentication fail closed.
            changed = conn.execute(
                update(personal_keys)
                .where(
                    personal_keys.c.tenant == self.tenant,
                    personal_keys.c.id == row["id"],
                    personal_keys.c.revoked_at.is_(None),
                    personal_keys.c.expires_at > now,
                    ~pause_exists,
                )
                .values(last_used_at=now)
            )
            if changed.rowcount != 1:
                return None
            return principal

    def static_bearer(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        return self._static_grant(clients.c.token_hash == digest(token))

    def workflow_principal(self, owner, grant):
        """Revalidate an accepted workflow's original grant without a token.

        Controller-only; no HTTP route exposes this method. A workflow cannot
        prolong a browser session, inherit an owner's browser permissions, or
        recover a paused/revoked/expired connection by copying old scopes.
        """
        fields = {
            "actor_id",
            "machine",
            "auth_mode",
            "password_version",
            "session_hash",
        }
        if (
            not isinstance(owner, str)
            or not 1 <= len(owner) <= 80
            or type(grant) is not dict
            or set(grant) != fields
        ):
            raise AuthenticationError("film_actor_grant_unavailable")
        actor = grant["actor_id"]
        principal = None
        if grant["machine"] is False and actor == "browser:" + owner:
            hashed = grant["session_hash"]
            if (
                isinstance(hashed, str)
                and re.fullmatch(r"[0-9a-f]{64}", hashed)
                and self.source_supported(grant["auth_mode"])
            ):
                principal = self._session_hash(hashed)
                if principal is not None and (
                    principal.password_version != grant["password_version"]
                    or principal.auth_mode != grant["auth_mode"]
                ):
                    principal = None
        elif grant["machine"] is True and isinstance(actor, str):
            from sqlalchemy import and_

            if actor.startswith("key:"):
                principal = self._personal_grant(
                    and_(
                        personal_keys.c.id == actor[4:], personal_keys.c.owner == owner
                    )
                )
            elif actor.startswith("client:"):
                principal = self._static_grant(
                    and_(clients.c.id == actor[7:], clients.c.owner == owner)
                )
        if (
            principal is None
            or principal.owner != owner
            or principal.actor_id != actor
            or principal.machine is not grant["machine"]
            or grant["machine"] is True
            and grant["auth_mode"] is not None
            and (
                principal.auth_mode != grant["auth_mode"]
                or principal.password_version != grant["password_version"]
            )
        ):
            raise AuthenticationError("film_actor_grant_unavailable")
        return principal

    def _static_grant(self, predicate):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(clients).where(
                        clients.c.tenant == self.tenant,
                        predicate,
                        clients.c.disabled == 0,
                    )
                )
                .mappings()
                .first()
            )
            source = "workos" if row and row["owner"] not in USERS else None
            if row and source == "workos":
                if self._grant_account(conn, row["owner"], "workos", 0.0) is None:
                    return None
            elif row and self.mode == "password":
                active = conn.execute(
                    select(accounts.c.username).where(
                        accounts.c.tenant == self.tenant,
                        accounts.c.username == row["owner"],
                        accounts.c.disabled == 0,
                    )
                ).first()
                if not active or not self.ready(conn):
                    return None
            if not row:
                return None
            from .identity import hydrate

            return hydrate(
                Principal(
                    row["owner"],
                    "client:" + row["id"],
                    True,
                    tuple(json.loads(row["projects"])),
                    tuple(json.loads(row["scopes"])),
                    auth_mode=source,
                    password_version=0.0 if source == "workos" else None,
                ),
                conn,
                self.tenant,
            )

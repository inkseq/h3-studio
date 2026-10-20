"""Pinned AuthKit integration with local sessions and portable account ownership."""

import base64
from contextlib import contextmanager
import hmac
import json
import re
import secrets
from urllib.parse import unquote, urlsplit
import uuid

from sqlalchemy import and_, delete, func, insert, select, update

from .auth import AuthenticationError, Principal, accounts, digest, sessions
from .identity import lock_account_gate
from .identity_schema import IdentitySchemaError, users, validate_connection
from .repository import documents
from .schema_version import IDENTITY_REVISIONS, SchemaVersionError, read_schema_version
from .workos_config import load_credentials

STATE_COOKIE = "__Host-inkseq-workos"
STATE_SECONDS = 600
STATE_CLEANUP_LIMIT = 100
CONTROL_OWNER = "__authkit"
CONTROL_PROJECT = "__authkit"
STATE_KIND = "authkit_state"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")
MESSAGES = {
    "workos_not_configured": "Hosted sign-in is unavailable.",
    "workos_schema_not_ready": "Hosted sign-in is not ready.",
    "workos_state_invalid": "Sign-in expired or changed. Please start again.",
    "workos_provider_unavailable": "The sign-in provider is unavailable. Please start again.",
    "workos_verification_required": "Verify your email in AuthKit, then sign in again.",
    "workos_identity_conflict": "This identity belongs to another account. Use the original account.",
    "workos_link_required": "Sign in to the original account before linking this identity.",
    "workos_link_session_changed": "The original account session changed. Start linking again.",
    "workos_sign_in_cancelled": "Sign-in was not completed. Please try again.",
    "account_suspended": "This account is suspended. Contact an administrator.",
    "account_unavailable": "The account is unavailable.",
    "browser_required": "Use your browser session for this account operation.",
    "profile_invalid": "Provide a display name of 1 to 200 characters.",
    "return_to_invalid": "Use a same-origin relative return path.",
}


class WorkOSError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        self.detail = MESSAGES[code]
        super().__init__(code)


def return_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise WorkOSError("return_to_invalid")
    # Reject encoded delimiters/control bytes, including double-encoded paths,
    # rather than interpreting a different navigation after a later decode.
    if re.search(r"%(?:25|2f|5c|0[0-9a-f]|1[0-9a-f]|7f)", value, re.I):
        raise WorkOSError("return_to_invalid")
    decoded = unquote(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in decoded
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        or decoded != decoded.strip()
    ):
        raise WorkOSError("return_to_invalid")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise WorkOSError("return_to_invalid")
    return value


def _cookie(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _read_cookie(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,1500}", value
        ):
            raise ValueError
        result = json.loads(
            base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        )
        if (
            type(result) is not dict
            or set(result) != {"state", "verifier", "binding"}
            or any(
                not isinstance(part, str) or TOKEN.fullmatch(part) is None
                for part in result.values()
            )
        ):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise WorkOSError("workos_state_invalid") from None


def _client(config):
    from workos import WorkOSClient

    return WorkOSClient(
        api_key=config["api_key"],
        client_id=config["client_id"],
        base_url="https://api.workos.com",
        request_timeout=15,
        max_retries=0,
    )


class WorkOSAuth:
    def __init__(self, repository, auth, settings, *, sdk_factory=_client):
        self.repo, self.auth, self.settings = repository, auth, settings
        self.sdk_factory = sdk_factory

    def config(self):
        try:
            value = load_credentials(getattr(self.settings, "workos_config_file", None))
            if value is None:
                raise ValueError
            return value
        except (OSError, UnicodeError, ValueError, TypeError):
            raise WorkOSError("workos_not_configured", 503) from None

    def _ready(self, connection):
        try:
            if read_schema_version(
                connection
            ) not in IDENTITY_REVISIONS or not validate_connection(connection):
                raise WorkOSError("workos_schema_not_ready", 503)
        except (IdentitySchemaError, SchemaVersionError):
            raise WorkOSError("workos_schema_not_ready", 503) from None

    def enabled(self):
        try:
            self.config()
            with self.repo.engine.connect() as connection:
                self._ready(connection)
            return True
        except WorkOSError:
            return False

    @contextmanager
    def sdk(self, config):
        client = None
        try:
            client = self.sdk_factory(config)
            yield client
        except Exception:
            raise WorkOSError("workos_provider_unavailable", 503) from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _legacy(self, connection, principal, expected_hash=None):
        allowed = {"password"} | (
            {"local-test"} if self.auth.mode == "local-test" else set()
        )
        if (
            not isinstance(principal, Principal)
            or principal.machine
            or principal.auth_mode not in allowed
            or not principal.session_hash
            or expected_hash is not None
            and not hmac.compare_digest(principal.session_hash, expected_hash)
        ):
            raise WorkOSError("workos_link_session_changed", 409)
        # Match password rotation's account-before-session lock order. Recheck
        # in the upsert transaction after the provider response, too.
        account = (
            connection.execute(
                select(accounts)
                .where(
                    accounts.c.tenant == self.settings.tenant_id,
                    accounts.c.username == principal.owner,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        session = (
            connection.execute(
                select(sessions)
                .where(
                    sessions.c.tenant == self.settings.tenant_id,
                    sessions.c.token_hash == principal.session_hash,
                    sessions.c.username == principal.owner,
                    sessions.c.auth_mode == principal.auth_mode,
                    sessions.c.expires > self.auth.clock(),
                    sessions.c.password_version == principal.password_version,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            session is None
            or principal.auth_mode == "password"
            and (
                account is None
                or account["disabled"]
                or account["updated"] != session["password_version"]
            )
        ):
            raise WorkOSError("workos_link_session_changed", 409)
        return principal.owner

    def start(self, *, origin, return_to="/", link=False, principal=None):
        destination, config = return_path(return_to), self.config()
        with self.sdk(config) as client:
            pair = client.pkce.generate()
            state = secrets.token_urlsafe(32)
            url = client.user_management.get_authorization_url(
                provider="authkit",
                redirect_uri=origin + "/api/auth/workos/callback",
                state=state,
                code_challenge=pair.code_challenge,
                code_challenge_method="S256",
            )
        binding = secrets.token_urlsafe(32)
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            self._ready(connection)
            if link:
                self._legacy(connection, principal)
            now = self.repo.clock()
            namespace = and_(
                documents.c.tenant_id == self.settings.tenant_id,
                documents.c.owner_id == CONTROL_OWNER,
                documents.c.project_id == CONTROL_PROJECT,
                documents.c.kind == STATE_KIND,
            )
            # Opportunistic, bounded retention of this private auth namespace.
            # Keep fresh/active transactions and every business document intact.
            old = connection.execute(
                select(documents.c.document_id, documents.c.payload)
                .where(namespace, documents.c.updated_at <= now - STATE_SECONDS)
                .order_by(documents.c.updated_at, documents.c.document_id)
                .limit(STATE_CLEANUP_LIMIT)
            ).all()
            expired = [
                ident
                for ident, payload in old
                if type(payload) is dict
                and type(payload.get("expires_at")) in (int, float)
                and payload["expires_at"] <= now
            ]
            if expired:
                connection.execute(
                    delete(documents).where(
                        namespace, documents.c.document_id.in_(expired)
                    )
                )
            connection.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    owner_id=CONTROL_OWNER,
                    project_id=CONTROL_PROJECT,
                    kind=STATE_KIND,
                    document_id=digest(state),
                    version=1,
                    payload={
                        "browser_hash": digest(binding),
                        "client_hash": digest(config["client_id"]),
                        "origin_hash": digest(origin),
                        "expires_at": now + STATE_SECONDS,
                        "purpose": "link" if link else "signin",
                        "return_to": destination,
                        "legacy_session_hash": principal.session_hash if link else None,
                    },
                    updated_at=now,
                )
            )
        return {
            "url": url,
            "cookie": _cookie(
                {"state": state, "verifier": pair.code_verifier, "binding": binding}
            ),
        }

    def _consume(self, *, state, cookie, origin, config, principal):
        material = _read_cookie(cookie)
        if (
            not isinstance(state, str)
            or TOKEN.fullmatch(state) is None
            or not hmac.compare_digest(state, material["state"])
        ):
            raise WorkOSError("workos_state_invalid")
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            self._ready(connection)
            where = and_(
                documents.c.tenant_id == self.settings.tenant_id,
                documents.c.owner_id == CONTROL_OWNER,
                documents.c.project_id == CONTROL_PROJECT,
                documents.c.kind == STATE_KIND,
                documents.c.document_id == digest(state),
            )
            row = (
                connection.execute(select(documents).where(where).with_for_update())
                .mappings()
                .first()
            )
            value = row["payload"] if row else {}
            if (
                not row
                or value["expires_at"] <= self.repo.clock()
                or not hmac.compare_digest(
                    value["browser_hash"], digest(material["binding"])
                )
                or not hmac.compare_digest(
                    value["client_hash"], digest(config["client_id"])
                )
                or not hmac.compare_digest(value["origin_hash"], digest(origin))
            ):
                raise WorkOSError("workos_state_invalid")
            if value["purpose"] == "link":
                self._legacy(connection, principal, value["legacy_session_hash"])
            connection.execute(
                update(documents)
                .where(where)
                .values(
                    payload={**value, "expires_at": 0},
                    version=row["version"] + 1,
                    updated_at=self.repo.clock(),
                )
            )
        return value, material["verifier"]

    @staticmethod
    def _provider_user(user):
        subject, email = getattr(user, "id", None), getattr(user, "email", None)
        if (
            not isinstance(subject, str)
            or not re.fullmatch(r"user_[A-Za-z0-9]{10,80}", subject)
            or not isinstance(email, str)
            or not 3 <= len(email) <= 320
            or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
        ):
            raise WorkOSError("workos_provider_unavailable", 503)
        if getattr(user, "email_verified", False) is not True:
            raise WorkOSError("workos_verification_required", 403)
        names = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        name = (
            " ".join(
                part.strip() for part in names if isinstance(part, str) and part.strip()
            )
            or email.split("@", 1)[0]
        )
        if len(name) > 200 or any(ord(c) < 32 for c in name):
            name = email.split("@", 1)[0][:200]
        return {"subject": subject, "email": email.lower(), "display_name": name}

    def _upsert(self, identity, transaction, principal):
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            self._ready(connection)
            target = (
                connection.execute(
                    select(users)
                    .where(
                        users.c.tenant == self.settings.tenant_id,
                        users.c.workos_user_id == identity["subject"],
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if transaction["purpose"] == "link":
                owner = self._legacy(
                    connection, principal, transaction["legacy_session_hash"]
                )
                legacy = (
                    connection.execute(
                        select(users)
                        .where(
                            users.c.tenant == self.settings.tenant_id,
                            users.c.owner == owner,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if legacy is None or legacy["status"] != "active":
                    raise WorkOSError("account_suspended", 403)
                if (
                    target is not None
                    and target["id"] != legacy["id"]
                    or legacy["workos_user_id"] not in {None, identity["subject"]}
                ):
                    raise WorkOSError("workos_identity_conflict", 409)
                target = legacy
            if target is not None:
                if target["status"] != "active":
                    raise WorkOSError("account_suspended", 403)
                if target["workos_user_id"] is not None:
                    return dict(target)
            email_owner = (
                connection.execute(
                    select(users.c.id).where(
                        users.c.tenant == self.settings.tenant_id,
                        func.lower(users.c.email) == identity["email"],
                    )
                )
                .scalars()
                .all()
            )
            if any(
                ident != (target["id"] if target else None) for ident in email_owner
            ):
                raise WorkOSError("workos_link_required", 409)
            now = self.repo.clock()
            if target is not None:
                connection.execute(
                    update(users)
                    .where(users.c.id == target["id"])
                    .values(
                        workos_user_id=identity["subject"],
                        email=identity["email"],
                        email_verified=1,
                        updated_at=now,
                    )
                )
                return dict(
                    target,
                    workos_user_id=identity["subject"],
                    email=identity["email"],
                    email_verified=1,
                )
            ident = str(uuid.uuid4())
            row = {
                "id": ident,
                "tenant": self.settings.tenant_id,
                "owner": "u_" + uuid.UUID(ident).hex,
                "role": "user",
                "status": "active",
                "display_name": identity["display_name"],
                "email": identity["email"],
                "email_verified": 1,
                "workos_user_id": identity["subject"],
                "created_at": now,
                "updated_at": now,
                "terms_version": None,
                "terms_accepted_at": None,
            }
            connection.execute(insert(users).values(**row))
            from .credit_schema import ready as credits_ready

            if credits_ready(connection):
                from .credits import grant
                from .credits_pricing import current
                from .welcome_eligibility import eligible_email

                price = current(connection, self.settings.tenant_id)
                welcome = price["configuration"]["welcome_credits"]
                if (
                    price["revision"] is not None
                    and welcome
                    and eligible_email(identity["email"])
                ):
                    grant(
                        connection,
                        self.settings.tenant_id,
                        row["owner"],
                        welcome,
                        actor="system:authkit-welcome",
                        reason="New account welcome credits",
                        idempotency_key="welcome:" + ident,
                        now=now,
                        reference_type="signup",
                        reference_id=ident,
                        price_revision=price["revision"],
                        origin="workos",
                    )
            return row

    def callback(self, *, code, state, cookie, origin, principal=None, cancelled=False):
        config = self.config()
        transaction, verifier = self._consume(
            state=state,
            cookie=cookie,
            origin=origin,
            config=config,
            principal=principal,
        )
        if cancelled:
            raise WorkOSError("workos_sign_in_cancelled")
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 4000
            or any(ord(c) < 33 for c in code)
        ):
            raise WorkOSError("workos_state_invalid")
        with self.sdk(config) as client:
            result = client.user_management.authenticate_with_code(
                code=code, code_verifier=verifier
            )
        identity = self._provider_user(getattr(result, "user", None))
        row = self._upsert(identity, transaction, principal)
        try:
            token = self.auth.issue_workos_session(row["owner"], identity["subject"])
        except AuthenticationError:
            # A suspension may commit between canonical upsert and issuance.
            # Keep that refusal finite and never expose internal exception text.
            raise WorkOSError("account_unavailable", 403) from None
        return {"token": token, "return_to": transaction["return_to"]}

    def _account(self, connection, principal, *, lock=False):
        if not isinstance(principal, Principal) or principal.machine:
            raise WorkOSError("browser_required", 403)
        self._ready(connection)
        statement = select(users).where(
            users.c.tenant == self.settings.tenant_id, users.c.owner == principal.owner
        )
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None or row["status"] != "active":
            raise WorkOSError("account_unavailable", 403)
        return dict(row)

    @staticmethod
    def public(row):
        return {
            key: row[key]
            for key in (
                "id",
                "display_name",
                "email",
                "role",
                "status",
                "created_at",
                "terms_version",
                "terms_accepted_at",
            )
        } | {
            "username": row["owner"],
            "email_verified": bool(row["email_verified"]),
            "authkit_connected": row["workos_user_id"] is not None,
        }

    def account(self, principal):
        from .content_policy import account_terms_metadata

        with self.repo.engine.connect() as connection:
            row = self._account(connection, principal)
            return self.public(row) | {
                "terms": account_terms_metadata(
                    connection, self.settings.tenant_id, row["owner"]
                )
            }

    def update_account(self, principal, body):
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            row = self._account(connection, principal, lock=True)
            if (
                type(body) is not dict
                or set(body) != {"display_name"}
                or not isinstance(body["display_name"], str)
                or not 1 <= len(body["display_name"].strip()) <= 200
                or any(ord(c) < 32 for c in body["display_name"])
            ):
                raise WorkOSError("profile_invalid", 422)
            name = body["display_name"].strip()
            if row["display_name"] != name:
                connection.execute(
                    update(users)
                    .where(users.c.id == row["id"])
                    .values(display_name=name, updated_at=self.repo.clock())
                )
                row["display_name"] = name
            return self.public(row)

    def widget_token(self, principal):
        with self.repo.engine.connect() as connection:
            row = self._account(connection, principal)
        if row["workos_user_id"] is None:
            raise WorkOSError("workos_link_required", 409)
        with self.sdk(self.config()) as client:
            result = client.widgets.create_token(
                user_id=row["workos_user_id"], scopes=[]
            )
        with self.repo.engine.connect() as connection:
            current = self._account(connection, principal)
        if current["workos_user_id"] != row["workos_user_id"]:
            raise WorkOSError("account_unavailable", 403)
        token = getattr(result, "token", None)
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise WorkOSError("workos_provider_unavailable", 503)
        return {"token": token}

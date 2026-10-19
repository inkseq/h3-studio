"""Fixed, owner-authorized QA identities; never enabled on production."""

import json
import re
import uuid

import bcrypt
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import insert, select

from .auth import PASSWORD_HASH, accounts
from .identity import lock_account_gate
from .identity_admin import AdminError, IdentityAdmin
from .identity_schema import admin_audit, users

ORIGIN = "https://staging.inkseq.com"
QA_ROLES = {"qa-admin": "admin", "qa-user": "user"}
PASSWORD = re.compile(r"[A-Za-z0-9_-]{43,64}\Z")
GRANT_KEY = "staging-qa-user-initial-credits-v1"
GRANT_REASON = "Owner-authorized staging QA acceptance credits"


class StagingQAError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def enabled(settings):
    return (
        getattr(settings, "deployment_target", "production") == "staging"
        and settings.public_origin == ORIGIN
        and not settings.public_origin_aliases
        and settings.auth_mode == "password"
    )


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_field")
        value[key] = item
    return value


class StagingQA:
    def __init__(self, repository, settings):
        self.repo, self.settings = repository, settings
        self.admin = IdentityAdmin(repository, settings)

    @staticmethod
    def _audit(connection, actor, user, *, action, before, after, now):
        connection.execute(
            insert(admin_audit).values(
                id=str(uuid.uuid4()),
                tenant=user["tenant"],
                actor_user_id=actor["id"],
                target_user_id=user["id"],
                action=action,
                reason="Owner-authorized staging-only QA acceptance",
                before=before,
                after={**after, "is_test": True, "username": user["owner"]},
                created_at=now,
            )
        )

    def provision(self, principal, body):
        # This fence precedes body handling, hashing, schema reads and writes.
        if not enabled(self.settings):
            raise StagingQAError("staging_qa_unavailable", 404)
        if (
            type(body) is not dict
            or set(body) != {"accounts", "grant_test_credits"}
            or type(body["grant_test_credits"]) is not bool
            or type(body["accounts"]) is not list
            or len(body["accounts"]) != 2
        ):
            raise StagingQAError("staging_qa_request_invalid", 422)
        supplied = {}
        for row in body["accounts"]:
            if (
                type(row) is not dict
                or set(row) != {"username", "password", "role"}
                or not isinstance(row["username"], str)
                or row["username"] not in QA_ROLES
                or row["username"] in supplied
                or row["role"] != QA_ROLES[row["username"]]
                or not isinstance(row["password"], str)
                or not PASSWORD.fullmatch(row["password"])
            ):
                raise StagingQAError("staging_qa_request_invalid", 422)
            supplied[row["username"]] = row
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            try:
                actor = self.admin._actor(connection, principal)
            except AdminError as error:
                raise StagingQAError("admin_required", error.status) from None
            if body["grant_test_credits"]:
                from .credit_schema import ready

                if not ready(connection):
                    raise StagingQAError("staging_qa_credits_not_ready", 503)
            result, canonical = [], {}
            now = self.repo.clock()
            for username, role in QA_ROLES.items():
                user = (
                    connection.execute(
                        select(users)
                        .where(
                            users.c.tenant == self.settings.tenant_id,
                            users.c.owner == username,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                credential = (
                    connection.execute(
                        select(accounts)
                        .where(
                            accounts.c.tenant == self.settings.tenant_id,
                            accounts.c.username == username,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                created = user is None and credential is None
                if not created:
                    if (
                        user is None
                        or credential is None
                        or user["role"] != role
                        or user["status"] != "active"
                        or user["workos_user_id"] is not None
                        or credential["disabled"]
                        or not PASSWORD_HASH.fullmatch(credential["password_hash"])
                        or not bcrypt.checkpw(
                            supplied[username]["password"].encode("ascii"),
                            credential["password_hash"].encode("ascii"),
                        )
                    ):
                        raise StagingQAError("staging_qa_existing_account_changed")
                else:
                    user = {
                        "id": str(uuid.uuid4()),
                        "tenant": self.settings.tenant_id,
                        "owner": username,
                        "role": role,
                        "status": "active",
                        "display_name": username,
                        "email_verified": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                    connection.execute(insert(users).values(**user))
                    encoded = bcrypt.hashpw(
                        supplied[username]["password"].encode("ascii"),
                        bcrypt.gensalt(rounds=12),
                    ).decode("ascii")
                    connection.execute(
                        insert(accounts).values(
                            tenant=self.settings.tenant_id,
                            username=username,
                            password_hash=encoded,
                            disabled=0,
                            updated=now,
                        )
                    )
                    self._audit(
                        connection,
                        actor,
                        user,
                        action="staging QA account provisioned",
                        before={},
                        after={"role": role, "status": "active"},
                        now=now,
                    )
                canonical[username] = dict(user)
                result.append(
                    {
                        "id": user["id"],
                        "username": username,
                        "role": role,
                        "created": created,
                    }
                )
            grant = None
            if body["grant_test_credits"]:
                from .credit_schema import entries
                from .credits import grant as grant_credits

                user = canonical["qa-user"]
                prior_actor = connection.execute(
                    select(entries.c.actor).where(
                        entries.c.tenant == self.settings.tenant_id,
                        entries.c.user_id == user["id"],
                        entries.c.idempotency_key == GRANT_KEY,
                    )
                ).scalar_one_or_none()
                applied = grant_credits(
                    connection,
                    self.settings.tenant_id,
                    "qa-user",
                    2000,
                    actor=prior_actor or principal.actor_id,
                    reason=GRANT_REASON,
                    idempotency_key=GRANT_KEY,
                    now=now,
                    reference_type="qa_provisioning",
                    reference_id=user["id"],
                    origin="staging-qa",
                )
                if applied["created"]:
                    self._audit(
                        connection,
                        actor,
                        user,
                        action="staging QA credits granted",
                        before={"balance": applied["account"]["balance"] - 2000},
                        after={
                            "balance": applied["account"]["balance"],
                            "credits": 2000,
                        },
                        now=now,
                    )
                grant = {
                    "created": applied["created"],
                    "credits": 2000,
                    "entry_id": applied["entry"]["id"],
                }
            return {"accounts": result, "credit_grant": grant}


def register_routes(app):
    if not enabled(app.state.settings):
        return
    service = StagingQA(app.state.repository, app.state.settings)

    @app.post("/v1/admin/staging/qa-accounts")
    async def provision(request: Request):
        try:
            raw = await request.body()
            if len(raw) > 4096:
                raise StagingQAError("staging_qa_request_invalid", 422)
            try:
                body = json.loads(raw, object_pairs_hook=unique_pairs)
            except (ValueError, UnicodeError):
                raise StagingQAError("staging_qa_request_invalid", 422) from None
            value = service.provision(request.state.principal, body)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except StagingQAError as error:
            return JSONResponse(
                {"code": error.code, "detail": error.code},
                status_code=error.status,
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            # Never serialize provider, SQL, request-body or credential errors.
            return JSONResponse(
                {
                    "code": "staging_qa_provision_failed",
                    "detail": "Staging QA provisioning failed.",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

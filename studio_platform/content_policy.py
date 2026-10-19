"""Versioned policy, human terms and bounded moderation in existing records.

No provider is selected by a URL, secret or dynamic import. Prompt keywords do
not classify video. The trusted output registry is deliberately empty by default.
"""

import copy
import math
import re
import time
import uuid

from sqlalchemy import and_, insert, select, update

from .auth import Principal
from .identity import account_identity, lock_account_gate
from .identity_schema import admin_audit, users
from .repository import documents, request_hash

# Trusted callable contract: (repo, original_job, attempt_id, canonical_specs)
# -> "allowed" | "refused". Production adapters must bound all outbound IO;
# failure retains the original result for retry. An empty registry selects no
# provider and claims no media-screening capability.
CLASSIFIERS = {}
POLICY_SCOPE = dict(
    owner_id="__content_policy",
    project_id="__content_policy",
    kind="content_policy",
    document_id="active",
)
DEFAULT_POLICY = {
    "revision": None,
    "version": "2026-10-draft",
    "status": "draft",
    "moderation": {"enabled": False, "keywords": [], "output_classifier": None},
}
RECEIPT_FIELDS = {
    "status",
    "policy_revision",
    "policy_version",
    "input_hash",
    "reason_code",
}


class ContentPolicyError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        self.detail = {
            "content_policy_invalid": "Provide a valid versioned content policy.",
            "content_policy_unavailable": "The content policy is unavailable. Please retry later.",
            "content_policy_changed": "The policy changed. Refresh before saving.",
            "idempotency_key_required": "Provide an Idempotency-Key.",
            "idempotency_conflict": "This request key was already used for another change.",
            "human_account_required": "Sign in with your personal account.",
            "admin_required": "An active administrator is required.",
            "account_unavailable": "This account is unavailable.",
            "terms_not_reviewed": "The terms are still a draft and cannot be accepted yet.",
            "terms_version_changed": "The terms changed. Read the current version before accepting.",
            "terms_acceptance_required": "Accept the current terms in your account before starting paid work.",
            "content_moderation_refused": "This request was refused by the configured content policy. Review the content policy before retrying.",
            "content_moderation_changed": "The content policy changed. Run preflight again.",
            "content_moderation_unavailable": "Content screening is unavailable. The original result is retained for retry.",
            "content_moderation_receipt_invalid": "Content screening evidence is missing or does not match this result.",
        }.get(code, "The content-policy request could not be completed.")
        super().__init__(code)


def _need(condition, code="content_policy_invalid", status=422):
    if not condition:
        raise ContentPolicyError(code, status)


def _token(value, maximum=64):
    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9_.:-]{1," + str(maximum) + "}", value
    )


def _where(tenant, scope=POLICY_SCOPE):
    return and_(
        documents.c.tenant_id == tenant,
        *[documents.c[key] == value for key, value in scope.items()],
    )


def _validate(value, *, selectable=False):
    _need(
        type(value) is dict
        and set(value) == {"revision", "version", "status", "moderation"}
    )
    _need(value["revision"] is None or _token(value["revision"]))
    _need(
        _token(value["version"])
        and isinstance(value["status"], str)
        and value["status"] in {"draft", "reviewed"}
    )
    mode = value["moderation"]
    _need(
        type(mode) is dict
        and set(mode) == {"enabled", "keywords", "output_classifier"}
        and type(mode["enabled"]) is bool
        and type(mode["keywords"]) is list
        and len(mode["keywords"]) <= 100
    )
    keywords = mode["keywords"]
    _need(
        all(
            isinstance(word, str)
            and 1 <= len(word.strip()) <= 100
            and word == word.strip()
            and not any(ord(c) < 32 for c in word)
            for word in keywords
        )
        and len({word.casefold() for word in keywords}) == len(keywords)
    )
    classifier = mode["output_classifier"]
    _need(classifier is None or _token(classifier))
    if selectable:
        _need(classifier is None or callable(CLASSIFIERS.get(classifier)))
    return copy.deepcopy(value)


def load_policy(connection, tenant):
    value = connection.execute(
        select(documents.c.payload).where(_where(tenant))
    ).scalar_one_or_none()
    if value is None:
        return copy.deepcopy(DEFAULT_POLICY)
    try:
        _need(type(value) is dict and _token(value.get("revision")))
        return _validate(value)
    except ContentPolicyError:
        raise ContentPolicyError("content_policy_unavailable", 503) from None


def _put(connection, repo, tenant, scope, value):
    row = connection.execute(
        select(documents.c.version).where(_where(tenant, scope))
    ).first()
    if row is None:
        connection.execute(
            insert(documents).values(
                tenant_id=tenant,
                **scope,
                version=1,
                payload=value,
                updated_at=repo.clock(),
            )
        )
    else:
        connection.execute(
            update(documents)
            .where(_where(tenant, scope))
            .values(payload=value, version=row.version + 1, updated_at=repo.clock())
        )


def _user(connection, tenant, owner, *, principal=None, admin=False):
    if principal is not None:
        _need(
            isinstance(principal, Principal)
            and not principal.machine
            and principal.owner == owner,
            "human_account_required",
            403,
        )
    identity = account_identity(connection, tenant, owner)
    _need(
        identity is not None
        and identity.get("status") == "active"
        and (not admin or identity.get("role") == "admin"),
        "admin_required" if admin else "account_unavailable",
        403,
    )
    return (
        connection.execute(
            select(users).where(users.c.tenant == tenant, users.c.id == identity["id"])
        )
        .mappings()
        .one()
    )


def _audit(connection, repo, tenant, actor, action, before, after, *, reason=None):
    connection.execute(
        insert(admin_audit).values(
            id=str(uuid.uuid4()),
            tenant=tenant,
            actor_user_id=actor,
            target_user_id=actor,
            action=action,
            reason=reason,
            before=before,
            after=after,
            created_at=repo.clock(),
        )
    )


def public_policy(connection, tenant):
    policy = load_policy(connection, tenant)
    mode = policy["moderation"]
    return {
        **{key: policy[key] for key in ("revision", "version", "status")},
        "moderation": {
            "enabled": mode["enabled"],
            "prompt_screening": mode["enabled"] and bool(mode["keywords"]),
            "output_screening": mode["enabled"]
            and callable(CLASSIFIERS.get(mode["output_classifier"])),
        },
    }


class ContentPolicyAPI:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def public(self):
        with self.repo.engine.connect() as connection:
            return public_policy(connection, self.settings.tenant_id)

    def admin(self, principal):
        _need(isinstance(principal, Principal), "human_account_required", 403)
        with self.repo.engine.connect() as connection:
            _user(
                connection,
                self.settings.tenant_id,
                principal.owner,
                principal=principal,
                admin=True,
            )
            return {
                **load_policy(connection, self.settings.tenant_id),
                "available_classifiers": sorted(
                    key for key, value in CLASSIFIERS.items() if callable(value)
                ),
            }

    def update(self, principal, body, *, key):
        _need(_token(key, 160), "idempotency_key_required")
        _need(
            type(body) is dict
            and set(body)
            == {"expected_revision", "version", "status", "moderation", "reason"}
        )
        reason = body["reason"]
        _need(isinstance(reason, str) and 1 <= len(reason.strip()) <= 500)
        candidate = _validate(
            {
                "revision": None,
                **{k: body[k] for k in ("version", "status", "moderation")},
            },
            selectable=True,
        )
        fingerprint = request_hash(body)
        tenant = self.settings.tenant_id
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            _need(isinstance(principal, Principal), "human_account_required", 403)
            actor = _user(
                connection, tenant, principal.owner, principal=principal, admin=True
            )
            scope = dict(
                owner_id="__content_policy",
                project_id="__content_policy",
                kind="content_policy_action",
                document_id=request_hash({"owner": principal.owner, "key": key}),
            )
            previous = connection.execute(
                select(documents.c.payload).where(_where(tenant, scope))
            ).scalar_one_or_none()
            if previous is not None:
                _need(
                    previous["fingerprint"] == fingerprint, "idempotency_conflict", 409
                )
                return previous["result"]
            old = load_policy(connection, tenant)
            _need(
                body["expected_revision"] == old["revision"],
                "content_policy_changed",
                409,
            )
            candidate["revision"] = str(uuid.uuid4())
            _put(connection, self.repo, tenant, POLICY_SCOPE, candidate)
            _audit(
                connection,
                self.repo,
                tenant,
                actor["id"],
                "content policy updated",
                old,
                candidate,
                reason=reason.strip(),
            )
            _put(
                connection,
                self.repo,
                tenant,
                scope,
                {"fingerprint": fingerprint, "result": candidate},
            )
            return candidate

    def accept_terms(self, principal, body):
        _need(
            isinstance(principal, Principal) and not principal.machine,
            "human_account_required",
            403,
        )
        _need(
            type(body) is dict and set(body) == {"version"} and _token(body["version"])
        )
        tenant = self.settings.tenant_id
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            user = _user(connection, tenant, principal.owner, principal=principal)
            policy = load_policy(connection, tenant)
            _need(policy["status"] == "reviewed", "terms_not_reviewed", 409)
            _need(body["version"] == policy["version"], "terms_version_changed", 409)
            accepted = user["terms_accepted_at"]
            if user["terms_version"] == policy["version"] and _valid_time(
                accepted, self.repo.clock()
            ):
                return {"version": policy["version"], "accepted_at": accepted}
            result = {"version": policy["version"], "accepted_at": self.repo.clock()}
            connection.execute(
                update(users)
                .where(users.c.tenant == tenant, users.c.id == user["id"])
                .values(
                    terms_version=result["version"],
                    terms_accepted_at=result["accepted_at"],
                    updated_at=self.repo.clock(),
                )
            )
            _audit(
                connection,
                self.repo,
                tenant,
                user["id"],
                "terms accepted",
                {"version": user["terms_version"], "accepted_at": accepted},
                result,
                reason="User accepted the current reviewed terms",
            )
            return result


def _valid_time(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now


def account_terms_metadata(connection, tenant, owner):
    policy = load_policy(connection, tenant)
    identity = account_identity(connection, tenant, owner)
    user = (
        connection.execute(
            select(users.c.terms_version, users.c.terms_accepted_at).where(
                users.c.tenant == tenant, users.c.owner == owner
            )
        )
        .mappings()
        .first()
        if identity is not None and identity.get("status") in {"active", "suspended"}
        else None
    )
    accepted_version = user["terms_version"] if user else None
    accepted_at = user["terms_accepted_at"] if user else None
    return {
        "version": policy["version"],
        "status": policy["status"],
        "accepted_version": accepted_version,
        "accepted_at": accepted_at,
        "acceptance_required": policy["status"] == "reviewed"
        and (
            accepted_version != policy["version"]
            or not _valid_time(accepted_at, time.time())
        ),
    }


def reviewed_terms(connection, principal, user):
    _need(
        isinstance(principal, Principal) and not principal.machine,
        "human_account_required",
        403,
    )
    tenant = user["tenant"]
    canonical = _user(connection, tenant, principal.owner, principal=principal)
    policy = load_policy(connection, tenant)
    _need(policy["status"] == "reviewed", "terms_not_reviewed", 503)
    _need(
        canonical["id"] == user["id"]
        and canonical["terms_version"] == policy["version"]
        and _valid_time(canonical["terms_accepted_at"], time.time()),
        "terms_acceptance_required",
        409,
    )
    return {"version": policy["version"], "accepted_at": canonical["terms_accepted_at"]}


def require_generation_terms(connection, tenant, owner, execution, compiled):
    policy = load_policy(connection, tenant)
    if policy["status"] != "reviewed":
        return None
    from .render_plans import RECIPE, MODEL, POOL

    quote = execution.get("credit_quote")
    paid_credits = (
        type(quote) is dict
        and type(quote.get("credits")) is int
        and quote["credits"] > 0
    )
    free = not paid_credits and (
        (execution.get("backend") == "mock" and execution.get("pool") == "mock")
        or (
            execution.get("backend") == "cpu-render"
            and execution.get("pool") == POOL
            and compiled.get("recipe_id") == RECIPE
            and compiled.get("request", {}).get("model") == MODEL
        )
    )
    if free:
        return None
    user = _user(connection, tenant, owner)
    _need(
        user["terms_version"] == policy["version"]
        and _valid_time(user["terms_accepted_at"], time.time()),
        "terms_acceptance_required",
        409,
    )
    return {
        "version": policy["version"],
        "accepted_at": user["terms_accepted_at"],
        "policy_revision": policy["revision"],
    }


def _input_hash(compiled):
    _need(type(compiled) is dict, "content_moderation_receipt_invalid", 409)
    return request_hash(
        {k: value for k, value in compiled.items() if k != "content_moderation"}
    )


def _prompt_refused(policy, compiled):
    request = compiled.get("request", {})
    _need(type(request) is dict, "content_moderation_receipt_invalid", 409)
    prompts = [request.get(key, "") for key in ("prompt", "negative_prompt")]
    _need(
        all(isinstance(value, str) and len(value) <= 12000 for value in prompts),
        "content_moderation_receipt_invalid",
        409,
    )
    return any(
        word.casefold() in prompt.casefold()
        for word in policy["moderation"]["keywords"]
        for prompt in prompts
    )


def _receipt(policy, digest, refused=False, *, output=False):
    return {
        "status": "refused" if refused else "allowed",
        "policy_revision": policy["revision"],
        "policy_version": policy["version"],
        "input_hash": digest,
        "reason_code": ("classifier_refused" if output else "prompt_keyword_refused")
        if refused
        else "allowed",
    }


def _decision_scope(owner, kind, revision, digest):
    return dict(
        owner_id="__content_policy",
        project_id="__content_policy",
        kind=kind,
        document_id=request_hash(
            {"owner": owner, "revision": revision, "hash": digest}
        ),
    )


def _decision(connection, tenant, owner, kind, receipt):
    scope = _decision_scope(
        owner, kind, receipt["policy_revision"], receipt["input_hash"]
    )
    return connection.execute(
        select(documents.c.payload).where(_where(tenant, scope))
    ).scalar_one_or_none()


def _valid_receipt(value):
    return (
        type(value) is dict
        and set(value) == RECEIPT_FIELDS
        and isinstance(value["status"], str)
        and value["status"] in {"allowed", "refused"}
        and _token(value["policy_revision"])
        and _token(value["policy_version"])
        and isinstance(value["input_hash"], str)
        and re.fullmatch(r"[0-9a-f]{64}", value["input_hash"])
        and isinstance(value["reason_code"], str)
        and value["reason_code"]
        in {"allowed", "prompt_keyword_refused", "classifier_refused"}
    )


def _persist_decision(connection, repo, tenant, owner, kind, receipt):
    # Concurrent checks may share the same immutable original. The existing
    # document uniqueness chooses one result without overwriting its first audit.
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as decision_insert
    elif connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as decision_insert
    else:
        raise ContentPolicyError("content_policy_unavailable", 503)
    scope = _decision_scope(
        owner, kind, receipt["policy_revision"], receipt["input_hash"]
    )
    connection.execute(
        decision_insert(documents)
        .values(
            tenant_id=tenant,
            **scope,
            version=1,
            payload=receipt,
            updated_at=repo.clock(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                "tenant_id",
                "owner_id",
                "project_id",
                "kind",
                "document_id",
            ]
        )
    )
    original = _decision(connection, tenant, owner, kind, receipt)
    _need(_valid_receipt(original), "content_moderation_receipt_invalid", 409)
    return original


def check_input(repo, tenant, owner, compiled):
    with repo.transaction() as connection:
        lock_account_gate(connection)
        policy = load_policy(connection, tenant)
        if not policy["moderation"]["enabled"]:
            return None
        receipt = _receipt(
            policy, _input_hash(compiled), _prompt_refused(policy, compiled)
        )
        _need(
            _persist_decision(connection, repo, tenant, owner, "content_input", receipt)
            == receipt,
            "content_moderation_receipt_invalid",
            409,
        )
    if receipt["status"] == "refused":
        raise ContentPolicyError("content_moderation_refused", 422)
    return receipt


def verify_input(connection, tenant, owner, compiled, receipt):
    policy = load_policy(connection, tenant)
    if not policy["moderation"]["enabled"]:
        return None
    if _prompt_refused(policy, compiled):
        raise ContentPolicyError("content_moderation_refused", 422)
    _need(
        _valid_receipt(receipt) and receipt == _receipt(policy, _input_hash(compiled)),
        "content_moderation_changed",
        409,
    )
    _need(
        _decision(connection, tenant, owner, "content_input", receipt) == receipt,
        "content_moderation_receipt_invalid",
        409,
    )
    return receipt


def _output_hash(job, attempt_id, specs):
    _need(
        isinstance(attempt_id, str)
        and attempt_id
        and type(specs) is list
        and bool(specs),
        "content_moderation_receipt_invalid",
        409,
    )
    return request_hash(
        {
            "tenant": job["tenant_id"],
            "owner": job["owner_id"],
            "job": job["id"],
            "request_hash": job["request_hash"],
            "attempt": attempt_id,
            "media": specs,
        }
    )


def check_output(repo, job, attempt_id, specs):
    tenant, owner = job["tenant_id"], job["owner_id"]
    digest = _output_hash(job, attempt_id, specs)
    with repo.engine.connect() as connection:
        policy = load_policy(connection, tenant)
        mode = policy["moderation"]
        if not mode["enabled"] or mode["output_classifier"] is None:
            return None
        probe = _receipt(policy, digest, output=True)
        previous = _decision(connection, tenant, owner, "content_output", probe)
        if previous is not None:
            _need(_valid_receipt(previous), "content_moderation_receipt_invalid", 409)
            return previous
    classifier = CLASSIFIERS.get(mode["output_classifier"])
    _need(callable(classifier), "content_moderation_unavailable", 503)
    try:
        decision = classifier(
            repo, copy.deepcopy(dict(job)), attempt_id, copy.deepcopy(specs)
        )
        if decision not in {"allowed", "refused"}:
            raise ValueError()
    except Exception:
        raise ContentPolicyError("content_moderation_unavailable", 503) from None
    receipt = _receipt(policy, digest, decision == "refused", output=True)
    with repo.transaction() as connection:
        lock_account_gate(connection)
        _need(
            load_policy(connection, tenant) == policy, "content_moderation_changed", 409
        )
        receipt = _persist_decision(
            connection, repo, tenant, owner, "content_output", receipt
        )
    return receipt


def verify_output(connection, job, attempt_id, specs, receipt):
    policy = load_policy(connection, job["tenant_id"])
    mode = policy["moderation"]
    configured = mode["enabled"] and mode["output_classifier"] is not None
    if receipt is None:
        _need(not configured, "content_moderation_receipt_invalid", 409)
        return None
    _need(
        _valid_receipt(receipt)
        and receipt["input_hash"] == _output_hash(job, attempt_id, specs)
        and receipt["reason_code"] != "prompt_keyword_refused",
        "content_moderation_receipt_invalid",
        409,
    )
    if configured:
        _need(
            receipt["policy_revision"] == policy["revision"]
            and receipt["policy_version"] == policy["version"],
            "content_moderation_changed",
            409,
        )
    _need(
        _decision(
            connection, job["tenant_id"], job["owner_id"], "content_output", receipt
        )
        == receipt,
        "content_moderation_receipt_invalid",
        409,
    )
    return copy.deepcopy(receipt)

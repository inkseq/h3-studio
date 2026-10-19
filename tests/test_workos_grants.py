"""Own WorkOS grants on disposable SQL; no provider SDK, network or credentials."""

from dataclasses import replace
import secrets
import uuid

from sqlalchemy import func, insert, select, update

from studio_platform.agent_connect import (
    AgentConnect,
    ConnectError,
    PROFILE_ID,
    PROFILE_VERSION,
    connections,
)
from studio_platform.auth import (
    Auth,
    AuthenticationError,
    accounts,
    digest,
    personal_keys,
    sessions,
)
from studio_platform.identity_schema import admin_audit, users
from test_platform_repository import LedgerCase


class WorkOSGrantTests(LedgerCase):
    def setUp(self):
        super().setUp()
        users.create(self.repo.engine)
        admin_audit.create(self.repo.engine)
        self.owner = "u_" + uuid.uuid4().hex
        self.subject = "user_" + uuid.uuid4().hex
        self.add_user(self.owner, self.subject, role="admin")
        # Public identity works before either legacy password is provisioned.
        self.auth = Auth(self.repo.engine, clock=lambda: self.now)
        self.token = self.auth.issue_workos_session(self.owner, self.subject)
        self.principal = self.auth.session(self.token)
        self.service = AgentConnect(self.auth)
        self.origin = "http://127.0.0.1:8850"

    def add_user(self, owner, subject, *, tenant="sixnine", **extra):
        with self.repo.engine.begin() as connection:
            connection.execute(
                insert(users)
                .values(
                    id=str(uuid.uuid4()),
                    tenant=tenant,
                    owner=owner,
                    role="user",
                    status="active",
                    display_name=owner,
                    workos_user_id=subject,
                    created_at=self.now,
                    updated_at=self.now,
                )
                .values(**extra)
            )

    def change(self, **values):
        with self.repo.engine.begin() as connection:
            connection.execute(
                update(users)
                .where(users.c.tenant == "sixnine", users.c.owner == self.owner)
                .values(**values)
            )

    def count(self, table):
        with self.repo.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()

    def key(self, *, principal=None, owner=None):
        return self.auth.create_key(
            owner or self.owner,
            authenticated_session=principal or self.principal,
            name="workos-agent",
            scopes=["projects:read", "jobs:read", "jobs:write"],
            project_ids=["owned-story"],
        )

    @staticmethod
    def grant(principal):
        return {
            field: getattr(principal, field)
            for field in (
                "actor_id",
                "machine",
                "auth_mode",
                "password_version",
                "session_hash",
            )
        }

    def issue(self, *, principal=None):
        return self.service.issue(
            principal or self.principal,
            name="workos-connect",
            authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION,
            idempotency_key=uuid.uuid4().hex,
            origin=self.origin,
        )

    def material(self, grant):
        token = "sxp_" + secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        return (
            token,
            verifier,
            {
                "code": grant["code"],
                "client_challenge": digest(verifier),
                "token_hash": digest(token),
                "key_prefix": token[:12],
                "source": "disposable-workos-test",
                "expected_authorization_fingerprint": grant["connection"][
                    "authorization_fingerprint"
                ],
                "origin": self.origin,
            },
        )

    def test_session_without_password_or_provider_configuration_slides_own_cookie(self):
        self.assertFalse(self.auth.ready())
        self.assertEqual(self.count(accounts), 0)
        self.assertEqual(self.principal.owner, self.owner)
        self.assertEqual(self.principal.actor_id, "browser:" + self.owner)
        self.assertEqual(self.principal.role, "admin")
        self.assertEqual(self.principal.auth_mode, "workos")
        self.assertEqual(self.principal.password_version, 0.0)
        with self.repo.engine.connect() as connection:
            original = dict(connection.execute(select(sessions)).mappings().one())
        self.assertEqual(original["auth_mode"], "workos")
        self.assertEqual(original["password_version"], 0.0)
        self.assertEqual(original["expires"] - original["created"], 7 * 86400)
        self.assertNotEqual(original["token_hash"], self.token)
        self.now += 7200
        # Auth does not need live WorkOS configuration to read issued grants.
        without_provider = Auth(
            self.repo.engine, mode="local-test", clock=lambda: self.now
        )
        self.assertEqual(without_provider.session(self.token).owner, self.owner)
        with self.repo.engine.connect() as connection:
            current = connection.execute(select(sessions)).mappings().one()
        self.assertEqual(current["auth_mode"], "workos")
        self.assertEqual(current["expires"], self.now + 7 * 86400)
        self.assertEqual(self.count(accounts), 0)

    def test_issuance_requires_exact_active_canonical_subject_and_tenant(self):
        foreign = "u_" + uuid.uuid4().hex
        self.add_user(foreign, "user_foreign", tenant="another")
        for owner, subject in (
            (self.owner, "user_wrong"),
            ("u_missing", self.subject),
            (foreign, "user_foreign"),
            (self.owner, ""),
        ):
            with self.subTest(owner=owner), self.assertRaises(AuthenticationError):
                self.auth.issue_workos_session(owner, subject)
        for values in (
            {"status": "suspended"},
            {"status": "active", "workos_user_id": None},
        ):
            self.change(**values)
            with self.assertRaises(AuthenticationError):
                self.auth.issue_workos_session(self.owner, self.subject)
        self.assertEqual(self.count(sessions), 1)
        self.assertEqual(self.count(accounts), 0)

    def test_role_profile_updates_are_not_credential_epochs(self):
        key = self.key()
        self.change(display_name="Renamed", role="user", updated_at=self.now + 900)
        self.assertEqual(self.auth.session(self.token).role, "user")
        self.assertIsNotNone(self.auth.bearer(key["api_key"]))
        self.change(role="admin", updated_at=self.now + 1800)
        self.assertEqual(self.auth.session(self.token).role, "admin")
        self.assertEqual(self.auth.bearer(key["api_key"]).role, "user")

    def test_personal_key_preserves_source_scopes_and_never_admin(self):
        key = self.key()
        principal = self.auth.bearer(key["api_key"])
        self.assertEqual(
            (principal.auth_mode, principal.password_version), ("workos", 0.0)
        )
        self.assertEqual(principal.owner, self.owner)
        self.assertEqual(principal.actor_id, "key:" + key["id"])
        self.assertTrue(principal.machine)
        self.assertEqual(principal.role, "user")
        self.assertTrue(principal.allows("owned-story", "jobs:write"))
        self.assertFalse(principal.allows("foreign-story", "jobs:write"))
        self.assertNotIn("operator:admin", principal.scopes)
        without_provider = Auth(
            self.repo.engine, mode="local-test", clock=lambda: self.now
        )
        self.assertEqual(without_provider.bearer(key["api_key"]).owner, self.owner)
        with self.repo.engine.connect() as connection:
            stored = connection.execute(select(personal_keys)).mappings().one()
        self.assertEqual(
            (stored["auth_mode"], stored["password_version"]), ("workos", 0.0)
        )

    def test_stale_or_forged_browser_cannot_mint_personal_keys(self):
        other = "u_" + uuid.uuid4().hex
        self.add_user(other, "user_other")
        for owner, principal in (
            (other, self.principal),
            (self.owner, replace(self.principal, auth_mode="password")),
            (self.owner, replace(self.principal, password_version=self.now)),
            (self.owner, replace(self.principal, session_hash="0" * 64)),
            (self.owner, replace(self.principal, machine=True)),
        ):
            with self.subTest(owner=owner), self.assertRaises(AuthenticationError):
                self.key(owner=owner, principal=principal)
        self.auth.logout(self.token)
        self.assertIsNone(self.auth.session(self.token))
        with self.assertRaises(AuthenticationError):
            self.key()
        self.assertEqual(self.count(personal_keys), 0)

    def test_suspension_and_missing_link_revoke_all_grant_sources_next_read(self):
        key = self.key()
        static = secrets.token_urlsafe(32)
        self.auth.register_client(
            "bound-static", static, self.owner, ["owned-story"], ["jobs:read"]
        )
        grants = [
            self.grant(self.principal),
            self.grant(self.auth.bearer(key["api_key"])),
        ]
        for values in (
            {"status": "suspended"},
            {"status": "active", "workos_user_id": None},
        ):
            self.change(**values)
            self.assertIsNone(self.auth.session(self.token))
            self.assertIsNone(self.auth.bearer(key["api_key"]))
            self.assertIsNone(self.auth.bearer(static))
            for grant in grants:
                with self.assertRaisesRegex(
                    AuthenticationError, "film_actor_grant_unavailable"
                ):
                    self.auth.workflow_principal(self.owner, grant)
            with self.assertRaises(AuthenticationError):
                self.key()

    def test_workflow_revalidates_original_browser_and_key_without_renewing(self):
        key = self.key()
        browser = self.grant(self.principal)
        machine = self.grant(self.auth.bearer(key["api_key"]))
        with self.repo.engine.connect() as connection:
            expires = connection.execute(select(sessions.c.expires)).scalar_one()
        self.now += 7200
        for grant in (browser, machine):
            current = self.auth.workflow_principal(self.owner, grant)
            self.assertEqual(current.auth_mode, "workos")
            with self.assertRaises(AuthenticationError):
                self.auth.workflow_principal("u_foreign", grant)
            with self.assertRaises(AuthenticationError):
                self.auth.workflow_principal(
                    self.owner, grant | {"auth_mode": "password"}
                )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(sessions.c.expires)).scalar_one(), expires
            )
        # Accepted pre-305 workflow machine grants did not carry source fields.
        self.assertEqual(
            self.auth.workflow_principal(
                self.owner, machine | {"auth_mode": None, "password_version": None}
            ).actor_id,
            machine["actor_id"],
        )
        self.auth.logout(self.token)
        with self.assertRaises(AuthenticationError):
            self.auth.workflow_principal(self.owner, browser)
        self.auth.revoke_key(self.owner, key["id"])
        with self.assertRaises(AuthenticationError):
            self.auth.workflow_principal(self.owner, machine)

    def test_static_target_requires_canonical_workos_and_preserves_scope(self):
        token = secrets.token_urlsafe(32)
        with self.assertRaises(ValueError):
            self.auth.register_client(
                "unknown", token, "u_missing", ["p"], ["jobs:read"]
            )
        self.auth.register_client(
            "owned-static", token, self.owner, ["p"], ["jobs:read"]
        )
        principal = self.auth.bearer(token)
        self.assertEqual(principal.owner, self.owner)
        self.assertEqual(principal.role, "user")
        self.assertEqual(principal.auth_mode, "workos")
        self.assertTrue(principal.allows("p", "jobs:read"))
        self.assertFalse(principal.allows("other", "jobs:read"))
        self.assertEqual(
            self.auth.workflow_principal(self.owner, self.grant(principal)).actor_id,
            principal.actor_id,
        )
        other_tenant = Auth(self.repo.engine, tenant="other", clock=lambda: self.now)
        self.assertIsNone(other_tenant.session(self.token))
        self.assertIsNone(other_tenant.bearer(token))

    def test_agent_exchange_and_recovery_preserve_workos_source(self):
        grant = self.issue()
        token, verifier, body = self.material(grant)
        without_provider = Auth(
            self.repo.engine, mode="local-test", clock=lambda: self.now
        )
        service = AgentConnect(without_provider)
        first = service.exchange(**body)
        recovered = service.exchange(**body, recovery_verifier=verifier)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(first["key"]["id"], recovered["key"]["id"])
        self.assertEqual(self.count(personal_keys), 1)
        principal = without_provider.bearer(token)
        self.assertEqual(
            (principal.owner, principal.auth_mode, principal.role),
            (self.owner, "workos", "user"),
        )
        with self.repo.engine.connect() as connection:
            connection_row = connection.execute(select(connections)).mappings().one()
            key = connection.execute(select(personal_keys)).mappings().one()
        self.assertEqual(
            (connection_row["auth_mode"], key["auth_mode"]), ("workos", "workos")
        )
        self.assertEqual(
            (connection_row["password_version"], key["password_version"]), (0.0, 0.0)
        )

    def test_pending_agent_exchange_rechecks_session_and_immutable_source(self):
        grant = self.issue()
        _, _, body = self.material(grant)
        self.auth.logout(self.token)
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            self.service.exchange(**body)
        self.assertEqual(self.count(personal_keys), 0)
        for source in ("password", "local-test"):
            with (
                self.subTest(source=source),
                self.assertRaises((AuthenticationError, ConnectError)),
            ):
                self.issue(principal=replace(self.principal, auth_mode=source))

    def test_pending_and_recovered_agent_grants_recheck_canonical_status(self):
        pending = self.issue()
        _, _, pending_body = self.material(pending)
        connected = self.issue()
        token, verifier, body = self.material(connected)
        self.service.exchange(**body)
        self.change(status="suspended")
        for value in (pending_body, body | {"recovery_verifier": verifier}):
            with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
                self.service.exchange(**value)
        self.assertIsNone(self.auth.bearer(token))
        self.assertEqual(self.count(personal_keys), 1)

    def test_public_workos_user_cannot_use_legacy_login_or_password_bootstrap(self):
        local = Auth(self.repo.engine, mode="local-test", clock=lambda: self.now)
        with self.assertRaises(AuthenticationError):
            local.login(self.owner, "", source="not-a-legacy-account")
        with self.assertRaisesRegex(ValueError, "Unsupported account"):
            self.auth.set_password(self.owner, secrets.token_urlsafe(18))
        with self.assertRaisesRegex(ValueError, "WorkOS"):
            self.auth.change_password(self.owner, "unused", "unused")
        self.assertEqual(self.count(accounts), 0)

    def test_linked_legacy_owner_issues_distinct_original_grants(self):
        self.add_user("superdan", "user_linked_dan")
        self.add_user("supervan", None)
        password = secrets.token_urlsafe(18)
        self.auth.set_password("superdan", password)
        self.auth.set_password("supervan", password)
        legacy = self.auth.login("superdan", password, source="legacy-linked")
        workos = self.auth.issue_workos_session("superdan", "user_linked_dan")
        legacy_principal = self.auth.session(legacy)
        workos_principal = self.auth.session(workos)
        self.assertEqual(legacy_principal.owner, workos_principal.owner)
        self.assertEqual(legacy_principal.auth_mode, "password")
        self.assertEqual(workos_principal.auth_mode, "workos")
        self.assertNotEqual(legacy_principal.password_version, 0.0)
        for original in (legacy_principal, workos_principal):
            key = self.key(owner="superdan", principal=original)
            current = self.auth.bearer(key["api_key"])
            self.assertEqual(current.auth_mode, original.auth_mode)
            self.assertEqual(current.password_version, original.password_version)
            self.assertEqual(
                self.auth.workflow_principal("superdan", self.grant(current)).actor_id,
                current.actor_id,
            )

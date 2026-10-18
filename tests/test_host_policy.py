"""Original-row policy, commit races and honest unknown host evidence."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update, func

import test_platform_repository as ledger
from studio_platform.auth import Principal
from studio_platform.host_policy import (
    HostPolicyStore,
    automatic_deny,
    bound_observation,
)
from studio_platform.host_scorecard import OperatorHosts
from studio_platform.operator_capacity import operator_policy
from studio_platform.repository import outbox


def physical(machine="19492", instance="1234567", at=1000):
    return {
        "version": 1,
        "provider": "vastai",
        "machine_id": machine,
        "provider_instance_id": instance,
        "offer_id": "12345",
        "observed_at": at,
        "region": "test-region",
        "gpu": "RTX5090",
        "gpu_count": 1,
        "allocated_ram_mib": 96 * 1024,
        "allocated_vram_mib": 32 * 1024,
        "cpu": 12,
        "disk_mib": 128 * 1024,
        "reliability": 0.99,
        "quoted_hourly_microusd": 700000,
        "identity_confirmed": True,
        "physical_id_source": "vast_machine_id",
    }


class HostPolicyTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id,
            operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",),
        )
        self.owner, self.other = (
            Principal("superdan", "browser"),
            Principal("supervan", "other"),
        )
        self.service = OperatorHosts(self.repo, self.settings)

    def body(self, manual="deny"):
        host = self.service.list(self.owner, admin=True, machine_id="19492")["hosts"][0]
        return {
            "provider": "vastai",
            "machine_id": "19492",
            "policy": manual,
            "reason_code": "operator_host_"
            + {"deny": "denied", "prefer": "preferred", "default": "default"}[manual],
            "expected_revision": host["policy"]["revision"],
            "expected_snapshot": host["policy"]["snapshot"],
        }

    def test_unknown_lookup_is_not_fabricated_and_policy_audit_replay(self):
        host = self.service.list(self.owner, admin=True, machine_id="19492")["hosts"][0]
        self.assertFalse(host["identity_confirmed"])
        self.assertEqual(host["runs"], 0)
        self.assertIsNone(host["latest_observation"])
        body = self.body()
        result = self.service.policy(self.owner, body, "deny-original")
        self.assertTrue(result["changed"])
        self.assertEqual(self.service.policy(self.owner, body, "deny-original"), result)
        self.assertEqual(len(self.service.audit(self.owner)["events"]), 1)
        self.assertEqual(self.service.list(self.other)["hosts"], [])
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                len(conn.execute(select(operator_policy.c.id)).scalar_one()), 30
            )

    def test_two_concurrent_cas_writers_only_one_commits(self):
        body = self.body()

        def write(i):
            try:
                return self.service.policy(self.owner, body, "host-race-" + str(i))[
                    "changed"
                ]
            except ValueError:
                return False

        self.assertEqual(sum(self.parallel(write, count=2)), 1)
        self.assertEqual(len(self.service.audit(self.owner)["events"]), 1)

    def test_audit_failure_rolls_back_original_policy_and_command(self):
        body = self.body()
        from sqlalchemy.engine import Connection

        real = Connection.execute

        def guarded(conn, statement, *args, **kwargs):
            if (
                getattr(statement, "is_insert", False)
                and getattr(statement, "table", None) is outbox
            ):
                raise RuntimeError("synthetic audit failure")
            return real(conn, statement, *args, **kwargs)

        with (
            patch.object(Connection, "execute", guarded),
            self.assertRaises(RuntimeError),
        ):
            self.service.policy(self.owner, body, "audit-rollback")
        self.assertEqual(self.body()["expected_revision"], 0)

    def test_owner_admin_machine_and_tenant_separation(self):
        with self.assertRaises(ValueError):
            self.service.policy(self.other, self.body(), "forbidden")
        with self.assertRaises(ValueError):
            self.service.list(
                Principal("superdan", "machine", machine=True), admin=True
            )
        self.service.policy(self.owner, self.body(), "tenant-policy")
        foreign = OperatorHosts(
            self.repo,
            SimpleNamespace(
                tenant_id="other-tenant",
                operator_capacity_owners=("superdan",),
                operator_admin_owners=("superdan",),
            ),
        )
        self.assertEqual(foreign.list(self.owner, admin=True)["hosts"], [])

    def test_automatic_two_distinct_qualified_starts_have_fixed_expiry(self):
        def node(identity, at, qualified=True):
            return {
                "intent_id": identity,
                "payload": {
                    "capacity_backend": "dstack-v1",
                    "host_observation": physical(at=at),
                    "dstack": {"backend": "vastai", "provider_instance_id": "1234567"},
                    "host_failure": {
                        "version": 1,
                        "stage": "runtime_start",
                        "reason_code": "provider_runner_timeout",
                        "at": at,
                        "qualified_start_failure": qualified,
                    },
                },
            }

        nodes = [node("first", 1000), node("second", 1100)]
        self.assertIsNone(automatic_deny(nodes[:1], "19492", now=1200))
        self.assertEqual(automatic_deny(nodes, "19492", now=1200), 1100 + 7 * 86400)
        self.assertIsNone(automatic_deny(nodes, "19492", now=1100 + 7 * 86400))
        self.assertIsNone(
            automatic_deny([nodes[0], node("other", 1100, False)], "19492", now=1200)
        )
        # No typed physical ID, no sample. Cleanup after a real failure does
        # not erase that immutable failure; owner stop never creates one.
        malformed = copy.deepcopy(nodes[1])
        malformed["payload"].pop("host_observation")
        self.assertIsNone(automatic_deny([nodes[0], malformed], "19492", now=1200))
        self.assertIsNone(automatic_deny([nodes[0], nodes[0]], "19492", now=1200))

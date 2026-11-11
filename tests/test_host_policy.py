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
    deny_failed_rental,
    recent_start_failures,
    failure,
    observation,
    offer_metadata,
    selection,
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


def rental(identity, at, *, failed=False, ready=None, stalled=False):
    """One original Vast rental of host 19492: how its start and its run went."""
    payload = {
        "capacity_backend": "dstack-v1",
        "host_observation": physical(at=at),
        "dstack": {"backend": "vastai", "provider_instance_id": "1234567"},
    }
    if failed:
        payload["host_failure"] = {
            "version": 1,
            "stage": "provisioning",
            "reason_code": "provider_provisioning_timeout",
            "at": at,
            "qualified_start_failure": True,
        }
    if ready is not None:
        payload["dstack"]["first_runtime_ready_at"] = ready
    if stalled:
        payload["capacity_watchdog"] = {
            "version": 1,
            "reason": "capacity_no_progress",
            "at": at,
        }
    return {"intent_id": identity, "desired_state": "stopped", "payload": payload}


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

    def test_closed_advertised_metadata_preserves_legacy_identity_and_rejects_poison(
        self,
    ):
        original = physical()
        self.assertEqual(observation(original, now=1200), original)
        self.assertEqual(
            offer_metadata({}),
            {"country": None, "region": None, "advertised_download_mbps": None},
        )
        metadata = {
            "country": "United States",
            "region": "us-ashburn",
            "advertised_download_mbps": 1000,
        }
        current = {
            **original,
            "country": metadata["country"],
            "advertised_download_mbps": metadata["advertised_download_mbps"],
        }
        self.assertEqual(observation(current, now=1200), current)
        frozen = {
            "version": 1,
            "observed_at": 1000,
            "expires_at": 1120,
            "policy_snapshot": "a" * 64,
            "offers": [{"offer_id": "12345", "machine_id": "19492", **metadata}],
        }
        self.assertEqual(selection(frozen, now=1050), frozen)
        for changes in (
            {"advertised_download_mbps": float("nan")},
            {"advertised_download_mbps": True},
            {"country": "https://PRIVATE"},
            {"region": "PRIVATE\nTOKEN"},
            {"unexpected": "PRIVATE"},
        ):
            with (
                self.subTest(changes=list(changes)),
                self.assertRaisesRegex(ValueError, "host_offer_metadata_invalid"),
            ):
                offer_metadata({**metadata, **changes})

    def test_two_download_timeboxes_are_history_only_never_automatic_deny(self):
        nodes = []
        for identity, at in (("first", 1000), ("second", 1100)):
            event = {
                "version": 1,
                "stage": "model_download",
                "reason_code": "dstack_download_timebox_exceeded",
                "at": at,
                "qualified_start_failure": False,
            }
            self.assertEqual(failure(event, now=1200), event)
            nodes.append(
                {
                    "intent_id": identity,
                    "payload": {
                        "capacity_backend": "dstack-v1",
                        "host_observation": physical(at=at),
                        "dstack": {
                            "backend": "vastai",
                            "provider_instance_id": "1234567",
                        },
                        "host_failure": event,
                    },
                }
            )
        self.assertIsNone(automatic_deny(nodes, "19492", now=1200))
        invalid = {
            **nodes[0]["payload"]["host_failure"],
            "qualified_start_failure": True,
        }
        with self.assertRaisesRegex(ValueError, "host_failure_invalid"):
            failure(invalid, now=1200)

    def test_slow_download_deny_persists_until_an_admin_restores_the_host(self):
        store = HostPolicyStore(self.repo, self.scope.tenant_id)
        with self.repo.transaction() as conn:
            self.assertTrue(
                store.deny_slow_download(conn, "19492", node_id="node-one", now=1000)
            )
            # A repeat for the same denied host writes nothing new.
            self.assertFalse(
                store.deny_slow_download(conn, "19492", node_id="node-two", now=1001)
            )
        host = self.service.list(self.owner, admin=True, machine_id="19492")["hosts"][0]
        self.assertEqual(
            {k: host["policy"][k] for k in ("manual", "effective", "reason_code")},
            {
                "manual": "deny",
                "effective": "deny",
                "reason_code": "automatic_slow_download",
            },
        )
        self.assertIsNone(host["policy"]["expires_at"])  # Never expires by itself.
        [event] = self.service.audit(self.owner)["events"]
        self.assertEqual(event["actor_id"], "system:download-speed-gate")
        self.assertEqual(event["reason_code"], "automatic_slow_download")
        self.assertTrue(isinstance(event["command_id"], str) and event["command_id"])
        # The ordinary admin write is the release; it is audited separately.
        result = self.service.policy(self.owner, self.body("default"), "release")
        self.assertTrue(result["changed"])
        self.assertEqual(result["policy"]["effective"], "default")
        self.assertEqual(len(self.service.audit(self.owner)["events"]), 2)
        with self.repo.transaction() as conn:
            # The same node again after the restore is a new, distinct receipt.
            self.assertTrue(
                store.deny_slow_download(conn, "19492", node_id="node-one", now=1002)
            )
        self.assertEqual(len(self.service.audit(self.owner)["events"]), 3)

    def test_slow_download_never_overrides_an_admin_prefer(self):
        # #903 item 4: a preferred host is only stopped, never auto-denied.
        self.service.policy(self.owner, self.body("prefer"), "prefer")
        store = HostPolicyStore(self.repo, self.scope.tenant_id)
        with self.repo.transaction() as conn:
            self.assertFalse(
                store.deny_slow_download(conn, "19492", node_id="node-one", now=1000)
            )
        host = self.service.list(self.owner, admin=True, machine_id="19492")["hosts"][0]
        self.assertEqual(host["policy"]["effective"], "prefer")
        self.assertEqual(len(self.service.audit(self.owner)["events"]), 1)

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

    def test_a_good_start_between_two_start_failures_is_not_a_pair(self):
        # #1198: a slow image pull can time out once on a host that works.
        first, last = rental("a", 1000, failed=True), rental("c", 3000, failed=True)
        until = 3000 + 7 * 86400
        self.assertEqual(automatic_deny([first, last], "19492", now=4000), until)
        good = rental("b", 2000, ready=2100)
        self.assertIsNone(automatic_deny([first, good, last], "19492", now=4000))
        # A good start before the first failure or after the second is no excuse.
        for ready in (900, 3100):
            with self.subTest(ready=ready):
                other = rental("b", 800, ready=ready)
                self.assertEqual(
                    automatic_deny([first, other, last], "19492", now=4000), until
                )
        # A machine that started and then stopped making progress is a
        # different problem: a good start in between does not clear the pair.
        stalled = rental("c", 3000, stalled=True)
        for before in (first, rental("a", 1000, stalled=True)):
            with self.subTest(before=sorted(before["payload"])):
                self.assertEqual(
                    automatic_deny([before, good, stalled], "19492", now=4000), until
                )

    def denied(self, node, now, history=()):
        node["payload"]["tenant_id"] = self.scope.tenant_id
        with (
            patch(
                "studio_platform.host_policy.original_nodes",
                return_value=list(history),
            ),
            self.repo.transaction() as connection,
        ):
            return deny_failed_rental(
                self.repo, self.scope.tenant_id, connection, node, now=now
            )

    def stored(self, machine_id="19492"):
        host = self.service.list(self.owner, admin=True, machine_id=machine_id)[
            "hosts"
        ][0]["policy"]
        return host["manual"], host["reason_code"], host["expires_at"]

    def test_a_rental_that_never_started_denies_its_host_at_once(self):
        # Owner, 2026-11-10: a slow or unreachable host is not rented again.
        node = rental("a", 1000, failed=True)
        self.assertTrue(self.denied(node, 1000))
        self.assertEqual(self.stored(), ("deny", "automatic_start_failure", None))
        # Seen again, the same rental changes nothing.
        self.assertFalse(self.denied(node, 1002))
        [event] = self.service.audit(self.owner)["events"]
        self.assertEqual(
            (event["actor_id"], event["reason_code"]),
            ("system:host-failure", "automatic_start_failure"),
        )
        # Only the ordinary admin write releases it.
        self.assertTrue(
            self.service.policy(self.owner, self.body("default"), "release")["changed"]
        )
        self.assertEqual(self.stored()[0], "default")

    def test_many_hosts_failing_to_start_at_once_are_not_denied(self):
        # Three fresh hosts in an hour is our own image or registry: the
        # first two are denied, then the brake holds and raises one alert.
        def fresh(machine, at):
            node = rental("node-" + machine, at, failed=True)
            node["payload"]["host_observation"] = physical(machine=machine, at=at)
            return node

        results = [
            self.denied(fresh(str(19500 + i), 1000 + i), 1000 + i) for i in range(4)
        ]
        self.assertEqual(results, [True, True, False, False])
        with self.repo.engine.connect() as connection:
            alerts = list(
                connection.execute(
                    select(outbox.c.payload).where(
                        outbox.c.event_type == "capacity.health_alert"
                    )
                ).scalars()
            )
        self.assertEqual(
            [(row["code"], row["diagnosis"], row["failed_hosts"]) for row in alerts],
            [("capacity_start_failures_upstream", "start_failures_upstream", 3)],
        )
        self.assertEqual(self.stored("19502")[0], "default")
        # An hour later the window is clear and a single failure denies again.
        self.assertTrue(self.denied(fresh("19510", 1000 + 3700), 1000 + 3700))

    def test_a_host_that_started_and_failed_twice_in_a_day_is_denied(self):
        first = rental("a", 1000, ready=900, stalled=True)
        second = rental("b", 5000, ready=4900, stalled=True)
        # One such failure is not enough,
        self.assertFalse(self.denied(first, 1000))
        self.assertEqual(self.stored()[0], "default")
        # the second within a day is, and it has no end either.
        self.assertTrue(self.denied(second, 5000, history=[first]))
        self.assertEqual(self.stored(), ("deny", "automatic_repeated_failure", None))

    def test_many_hosts_failing_after_ready_at_once_are_not_denied(self):
        # #1214: three hosts going silent within the hour is a request or a
        # release of ours. From the third on nothing is denied, one alert.
        def stalled(machine, name, at):
            node = rental(name, at, ready=at - 100, stalled=True)
            node["payload"]["host_observation"] = physical(machine=machine, at=at)
            return node

        history = []
        for i in range(3):
            node = stalled(str(19600 + i), "first-%d" % i, 1000 + i)
            self.assertFalse(self.denied(node, 1000 + i))
            history.append(node)
        # Each of them fails a second time inside the hour: a pair, but braked.
        for i in range(3):
            again = stalled(str(19600 + i), "again-%d" % i, 2000 + i)
            self.assertFalse(self.denied(again, 2000 + i, history=history))
            history.append(again)
        for i in range(3):
            self.assertEqual(self.stored(str(19600 + i))[0], "default")
        with self.repo.engine.connect() as connection:
            alerts = list(
                connection.execute(
                    select(outbox.c.payload).where(
                        outbox.c.event_type == "capacity.health_alert"
                    )
                ).scalars()
            )
        self.assertEqual(
            [(row["code"], row["diagnosis"], row["failed_hosts"]) for row in alerts],
            [("capacity_generate_failures_upstream", "generate_failures_upstream", 3)],
        )
        # Once the hour is clear, a host that fails twice in a day is denied again.
        late = [stalled("19600", "late-%d" % i, 9000 + 100 * i) for i in range(2)]
        self.assertFalse(self.denied(late[0], 9000, history=[]))
        self.assertTrue(self.denied(late[1], 9100, history=[late[0]]))

    def test_only_losses_that_say_something_about_the_host_count(self):
        # #1214 and its review: what the jobs lost on the two rentals say.
        first = rental("a", 1000, ready=900, stalled=True)
        second = rental("b", 5000, ready=4900, stalled=True)

        def denied_with(verdicts):
            with patch(
                "studio_platform.capacity_reclaim.lost_jobs",
                side_effect=lambda connection, nodes: {
                    node: verdicts.get(node, []) for node in nodes
                },
            ):
                return self.denied(second, 5000, history=[first])

        for verdicts in (
            # The request was the cause of the new loss, or of the old one.
            {"a": [("job-1", "ended")], "b": [("job-2", "suspect")]},
            {"a": [("job-1", "suspect")], "b": [("job-2", "open")]},
            # Two different jobs, neither with a verdict yet.
            {"a": [("job-1", "open")], "b": [("job-2", "open")]},
        ):
            self.assertFalse(denied_with(verdicts), verdicts)
            self.assertEqual(self.stored()[0], "default")
        # The host already lost a job that ended elsewhere, and loses another.
        self.assertTrue(
            denied_with({"a": [("job-1", "ended")], "b": [("job-2", "open")]})
        )
        self.assertEqual(self.stored(), ("deny", "automatic_repeated_failure", None))
        self.service.policy(self.owner, self.body("default"), "release")
        # The same open job lost twice on this host is the host.
        self.assertTrue(
            denied_with({"a": [("job-1", "open")], "b": [("job-1", "open")]})
        )

    def test_the_derived_deny_follows_the_same_evidence(self):
        from studio_platform.host_policy import HostPolicyStore

        nodes = [
            rental("a", 1000, ready=900, stalled=True),
            rental("b", 5000, ready=4900, stalled=True),
        ]

        def effective(verdicts):
            with (
                patch(
                    "studio_platform.capacity_reclaim.lost_jobs",
                    side_effect=lambda connection, wanted: {
                        node: verdicts.get(node, []) for node in wanted
                    },
                ),
                self.repo.engine.connect() as connection,
            ):
                return HostPolicyStore(self.repo, self.scope.tenant_id).read(
                    connection, "19492", nodes=nodes, now=6000
                )[0]["effective"]

        self.assertEqual(effective({}), "deny")
        self.assertEqual(
            effective({"a": [("job-1", "ended")], "b": [("job-2", "ended")]}), "deny"
        )
        # Request-caused or unjudged losses deny nothing, stored or derived.
        self.assertEqual(
            effective({"a": [("job-1", "suspect")], "b": [("job-1", "suspect")]}),
            "default",
        )
        self.assertEqual(
            effective({"a": [("job-1", "ended")], "b": [("job-2", "open")]}), "default"
        )

    def test_a_failed_start_never_overrides_an_admin_prefer(self):
        self.service.policy(self.owner, self.body("prefer"), "prefer")
        self.assertFalse(self.denied(rental("a", 1000, failed=True), 1000))
        self.assertEqual(self.stored()[0], "prefer")

    def test_recent_start_failure_lasts_an_hour_or_until_a_good_start(self):
        failed = rental("a", 1000, failed=True)
        self.assertEqual(recent_start_failures([failed], now=1000 + 3600), {"19492"})
        self.assertEqual(recent_start_failures([failed], now=1000 + 3601), set())
        self.assertEqual(
            recent_start_failures([failed], now=1100, provider="lium"), set()
        )
        self.assertEqual(
            recent_start_failures([failed, rental("b", 2000, ready=2100)], now=2200),
            set(),
        )
        self.assertEqual(
            recent_start_failures([failed, rental("b", 500, ready=600)], now=1100),
            {"19492"},
        )
        # This rule looks at starts only; a run that stalled later has its own.
        self.assertEqual(
            recent_start_failures([rental("a", 1000, stalled=True)], now=1100), set()
        )
        # A failure after ready puts the host last too, by its own rule (#1214):
        # the retry is tried elsewhere first, until the host has run well again.
        from studio_platform.host_policy import recent_silent_failures

        stalled = rental("a", 1000, ready=900, stalled=True)
        self.assertEqual(recent_silent_failures([stalled], now=1100), {"19492"})
        self.assertEqual(recent_silent_failures([stalled], now=1000 + 3601), set())
        self.assertEqual(
            recent_silent_failures([stalled, rental("b", 2000, ready=2100)], now=2200),
            set(),
        )
        self.assertEqual(recent_silent_failures([failed], now=1100), set())


EXECUTOR = "0453b369-1111-4222-8333-444455556666"
POD = "758e7d2d-17da-4ed0-95a5-057b33483ad8"


def lium_provisioning(executor=EXECUTOR, pod=POD, price=0.9):
    return {
        "backend": "lium",
        "instance_id": pod,
        "region": "us",
        "price": price,
        "instance_type": {
            "name": executor,
            "resources": {
                "gpus": [{"name": "RTX5090", "memory_mib": 32 * 1024}],
                "memory_mib": 96 * 1024,
                "cpus": 12,
                "disk": {"size_mib": 200 * 1024},
            },
        },
    }


class LiumHostIdentityTests(ledger.LedgerCase):
    """#900: a Lium pod is bound to its executor; deny keys on provider + id."""

    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id,
            operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",),
        )
        self.owner = Principal("superdan", "browser")
        self.service = OperatorHosts(self.repo, self.settings)
        self.store = HostPolicyStore(self.repo, self.scope.tenant_id)

    def node(self, value):
        return {
            "intent_id": "node-lium",
            "desired_state": "stopped",
            "runtime_state": "stopped",
            "payload": {
                "capacity_backend": "dstack-v1",
                "tenant_id": self.scope.tenant_id,
                "owner_id": "superdan",
                "host_observation": value,
                "dstack": {
                    "backend": "lium",
                    "provider_instance_id": POD,
                    "profile_id": "fl",
                    "mode": "fl",
                },
            },
        }

    def test_a_lium_executor_that_failed_to_start_is_a_recent_failure(self):
        from studio_platform.host_policy import lium_observation

        node = self.node(lium_observation(lium_provisioning(), {}, 1000))
        self.assertEqual(
            recent_start_failures([node], now=1100, provider="lium"), set()
        )
        node["payload"]["host_failure"] = {
            "version": 1,
            "stage": "provisioning",
            "reason_code": "provider_provisioning_timeout",
            "at": 1000,
            "qualified_start_failure": True,
        }
        self.assertEqual(
            recent_start_failures([node], now=1100, provider="lium"), {EXECUTOR}
        )
        self.assertEqual(recent_start_failures([node], now=1100), set())
        self.assertIsNone(automatic_deny([node], EXECUTOR, now=1100, provider="lium"))

    def test_vast_policy_key_is_unchanged(self):
        from studio_platform.repository import request_hash

        self.assertEqual(
            self.store.key("19492"),
            "host:"
            + request_hash(
                {
                    "tenant": self.scope.tenant_id,
                    "provider": "vastai",
                    "machine_id": "19492",
                }
            )[:25],
        )
        self.assertNotEqual(self.store.key(EXECUTOR, "lium"), self.store.key("19492"))
        with self.assertRaisesRegex(ValueError, "host_identity_invalid"):
            self.store.key("19492", "lium")
        with self.assertRaisesRegex(ValueError, "host_provider_unsupported"):
            self.store.key("abc", "runpod")

    def test_executor_from_the_allocation_is_the_bound_host(self):
        from studio_platform.host_policy import lium_observation

        value = lium_observation(lium_provisioning(), {}, 1000)
        self.assertEqual(
            (
                value["provider"],
                value["machine_id"],
                value["provider_instance_id"],
                value["physical_id_source"],
                value["quoted_hourly_microusd"],
            ),
            ("lium", EXECUTOR, POD, "lium_executor_id", 900000),
        )
        self.assertEqual(bound_observation(self.node(value), now=1100), value)
        # Not yet an executor id, another pod already bound, or a Vast JPD.
        for provisioning, binding in (
            (lium_provisioning(executor="RTX5090"), {}),
            (lium_provisioning(), {"provider_instance_id": "other-pod"}),
            ({**lium_provisioning(), "backend": "vastai"}, {}),
        ):
            with self.subTest(binding=binding):
                self.assertIsNone(lium_observation(provisioning, binding, 1000))
        frozen = {
            "version": 1,
            "provider": "lium",
            "observed_at": 900,
            "expires_at": 1020,
            "policy_snapshot": "a" * 64,
            "offers": [
                {
                    "offer_id": "11111111-1111-4111-8111-111111111111",
                    "machine_id": "11111111-1111-4111-8111-111111111111",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "dstack_host_allocation_changed"):
            lium_observation(lium_provisioning(), {"host_selection": frozen}, 1000)

    def test_slow_download_and_bad_output_deny_a_lium_executor(self):
        from studio_platform.host_policy import lium_observation

        value = lium_observation(lium_provisioning(), {}, 1000)
        with self.repo.transaction() as conn:
            self.assertTrue(
                self.store.deny_slow_download(
                    conn, EXECUTOR, node_id="node-lium", now=1000, provider="lium"
                )
            )
            read = self.store.read(conn, EXECUTOR, provider="lium", nodes=[])[0]
            vast = self.store.read(conn, "19492", nodes=[])[0]
        self.assertEqual(read["effective"], "deny")
        self.assertEqual(vast["effective"], "default")
        listing = self.service.list(
            self.owner, admin=True, provider="lium", machine_id=EXECUTOR
        )
        self.assertEqual(listing["provider"], "lium")
        [host] = listing["hosts"]
        self.assertEqual(
            (host["provider"], host["machine_id"], host["policy"]["reason_code"]),
            ("lium", EXECUTOR, "automatic_slow_download"),
        )
        # Vast stays the default listing; the Lium host is not in it.
        self.assertEqual(self.service.list(self.owner, admin=True)["hosts"], [])
        # An admin marks it as producing bad output; the change is audited.
        body = {
            "provider": "lium",
            "machine_id": EXECUTOR,
            "policy": "deny",
            "reason_code": "operator_host_bad_output",
            "expected_revision": host["policy"]["revision"],
            "expected_snapshot": host["policy"]["snapshot"],
        }
        result = self.service.policy(self.owner, body, "bad-output")
        self.assertTrue(result["changed"])
        self.assertEqual(result["host"], {"provider": "lium", "machine_id": EXECUTOR})
        self.assertEqual(result["policy"]["reason_code"], "operator_host_bad_output")
        events = self.service.audit(self.owner)["events"]
        self.assertEqual(
            sorted((e["provider"], e["reason_code"]) for e in events),
            [("lium", "automatic_slow_download"), ("lium", "operator_host_bad_output")],
        )
        # The bound pod's history shows on the host once a node carries it.
        with patch(
            "studio_platform.host_scorecard.original_nodes",
            return_value=[self.node(value)],
        ):
            [host] = self.service.list(self.owner, admin=True, provider="lium")["hosts"]
        self.assertEqual((host["runs"], host["identity_confirmed"]), (1, True))
        for bad in (
            {**body, "reason_code": "automatic_slow_download"},
            {**body, "policy": "prefer"},
            {**body, "provider": "runpod"},
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "host_policy_request_invalid"),
            ):
                self.service.policy(self.owner, bad, "bad-" + bad["policy"])

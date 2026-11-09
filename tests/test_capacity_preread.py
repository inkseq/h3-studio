"""#1148: provider reads outside the capacity lock, consumer readiness proof."""

import copy
from types import SimpleNamespace
import unittest

from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.native_capacity_inventory import (
    PREREAD_CHANGED,
    committed_facts,
    observed_usage,
)
from studio_platform.postgres_dispatch import _live_readiness, _runtime_ready_at
from studio_platform.repository import BudgetExceeded
from studio_platform.worker_admission import _gate, readiness_proven_at

INCARNATION = "a" * 32


def node(**dstack):
    return {
        "intent_id": "node-1",
        "desired_state": "running",
        "payload": {
            "capacity_backend": "dstack-v1",
            "hourly_cost_microusd": 700_000,
            "dstack": {"run_id": "run-1", "state": "ready", **dstack},
        },
    }


class PrereadFactsTests(unittest.TestCase):
    def service(self, now=1000.0):
        reads = []
        service = SimpleNamespace(
            repo=SimpleNamespace(clock=lambda: now),
            policy={"observation_fresh_seconds": 30},
            _observed_registration_usage=lambda nodes: (
                reads.append(nodes) or ({"live": True}, {})
            ),
        )
        return service, reads

    def preread(self, nodes, read_at=990.0):
        return {
            "facts": committed_facts(nodes),
            "usage": {"live": "preread"},
            "observed_runs": {},
            "read_at": read_at,
        }

    def test_worker_liveness_writes_do_not_void_the_preread(self):
        before = [node()]
        after = [node(consumer_observation={"observed_at": 999})]
        service, reads = self.service()
        usage, _ = observed_usage(service, after, self.preread(before))
        self.assertEqual(usage, {"live": "preread"})
        self.assertEqual(reads, [])

    def test_a_changed_committed_fact_fails_closed_or_rereads_under_the_lock(self):
        before = [node()]
        stopped = copy.deepcopy(before)
        stopped[0]["desired_state"] = "stopped"
        added = before + [dict(node(), intent_id="node-2")]
        observed = [node(state="busy")]
        for current in (stopped, added, observed):
            service, reads = self.service()
            with self.assertRaisesRegex(BudgetExceeded, PREREAD_CHANGED):
                observed_usage(service, current, self.preread(before))
            self.assertEqual(reads, [])
            usage, _ = observed_usage(
                service, current, self.preread(before), reread=True
            )
            self.assertEqual((usage, reads), ({"live": True}, [current]))

    def test_a_preread_older_than_observation_freshness_is_not_used(self):
        nodes = [node()]
        service, _ = self.service(now=1000.0)
        with self.assertRaisesRegex(BudgetExceeded, PREREAD_CHANGED):
            observed_usage(service, nodes, self.preread(nodes, read_at=969.0))
        with self.assertRaisesRegex(BudgetExceeded, PREREAD_CHANGED):
            observed_usage(service, nodes, self.preread(nodes, read_at=1001.0))


class ConsumerReadinessTests(unittest.TestCase):
    def payload(self, *, observed=900.0, ready_at=None, incarnation=INCARNATION):
        consumer = {
            "runtime_incarnation": incarnation,
            "instance_id": "i-1",
            "runtime_ready_at": ready_at,
        }
        return {
            "observation_fresh_seconds": 30,
            "dstack_observation": {"observed_at": observed},
            "dstack": {
                "runtime_incarnation": INCARNATION,
                "consumer_observation": consumer,
            },
        }

    def test_controller_proof_alone_is_unchanged(self):
        intent = {"provider_instance_id": "i-1"}
        self.assertEqual(readiness_proven_at(self.payload(), intent, 950.0), 900.0)
        self.assertIsNone(readiness_proven_at(self.payload(), intent, 899.0))

    def test_consumer_read_renews_only_the_runtime_half_while_controller_is_live(
        self,
    ):
        intent = {"provider_instance_id": "i-1"}
        payload = self.payload(observed=900.0, ready_at=1050.0)
        self.assertEqual(readiness_proven_at(payload, intent, 1060.0), 1050.0)
        # A controller observation older than the liveness alert is not renewed.
        self.assertEqual(readiness_proven_at(payload, intent, 1081.0), 900.0)
        for changed in (
            self.payload(observed=900.0, ready_at=1050.0, incarnation="b" * 32),
            self.payload(observed=900.0, ready_at=1070.0),  # read in the future
        ):
            self.assertEqual(readiness_proven_at(changed, intent, 1060.0), 900.0)
        self.assertEqual(
            readiness_proven_at(payload, {"provider_instance_id": "i-2"}, 1060.0),
            900.0,
        )

    def test_record_consumer_keeps_only_an_exact_live_read(self):
        bound = {"intent_id": "node-1"}
        binding = {"manifest_digest": "m" * 64}
        good = HostReadiness("m" * 64, "node-1", INCARNATION, True)
        self.assertEqual(
            _runtime_ready_at((10.0, good), bound, binding, INCARNATION, 12.0), 10.0
        )
        for value in (
            HostReadiness("x" * 64, "node-1", INCARNATION, True),
            HostReadiness("m" * 64, "node-2", INCARNATION, True),
            HostReadiness("m" * 64, "node-1", "b" * 32, False),
        ):
            self.assertIsNone(
                _runtime_ready_at((10.0, value), bound, binding, INCARNATION, 12.0)
            )
        self.assertIsNone(_runtime_ready_at(None, bound, binding, INCARNATION, 12.0))

    def test_failed_or_missing_probe_proves_nothing(self):
        repo = SimpleNamespace(clock=lambda: 5.0)

        def broken():
            raise OSError("tunnel down")

        self.assertIsNone(_live_readiness(repo, SimpleNamespace()))
        self.assertIsNone(_live_readiness(repo, SimpleNamespace(live_readiness=broken)))
        self.assertIsNone(
            _live_readiness(repo, SimpleNamespace(live_readiness=lambda: {}))
        )


class RuntimeGateTests(unittest.TestCase):
    def test_gate_never_exceeds_the_reservation_allowance(self):
        self.assertEqual(_gate(1800, 420), 420)
        self.assertEqual(_gate(1800, 2400), 1800)
        self.assertEqual(_gate(1800, None), 1800)
        self.assertEqual(_gate(None, 420), 0)


if __name__ == "__main__":
    unittest.main()

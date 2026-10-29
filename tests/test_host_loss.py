"""#816: the off-host stop list names only our live paid Vast rentals."""

import json
from pathlib import Path
import tempfile
import uuid

from sqlalchemy import insert, update

from studio_platform.host_loss import FILE_NAME, Manifest, stoppable
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import instance_intents
from test_platform_repository import LedgerCase


class HostLossTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool("test-pool", max_instances=10, max_physical_gpus=10)

    def rental(self, key, owner, *, instance="58211106", provider="vast", state=None):
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            key,
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=1,
            hard_deadline=self.now + 3600,
            budget_account_ids=("owner-budget",),
            provider=provider,
            dry_run=False,
        )
        command = str(uuid.uuid4())
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(
                    provider_instance_id=instance, **({"state": state} if state else {})
                )
            )
            c.execute(
                insert(operator_commands).values(
                    id=command,
                    actor="original",
                    idempotency_key=key,
                    request_hash="a" * 64,
                    kind="dstack_start",
                    state="accepted",
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            c.execute(
                insert(operator_nodes).values(
                    intent_id=intent["id"],
                    command_id=command,
                    ordinal=0,
                    binding_id=key,
                    binding_hash="a" * 64,
                    desired_state="running",
                    runtime_state="ready",
                    payload={"owner_id": owner} if owner else {},
                    updated_at=self.now,
                )
            )
        return intent["id"]

    def test_lists_only_live_vast_rentals_of_unprotected_owners(self):
        ours = self.rental("ours", "superdan", instance="100")
        self.rental("theirs", "supervan", instance="200")
        self.rental("unknown", None, instance="300")
        self.rental("gone", "superdan", instance="400", state="destroyed")
        self.rental("modal", "superdan", instance="sb-1", provider="modal")
        with self.repo.engine.connect() as c:
            self.assertEqual(stoppable(c), [{"instance_id": "100", "intent_id": ours}])

    def test_manifest_writes_at_most_once_per_interval(self):
        self.rental("ours", "superdan", instance="100")
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000.0]
            manifest = Manifest(self.repo, directory, clock=lambda: clock[0])
            manifest.beat()
            path = Path(directory) / FILE_NAME
            value = json.loads(path.read_text())
            self.assertEqual(value["updated_at"], 1000.0)
            self.assertEqual([i["instance_id"] for i in value["instances"]], ["100"])
            clock[0] += 30
            manifest.beat()
            self.assertEqual(json.loads(path.read_text())["updated_at"], 1000.0)
            clock[0] += 31
            manifest.beat()
            self.assertEqual(json.loads(path.read_text())["updated_at"], 1061.0)

    def test_a_read_failure_never_raises_into_the_loop(self):
        broken = Manifest(object(), "/nonexistent")
        broken.beat()

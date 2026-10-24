"""Original-ledger dstack commands, using isolated databases and fake HTTP only."""

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import func, insert, select, update

from studio_platform.auth import Principal
from studio_platform.dstack_capacity import DstackCapacity, DstackError
from studio_platform.dstack_operator import (
    DstackOperator,
    LedgerDstackStore,
    OperatorError,
    from_environment,
)
from studio_platform.dstack_workspace import DstackWorkspace
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import (
    BudgetExceeded,
    attempts,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    registered_workers,
    scaler_actions,
)
from test_platform_repository import LedgerCase

PROFILE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey"


class Client:
    project = "sixnine"

    def __init__(self, store):
        self.store = store
        self.runs = {}
        self.applied = []
        self.stopped = []
        self.lost = False
        self.absent = False

    def plan(self, spec):
        return {
            "run_spec": spec,
            "job_plans": [
                {
                    "offers": [
                        {
                            "backend": "vastai",
                            "region": "test-region",
                            "price": 0.7,
                            "availability": "available",
                            "backend_data": "SECRET",
                            "instance": {
                                "name": "12345",
                                "resources": {
                                    "cpus": 12,
                                    "memory_mib": 96 * 1024,
                                    "disk": {"size_mib": 128 * 1024},
                                    "gpus": [
                                        {"name": "RTX5090", "memory_mib": 32 * 1024}
                                    ],
                                },
                            },
                        }
                    ]
                }
            ],
        }

    def vast_offer_mapping(self, request, *, clock):
        return {
            "observed_at": clock(),
            "offers": [
                {
                    "offer_id": "12345",
                    "machine_id": "19493",
                    "country": "US",
                    "region": "test-region",
                    "advertised_download_mbps": 2000,
                }
            ],
        }

    def reusable_hosts_confirmed(self, approved):
        return True

    def vast_instance_identity(self, identity):
        return {
            "provider_instance_id": identity,
            "machine_id": "19493",
            "reliability": 0.99,
        }

    def get(self, *, run_name=None, run_id=None):
        if self.absent:
            return None
        result = (
            self.runs.get(run_name)
            if run_name
            else next((run for run in self.runs.values() if run["id"] == run_id), None)
        )
        result = copy.deepcopy(result)
        if result and result.get("latest_job_submission", {}).get(
            "job_provisioning_data"
        ):
            data = result["latest_job_submission"]["job_provisioning_data"]
            data.setdefault("region", "test-region")
            data.setdefault("price", 0.7)
            kind = data.setdefault("instance_type", {})
            kind.setdefault("name", "12345")
            resource = kind.setdefault("resources", {})
            resource.setdefault("cpus", 12)
            resource.setdefault("memory_mib", 96 * 1024)
            resource.setdefault("disk", {"size_mib": 128 * 1024})
            gpus = resource.get("gpus")
            if isinstance(gpus, list):
                for gpu in gpus:
                    if isinstance(gpu, dict):
                        gpu.setdefault("memory_mib", 32 * 1024)
        return result

    def apply(self, spec):
        intent = spec["configuration"]["tags"]["sixnine_intent"]
        binding = self.store.load(intent)
        assert binding["apply_started"] is True
        with self.store.repo.engine.connect() as connection:
            assert (
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == intent
                    )
                )
                .mappings()
                .one()["state"]
                == "reserved"
            )
        self.applied.append(intent)
        value = {
            "id": str(uuid.uuid4()),
            "project_name": self.project,
            "run_spec": copy.deepcopy(spec),
            "status": "submitted",
            "cost": 0.1,
        }
        self.runs[spec["run_name"]] = value
        if self.lost:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(value)

    def stop(self, name):
        self.stopped.append(name)


class DstackOperatorTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self._setup_dstack()

    def _setup_dstack(self):
        self.repo.configure_pool("dstack-test", max_instances=3, max_physical_gpus=3)
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id,
            operator_capacity_owners=("superdan", "supervan"),
        )
        self.owner = Principal("superdan", "browser-1")
        self.other_owner = Principal("supervan", "browser-2")
        self.store = LedgerDstackStore(self.repo, self.scope.tenant_id)
        self.client = Client(self.store)
        self.capacity = DstackCapacity(
            self.client,
            self.store,
            clock=self.repo.clock,
            readiness=lambda binding, run: HostReadiness(
                binding["manifest_digest"], binding["intent_id"], "c" * 32, True
            ),
        )
        self.config = {
            "version": 1,
            "ssh_public_key": PUB,
            "policy": {
                "enabled": True,
                "expires_at": 5000.0,
                "max_instances": 3,
                "max_physical_gpus": 3,
                "max_hourly_cost_microusd": 3000000,
                "max_ttl_seconds": 1800,
                "idle_shutdown_seconds": 600,
                "observation_fresh_seconds": 30,
            },
            "profiles": [
                {
                    "id": "5090-fl",
                    "owner_id": "superdan",
                    "project_id": "project-1",
                    "budget_account_ids": ["owner-budget"],
                    "pool": "dstack-test",
                    "backend": "vastai",
                    "runtime_profile_id": PROFILE,
                    "mode": "fl",
                    "configuration_id": "test-native",
                    "image": "example/native@sha256:" + "a" * 64,
                    "gpu_names": ["RTX5090"],
                    "memory_gib": 96,
                    "disk_gib": 128,
                    "gpu_memory_gib": 30,
                    "cpu_count": 12,
                    "max_price_microusd": 750000,
                    "max_ttl_seconds": 1800,
                    "extra_reservation_microusd": 0,
                }
            ],
        }
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )

    def preview(self):
        return self.service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )

    def start(self, key="start-1", preview=None):
        preview = preview or self.preview()
        return self.service.start(
            self.owner, {"preview_id": preview["preview_id"]}, key
        )

    def counts(self):
        with self.repo.engine.connect() as connection:
            return tuple(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (
                    instance_intents,
                    budget_reservations,
                    operator_nodes,
                    scaler_actions,
                )
            )

    def running(self, node_id):
        binding = self.store.load(node_id)
        run = self.client.runs[binding["run_name"]]
        run.update(
            status="running",
            latest_job_submission={
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "1234567",
                    "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}},
                }
            },
        )
        return self.capacity.observe(node_id)

    def test_reserves_atomically_before_apply_and_replay_does_not_rent(self):
        preview = self.preview()
        result = self.start(preview=preview)
        again = self.start(preview=preview)
        self.assertEqual(result, again)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            account = connection.execute(select(budget_accounts)).mappings().one()
            self.assertEqual(
                account["reserved_microusd"], preview["reservation_microusd"]
            )
            self.assertEqual(account["spent_microusd"], 0)
            command = connection.execute(select(operator_commands)).mappings().one()
            self.assertEqual(command["kind"], "dstack_start")

    def test_parallel_identical_start_has_one_original_reservation(self):
        preview = self.preview()
        results = self.parallel(lambda _: self.start(preview=preview))
        self.assertEqual(len({value["node_id"] for value in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(len(self.client.applied), 1)

    def test_budget_failure_rolls_back_command_and_intent_without_apply(self):
        self.repo.configure_budget(
            "owner-budget",
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=1,
        )
        with self.assertRaises(BudgetExceeded):
            self.start()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.client.applied, [])

    def test_global_or_hourly_limits_prevent_side_effects(self):
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        with self.assertRaises(BudgetExceeded):
            self.start()
        self.assertEqual(self.client.applied, [])
        self.repo.configure_capacity(max_instances=10, max_physical_gpus=10)
        self.service.policy["max_hourly_cost_microusd"] = 1
        with self.assertRaisesRegex(OperatorError, "dstack_hourly_limit_exceeded"):
            self.start()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_profile_floors_and_mode_cannot_be_weakened(self):
        for field, value in (
            ("memory_gib", 32),
            ("disk_gib", 80),
            ("gpu_memory_gib", 16),
            ("gpu_names", ["A100"]),
            ("mode", "other"),
            ("image", "runtime:latest"),
        ):
            config = copy.deepcopy(self.config)
            config["profiles"][0][field] = value
            with self.assertRaises((OperatorError, DstackError)):
                DstackOperator(self.repo, self.settings, self.capacity, config)

    def test_catalog_prefers_authoring_default_without_changing_saved_profiles(self):
        target = 'h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        config = copy.deepcopy(self.config)
        config['profiles'].append(
            {
                **config['profiles'][0],
                'id': 'controls-v6-fl',
                'runtime_profile_id': target,
            }
        )
        settings = copy.copy(self.settings)
        settings.default_deployment_profile_id = target
        service = DstackOperator(self.repo, settings, self.capacity, config)
        original = copy.deepcopy(service.profiles)
        self.assertEqual(
            [p['id'] for p in service.catalog(self.owner)['profiles']],
            ['controls-v6-fl', '5090-fl'],
        )
        self.assertEqual(service.profiles, original)
        self.assertEqual(service.catalog(self.other_owner)['profiles'], [])

    def test_owner_isolation_and_machine_principal_denied(self):
        node = self.start()["node_id"]
        self.assertEqual(self.service.catalog(self.other_owner)["profiles"], [])
        self.assertEqual(self.service.state(self.other_owner)["nodes"], [])
        with self.assertRaisesRegex(OperatorError, "dstack_node_not_found"):
            self.service.node_command(self.other_owner, node, {}, "stop-1")
        with self.assertRaisesRegex(OperatorError, "operator_forbidden"):
            self.service.catalog(Principal("superdan", "api", machine=True))

    def test_29gib_profile_admission_binds_each_mode_and_preserves_legacy_floor(self):
        from studio_platform.runtime_catalog import engine_manifest, PROFILE_IDS

        for revised in (PROFILE_IDS[4], PROFILE_IDS[5]):
            for mode in ('fl', 'ref'):
                config = copy.deepcopy(self.config)
                profile = config['profiles'][0]
                profile.update(
                    runtime_profile_id=revised,
                    mode=mode,
                    memory_gib=29,
                    configuration_id='test-native-29gib-' + mode,
                )
                service = DstackOperator(
                    self.repo, self.settings, self.capacity, config
                )
                request = service._capacity_request(
                    service.profiles['5090-fl'], str(uuid.uuid4()), 1.0, 121.0, 120
                )
                self.assertEqual(request.memory_gib, 29)
                self.assertEqual(
                    request.manifest_digest, engine_manifest(revised, mode).digest
                )
                self.assertEqual(request.configuration_id, 'test-native-29gib-' + mode)
                profile['memory_gib'] = 28
                with self.assertRaisesRegex(OperatorError, 'resource_floor_invalid'):
                    DstackOperator(self.repo, self.settings, self.capacity, config)
        original = copy.deepcopy(self.config)
        original['profiles'][0]['memory_gib'] = 29
        with self.assertRaisesRegex(OperatorError, 'resource_floor_invalid'):
            DstackOperator(self.repo, self.settings, self.capacity, original)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_base_single_gpu_fl_candidate_binds_144gib_floor_without_rental(self):
        from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest

        candidate = PROFILE_IDS[7]
        config = copy.deepcopy(self.config)
        profile = config['profiles'][0]
        profile.update(
            runtime_profile_id=candidate,
            memory_gib=144,
            disk_gib=350,
            gpu_memory_gib=90,
            gpu_names=['RTXPRO6000S', 'RTXPRO6000WS'],
            max_price_microusd=2000000,
            configuration_id='new-base-single-fl',
        )
        service = DstackOperator(self.repo, self.settings, self.capacity, config)
        request = service._capacity_request(
            service.profiles['5090-fl'], str(uuid.uuid4()), 1.0, 121.0, 120
        )
        self.assertEqual(
            (
                request.memory_gib,
                request.gpu_count,
                request.disk_gib,
                request.max_price_microusd,
            ),
            (144, 1, 350, 2000000),
        )
        self.assertEqual(
            request.manifest_digest, engine_manifest(candidate, 'fl').digest
        )
        for field, value in (
            ('memory_gib', 143),
            ('disk_gib', 349),
            ('gpu_memory_gib', 89),
            ('mode', 'ref'),
            ('gpu_names', ['RTXPRO6000Max-Q']),
        ):
            bad = copy.deepcopy(config)
            bad['profiles'][0][field] = value
            with (
                self.subTest(field=field),
                self.assertRaises((OperatorError, ValueError)),
            ):
                DstackOperator(self.repo, self.settings, self.capacity, bad)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_base_fl_examples_bind_only_qualified_scheduler_aliases_and_original_floors(
        self,
    ):
        from studio_platform.dstack_operator import plan_offers
        from studio_platform.execution_policy import validate_policy
        from studio_platform.runtime_catalog import engine_manifest

        root = Path(__file__).resolve().parents[1] / 'deploy/dstack'
        examples = json.loads(
            (root / 'base-fl.operator-profiles.example.json').read_text()
        )
        policy = validate_policy(
            json.loads((root / 'base-fl.execution-policy.example.json').read_text())
        )
        self.assertFalse(policy['enabled'])
        config = copy.deepcopy(self.config)
        config['profiles'] = examples
        service = DstackOperator(self.repo, self.settings, self.capacity, config)
        for owner, principal in (
            ('superdan', self.owner),
            ('supervan', self.other_owner),
        ):
            profile = next(
                item for item in service.profiles.values() if item['owner_id'] == owner
            )
            self.assertEqual(
                (profile['memory_gib'], profile['disk_gib'], profile['gpu_memory_gib']),
                (256, 350, 90),
            )
            self.assertEqual(
                profile['manifest_digest'],
                engine_manifest(profile['runtime_profile_id'], 'fl').digest,
            )
            for field in ('pool', 'configuration_id', 'model_id'):
                self.assertEqual(profile[field], policy[field])
            self.assertEqual(
                policy['engine_manifest_digest'], profile['manifest_digest']
            )
            request = service._capacity_request(
                profile, str(uuid.uuid4()), 1.0, 121.0, 120
            )
            self.assertEqual(request.gpu_count, 1)
            self.assertEqual(request.gpu_names, ('RTXPRO6000S', 'RTXPRO6000WS'))
            self.assertEqual(request.max_price_microusd, 2000000)
            spec = request.run_spec(PUB)
            self.assertEqual(spec['configuration']['resources']['gpu']['count'], 1)
            offered = {
                'backend': 'vastai',
                'region': 'us-wisconsin',
                'price': 1.51722,
                'availability': 'available',
                'instance': {
                    'resources': {
                        'cpus': 128,
                        'memory_mib': 527974,
                        'disk': {'size_mib': 350 * 1024},
                        'gpus': [{'name': 'RTXPRO6000S', 'memory_mib': 98304}],
                    }
                },
            }
            self.assertEqual(
                len(plan_offers({'job_plans': [{'offers': [offered]}]}, request)), 1
            )
            offered['instance']['resources']['gpus'][0]['name'] = 'RTXPRO6000Max-Q'
            self.assertEqual(
                plan_offers({'job_plans': [{'offers': [offered]}]}, request), []
            )
            # Catalog remains owner-scoped even when both templates are installed.
            self.assertEqual(
                [item['id'] for item in service.catalog(principal)['profiles']],
                [profile['id']],
            )
        for field, value in (
            ('memory_gib', 255),
            ('disk_gib', 349),
            ('gpu_memory_gib', 87),
            ('gpu_names', ['RTXPRO6000Max-Q']),
            ('gpu_names', ['RTXPRO6000']),
            ('gpu_names', ['RTX5090']),
        ):
            bad = copy.deepcopy(config)
            bad['profiles'][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(OperatorError),
            ):
                DstackOperator(self.repo, self.settings, self.capacity, bad)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_lost_apply_and_missing_run_never_reapply_or_release_reservation(self):
        self.client.lost = True
        node = self.start()["node_id"]
        binding = self.store.load(node)
        self.client.absent = True
        request = self.service._capacity_request(
            self.service.profiles["5090-fl"],
            node,
            binding["created_at"],
            binding["hard_deadline"],
            900,
        )
        request = replace(
            request, host_selection=copy.deepcopy(binding["host_selection"])
        )
        for _ in range(3):
            self.assertEqual(
                self.capacity.start(request, PUB)["state"], "creation_unknown"
            )
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(budget_reservations.c.state)).scalar_one(),
                "reserved",
            )

    def test_run_identity_and_incarnation_are_immutable(self):
        node = self.start()["node_id"]
        self.running(node)
        for key, value in (
            ("run_id", str(uuid.uuid4())),
            ("provider_instance_id", "other-gpu"),
            ("runtime_incarnation", "d" * 32),
        ):
            with self.assertRaisesRegex(
                OperatorError, "dstack_observation_identity_changed"
            ):
                self.store.record(
                    node,
                    {
                        key: value,
                        "state": "ready",
                        "ready": True,
                        "observed_at": self.now,
                    },
                )

    def test_ready_requires_native_proof_and_fresh_observation(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.capacity.readiness = lambda binding, run: HostReadiness(
            binding["manifest_digest"], node, "c" * 32, True
        )
        self.running(node)
        state = self.service.state(self.owner)["nodes"][0]
        self.assertTrue(state["native_ready"])
        self.assertFalse(state["ready"])  # Native endpoint alone has no CPU consumer.
        self.now += 31
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["native_ready"])

    def test_no_stock_or_unconfirmed_plan_cannot_offer_paid_start(self):
        self.client.plan = lambda spec: {"job_plans": [{"offers": []}]}
        with self.assertRaisesRegex(OperatorError, "dstack_no_compatible_offers"):
            self.preview()
        self.client.plan = lambda spec: {"job_plans": []}
        with self.assertRaisesRegex(OperatorError, "dstack_plan_unconfirmed"):
            self.preview()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_quote_projection_is_safe_and_unknown_is_not_confirmed_stock(self):
        original = self.client.plan

        def unknown(spec):
            value = original(spec)
            value["job_plans"][0]["offers"][0]["availability"] = "unknown"
            return value

        self.client.plan = unknown
        preview = self.preview()
        self.assertFalse(preview["offers"][0]["inventory_confirmed"])
        self.assertNotIn("SECRET", json.dumps(preview))

    def test_hold_does_not_extend_deadline_increase_budget_or_repeat(self):
        node = self.start()["node_id"]
        first = self.service.set_hold(self.owner, node, {"hold_seconds": 300}, "hold-1")
        self.now += 10
        self.assertEqual(
            self.service.set_hold(self.owner, node, {"hold_seconds": 300}, "hold-1"),
            first,
        )
        self.assertEqual(first["hard_deadline"], 1900)
        self.assertEqual(first["additional_reservation_microusd"], 0)
        with self.assertRaisesRegex(OperatorError, "dstack_hold_deadline_exceeded"):
            self.service.set_hold(self.owner, node, {"hold_seconds": 1000}, "hold-2")

    def test_stop_needs_owner_drain_and_preserves_unverified_bill(self):
        node = self.start()["node_id"]
        self.running(node)
        self.assertFalse(self.store.begin_stop(node, self.store.load(node)["run_id"]))
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.capacity.stop(node)
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)
        self.client.runs[self.store.load(node)["run_name"]]["status"] = "terminated"
        self.capacity.observe(node)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(instance_intents.c.state)).scalar_one(),
                "destroying",
            )
            self.assertEqual(
                connection.execute(select(budget_reservations.c.state)).scalar_one(),
                "reserved",
            )

    def test_original_worker_and_job_obligations_block_stop(self):
        node = self.start()["node_id"]
        self.running(node)
        job = self.job()
        with self.repo.transaction() as connection:
            connection.execute(
                insert(registered_workers).values(
                    id="worker-1",
                    pool="dstack-test",
                    provider="vast",
                    instance_id="1234567",
                    spec={},
                    spec_hash="a" * 64,
                    state="busy",
                    current_job_id=job["id"],
                    drain_requested=0,
                    fence=1,
                    expires_at=self.now + 60,
                    updated_at=self.now,
                )
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(lease_worker_id="worker-1")
            )
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers).values(state="retired", current_job_id=None)
            )
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="cancelled")
            )
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)

    def test_consumed_expired_or_changed_preview_cannot_create(self):
        preview = self.preview()
        self.start(preview=preview)
        with self.assertRaisesRegex(OperatorError, "dstack_preview_consumed"):
            self.start(key="different", preview=preview)
        preview = self.preview()
        self.now += 121
        with self.assertRaisesRegex(OperatorError, "dstack_preview_expired"):
            self.start(key="expired", preview=preview)
        self.assertEqual(len(self.client.applied), 1)

    def test_bootstrap_journal_precedes_remote_start_and_unknown_blocks_stop(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        binding = self.store.load(node)
        self.assertTrue(self.store.begin_bootstrap(node, binding["run_id"], "1234567"))
        self.assertFalse(self.store.begin_bootstrap(node, binding["run_id"], "1234567"))
        self.assertTrue(
            self.store.begin_runtime_launch(node, binding["run_id"], "1234567")
        )
        # A runtime incarnation proves this is no longer cold unadmitted
        # preparation; missing native idle must continue to block shutdown.
        self.store.record(
            node,
            {
                "state": "runtime_unconfirmed",
                "ready": False,
                "runtime_incarnation": "c" * 32,
                "observed_at": self.now,
            },
        )
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        self.assertEqual(self.client.stopped, [])

    def test_retired_worker_and_terminal_job_with_unknown_upstream_still_block_stop(
        self,
    ):
        node = self.start()["node_id"]
        self.running(node)
        job = self.job()
        with self.repo.transaction() as connection:
            connection.execute(
                insert(registered_workers).values(
                    id="worker-1",
                    pool="dstack-test",
                    provider="vast",
                    instance_id="1234567",
                    spec={},
                    spec_hash="a" * 64,
                    state="retired",
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now - 60,
                    updated_at=self.now,
                )
            )
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
            connection.execute(
                insert(attempts).values(
                    id=str(uuid.uuid4()),
                    job_id=job["id"],
                    number=1,
                    status="submission_unknown",
                    fence=1,
                    worker_id="worker-1",
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now,
                    upstream_stopped=0,
                    collection_failures=0,
                )
            )
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.job_id == job["id"])
                .values(upstream_stopped=1)
            )
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)

    def test_cancel_releases_only_atomic_never_applied_proof(self):
        # Crash before the start journal is a provably unpaid reserved command.
        with patch.object(
            self.capacity, "start", side_effect=DstackError("dstack_client_not_started")
        ):
            node = self.start()["node_id"]
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertTrue(self.store.cancel_unapplied(node))
        self.assertFalse(self.store.cancel_unapplied(node))
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(budget_reservations.c.state)).scalar_one(),
                "released",
            )
            self.assertEqual(
                connection.execute(
                    select(budget_reservations.c.actual_cost_microusd)
                ).scalar_one(),
                0,
            )
        node = self.start(key="started-2")["node_id"]
        self.service.node_command(self.owner, node, {}, "stop-2")
        self.assertFalse(self.store.cancel_unapplied(node))

    def test_disabled_factory_has_no_client_io_and_protected_config_no_plaintext_token(
        self,
    ):
        with patch.dict(os.environ, {"DSTACK_OPERATOR_CONFIG": ""}):
            self.assertFalse(
                from_environment(self.repo, self.settings).state(self.owner)["enabled"]
            )
        root = Path(self.temp.name)
        token = root / "token"
        token.write_text("fake-private-token-32-characters-long")
        pub = root / "key.pub"
        pub.write_text(PUB)
        filename = root / "config.json"
        config = copy.deepcopy(self.config)
        config.pop("ssh_public_key")
        config.update(
            endpoint="http://127.0.0.1:3000",
            project="sixnine",
            token_file=str(token),
            ssh_public_key_file=str(pub),
        )
        filename.write_text(json.dumps(config))
        for path in (filename, token, pub):
            path.chmod(0o600)
        with (
            patch.dict(os.environ, {"DSTACK_OPERATOR_CONFIG": str(filename)}),
            patch("httpx.Client.post", side_effect=AssertionError("network forbidden")),
        ):
            service = from_environment(self.repo, self.settings)
            self.assertTrue(service.catalog(self.owner)["enabled"])
            self.assertNotIn("fake-private-token", json.dumps(service.config))
            service.capacity.client.close()


class DstackLifetimeTests(LedgerCase):
    """Continuing policy uses original money/identity authority, never a TTL."""

    start = DstackOperatorTests.start
    running = DstackOperatorTests.running
    counts = DstackOperatorTests.counts

    def setUp(self):
        super().setUp()
        DstackOperatorTests._setup_dstack(self)
        self.config['policy'].update(lifecycle_version=2, reservation_seconds=3600)
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        profile = self.service.profiles['5090-fl']
        self.execution_policy = dict(
            enabled=True,
            backend='wangp-worker',
            dispatch_backend='hatchet-v1',
            pool=profile['pool'],
            configuration_id=profile['configuration_id'],
            model_id=profile['model_id'],
            engine_manifest_digest=profile['manifest_digest'],
            qualification=dict(status='accepted', verified_at=1.0, expires_at=20000.0),
            reservation=dict(expected_runtime_s=1800, expires_at=20000.0),
            envelope=dict(max_duration_seconds=4.0),
        )
        patched = patch(
            'studio_platform.execution_profiles.selected_policy',
            return_value=self.execution_policy,
        )
        patched.start()
        self.addCleanup(patched.stop)
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts).values(limit_microusd=10_000_000)
            )

    def preview(self, **options):
        return self.service.preview(self.owner, {'profile_id': '5090-fl', **options})

    def controller(self):
        from test_dstack_controller import DstackControllerTests

        return DstackControllerTests.controller(self)

    def test_v2_preview_initial_chunk_is_not_lifetime_and_start_freezes_original_authority(
        self,
    ):
        preview = self.preview(ttl_seconds=900, keep_on='until_stopped')
        self.assertIsNone(preview['hard_deadline'])
        self.assertEqual(preview['initial_reservation_seconds'], 3600)
        self.assertEqual(preview['reservation_microusd'], 812500)
        result = self.start(preview=preview)
        self.assertEqual(self.start(preview=preview), result)
        node = result['node_id']
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
            payload = connection.execute(select(operator_nodes.c.payload)).scalar_one()
        self.assertIsNone(intent['hard_deadline'])
        self.assertIsNone(payload['request']['max_duration_s'])
        self.assertEqual(payload['request']['lifecycle_version'], 2)
        self.assertEqual(
            payload['dstack']['run_spec']['configuration']['max_duration'], 'off'
        )
        self.assertEqual(payload['lifecycle']['budget_account_ids'], ['owner-budget'])
        self.assertEqual(payload['lifecycle']['expected_runtime_s'], 1800)
        self.assertEqual(payload['lifecycle']['keep_on'], 'until_stopped')
        self.assertEqual(self.client.applied, [node])

    def test_original_protected_execution_policy_and_owner_money_cap_required_before_apply(
        self,
    ):
        preview = self.preview()
        self.execution_policy['reservation']['expected_runtime_s'] = 2000
        with self.assertRaisesRegex(OperatorError, 'execution_policy_changed'):
            self.start(preview=preview)
        self.execution_policy['reservation']['expected_runtime_s'] = 1800
        with self.repo.transaction() as connection:
            connection.execute(update(budget_accounts).values(owner_id=None))
        with self.assertRaisesRegex(OperatorError, 'owner_spend_limit_required'):
            self.start(preview=preview)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.execution_policy['configuration_id'] = 'different'
        with self.assertRaisesRegex(OperatorError, 'execution_policy_unconfirmed'):
            self.preview()

    def test_short_idle_funds_collection_margin_for_initial_and_rolling_windows(self):
        from studio_platform.worker_admission import dstack_budget_reason

        self.execution_policy['reservation']['expected_runtime_s'] = 1765
        self.service.policy['reservation_seconds'] = 120
        preview = self.preview(idle_shutdown_seconds=1)
        self.assertEqual(preview['initial_reservation_seconds'], 1920)
        self.assertEqual(preview['reservation_microusd'], 462500)
        node_id = self.start(preview=preview)['node_id']
        self.running(node_id)
        with self.repo.transaction() as connection:
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            self.assertEqual(
                self.service.fund_window(connection, intent, node, self.now), 462500
            )
            self.assertIsNone(
                dstack_budget_reason(
                    connection, intent, node, self.now, expected_runtime_s=1765
                )
            )

    def test_exact_chunk_boundary_funds_strictly_more_than_required_job_runway(self):
        from studio_platform.worker_admission import dstack_budget_reason

        self.service.policy['reservation_seconds'] = 120
        preview = self.preview(idle_shutdown_seconds=1)
        self.assertEqual(preview['initial_reservation_seconds'], 2040)
        self.assertEqual(preview['reservation_microusd'], 487500)
        node_id = self.start(preview=preview)['node_id']
        self.running(node_id)
        with self.repo.transaction() as connection:
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            self.assertEqual(
                self.service.fund_window(connection, intent, node, self.now), 487500
            )
            self.assertIsNone(
                dstack_budget_reason(
                    connection, intent, node, self.now, expected_runtime_s=1800
                )
            )

    def test_hold_preferences_preserve_null_lifetime_original_identity_and_idempotency(
        self,
    ):
        node = self.start()['node_id']
        original = self.store.load(node)
        self.now += 1
        body = {
            'keep_on': 'until_stopped',
            'expected_version': self.service.state(self.owner)['nodes'][0]['version'],
        }
        self.now += 1
        self.store.record(
            node,
            {
                'observed_at': self.now,
                'state': 'starting',
                'ready': False,
            },
        )
        result = self.service.set_hold(self.owner, node, body, 'always-original')
        self.assertEqual(
            self.service.set_hold(self.owner, node, body, 'always-original'), result
        )
        self.assertIsNone(result['hard_deadline'])
        self.assertEqual(result['lifecycle']['keep_on'], 'until_stopped')
        self.assertEqual(self.service.state(self.owner)['nodes'][0]['version'], 1)
        self.service.set_hold(
            self.owner, node, {'idle_shutdown_seconds': 900}, 'idle-adjust-only'
        )
        state = self.service.state(self.owner)['nodes'][0]
        self.assertEqual(state['lifecycle']['keep_on'], 'until_stopped')
        self.assertEqual(state['lifecycle']['idle_shutdown_seconds'], 900)
        self.assertEqual(state['version'], 2)
        for field in (
            'run_id',
            'run_name',
            'spec_digest',
            'created_at',
            'hard_deadline',
            'apply_started_at',
        ):
            self.assertEqual(self.store.load(node)[field], original[field])
        with self.assertRaisesRegex(OperatorError, 'node_not_found'):
            self.service.set_hold(
                self.other_owner, node, {'keep_on': 'idle'}, 'wrong-owner'
            )
        with self.assertRaisesRegex(OperatorError, 'version_conflict'):
            self.service.set_hold(
                self.owner,
                node,
                {'keep_on': 'idle', 'expected_version': -1},
                'stale-version',
            )

    def test_cold_paid_elapsed_is_funded_before_native_callback_without_second_apply(
        self,
    ):
        node = self.start()['node_id']
        self.capacity.readiness = None
        self.running(node)
        self.now += 1400
        controller = self.controller()

        def readiness(binding, run):
            with self.repo.engine.connect() as connection:
                intent = connection.execute(select(instance_intents)).mappings().one()
                self.assertGreater(intent['reserved_cost_microusd'], 812500)
            return HostReadiness(
                binding['manifest_digest'], binding['intent_id'], 'c' * 32, True
            )

        controller.runtime.readiness = readiness
        self.capacity.readiness = readiness
        self.assertEqual(controller.tick()['nodes'][0]['state'], 'ready')
        self.assertEqual(self.client.applied, [node])
        self.assertEqual(self.client.stopped, [])
        self.assertEqual(self.repo.get_budget('owner-budget')['spent_microusd'], 0)

    def terminal_history(self, *, allocated=False):
        from test_dstack_capacity import no_capacity_run

        node = self.start()['node_id']
        binding = self.store.load(node)
        run = self.client.runs[binding['run_name']]
        if allocated:
            run.update(
                status='failed',
                termination_reason='job_failed',
                latest_job_submission={
                    'status': 'failed',
                    'termination_reason': 'waiting_instance_limit_exceeded',
                    'job_provisioning_data': {
                        'backend': 'vastai',
                        'instance_id': '1234567',
                        'instance_type': {'resources': {'gpus': [{'name': 'RTX5090'}]}},
                    },
                },
            )
            # New host observation latches a positive allocation as soon as
            # JPD is known. Seed that real pre-terminal boundary, then test
            # that terminal refresh still cannot settle/remove its history.
            run['status'] = 'provisioning'
            self.capacity.observe(node)
            run['status'] = 'failed'
        else:
            self.client.runs[binding['run_name']] = no_capacity_run(run)
        # Reproduce the already-retained stopping projection: funding's policy
        # rejects destroying while desired state is still running.
        self.store.record(
            node,
            {
                'state': 'stopping',
                'ready': False,
                'reason_code': 'dstack_stop_pending',
                'observed_at': self.now,
            },
        )
        return node

    def assert_terminal_history_refresh(self, *, allocated):
        node = self.terminal_history(allocated=allocated)
        with self.repo.engine.connect() as connection:
            before = tuple(
                [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in (instance_intents, budget_reservations, budget_accounts)
            )
        self.now += 1
        controller = self.controller()
        with (
            patch.object(
                self.service,
                'fund_window',
                side_effect=AssertionError('terminal must not fund'),
            ) as fund,
            patch.object(
                self.capacity,
                'readiness',
                side_effect=AssertionError('terminal must not bootstrap'),
            ) as native,
        ):
            self.assertEqual(controller.tick()['nodes'][0]['state'], 'stopped')
            fund.assert_not_called()
            native.assert_not_called()
        state = DstackWorkspace(self.service).state(self.owner)['nodes'][0]
        self.assertEqual(state['lifecycle_phase'], 'stopped')
        self.assertFalse(state['ready'])
        self.assertEqual(state['billing_state'], 'unsettled')
        self.assertEqual(
            self.store.load(node)['reason_code'],
            'dstack_run_terminal_billing_pending'
            if allocated
            else 'dstack_no_capacity_billing_pending',
        )
        with self.repo.engine.connect() as connection:
            after = tuple(
                [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in (instance_intents, budget_reservations, budget_accounts)
            )
            self.assertIsNone(
                connection.execute(select(registered_workers.c.id)).first()
            )
        self.assertEqual(after, before)
        self.assertEqual(self.client.applied, [node])
        self.assertEqual(self.client.stopped, [])

    def test_destroying_no_allocation_history_refreshes_without_funding_or_bootstrap(
        self,
    ):
        self.assert_terminal_history_refresh(allocated=False)

    def test_destroying_allocated_terminal_history_ends_without_settling_removal_or_bill(
        self,
    ):
        self.assert_terminal_history_refresh(allocated=True)

    def test_destroying_nonterminal_cannot_bypass_funding_or_reopen_native_admission(
        self,
    ):
        node = self.terminal_history()
        binding = self.store.load(node)
        run = self.client.runs[binding['run_name']]
        run.update(
            status='running',
            latest_job_submission={
                'job_provisioning_data': {
                    'backend': 'vastai',
                    'instance_id': 'original-allocation',
                    'instance_type': {'resources': {'gpus': [{'name': 'RTX5090'}]}},
                }
            },
        )
        before = self.store.load(node)
        budget = self.repo.get_budget('owner-budget')
        controller = self.controller()
        with (
            patch.object(
                self.service, 'fund_window', wraps=self.service.fund_window
            ) as fund,
            patch.object(
                self.capacity,
                'readiness',
                side_effect=AssertionError('unfunded bootstrap'),
            ) as native,
        ):
            result = controller.tick()['nodes'][0]
            self.assertEqual(result['code'], 'dstack_controller_observation_failed')
            fund.assert_called_once()
            native.assert_not_called()
        self.assertEqual(self.store.load(node), before)
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        self.assertEqual(self.client.applied, [node])
        self.assertEqual(self.client.stopped, [])

    def test_always_cannot_bypass_money_cap_cold_stop_keeps_reserved_unknown_bill(self):
        node = self.start(preview=self.preview(keep_on='until_stopped'))['node_id']
        self.capacity.readiness = None
        self.running(node)
        self.now += 1400
        before = self.repo.get_budget('owner-budget')
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts).values(
                    limit_microusd=before['reserved_microusd']
                )
            )
        controller = self.controller()
        self.capacity.readiness = None
        result = controller.tick()['nodes'][0]
        self.assertEqual(result['state'], 'stopping')
        self.assertEqual(len(self.client.stopped), 1)
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
            payload = connection.execute(select(operator_nodes.c.payload)).scalar_one()
            reservation = (
                connection.execute(select(budget_reservations)).mappings().one()
            )
        self.assertEqual(intent['reserved_cost_microusd'], before['reserved_microusd'])
        self.assertEqual(reservation['state'], 'reserved')
        self.assertIsNone(reservation['actual_cost_microusd'])
        self.assertEqual(payload['stop_reason_code'], 'dstack_spend_limit_reached')

    def test_legacy_obligation_does_not_count_new_path_and_terminal_absence_retains_old_money(
        self,
    ):
        legacy = self.repo.reserve_instance_intent(
            self.scope,
            'dstack-test',
            'legacy-original',
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=100000,
            hard_deadline=self.now + 9000,
            budget_account_ids=['owner-budget'],
            provider='lium',
            dry_run=False,
        )
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool('dstack-test', max_instances=1, max_physical_gpus=1)
        self.service.policy.update(max_instances=1, max_physical_gpus=1)
        node = self.start()['node_id']
        self.running(node)
        run = self.client.runs[self.store.load(node)['run_name']]
        run['status'] = 'terminated'
        self.service.provider_inventory = lambda backend: dict(
            backend=backend, complete=True, observed_at=self.now, instance_ids=[]
        )
        old_budget = self.repo.get_budget('owner-budget')
        with self.repo.transaction() as connection:
            usage = self.service._limits(connection, 750000)
        self.assertEqual(usage['instances'], 0)
        self.assertEqual(usage['active_instance_ids'], ())
        self.assertEqual(self.repo.get_budget('owner-budget'), old_budget)
        self.assertEqual(len(self.client.applied), 1)
        self.service.provider_inventory = lambda backend: dict(
            backend=backend, complete=True, observed_at=self.now - 31, instance_ids=[]
        )
        with self.repo.transaction() as connection:
            with self.assertRaisesRegex(OperatorError, 'capacity_limit_exceeded'):
                self.service._limits(connection, 750000)
        with self.repo.engine.connect() as connection:
            original = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == legacy['id']
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(original['state'], 'reserved')
            self.assertEqual(original['reserved_cost_microusd'], 100000)

    def test_budget_exhaustion_drains_active_original_attempt_without_provider_stop_or_settlement(
        self,
    ):
        node = self.start(preview=self.preview(keep_on='until_stopped'))['node_id']
        self.running(node)
        controller = self.controller()
        controller.tick()
        job = self.job(status='queued')
        with self.repo.transaction() as connection:
            worker = connection.execute(select(registered_workers)).mappings().one()
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(
                    status='running', pool='dstack-test', lease_worker_id=worker['id']
                )
            )
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker['id'])
                .values(current_job_id=job['id'], state='busy')
            )
            connection.execute(
                insert(attempts).values(
                    id='original-running',
                    job_id=job['id'],
                    number=1,
                    status='running',
                    fence=1,
                    worker_id=worker['id'],
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now,
                    upstream_stopped=0,
                )
            )
            connection.execute(
                update(budget_accounts).values(
                    limit_microusd=budget_accounts.c.reserved_microusd
                )
            )
        self.now += 1400
        self.assertEqual(controller.tick()['nodes'][0]['state'], 'draining')
        self.assertEqual(self.client.stopped, [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(jobs.c.status).where(jobs.c.id == job['id'])
                ).scalar_one(),
                'running',
            )
            self.assertEqual(
                connection.execute(select(attempts.c.upstream_stopped)).scalar_one(), 0
            )
            self.assertEqual(
                connection.execute(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_type == 'instance',
                        budget_reservations.c.reference_id == node,
                    )
                ).scalar_one(),
                'reserved',
            )
            self.assertEqual(
                connection.execute(select(registered_workers.c.state)).scalar_one(),
                'draining',
            )

    def test_failed_hold_funding_rolls_back_policy_and_preserves_original_reservation(
        self,
    ):
        node = self.start()['node_id']
        original = self.service.state(self.owner)['nodes'][0]['lifecycle']
        before = self.repo.get_budget('owner-budget')
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts).values(
                    limit_microusd=budget_accounts.c.reserved_microusd
                )
            )
        with self.assertRaises(BudgetExceeded):
            self.service.set_hold(
                self.owner,
                node,
                {'keep_on': 'until_stopped', 'idle_shutdown_seconds': 7200},
                'unfunded-hold',
            )
        self.assertEqual(
            self.service.state(self.owner)['nodes'][0]['lifecycle'], original
        )
        after = self.repo.get_budget('owner-budget')
        self.assertEqual(after['reserved_microusd'], before['reserved_microusd'])
        self.assertEqual(after['spent_microusd'], before['spent_microusd'])

    def accepted_queue(self, *, profile_id=None, configuration_id=None):
        profile = self.service.profiles['5090-fl']
        request = {
            'recipe_id': profile['recipe_id'],
            'deployment_profile_id': profile_id or profile['runtime_profile_id'],
            'request': {'model': profile['model_id'], 'mode': profile['mode']},
        }
        execution = dict(
            pool=profile['pool'],
            backend='wangp-worker',
            enabled=True,
            dispatch_backend='hatchet-v1',
            output_delivery='native-frames-v1',
            configuration_id=configuration_id or profile['configuration_id'],
            engine_manifest_digest=profile['manifest_digest'],
            expected_runtime_s=1800,
            deployment_profile_id=profile['runtime_profile_id'],
        )
        plan = self.repo.create_plan(
            self.scope,
            request,
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        return self.repo.create_job(
            self.scope, plan['id'], str(__import__('uuid').uuid4())
        )

    def queue_controller(self):
        controller = self.controller()
        original_slot = controller.runtime.slot

        def native_slot(intent_id):
            slot = original_slot(intent_id)
            return replace(
                slot, spec=replace(slot.spec, output_delivery='native-frames-v1')
            )

        controller.runtime.slot = native_slot
        return controller

    def test_accepted_matching_queue_pauses_idle_but_other_configuration_does_not(self):
        node = self.start()['node_id']
        self.running(node)
        controller = self.queue_controller()
        controller.tick()
        job = self.accepted_queue()
        self.now += 601
        self.assertEqual(controller.tick()['nodes'][0]['state'], 'ready')
        self.assertEqual(self.client.stopped, [])
        with self.repo.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    select(jobs.c.status).where(jobs.c.id == job['id'])
                ).scalar_one(),
                'queued',
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(status='cancelled', updated_at=self.now - 601)
            )
        self.accepted_queue(configuration_id='another-original-config')
        self.assertEqual(controller.tick()['nodes'][0]['state'], 'stopping')
        self.assertEqual(len(self.client.stopped), 1)

    def test_idle_request_rechecks_new_queue_before_drain_and_spend_stop_still_drains(
        self,
    ):
        node_id = self.start()['node_id']
        self.running(node_id)
        controller = self.queue_controller()
        controller.tick()
        self.now += 601
        original_request_stop = controller._request_stop

        def accept_between_observation_and_stop(node, **options):
            self.accepted_queue()
            return original_request_stop(node, **options)

        with patch.object(
            controller, '_request_stop', side_effect=accept_between_observation_and_stop
        ):
            self.assertEqual(controller.tick()['nodes'][0]['state'], 'ready')
        with self.repo.engine.connect() as connection:
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
        self.assertEqual(node['desired_state'], 'running')
        self.assertEqual(worker['state'], 'ready')
        self.assertEqual(self.client.stopped, [])
        self.assertTrue(
            controller._request_stop(node, reason_code='dstack_spend_limit_reached')
        )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(operator_nodes.c.desired_state)).scalar_one(),
                'stopped',
            )

"""Exact local source/ledger/SSH contracts; no supplier or paid operation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tarfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update

from deploy.wangp.package_tool import PRIVATE_FILES
from studio_platform.auth import Principal
from studio_platform.dstack_capacity import DstackCapacity, DstackError
from studio_platform.dstack_factory import RuntimeFactory, coordinates_for_run, create_runtime
from studio_platform.dstack_operator import DstackOperator, LedgerDstackStore
from studio_platform.dstack_runtime import DstackNativeRuntime
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.operator_capacity import operator_nodes
from studio_platform.runtime_catalog import engine_manifest, PROFILE_IDS
from test_dstack_operator import Client, PROFILE, PUB
from test_platform_repository import LedgerCase
from tools.build_dstack_sources import build


class FactoryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.repo.configure_pool("dstack-test", max_instances=3, max_physical_gpus=3)
        self.settings = SimpleNamespace(tenant_id=self.scope.tenant_id, operator_capacity_owners=("superdan",))
        self.store = LedgerDstackStore(self.repo, self.scope.tenant_id)
        self.client = Client(self.store)
        self.capacity = DstackCapacity(self.client, self.store, clock=self.repo.clock)
        profiles = [{"id": "5090-" + mode, "owner_id": "superdan", "project_id": "project-1",
            "budget_account_ids": ["owner-budget"], "pool": "dstack-test", "backend": "vastai",
            "runtime_profile_id": PROFILE, "mode": mode, "configuration_id": "test-native-" + mode,
            "image": "example/native@sha256:" + "a"*64, "gpu_names": ["RTX5090"],
            "memory_gib": 96, "disk_gib": 128, "gpu_memory_gib": 30, "cpu_count": 12,
            "max_price_microusd": 750000, "max_ttl_seconds": 1800, "extra_reservation_microusd": 0}
            for mode in ("fl", "ref")]
        self.operator = DstackOperator(self.repo, self.settings, self.capacity, {"version": 1,
            "ssh_public_key": PUB, "policy": {"enabled": True, "expires_at": 5000., "max_instances": 3,
                "max_physical_gpus": 3, "max_hourly_cost_microusd": 3000000, "max_ttl_seconds": 1800,
                "idle_shutdown_seconds": 600, "observation_fresh_seconds": 30}, "profiles": profiles})
        self.owner = Principal("superdan", "browser-1")
        build(self.root / "sources", [(PROFILE, "fl"), (PROFILE, "ref")])
        self.key = self.root / "ssh-key"; self.key.write_text("private test fixture", encoding="utf-8"); self.key.chmod(0o600)
        self.broker = self.root / "hatchet.json"; self.broker.write_text('{"token":"secret test fixture"}'); self.broker.chmod(0o600)
        self.config = {"version": 1, "work_dir": str(self.root / "runtime"),
            "source_index_file": str(self.root / "sources/index.json"), "ssh_key_file": str(self.key),
            "known_hosts_file": str(self.root / "known-hosts"), "broker_config_file": str(self.broker),
            "first_local_port": 19100, "last_local_port": 19102, "trust_first_host_key": False}
        self.config_path = self.root / "runtime-config.json"
        self.save_config()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config)); self.config_path.chmod(0o600)

    def allocation(self, mode="fl", key="one"):
        preview = self.operator.preview(self.owner, {"profile_id": "5090-"+mode, "ttl_seconds": 900})
        accepted = self.operator.start(self.owner, {"preview_id": preview["preview_id"]}, key)
        binding = self.store.load(accepted["node_id"])
        run = self.client.runs[binding["run_name"]]
        run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "gpu-"+key, "hostname": "198.51.100.8",
            "ssh_port": 21882, "username": "root", "dockerized": False, "ssh_proxy": None,
            "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})
        self.store.record(binding["intent_id"], {"run_id": run["id"], "provider_instance_id": "gpu-"+key,
            "state": "runtime_unconfirmed", "observed_at": self.now})
        return self.store.load(binding["intent_id"]), run

    def factory(self):
        return RuntimeFactory(self.repo, self.store, self.config_path)

    def test_explicit_source_selection_has_four_files_and_original_small_bundle_only(self):
        index = json.loads((self.root / "sources/index.json").read_text())
        self.assertEqual(len(index["sources"]), 2)
        for entry in index["sources"]:
            self.assertEqual((entry["gpu_count"], entry["profile_slot_index"]), (1, 0))
            directory = self.root / "sources" / entry["directory"]
            self.assertEqual({p.name for p in directory.iterdir()}, set(entry["source_sha256"]))
            with tarfile.open(directory / "wangp-package.tar.gz") as archive:
                self.assertEqual(set(archive.getnames()), set(PRIVATE_FILES))
                self.assertTrue(all(item.isfile() for item in archive.getmembers()))
            self.assertEqual(entry["engine_manifest_digest"], engine_manifest(PROFILE, entry["mode"]).digest)
        with self.assertRaises(ValueError):
            build(self.root / "sources", [(PROFILE, "fl")])
        with self.assertRaises(ValueError):
            build(self.root / "invalid", [(PROFILE, "unsupported")])
        self.assertFalse((self.root / "invalid").exists())

    def test_29gib_sources_package_and_bind_both_independent_modes(self):
        revised = PROFILE_IDS[4]
        directory = self.root / 'sources-29gib'
        receipt = build(directory, [(revised, 'fl'), (revised, 'ref')])
        self.assertEqual({entry['mode'] for entry in receipt['sources']}, {'fl', 'ref'})
        for entry in receipt['sources']:
            self.assertEqual(entry['runtime_profile_id'], revised)
            source = directory / entry['directory']
            manifest = json.loads((source / 'wangp-manifest.json').read_text())
            self.assertEqual(manifest['runtime_profile']['minimum_available_ram_bytes'], 29 * 2**30)
            self.assertEqual(entry['engine_manifest_digest'], engine_manifest(revised, entry['mode']).digest)
            with tarfile.open(source / 'wangp-package.tar.gz') as archive:
                packaged = json.load(archive.extractfile('deploy/wangp/profiles/' + revised + '.json'))
                self.assertEqual(packaged['minimum_ram_bytes'], 29 * 2**30)
        self.assertEqual(self.client.applied, [])

    def test_total_capacity_sources_keep_new_manifest_and_both_mode_bindings(self):
        revised = PROFILE_IDS[5]
        directory = self.root / 'sources-29gib-total'
        receipt = build(directory, [(revised, 'fl'), (revised, 'ref')])
        self.assertEqual({entry['mode'] for entry in receipt['sources']}, {'fl', 'ref'})
        for entry in receipt['sources']:
            source = directory / entry['directory']
            manifest = json.loads((source / 'wangp-manifest.json').read_text())
            self.assertEqual(entry['runtime_profile_id'], revised)
            self.assertEqual(entry['engine_manifest_digest'], engine_manifest(revised, entry['mode']).digest)
            self.assertEqual(manifest['runtime_profile']['ram_admission_metric'], 'effective-total-v1')
            self.assertNotIn('minimum_available_ram_bytes', manifest['runtime_profile'])
            with tarfile.open(source / 'wangp-package.tar.gz') as archive:
                packaged = json.load(archive.extractfile('deploy/wangp/profiles/' + revised + '.json'))
                self.assertEqual(packaged['runtime']['minimum_total_ram_bytes'], 29 * 2**30)
                session = archive.extractfile('studio_platform/runtime_hosts/wangp_session.py').read()
                self.assertIn(b'def _effective_total_profile_ram(', session)
        self.assertEqual(self.client.applied, [])

    def test_construction_reads_only_no_journal_secret_output_or_runtime_directory(self):
        binding, _ = self.allocation()
        before = self.store.load(binding["intent_id"])
        with patch.dict(os.environ, {"DSTACK_RUNTIME_CONFIG": str(self.config_path)}):
            runtime, broker, work_dir = create_runtime(self.repo, self.store)
        self.assertIsInstance(runtime, DstackNativeRuntime)
        self.assertEqual((broker, work_dir), (self.broker, self.root / "runtime"))
        self.assertEqual(self.store.load(binding["intent_id"]), before)
        self.assertFalse(work_dir.exists())
        self.assertNotIn("local_port", before)
        self.assertNotIn("bootstrap_started", before)

    def test_atomic_port_reuse_and_two_nodes_remain_distinct_after_restart(self):
        first, _ = self.allocation()
        second, _ = self.allocation("ref", "two")
        factory = self.factory()
        repeated = self.parallel(lambda _: factory.config_for_binding(first))
        self.assertEqual({config.local_port for config in repeated}, {19100})
        a = factory.config_for_binding(first)
        b = self.factory().config_for_binding(second)
        self.assertEqual((a.local_port, b.local_port), (19100, 19101))
        self.assertEqual(a.pool, "dstack-test")
        self.assertEqual(a.configuration_id, "test-native-fl")
        self.assertEqual(a.engine_manifest_digest, first["manifest_digest"])
        self.assertNotEqual(a.known_hosts_file, b.known_hosts_file)
        self.assertEqual(a.known_hosts_file, self.root / ("known-hosts."+first["intent_id"]))
        self.assertEqual(self.factory().config_for_binding(first).known_hosts_file, a.known_hosts_file)
        self.assertFalse(a.known_hosts_file.exists())
        self.assertFalse(a.trust_first_host_key)

    def test_factory_drives_original_runtime_once_per_node_and_connects_distinct_slots(self):
        first, first_run = self.allocation()
        second, second_run = self.allocation("ref", "two")
        runtime, _, _ = create_runtime(self.repo, self.store, configuration_path=self.config_path)
        remotes = {}
        def ssh_factory(config, coordinates):
            remote = remotes.setdefault(config.local_port, {"starts": 0, "uploads": 0, "config": config})
            class Host:
                def ensure_connected(self):
                    pass  # Verified read-only fake SSH handshake; no network.
                def upload(inner, files):
                    self.assertEqual(set(files), set(config.source_sha256)); remote["uploads"] += 1
                def start(inner, identity):
                    self.assertTrue(self.store.load(identity["intent_id"])["bootstrap_started"])
                    self.assertEqual(identity["instance_id"], coordinates["instance_id"])
                    remote.update(identity=identity, starts=remote["starts"]+1)
                def report(inner):
                    identity = remote["identity"]
                    return {"identity": identity, "state": "ready", "runtime_verified": True,
                        "engine_manifest_digest": config.engine_manifest_digest,
                        "source_revision": engine_manifest(PROFILE, config.mode).document["source_revision"],
                        "gpus": [{"uuid": "GPU-"+identity["intent_id"]}], "runtime": {"gpu_total_bytes": 32*1024**3}}
                def open_tunnel(inner, port):
                    self.assertEqual(port, config.local_port)
                def close(inner):
                    pass
            return Host()
        def transport_factory(endpoint, token):
            remote = remotes[int(endpoint.rsplit(":", 1)[1])]
            self.assertGreaterEqual(len(token), 32)
            return SimpleNamespace(readiness=lambda: HostReadiness(remote["config"].engine_manifest_digest,
                remote["identity"]["intent_id"], "c"*32, True), close=lambda: None)
        runtime.ssh_factory, runtime.transport_factory = ssh_factory, transport_factory
        try:
            for binding, run in ((first, first_run), (second, second_run)):
                runtime.readiness(binding, run)
                runtime.readiness(self.store.load(binding["intent_id"]), run)
                slot = runtime.slot(binding["intent_id"])
                self.assertEqual(slot.spec.dispatch_backend, "hatchet-v1")
                self.assertEqual(slot.spec.instance_id, binding["provider_instance_id"])
                self.assertEqual(slot.spec.engine_manifest_digest, binding["manifest_digest"])
                self.assertEqual(slot.spec.physical_gpu_ids, ("GPU-"+binding["intent_id"],))
            self.assertNotEqual(runtime.slot(first["intent_id"]).endpoint,
                                runtime.slot(second["intent_id"]).endpoint)
            self.assertEqual(set(remotes), {19100, 19101})
            self.assertEqual([(remote["starts"], remote["uploads"]) for remote in remotes.values()], [(1, 1), (1, 1)])
        finally:
            runtime.close()

    def test_retained_stopped_node_port_is_not_reused_or_bill_settled(self):
        self.config["last_local_port"] = 19100; self.save_config()
        first, _ = self.allocation()
        factory = self.factory(); factory.config_for_binding(first)
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == first["intent_id"])
                .values(runtime_state="stopped", desired_state="stopped"))
        second, _ = self.allocation("ref", "two")
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_ports_exhausted$"):
            factory.config_for_binding(second)
        self.assertNotIn("local_port", self.store.load(second["intent_id"]))
        self.assertEqual(self.store.load(first["intent_id"])["billing_state"], "unsettled")

    def test_binding_changes_and_foreign_tenant_fail_before_port_assignment(self):
        binding, _ = self.allocation()
        factory = self.factory()
        for key, value in (("mode", "ref"), ("configuration_id", "wrong"), ("manifest_digest", "a"*64),
                           ("provider_instance_id", "wrong")):
            with self.assertRaises(DstackError):
                factory.config_for_binding({**binding, key: value})
        foreign = RuntimeFactory(self.repo, LedgerDstackStore(self.repo, "different-tenant"), self.config_path)
        with self.assertRaisesRegex(Exception, "dstack_node_not_found"):
            foreign.config_for_binding(binding)
        self.assertNotIn("local_port", self.store.load(binding["intent_id"]))

    def test_conflicting_retained_port_refuses_existing_assignment(self):
        binding, _ = self.allocation()
        other, _ = self.allocation("ref", "two")
        factory = self.factory(); factory.config_for_binding(binding)
        with self.repo.transaction() as connection:
            row = connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == other["intent_id"])).mappings().one()
            payload = copy.deepcopy(row["payload"]); payload["dstack"]["local_port"] = 19100
            connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == other["intent_id"]).values(payload=payload))
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_port_conflict$"):
            factory.config_for_binding(binding)

    def test_configuration_and_sources_reject_drift_links_and_unbounded_ports(self):
        for first, last in ((1023, 1024), (19100, 19228), (2000, 1999), (True, 2000)):
            self.config.update(first_local_port=first, last_local_port=last); self.save_config()
            with self.assertRaisesRegex(DstackError, "port_range_invalid"):
                self.factory()
        self.config.update(first_local_port=19100, last_local_port=19102, provider_token="never accepted")
        self.save_config()
        with self.assertRaisesRegex(DstackError, "config_invalid"):
            self.factory()
        del self.config["provider_token"]; self.save_config()
        index_path = Path(self.config["source_index_file"])
        index = json.loads(index_path.read_text())
        index["sources"][0]["directory"] = "../secret"
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(DstackError, "source_binding_invalid"):
            self.factory()

    def test_hash_bound_runtime_cannot_inject_urls_or_secret_environment(self):
        index_path = Path(self.config["source_index_file"])
        index = json.loads(index_path.read_text()); entry = index["sources"][0]
        source = index_path.parent / entry["directory"] / "wangp-runtime.json"
        runtime = json.loads(source.read_text()); runtime["env"] = {"SECRET": "never-upload"}
        source.write_text(json.dumps(runtime))
        entry["source_sha256"][source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_prepared_source_required$"):
            self.factory()

    def test_private_config_permissions_and_links_are_not_followed(self):
        if os.name != "nt":
            self.config_path.chmod(0o644)
            with self.assertRaisesRegex(DstackError, "permissions_invalid"):
                self.factory()
            self.config_path.chmod(0o600)
            linked = self.root / "linked-config"; linked.symlink_to(self.config_path)
            with self.assertRaisesRegex(DstackError, "link_forbidden"):
                create_runtime(self.repo, self.store, configuration_path=linked)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(DstackError, "configuration_missing"):
                create_runtime(self.repo, self.store)

    def test_pinned_direct_coordinates_and_unsupported_topologies(self):
        binding, run = self.allocation()
        coordinates = coordinates_for_run(binding, run)
        self.assertEqual(coordinates, {"host": "198.51.100.8", "port": 21882,
            "username": "root", "instance_id": "gpu-one"})
        for backend in ("vastai", "runpod"):
            detached = copy.deepcopy(run)
            detached["latest_job_submission"]["job_provisioning_data"]["backend"] = backend
            self.assertEqual(coordinates_for_run({**binding, "backend": backend}, detached), coordinates)
        for field, value in (("hostname", None), ("ssh_port", None), ("ssh_port", True),
            ("hostname", "host;secret"), ("instance_id", "other"), ("dockerized", True),
            ("ssh_proxy", {"hostname": "proxy"}), ("username", "ubuntu"), ("instance_type", None),
            ("instance_type", {"resources": {"gpus": [{}, {}]}})):
            detached = copy.deepcopy(run)
            detached["latest_job_submission"]["job_provisioning_data"][field] = value
            with self.assertRaises(DstackError):
                coordinates_for_run(binding, detached)
        detached = copy.deepcopy(run)
        detached["latest_job_submission"]["job_runtime_data"] = {"username": "ubuntu"}
        with self.assertRaisesRegex(DstackError, "username_unsupported"):
            coordinates_for_run(binding, detached)
        self.assertNotIn("bootstrap_started", self.store.load(binding["intent_id"]))


if __name__ == "__main__":
    unittest.main()

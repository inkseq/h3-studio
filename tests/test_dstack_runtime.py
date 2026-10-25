"""Original ledger/native contracts with fake SSH; no provider/GPU operations."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select

from studio_platform.auth import Principal
from studio_platform.dstack_capacity import DstackCapacity, DstackError
from studio_platform.dstack_operator import DstackOperator, LedgerDstackStore
from studio_platform.dstack_runtime import DstackNativeRuntime, DstackRuntimeConfig
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.repository import registered_workers, scaler_receipts
from studio_platform.runtime_catalog import engine_manifest
from studio_platform.runtime_hosts.wangp_http import private_token_file
from studio_platform.wangp_bootstrap import BootError
from test_dstack_operator import Client, PROFILE, PUB
from test_platform_repository import LedgerCase
from tools.build_operator_sources import _runtime

GPU = "GPU-12345678-1234-1234-1234-123456789abc"


class NativeRuntimeTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.repo.configure_pool("dstack-test", max_instances=3, max_physical_gpus=3)
        settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id, operator_capacity_owners=("superdan",)
        )
        self.store = LedgerDstackStore(self.repo, self.scope.tenant_id)
        self.client = Client(self.store)
        self.capacity = DstackCapacity(self.client, self.store, clock=self.repo.clock)
        profiles = [
            {
                "id": "5090-" + mode,
                "owner_id": "superdan",
                "project_id": "project-1",
                "budget_account_ids": ["owner-budget"],
                "pool": "dstack-test",
                "backend": "vastai",
                "runtime_profile_id": PROFILE,
                "mode": mode,
                "configuration_id": "test-native-" + mode,
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
            for mode in ("fl", "ref")
        ]
        self.operator = DstackOperator(
            self.repo,
            settings,
            self.capacity,
            {
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
                "profiles": profiles,
            },
        )
        self.configs, self.remotes = {}, {}
        self.owner = Principal("superdan", "browser-1")
        for mode in ("fl", "ref"):
            source = self.root / ("sources-" + mode)
            source.mkdir()
            manifest = engine_manifest(PROFILE, mode)
            package = b"Offline fixture only, not a deployable bundle"
            files = {
                "wangp-package.tar.gz": package,
                "wangp-bootstrap.py": b"# Offline SSH fixture",
                "wangp-manifest.json": json.dumps(manifest.document).encode(),
                "wangp-runtime.json": json.dumps(
                    _runtime(PROFILE, 0, 1, hashlib.sha256(package).hexdigest())
                ).encode(),
            }
            for name, data in files.items():
                (source / name).write_bytes(data)
            self.configs[mode] = DstackRuntimeConfig(
                self.root / "runtime",
                source,
                self.root / "ssh-key",
                self.root / "known-hosts",
                18900 if mode == "fl" else 18901,
                "dstack-test",
                "vast",
                PROFILE,
                mode,
                manifest.document["model_id"],
                "test-native-" + mode,
                manifest.digest,
                {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()
                },
            )

    def allocation(self, mode="fl"):
        preview = self.operator.preview(
            self.owner, {"profile_id": "5090-" + mode, "ttl_seconds": 900}
        )
        node = self.operator.start(
            self.owner, {"preview_id": preview["preview_id"]}, "start-" + mode
        )
        binding = self.store.load(node["node_id"])
        run = self.client.runs[binding["run_name"]]
        run.update(
            status="running",
            latest_job_submission={
                "job_provisioning_data": {
                    "backend": "vastai",
                    "instance_id": "gpu-" + mode,
                    "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}},
                }
            },
        )
        self.store.record(
            binding["intent_id"],
            {
                "run_id": run["id"],
                "provider_instance_id": "gpu-" + mode,
                "state": "runtime_unconfirmed",
                "observed_at": self.repo.clock(),
            },
        )
        return self.store.load(binding["intent_id"]), run

    def test_hard_interruption_after_token_link_reuses_original_complete_token(self):
        binding, run = self.allocation()
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        directory.mkdir(parents=True, mode=0o700)
        token = directory / "wangp-token"
        token.write_text("x" * 64, encoding="ascii")
        token.chmod(0o600)
        alias = directory / ("wangp-token." + "a" * 32 + ".tmp")
        os.link(token, alias)
        self.assertEqual(token.stat().st_nlink, 2)
        self.assertTrue(self.bridge().readiness(binding, run).idle)
        self.assertFalse(alias.exists())
        self.assertEqual(private_token_file(token), "x" * 64)
        self.assertEqual(self.remotes["fl"]["starts"], 1)

    def test_unknown_token_hardlink_is_not_removed_or_replaced(self):
        binding, run = self.allocation()
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        directory.mkdir(parents=True, mode=0o700)
        token = directory / "wangp-token"
        token.write_text("x" * 64, encoding="ascii")
        token.chmod(0o600)
        alias = directory / "unrecognized-private-copy"
        os.link(token, alias)
        with self.assertRaises(DstackError):
            self.bridge().readiness(binding, run)
        self.assertTrue(alias.exists())
        self.assertEqual(token.read_text(encoding="ascii"), "x" * 64)
        self.assertEqual(self.remotes["fl"]["starts"], 0)
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))

    def bridge(self, *, coordinates_for_run=None, ssh_factory=None):
        outer = self

        def host_factory(config, coordinates):
            remote = outer.remotes.setdefault(
                config.mode,
                {
                    "starts": 0,
                    "uploads": 0,
                    "incarnation": "c" * 32,
                    "gpu": GPU,
                    "idle": True,
                    "identity": None,
                },
            )

            class Host:
                def ensure_connected(self):
                    if remote.get("ssh_unavailable"):
                        raise RuntimeError("SECRET SSH connection diagnostic")

                def upload(self, files):
                    remote["uploads"] += 1
                    assert set(files) == set(config.source_sha256)
                    if remote.pop("fail_upload_once", False):
                        raise RuntimeError("SECRET SSH upload diagnostic")
                    if remote.get("source_mismatch"):
                        raise BootError("bootstrap_existing_source_mismatch")

                def start(self, identity):
                    # Real transactional boot intent and protected token are
                    # present BEFORE anything resembling a remote launch.
                    assert (
                        outer.store.load(identity["intent_id"])["bootstrap_started"]
                        is True
                    )
                    private_token_file(
                        config.work_dir / identity["intent_id"] / "wangp-token"
                    )
                    remote["starts"] += 1
                    remote["identity"] = copy.deepcopy(identity)
                    if remote.get("lose_start"):
                        raise RuntimeError("SECRET upstream SSH response")

                def report(self):
                    if remote.get("report_phase"):
                        return {
                            "identity": remote["identity"],
                            "state": "preparing",
                            "phase": remote["report_phase"],
                            "private_text": "SECRET diagnostics",
                        }
                    return {
                        "identity": remote["identity"],
                        "state": "ready",
                        "runtime_verified": True,
                        "engine_manifest_digest": config.engine_manifest_digest,
                        "source_revision": engine_manifest(
                            PROFILE, config.mode
                        ).document["source_revision"],
                        "gpus": [{"uuid": remote["gpu"]}],
                        "runtime": {"gpu_total_bytes": 32 * 1024**3},
                    }

                def open_tunnel(self, port):
                    assert port == config.local_port
                    if remote.get("tunnel_error"):
                        raise remote["tunnel_error"]
                    remote["tunnel"] = True

                def close(self):
                    remote["tunnel"] = False

            return Host()

        def transport_factory(endpoint, token):
            mode = "fl" if endpoint.endswith(":18900") else "ref"
            config, remote = outer.configs[mode], outer.remotes[mode]
            assert len(token) >= 32

            def readiness():
                if remote.get("readiness_down"):
                    raise ConnectionError("SECRET tunnel diagnostic")
                return HostReadiness(
                    config.engine_manifest_digest,
                    remote["identity"]["intent_id"],
                    remote["incarnation"],
                    remote["idle"],
                )

            return SimpleNamespace(readiness=readiness, close=lambda: None)

        coordinates_for_run = coordinates_for_run or (
            lambda binding, _run: {
                "host": "offline.invalid",
                "port": 22,
                "username": "root",
                "instance_id": binding["provider_instance_id"],
            }
        )
        return DstackNativeRuntime(
            self.store,
            lambda binding: self.configs[binding["mode"]],
            coordinates_for_run,
            ssh_factory=ssh_factory or host_factory,
            transport_factory=transport_factory,
        )

    def test_missing_ssh_metadata_then_available_does_not_consume_bootstrap(self):
        binding, run = self.allocation()
        coordinates = {
            "host": None,
            "port": None,
            "username": "root",
            "instance_id": "gpu-fl",
        }
        bridge = self.bridge(
            coordinates_for_run=lambda _binding, _run: dict(coordinates)
        )
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        with self.assertRaisesRegex(
            DstackError, "^dstack_runtime_ssh_coordinates_unavailable$"
        ):
            bridge.readiness(binding, run)
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))
        self.assertFalse((directory / "bootstrap-state.json").exists())
        self.assertFalse((directory / "wangp-token").exists())
        self.assertEqual(self.remotes, {})
        coordinates.update(host="offline.invalid", port=22345)
        self.assertTrue(
            bridge.readiness(self.store.load(binding["intent_id"]), run).idle
        )
        self.assertEqual(
            (self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 1)
        )

    def test_ssh_connection_failure_does_not_consume_bootstrap(self):
        binding, run = self.allocation()

        def unavailable(_config, _coordinates):
            raise RuntimeError("SECRET host-key/SSH diagnostic")

        with self.assertRaisesRegex(
            DstackError, "^dstack_runtime_ssh_unavailable_or_host_key_untrusted$"
        ):
            self.bridge(ssh_factory=unavailable).readiness(binding, run)
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))
        self.assertFalse((directory / "bootstrap-state.json").exists())
        self.assertFalse((directory / "wangp-token").exists())
        self.assertTrue(
            self.bridge().readiness(self.store.load(binding["intent_id"]), run).idle
        )
        self.assertEqual(self.remotes["fl"]["starts"], 1)

    def test_upload_failure_resumes_original_journal_and_token_without_replaying_start(
        self,
    ):
        binding, run = self.allocation()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "fail_upload_once": True,
        }
        bridge = self.bridge()
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_upload_unconfirmed$"):
            bridge.readiness(binding, run)
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        self.assertTrue(self.store.load(binding["intent_id"])["bootstrap_started"])
        self.assertEqual(
            json.loads((directory / "bootstrap-state.json").read_text())["phase"],
            "journaled",
        )
        self.assertEqual(self.remotes["fl"]["starts"], 0)
        token = private_token_file(directory / "wangp-token")
        bridge.close()
        recovered = self.bridge()
        self.assertTrue(
            recovered.readiness(self.store.load(binding["intent_id"]), run).idle
        )
        recovered.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(token, private_token_file(directory / "wangp-token"))
        self.assertEqual(
            (self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 2)
        )

    def test_partial_remote_source_is_explicitly_blocked_without_overwrite_or_launch(
        self,
    ):
        binding, run = self.allocation()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "source_mismatch": True,
        }
        bridge = self.bridge()
        for _ in range(2):
            with self.assertRaisesRegex(
                DstackError, "^dstack_runtime_upload_reconciliation_required$"
            ):
                bridge.readiness(self.store.load(binding["intent_id"]), run)
        directory = self.configs["fl"].work_dir / binding["intent_id"]
        self.assertEqual(
            json.loads((directory / "bootstrap-state.json").read_text())["phase"],
            "journaled",
        )
        self.assertEqual(self.remotes["fl"]["starts"], 0)
        self.assertTrue(self.store.load(binding["intent_id"])["bootstrap_started"])

    def test_owned_ledger_journals_once_and_original_native_factory_creates_hatchet_slot(
        self,
    ):
        binding, run = self.allocation()
        bridge = self.bridge()
        native = bridge.readiness(binding, run)
        slot = bridge.slot(binding["intent_id"])
        self.assertEqual(native.slot_key, binding["intent_id"])
        self.assertEqual(slot.spec.dispatch_backend, "hatchet-v1")
        self.assertEqual(slot.spec.physical_gpu_ids, (GPU,))
        self.assertEqual(slot.spec.recipe_ids, ("h3-base-fl2va-v1",))
        self.assertEqual(slot.spec.engine_manifest_digest, binding["manifest_digest"])
        self.assertEqual(slot.spec.instance_id, "gpu-fl")
        self.assertTrue(slot.confirmed_idle)
        bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(
            (self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 1)
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(func.count())
                    .select_from(scaler_receipts)
                    .where(
                        scaler_receipts.c.intent_id == binding["intent_id"],
                        scaler_receipts.c.operation == "bootstrap",
                    )
                ).scalar_one(),
                2,
            )
            self.assertEqual(
                conn.execute(
                    select(func.count()).select_from(registered_workers)
                ).scalar_one(),
                0,
            )

    def test_local_staging_failure_does_not_consume_bootstrap_then_reuses_original_token(
        self,
    ):
        binding, run = self.allocation()
        bridge = self.bridge()
        with patch.object(bridge, "_save", side_effect=OSError("disk fixture")):
            with self.assertRaisesRegex(DstackError, "native_runtime_unconfirmed"):
                bridge.readiness(binding, run)
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))
        token = private_token_file(
            self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
        )
        bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 1)
        self.assertEqual(
            token,
            private_token_file(
                self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
            ),
        )

    def test_launch_database_failure_proves_no_remote_start_and_resumes_once(self):
        binding, run = self.allocation()
        bridge = self.bridge()
        with patch.object(
            self.store, "begin_runtime_launch", side_effect=RuntimeError("db fixture")
        ):
            with self.assertRaisesRegex(DstackError, "native_runtime_unconfirmed"):
                bridge.readiness(binding, run)
        self.assertFalse(
            self.store.load(binding["intent_id"])["bootstrap_launch_started"]
        )
        self.assertEqual(self.remotes["fl"]["starts"], 0)
        bridge.readiness(self.store.load(binding["intent_id"]), run)
        bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 1)

    def test_resumed_upload_after_stop_cannot_launch_but_exact_capacity_can_stop(self):
        binding, run = self.allocation()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "fail_upload_once": True,
        }
        bridge = self.bridge()
        with self.assertRaisesRegex(DstackError, "upload_unconfirmed"):
            bridge.readiness(binding, run)
        self.operator.node_command(self.owner, binding["intent_id"], {}, "stop-1")
        with self.assertRaisesRegex(Exception, "bootstrap_window_expired"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 0)
        self.assertEqual(self.capacity.stop(binding["intent_id"])["state"], "stopping")

    def test_resumed_upload_after_deadline_cannot_launch(self):
        binding, run = self.allocation()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "fail_upload_once": True,
        }
        bridge = self.bridge()
        with self.assertRaisesRegex(DstackError, "upload_unconfirmed"):
            bridge.readiness(binding, run)
        self.now += 1000
        with self.assertRaisesRegex(Exception, "bootstrap_window_expired"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 0)

    def test_lost_start_reply_reconnects_original_marker_without_install_start_or_token_recreation(
        self,
    ):
        binding, run = self.allocation()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "lose_start": True,
        }
        bridge = self.bridge()
        with self.assertRaisesRegex(
            DstackError, "^dstack_runtime_bootstrap_response_unknown$"
        ):
            bridge.readiness(binding, run)
        token = private_token_file(
            self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
        )
        bridge.close()
        recovered = self.bridge()
        self.assertTrue(
            recovered.readiness(self.store.load(binding["intent_id"]), run).idle
        )
        self.assertEqual(
            token,
            private_token_file(
                self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
            ),
        )
        self.assertEqual(
            (self.remotes["fl"]["starts"], self.remotes["fl"]["uploads"]), (1, 1)
        )

    def test_missing_remote_marker_or_cpu_token_stays_unknown_without_relaunch(self):
        binding, run = self.allocation()
        bridge = self.bridge()
        bridge.readiness(binding, run)
        self.remotes["fl"]["identity"] = None
        with self.assertRaisesRegex(DstackError, "remote_identity_unconfirmed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        token = self.configs["fl"].work_dir / binding["intent_id"] / "wangp-token"
        token.unlink()
        with self.assertRaisesRegex(DstackError, "recovery_material_missing"):
            self.bridge().readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 1)
        self.assertFalse(token.exists())

    def test_post_report_failures_name_their_step_without_private_text(self):
        # #193: these all used to read dstack_native_runtime_unconfirmed.
        binding, run = self.allocation()
        bridge = self.bridge()
        bridge.readiness(binding, run)
        remote = self.remotes["fl"]
        remote["readiness_down"] = True
        with self.assertRaisesRegex(
            DstackError, "^dstack_runtime_readiness_unreachable$"
        ):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        remote["readiness_down"] = False
        remote["tunnel_error"] = BootError("bootstrap_tunnel_port_already_in_use")
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_tunnel_port_in_use$"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        remote["tunnel_error"] = OSError("SECRET socket diagnostic")
        with self.assertRaisesRegex(DstackError, "^dstack_runtime_tunnel_failed$"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        remote["tunnel_error"] = None
        self.assertEqual(
            bridge.readiness(self.store.load(binding["intent_id"]), run).slot_key,
            binding["intent_id"],
        )
        self.assertEqual(remote["starts"], 1)

    def test_changed_physical_gpu_or_incarnation_never_produces_a_new_slot(self):
        binding, run = self.allocation()
        bridge = self.bridge()
        bridge.readiness(binding, run)
        self.remotes["fl"]["gpu"] = "GPU-aaaaaaaa-1234-1234-1234-123456789abc"
        with self.assertRaisesRegex(DstackError, "physical_gpu_changed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.remotes["fl"]["gpu"] = GPU
        self.remotes["fl"]["incarnation"] = "d" * 32
        with self.assertRaisesRegex(DstackError, "incarnation_changed"):
            bridge.readiness(self.store.load(binding["intent_id"]), run)
        with self.assertRaisesRegex(DstackError, "slot_not_connected"):
            bridge.slot(binding["intent_id"])
        self.assertEqual(self.remotes["fl"]["starts"], 1)

    def test_altered_sources_and_multiple_allocated_gpus_are_rejected_before_bootstrap(
        self,
    ):
        binding, run = self.allocation()
        (self.configs["fl"].source_dir / "wangp-bootstrap.py").write_bytes(b"changed")
        with self.assertRaisesRegex(DstackError, "source_changed"):
            self.bridge().readiness(binding, run)
        self.assertFalse(self.store.load(binding["intent_id"]).get("bootstrap_started"))
        ref, ref_run = self.allocation("ref")
        ref_run["latest_job_submission"]["job_provisioning_data"]["instance_type"][
            "resources"
        ]["gpus"] *= 2
        with self.assertRaisesRegex(DstackError, "allocation_mismatch"):
            self.bridge().readiness(ref, ref_run)
        self.assertFalse(self.store.load(ref["intent_id"]).get("bootstrap_started"))

    def test_preparation_reports_exact_phase_without_relaunching_bootstrap(self):
        binding, run = self.allocation()
        bridge = self.bridge()
        self.remotes["fl"] = {
            "starts": 0,
            "uploads": 0,
            "incarnation": "c" * 32,
            "gpu": GPU,
            "idle": True,
            "identity": None,
            "report_phase": "model_download",
        }
        for phase, code in (
            ("model_download", "downloading_weights"),
            ("runtime_start", "starting_runtime"),
            ("model_load", "loading_model"),
            ("SECRET diagnostics", "preparing"),
        ):
            self.remotes["fl"]["report_phase"] = phase
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(DstackError, "^dstack_runtime_" + code + "$"),
            ):
                bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertEqual(self.remotes["fl"]["starts"], 1)
        self.assertEqual(self.remotes["fl"]["uploads"], 1)

    def test_ref_mode_and_busy_native_runtime_are_explicit_and_close_only_closes_local_tunnel(
        self,
    ):
        binding, run = self.allocation("ref")
        bridge = self.bridge()
        bridge.readiness(binding, run)
        self.remotes["ref"]["idle"] = False
        native = bridge.readiness(self.store.load(binding["intent_id"]), run)
        self.assertFalse(native.idle)
        self.assertEqual(
            bridge.slot(binding["intent_id"]).spec.recipe_ids, ("h3-base-ref2va-v1",)
        )
        self.assertFalse(bridge.slot(binding["intent_id"]).confirmed_idle)
        bridge.close(binding["intent_id"])
        self.assertFalse(self.remotes["ref"]["tunnel"])
        self.assertEqual(self.client.stopped, [])
        self.assertEqual(
            self.store.load(binding["intent_id"])["provider_instance_id"], "gpu-ref"
        )


if __name__ == "__main__":
    import unittest

    unittest.main()

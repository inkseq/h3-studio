"""The provider registry reproduces every set it replaced (#931 step 1)."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from studio_platform.providers.registry import (
    ADMISSION_PAIRS,
    BACKEND_PROVIDERS,
    DSTACK_BACKENDS,
    DSTACK_V1_PROVIDERS,
)


class ProviderRegistryTests(unittest.TestCase):
    def test_execution_rows_preserve_each_distinct_purpose(self):
        from studio_platform.providers import registry
        from studio_platform import control, dbos_transport, supplier_spending
        from studio_platform import enhance_plans
        from studio_platform.render_backend import CPURenderBackend
        from studio_platform.providers import hosted_models

        self.assertEqual(
            registry.WORKER_BACKENDS,
            {
                "mock",
                "comfy-worker",
                "wangp-worker",
                "cpu-render",
                "runpod-enhance",
                "everyframe-render",
            },
        )
        self.assertEqual(
            registry.NO_ENDPOINT_BACKENDS,
            {"mock", "cpu-render", "runpod-enhance", "everyframe-render"},
        )
        self.assertEqual(
            registry.SERVICE_SWITCHES,
            {
                "cpu-render": "render_enabled",
                "runpod-enhance": "enhance_enabled",
                "everyframe-render": "everyframe_enabled",
            },
        )
        self.assertEqual(
            registry.PRIMARY_EXECUTION_BACKENDS,
            {"disabled", "mock", "comfy-worker", "wangp-worker"},
        )
        self.assertEqual(
            registry.STANDALONE_EXECUTION_BACKENDS,
            ("disabled", "mock", "comfy-worker"),
        )
        self.assertIs(control.REAL_GPU_BACKENDS, registry.REAL_GPU_BACKENDS)
        self.assertIs(dbos_transport.LOCAL_BACKENDS, registry.LOCAL_BACKENDS)
        self.assertIs(supplier_spending.INTERNAL_BACKENDS, registry.INTERNAL_BACKENDS)
        self.assertEqual(
            (hosted_models.BACKEND, hosted_models.PROVIDER),
            ("everyframe-render", "everyframe"),
        )
        self.assertEqual(enhance_plans.BACKEND, "runpod-enhance")
        self.assertEqual(CPURenderBackend.kind, "cpu-render")

    def test_execution_classifications_do_not_expand_rental_admission(self):
        from studio_platform.control import REAL_GPU_BACKENDS
        from studio_platform.dbos_transport import LOCAL_BACKENDS
        from studio_platform.supplier_spending import INTERNAL_BACKENDS

        self.assertEqual(REAL_GPU_BACKENDS, {"comfy-worker", "wangp-worker"})
        self.assertEqual(
            LOCAL_BACKENDS, {"cpu-render", "runpod-enhance", "everyframe-render"}
        )
        self.assertEqual(
            INTERNAL_BACKENDS, {"mock", "cpu-render", "comfy-worker", "wangp-worker"}
        )
        for backend in LOCAL_BACKENDS | REAL_GPU_BACKENDS | {"mock"}:
            self.assertNotIn(backend, DSTACK_BACKENDS)
            self.assertNotIn(("dstack-v1", backend), ADMISSION_PAIRS)
        self.assertNotIn(("dstack-v1", "everyframe"), ADMISSION_PAIRS)
        self.assertNotIn(("dstack-v1", "local-cpu"), ADMISSION_PAIRS)

    def test_primary_and_recovery_selection_stays_narrow(self):
        from studio_platform.settings import Settings

        primary = {"disabled", "mock", "comfy-worker", "wangp-worker"}
        local = {"cpu-render", "runpod-enhance", "everyframe-render"}
        for backend in primary | local | set(DSTACK_BACKENDS) | {"other"}:
            with self.subTest(backend=backend):
                if backend in primary:
                    Settings(Path("unused"), execution_backend=backend)
                else:
                    with self.assertRaisesRegex(ValueError, "Unsupported execution"):
                        Settings(Path("unused"), execution_backend=backend)
                if backend in {"comfy-worker", "wangp-worker"}:
                    Settings(Path("unused"), recovery_backends=(backend,))
                else:
                    with self.assertRaisesRegex(ValueError, "Recovery backends"):
                        Settings(Path("unused"), recovery_backends=(backend,))

    def test_standalone_cli_does_not_admit_other_execution_engines(self):
        from studio_platform.worker import main

        # The standalone CLI has never supported WangP or the local service
        # adapters. Reject before loading settings, credentials or a database.
        with patch("studio_platform.settings.Settings.from_environment") as settings:
            for backend in (
                "wangp-worker",
                "cpu-render",
                "runpod-enhance",
                "everyframe-render",
            ):
                with (
                    self.subTest(backend=backend),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as raised:
                        main(
                            [
                                "--backend",
                                backend,
                                "--worker-id",
                                "w",
                                "--pool",
                                "p",
                                "--work-dir",
                                "unused",
                            ]
                        )
                    self.assertEqual(raised.exception.code, 2)
            settings.assert_not_called()

    def test_registration_payload_keeps_original_canonical_bytes(self):
        from studio_platform.control import WorkerSpec, worker_spec_payload

        # Frozen from unchanged main. These bytes are persisted registration
        # identity; moving names must not insert registry metadata/defaults.
        payloads = (
            '{"backend":"mock","configuration_id":"config-1","instance_id":"instance-1",'
            '"model_id":"SIMULATION","physical_gpu_ids":["gpu-1"],"pool":"pool-1",'
            '"provider":"mock","recipe_ids":["recipe-1"],"worker_id":"worker-1"}',
            '{"backend":"comfy-worker","configuration_id":"config-1","instance_id":"instance-1",'
            '"model_id":"model-1","physical_gpu_ids":["gpu-1"],"pool":"pool-1",'
            '"provider":"vast","recipe_ids":["recipe-1"],"worker_id":"worker-1"}',
            '{"backend":"wangp-worker","configuration_id":"config-1",'
            '"engine_manifest_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"instance_id":"instance-1","model_id":"model-1","physical_gpu_ids":["gpu-1"],'
            '"pool":"pool-1","provider":"vast","recipe_ids":["recipe-1"],"worker_id":"worker-1"}',
            '{"backend":"cpu-render","configuration_id":"cpu-render-v1","instance_id":"instance-1",'
            '"model_id":"sixnine-chapter-roughcut-v1","physical_gpu_ids":[],"pool":"pool-1",'
            '"provider":"local-cpu","recipe_ids":["chapter-roughcut-v1"],"worker_id":"worker-1"}',
            '{"backend":"runpod-enhance","configuration_id":"runpod-enhance-v1",'
            '"instance_id":"instance-1","model_id":"open-h3-regenerate-2k","physical_gpu_ids":[],"pool":"pool-1",'
            '"provider":"runpod","recipe_ids":["enhance-v1"],"worker_id":"worker-1"}',
            '{"backend":"everyframe-render","configuration_id":"everyframe-h3-max-turbo-v1",'
            '"instance_id":"instance-1","model_id":"h3-max-turbo","physical_gpu_ids":[],"pool":"pool-1",'
            '"provider":"everyframe","recipe_ids":["h3-base-fl2va-v1","h3-base-ref2va-v1"],"worker_id":"worker-1"}',
        )
        for expected in payloads:
            fields = json.loads(expected)
            for key in ("physical_gpu_ids", "recipe_ids"):
                fields[key] = tuple(fields[key])
            with self.subTest(backend=fields["backend"]):
                spec = WorkerSpec(**fields)
                actual = json.dumps(
                    worker_spec_payload(spec), sort_keys=True, separators=(",", ":")
                )
                self.assertEqual(actual.encode(), expected.encode())
                if fields["backend"] in {"mock", "comfy-worker", "wangp-worker"}:
                    with self.assertRaisesRegex(ValueError, "invalid_physical_gpu_ids"):
                        WorkerSpec(**(fields | {"physical_gpu_ids": ()}))

    def test_derived_sets_match_the_literals_they_replaced(self):
        from studio_platform.providers.registry import BACKENDS

        self.assertEqual(
            [(backend.id, backend.provider) for backend in BACKENDS],
            [
                ("vastai", "vast"),
                ("runpod", "runpod"),
                ("lium", "lium"),
                ("cloudrift", "cloudrift"),
                ("runpod-serverless", "runpod"),
                ("modal-dialout", "modal-dialout"),
            ],
        )
        self.assertEqual(
            DSTACK_V1_PROVIDERS,
            {"vast", "runpod", "lium", "cloudrift", "modal-dialout"},
        )
        self.assertEqual(
            DSTACK_BACKENDS,
            {
                "vastai",
                "runpod",
                "lium",
                "cloudrift",
                "runpod-serverless",
                "modal-dialout",
            },
        )
        self.assertEqual(BACKEND_PROVIDERS["runpod-serverless"], "runpod")
        self.assertEqual(
            ADMISSION_PAIRS,
            {("dstack-v1", p) for p in DSTACK_V1_PROVIDERS} | {("modal-v1", "modal")},
        )

    def test_callers_use_the_registry(self):
        from studio_platform import dstack_inventory, dstack_operator, dstack_runtime
        from studio_platform import modal_dialout, runpod_serverless
        from studio_platform.providers.registry import MODAL_DIALOUT, RUNPOD_SERVERLESS
        from studio_platform import telemetry

        self.assertIs(dstack_operator.PROVIDERS, BACKEND_PROVIDERS)
        self.assertIs(dstack_runtime.PROVIDERS, BACKEND_PROVIDERS)
        self.assertIs(dstack_inventory.SUPPORTED, DSTACK_BACKENDS)
        self.assertIs(modal_dialout.BACKEND, MODAL_DIALOUT.id)
        self.assertIs(runpod_serverless.BACKEND, RUNPOD_SERVERLESS.id)
        self.assertEqual(
            telemetry.PROVIDERS,
            DSTACK_V1_PROVIDERS | {"targon", "modal", "local", "unknown"},
        )


if __name__ == "__main__":
    unittest.main()

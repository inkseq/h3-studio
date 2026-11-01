"""Modal dial-out sandboxes (#859); a fake Modal SDK, no network or GPU."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from studio_platform.dstack_capacity import DstackError
from studio_platform.modal_dialout import BACKEND, CACHE, ModalDialoutClient
from studio_platform.runpod_serverless import RoutedClient
from test_runpod_serverless import INTENT, RUN, TOKEN, URL, spec as base_spec
from studio_platform.worker_boot import boot_env


def spec(**changes):
    return base_spec(backends=[BACKEND], **changes)


class FakeModal:
    def __init__(self):
        self.created, self.terminated, self.running = [], [], {}
        modal = self

        class Sandbox:
            def __init__(self, identity):
                self.object_id = identity

            @staticmethod
            def create(*command, **kwargs):
                modal.created.append((command, kwargs))
                modal.running["sb-1"] = None
                return Sandbox("sb-1")

            @staticmethod
            def from_id(identity, client=None):
                box = Sandbox(identity)
                box.poll = lambda: modal.running.get(identity, 0)
                box.terminate = lambda: (
                    modal.terminated.append(identity),
                    modal.running.__setitem__(identity, 137),
                )
                return box

            @staticmethod
            def list(app_id=None, client=None):
                assert app_id == "ap-1"
                return [i for i, code in modal.running.items() if code is None]

        self.Sandbox = Sandbox
        self.Client = SimpleNamespace(from_credentials=lambda *a: "client")
        self.App = SimpleNamespace(
            lookup=lambda name, **k: SimpleNamespace(app_id="ap-1", name=name)
        )
        self.Image = SimpleNamespace(from_registry=lambda ref: ("image", ref))
        self.Secret = SimpleNamespace(from_dict=lambda d: ("secret", dict(d)))
        self.Volume = SimpleNamespace(from_id=lambda i, client=None: ("volume", i))


class ModalDialoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.modal = FakeModal()
        self.config = {
            "token_id": "ak-" + "a" * 20,
            "token_secret": "as-" + "b" * 20,
            "app_name": "inkseq-staging-dialout",
            "image": "example/native@sha256:" + "a" * 64,
            "volume_id": "vo-abc123",
            "gpu": "RTX-PRO-6000",
            "gpu_name": "RTXPRO6000",
            "gpu_memory_gib": 96,
            "cpus": 8,
            "memory_gib": 48,
            "disk_gib": 200,
            "hourly_microusd": 4_800_000,
            "state_dir": str(Path(self.temp.name) / "modal"),
        }
        self.client = ModalDialoutClient(self.config, "inkseq-staging", sdk=self.modal)

    def tearDown(self):
        self.temp.cleanup()

    def test_sandbox_runs_the_boot_command_with_boot_env_cache_and_volume(self):
        names = {"resources": {"gpu": {"name": ["RTXPRO6000"]}}, "max_price": 5.0}
        offers = self.client.plan(spec(**names))["job_plans"][0]["offers"]
        self.assertEqual([o["backend"] for o in offers], [BACKEND])
        self.assertEqual(
            self.client.plan(spec())["job_plans"][0]["offers"], []
        )  # Above 1.2/h.
        run = self.client.apply(spec())
        command, kwargs = self.modal.created[0]
        self.assertEqual(
            command, ("bash", "-c", spec()["configuration"]["commands"][0])
        )
        self.assertEqual(
            kwargs["secrets"],
            [("secret", {**boot_env(INTENT, TOKEN, URL), "INKSEQ_MODEL_CACHE": CACHE})],
        )
        self.assertEqual(kwargs["volumes"], {"/ckpts": ("volume", "vo-abc123")})
        self.assertEqual(
            (kwargs["name"], kwargs["gpu"], kwargs["timeout"]),
            (RUN, "RTX-PRO-6000", 7200),
        )
        self.assertEqual(run["status"], "running")
        jpd = run["latest_job_submission"]["job_provisioning_data"]
        self.assertEqual((jpd["backend"], jpd["instance_id"]), (BACKEND, "sb-1"))

    def test_terminate_is_journaled_as_cancelled_and_inventory_matches_listing(self):
        self.client.apply(spec())
        inventory = self.client.provider_inventory(BACKEND)
        self.assertEqual(inventory["instance_ids"], ["sb-1"])
        self.client.stop(RUN)
        self.assertEqual(self.modal.terminated, ["sb-1"])
        self.assertEqual(self.client.get(run_name=RUN)["status"], "terminated")
        self.assertEqual(self.client.provider_inventory(BACKEND)["instance_ids"], [])
        self.modal.running["sb-ghost"] = None  # A sandbox this journal never made.
        with self.assertRaisesRegex(DstackError, "inventory_unconfirmed"):
            self.client.provider_inventory(BACKEND)

    def test_exit_without_stop_is_completed_or_failed(self):
        self.client.apply(spec())
        self.modal.running["sb-1"] = 1
        self.assertEqual(self.client.get(run_name=RUN)["status"], "failed")

    def test_routed_client_sends_each_backend_to_its_lane(self):
        from test_runpod_serverless import FakeRunPod
        import httpx

        from studio_platform.runpod_serverless import (
            BACKEND as RUNPOD,
            RunPodServerlessClient,
        )

        runpod = RunPodServerlessClient(
            {
                "api_key": "k" * 32,
                "endpoint_id": "abc123",
                "gpu_name": "RTX5090",
                "gpu_memory_gib": 32,
                "cpus": 8,
                "memory_gib": 46,
                "disk_gib": 200,
                "hourly_microusd": 1_100_000,
                "state_dir": str(Path(self.temp.name) / "runpod"),
            },
            "inkseq-staging",
            http=httpx.Client(transport=httpx.MockTransport(FakeRunPod())),
        )

        class Dstack:
            project = "p"

            def configured_backends(self):
                return frozenset({"vastai"})

            def get(self, **kwargs):
                return None

        routed = RoutedClient(Dstack(), runpod, self.client)
        self.assertEqual(routed.configured_backends(), {"vastai", RUNPOD, BACKEND})
        routed.apply(spec())
        self.assertEqual(
            routed.get(run_name=RUN)["latest_job_submission"]["job_provisioning_data"][
                "backend"
            ],
            BACKEND,
        )
        routed.stop(RUN)
        self.assertEqual(self.modal.terminated, ["sb-1"])

    def test_config_is_closed(self):
        for bad in ({"gpu": "H100"}, {"image": "unpinned:latest"}, {"extra": 1}):
            with self.assertRaisesRegex(DstackError, "modal_dialout_config_invalid"):
                ModalDialoutClient({**self.config, **bad}, "p", sdk=self.modal)


if __name__ == "__main__":
    unittest.main()

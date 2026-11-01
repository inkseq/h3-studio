"""One hosted take end to end on real code, with only the vendor's HTTP faked.

Rebind, a real WorkerRunner on a hosted slot, the real EveryFrame adapter, real
ffmpeg normalisation, the object store and settlement. No network or GPU.
"""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest

import httpx
from sqlalchemy import select

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.providers.everyframe import Config, EveryFrameBackend
from studio_platform.providers.hosted_models import (
    BACKEND,
    MODELS,
    NO_CUT,
    POOL,
    PROVIDER,
    RECIPES,
)
from studio_platform.repository import attempts, jobs
from studio_platform.scheduling.rebind import rebind
from studio_platform.storage import LocalObjectStore
from studio_platform.worker import WorkerRunner
from test_platform_api import generation_request
from test_platform_repository import LedgerCase

TURBO = MODELS["h3-max-turbo"]


class FakeVendor:
    """The vendor's documented contract: one render, polled, then a download."""

    def __init__(self, video, states=("running", "succeeded")):
        self.video, self.states = video, list(states)
        self.renders = []

    def __call__(self, request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/render":
            self.renders.append(json.loads(request.content))
            return httpx.Response(202, json={"render": {"id": "rnd_1"}})
        if request.method == "GET" and path == "/v1/render/rnd_1":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            render = {"id": "rnd_1", "status": state}
            if state == "succeeded":
                render.update(costUsd=0.038, videoFullUrl="/render/rnd_1/video?full=1")
            return httpx.Response(200, json={"render": render})
        if request.method == "GET" and path == "/v1/render/rnd_1/video":
            return httpx.Response(200, content=self.video)
        raise AssertionError("unexpected vendor call: " + path)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class HostedEndToEndTests(LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.work = root / "worker"
        self.work.mkdir()
        self.store = LocalObjectStore(root / "objects")
        source = root / "vendor.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=832x480:rate=24:duration=5",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=5",
                "-shortest",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
        )
        self.video = source.read_bytes()

    def queued_job(self):
        value, _ = compile_request(generation_request(), lambda _: None)
        plan = self.repo.create_plan(
            self.scope,
            copy.deepcopy(value),
            {
                "pool": "own",
                "backend": "wangp-worker",
                "enabled": True,
                "expected_runtime_s": 60,
                "configuration_id": "v9",
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            self.scope, plan["id"], "e2e-key", budget_account_ids=["owner-budget"]
        )

    def row(self, table, column, value):
        with self.repo.engine.connect() as connection:
            return [
                dict(item)
                for item in connection.execute(
                    select(table).where(column == value)
                ).mappings()
            ]

    def runner(self, vendor):
        control = WorkerControl(self.repo)
        worker = "hosted-turbo"
        control.register(
            WorkerSpec(
                worker,
                POOL,
                PROVIDER,
                worker,
                (),
                RECIPES,
                TURBO.id,
                TURBO.configuration_id,
                BACKEND,
            )
        )
        control.mark_ready(worker, upstream_idle_confirmed=True)
        backend = EveryFrameBackend(
            self.store,
            "/unused",
            transport=httpx.MockTransport(vendor) if vendor else None,
            slot_key=worker,
        )
        backend._config = Config("key-1")
        return (
            WorkerRunner(
                self.repo,
                self.store,
                self.work,
                backend=backend,
                control=control,
                retry_after_s=0,
                submission_guard=lambda _: True,
            ),
            worker,
        )

    def test_a_rebound_take_renders_is_stored_and_settles_at_the_vendor_cost(self):
        created = self.queued_job()
        job = self.row(jobs, jobs.c.id, created["id"])[0]
        with self.repo.transaction() as connection:
            self.assertTrue(rebind(self.repo, connection, job, TURBO.id, self.now))
        vendor = FakeVendor(self.video)
        runner, worker = self.runner(vendor)
        for _ in range(5):
            self.now += 1
            runner.run_once(worker, POOL)
            current = self.row(jobs, jobs.c.id, created["id"])[0]
            if current["status"] in ("succeeded", "failed"):
                break
        self.assertEqual(
            (current["status"], current["error_code"]), ("succeeded", None)
        )
        # Exactly one render, on the bound model, with the prompt rule applied.
        self.assertEqual(len(vendor.renders), 1)
        self.assertEqual(vendor.renders[0]["modelId"], TURBO.id)
        self.assertTrue(vendor.renders[0]["prompt"].endswith(NO_CUT))
        self.assertNotIn("seed", vendor.renders[0])
        # The output reached the store and the vendor's cost was settled.
        self.assertEqual(current["result"]["actual_cost_microusd"], 38_000)
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(budget["reserved_microusd"], 0)
        self.assertEqual(budget["spent_microusd"], 38_000)
        (attempt,) = self.row(attempts, attempts.c.job_id, created["id"])
        self.assertEqual(attempt["actual_cost_microusd"], 38_000)
        self.assertEqual(len(current["result"]["artifact_ids"]), 2)  # Video, audio.
        self.assert_stored_video((832, 480))

    def assert_stored_video(self, size):
        """The stored output has the take's size."""
        found = []
        for path in (Path(self.temp.name) / "objects").rglob("*"):
            if not path.is_file():
                continue
            probed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
            )
            if probed.returncode == 0:
                found += json.loads(probed.stdout).get("streams", [])
        # The original and its browser copy; a smaller poster may sit beside them.
        self.assertIn(size, [(stream["width"], stream["height"]) for stream in found])

    @unittest.skipUnless(os.environ.get("INKSEQ_EVERYFRAME_LIVE") == "1", "paid call")
    def test_live_one_real_render_on_the_vendor(self):
        """Opt-in and paid (about USD 0.04): the real API, through the same path."""
        from deploy.platform.everyframe_credentials import ENDPOINT, PROFILE, VARIABLE
        from studio_platform.central_registry import load_api

        config = load_api("everyframe", profile=PROFILE)
        self.assertEqual(config.base_url, ENDPOINT)
        created = self.queued_job()
        job = self.row(jobs, jobs.c.id, created["id"])[0]
        with self.repo.transaction() as connection:
            self.assertTrue(rebind(self.repo, connection, job, TURBO.id, self.now))
        runner, worker = self.runner(None)
        runner.backend._transport = None
        runner.backend._config = Config(config.env[VARIABLE])
        started = time.time()
        while time.time() - started < 480:
            self.now += 5
            runner.run_once(worker, POOL)
            current = self.row(jobs, jobs.c.id, created["id"])[0]
            if current["status"] in ("succeeded", "failed"):
                break
            time.sleep(5)
        cost = (current["result"] or {}).get("actual_cost_microusd")
        print(
            "LIVE",
            current["status"],
            current["error_code"],
            "seconds",
            round(time.time() - started),
            "cost_microusd",
            cost,
        )
        self.assertEqual(current["status"], "succeeded")
        self.assertGreater(cost, 0)
        self.assert_stored_video((832, 480))


if __name__ == "__main__":
    unittest.main()

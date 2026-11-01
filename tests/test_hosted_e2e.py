"""One hosted take end to end on real code, replaying recorded vendor answers.

Rebind, a real WorkerRunner on a hosted slot, the real EveryFrame adapter, real
ffmpeg normalisation, the object store and settlement. The vendor's answers come
from tests/cassettes (VCR.py), so this costs nothing and needs no network.

Re-record only when the adapter or the vendor changes (paid, about USD 0.04):
    INKSEQ_EVERYFRAME_RECORD=1 python -m unittest test_hosted_e2e
Then read the cassette diff before committing: this repository is public.
"""

import copy
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
import vcr

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.inference.protocol import SubmissionRejected
from studio_platform.providers.everyframe import Config, EveryFrameBackend, Prepared
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
TAPES = Path(__file__).parent / "cassettes"
RECORDING = os.environ.get("INKSEQ_EVERYFRAME_RECORD") == "1"
# The only vendor fields a cassette keeps. Balance and account fields never
# reach this public repository.
RENDER_FIELDS = {"id", "status", "costUsd", "videoFullUrl"}
TOP_FIELDS = {"render", "code", "error", "message"}


def small_video(path):
    """A real, tiny MP4 that stands in for the vendor's video in the cassette."""
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:size=832x480:rate=24:duration=5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "16k",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def scrub_request(request):
    parts = urlsplit(request.uri)
    request.uri = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    request.headers = {}
    return request


def scrub_answer(video):
    def scrub(response):
        kind = next(
            (
                value[0]
                for name, value in response["headers"].items()
                if name.lower() == "content-type"
            ),
            "",
        )
        coding = next(
            (
                value[0]
                for name, value in response["headers"].items()
                if name.lower() == "content-encoding"
            ),
            "",
        )
        response["headers"] = {"Content-Type": [kind]}
        if kind.startswith("video/"):
            # VCR also runs this hook when loading a cassette; keep what it has.
            if video:
                response["body"]["string"] = video
            return response
        # The hook sees the raw bytes; a replay gets them plain, with no header.
        body = response["body"]["string"] or b"{}"
        value = json.loads(gzip.decompress(body) if coding == "gzip" else body)
        kept = {key: value[key] for key in TOP_FIELDS if key in value}
        if "render" in kept:
            render = {
                key: item
                for key, item in kept["render"].items()
                if key in RENDER_FIELDS
            }
            if render.get("videoFullUrl"):  # Empty until the render succeeds.
                render["videoFullUrl"] = render["videoFullUrl"].split("?")[0]
            kept["render"] = render
        response["body"]["string"] = json.dumps(kept).encode()
        return response

    return scrub


def tape(name, video=b""):
    return vcr.VCR(
        match_on=("method", "path"),
        record_mode="all" if RECORDING else "none",
        before_record_request=scrub_request,
        before_record_response=scrub_answer(video),
    ).use_cassette(str(TAPES / name), decode_compression=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class HostedEndToEndTests(LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.work = root / "worker"
        self.work.mkdir()
        self.store = LocalObjectStore(root / "objects")

    def key(self):
        if not RECORDING:
            return "replay-only"
        from deploy.platform.everyframe_credentials import (
            ENDPOINT,
            PROFILE,
            VARIABLE,
        )
        from studio_platform.central_registry import load_api

        config = load_api("everyframe", profile=PROFILE)
        self.assertEqual(config.base_url, ENDPOINT)
        return config.env[VARIABLE]

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

    def rows(self, table, column, value):
        with self.repo.engine.connect() as connection:
            return [
                dict(item)
                for item in connection.execute(
                    select(table).where(column == value)
                ).mappings()
            ]

    def runner(self):
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
        backend = EveryFrameBackend(self.store, "/unused", slot_key=worker)
        backend._config = Config(self.key())
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

    def stored_video_sizes(self):
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
        return [(stream["width"], stream["height"]) for stream in found]

    def test_a_rebound_take_renders_is_stored_and_settles_at_the_vendor_cost(self):
        created = self.queued_job()
        job = self.rows(jobs, jobs.c.id, created["id"])[0]
        with self.repo.transaction() as connection:
            self.assertTrue(rebind(self.repo, connection, job, TURBO.id, self.now))
        runner, worker = self.runner()
        video = small_video(Path(self.temp.name) / "stand-in.mp4") if RECORDING else b""
        with tape("everyframe_render.yaml", video) as cassette:
            # A replay never waits; a recording polls the real render.
            for _ in range(96 if RECORDING else 12):
                self.now += 5
                runner.run_once(worker, POOL)
                current = self.rows(jobs, jobs.c.id, created["id"])[0]
                if current["status"] in ("succeeded", "failed", "submission_unknown"):
                    break
                if RECORDING:
                    time.sleep(5)
            renders = [
                json.loads(request.body)
                for request in cassette.requests
                if request.method == "POST" and request.path == "/v1/render"
            ]
        self.assertEqual(
            (current["status"], current["error_code"]), ("succeeded", None)
        )
        # Exactly one render, on the bound model, with the prompt rule applied.
        self.assertEqual(len(renders), 1)
        self.assertEqual(renders[0]["modelId"], TURBO.id)
        self.assertTrue(renders[0]["prompt"].endswith(NO_CUT))
        self.assertNotIn("seed", renders[0])
        # The output reached the store and the vendor's cost was settled.
        cost = current["result"]["actual_cost_microusd"]
        self.assertEqual(cost, 38_000)
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(budget["reserved_microusd"], 0)
        self.assertEqual(budget["spent_microusd"], cost)
        (attempt,) = self.rows(attempts, attempts.c.job_id, created["id"])
        self.assertEqual(attempt["actual_cost_microusd"], cost)
        self.assertEqual(len(current["result"]["artifact_ids"]), 2)  # Video, audio.
        self.assertIn((832, 480), self.stored_video_sizes())

    def test_the_vendor_refusing_a_mode_is_a_rejection_and_is_free(self):
        """Turbo with a reference is a 400; quote would have priced it (#918)."""
        backend = EveryFrameBackend(None, "/unused")
        backend._config = Config(self.key())
        body = {
            "modelId": TURBO.id,
            "prompt": "test-only",
            "duration": 5,
            "resolution": "480p",
            "aspect": "16:9",
            "referenceMode": "omni",
            "attachmentIds": ["att_test_only"],
        }
        with tape("everyframe_refused.yaml"), self.assertRaises(SubmissionRejected):
            backend.submit(Prepared("t", json.dumps(body).encode()), "t")


class CassetteContentTests(unittest.TestCase):
    def test_no_secret_or_account_field_is_committed(self):
        tapes = list(TAPES.glob("everyframe_*.yaml"))
        self.assertEqual(len(tapes), 2)
        for path in tapes:
            with self.subTest(path=path.name):
                text = path.read_bytes()
                for forbidden in (
                    b"uthorization",
                    b"Bearer",
                    b"balanceUsd",
                    b"Set-Cookie",
                    b"email",
                    b"?t=",
                ):
                    self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()

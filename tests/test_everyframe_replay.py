"""Staging replay of the hosted vendor: recorded answers, no vendor call (#1301)."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from studio_platform.inference.protocol import SubmissionRejected
from studio_platform.providers import everyframe_replay as replay
from studio_platform.providers import hosted_models as plan
from studio_platform.providers.everyframe import Config, EveryFrameBackend
from test_everyframe import FAST, Store, asset, compiled, job


def shape(value):
    body, _ = plan.vendor_request(value, FAST)
    return "%s|%s|%s" % (body["resolution"], body["aspect"], body["duration"])


def settings(root, target="staging", origin=replay.STAGING_ORIGIN):
    return SimpleNamespace(
        deployment_target=target, public_origin=origin, data_dir=root
    )


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "runtime-secrets").mkdir()
        self.file = self.root / "runtime-secrets" / replay.FILENAME
        self.value = {
            "version": 1,
            "owners": ["qa-credit"],
            "render_seconds": 60,
            "attachment_seconds": 5,
            "videos": {"480p|16:9|5": "a.mp4"},
        }
        patcher = mock.patch.object(
            replay,
            "read_protected_json",
            lambda path, error: json.loads(path.read_text()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        replay._owners.update(read_at=None, value=frozenset())

    def write(self, **changes):
        self.file.write_text(json.dumps({**self.value, **changes}), encoding="utf-8")

    def test_only_staging_with_a_valid_host_file_replays(self):
        self.assertIsNone(replay.load(settings(self.root)))  # No file.
        self.write()
        self.assertIsNotNone(replay.load(settings(self.root)))
        # Production never replays, whatever is on its disk.
        for target, origin in (
            ("production", "https://inkseq.com"),
            ("production", replay.STAGING_ORIGIN),
            ("staging", "https://inkseq.com"),
        ):
            with self.subTest(target=target, origin=origin):
                other = settings(self.root, target, origin)
                self.assertIsNone(replay.load(other))
                replay._owners.update(read_at=None)
                self.assertEqual(replay.owners(other), frozenset())
        for changes in (
            {"version": 2},
            {"owners": []},
            {"owners": ["../x"]},
            {"render_seconds": -1},
            {"videos": {}},
            {"videos": {"480p|16:9|5": "../a.mp4"}},
            {"videos": {"anything": "a.mp4"}},
            {"extra": 1},
        ):
            with self.subTest(changes=changes):
                self.write(**changes)
                self.assertIsNone(replay.load(settings(self.root)))

    def test_owners_are_read_from_the_file_at_most_every_half_minute(self):
        self.write()
        clock = iter([0, 10, 29, 31]).__next__
        here = settings(self.root)
        self.assertEqual(replay.owners(here, clock=clock), {"qa-credit"})
        self.file.unlink()
        self.assertEqual(replay.owners(here, clock=clock), {"qa-credit"})
        self.assertEqual(replay.owners(here, clock=clock), {"qa-credit"})
        self.assertEqual(replay.owners(here, clock=clock), frozenset())


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class ReplayedRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "take.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i"]
            + ["testsrc=size=848x480:rate=24:duration=5", "-f", "lavfi", "-i"]
            + ["sine=duration=5", "-shortest", "-pix_fmt", "yuv420p", self.video],
            check=True,
        )
        value = compiled(
            inputs={
                "first_frame": "a",
                "last_frame": None,
                "images": [],
                "videos": [],
                "audios": [],
            }
        )
        value["assets"] = {"a": asset()}
        self.compiled, self.now, self.slept, self.live = value, 1000.0, [], []
        config = {
            "owners": ["qa-credit"],
            "render_seconds": 60,
            "attachment_seconds": 5,
            "videos": {shape(value): "take.mp4"},
        }
        recording = replay.Replay(
            config, self.root, clock=lambda: self.now, sleep=self.slept.append
        )

        def vendor(request):
            self.live.append((request.method, request.url.path))
            return httpx.Response(201, json={"attachment": {"id": "att_live"}})

        self.backend = EveryFrameBackend(
            Store(), "/unused", transport=httpx.MockTransport(vendor), replay=recording
        )
        self.backend._config = Config("key-1")

    def job(self, owner):
        return {**job(self.compiled), "owner_id": owner}

    def test_a_listed_account_runs_the_whole_turn_on_the_recording(self):
        work = self.job("qa-credit")
        prepared = self.backend.prepare(work, "t", None, lambda: None)
        self.assertTrue(prepared.replay)
        self.assertEqual(self.slept, [5])  # The upload we did not make.
        task = self.backend.submit(prepared, "t")
        self.assertTrue(task.startswith("rpl_"), task)
        self.assertEqual(self.backend.poll("t", task).state, "running")
        self.now += 60
        outcome = self.backend.poll("t", task)
        self.assertEqual(
            (outcome.state, outcome.actual_cost_microusd), ("succeeded", 0)
        )
        target = self.root / "out"
        target.mkdir()
        paths = self.backend.fetch(work, "t", task, target, lambda: None)
        self.assertTrue(paths["video"].is_file())
        self.assertEqual(self.live, [])  # Nothing reached the vendor.

    def test_another_account_still_goes_to_the_vendor(self):
        prepared = self.backend.prepare(self.job("qa-user"), "t", None, lambda: None)
        self.assertFalse(prepared.replay)
        self.assertEqual(self.live, [("POST", "/v1/render/attachment")])
        self.assertEqual(self.slept, [])

    def test_a_shape_with_no_recording_is_refused_and_ids_stay_on_the_recording(self):
        self.compiled["request"]["duration"] = 8
        prepared = self.backend.prepare(self.job("qa-credit"), "t", None, lambda: None)
        with self.assertRaises(SubmissionRejected):
            self.backend.submit(prepared, "t")
        # A replay id is answered by the recording whoever asks, never the vendor.
        self.assertEqual(self.backend.poll("t", "rpl_zz_0_abc").state, "running")
        self.assertEqual(self.backend.poll("t", "rpl_zz_9_abc").state, "unknown")
        self.assertEqual(self.live, [])


if __name__ == "__main__":
    unittest.main()

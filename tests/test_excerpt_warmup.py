"""Voice excerpts are cut while the story chat draws pictures (#1258)."""

from types import SimpleNamespace
import unittest
from unittest import mock

from studio_platform import generation_admission
from studio_platform.generation_admission import GenerationAdmission


class Assets:
    def __init__(self):
        self.got, self.cuts = [], []

    def get(self, owner, asset_id, project_id):
        self.got.append((owner, asset_id, project_id))

    def derive(self, owner, asset_id, start, end):
        self.cuts.append((owner, asset_id, start, end))
        return {"status": "ready", "asset_id": "cut-" + asset_id}


class Access:
    def __init__(self, project):
        self.project_value, self.authorized = project, []

    def project(self, principal, project_id, operation):
        self.authorized.append(operation)
        return {"version": 3, "payload": self.project_value}

    def authorize(self, principal, project_id, operation):
        self.authorized.append(operation)


def admission(project, drafts):
    assets, access = Assets(), Access(project)
    value = GenerationAdmission(
        repo=None,
        assets=assets,
        settings=SimpleNamespace(tenant_id="t"),
        policies=None,
        access=access,
        planning=None,
        capabilities_provider=lambda: {},
    )

    def render(principal, project, shot_id, caps, setting):
        if isinstance(drafts[shot_id], Exception):
            raise drafts[shot_id]
        return project, None, {"inputs": drafts[shot_id]}

    value._render_draft = render
    return value, assets, access


PROJECT = {
    "id": "story",
    "managed": True,
    "entities": [
        {"id": "shot-1", "type": "shot"},
        {"id": "shot-2", "type": "shot"},
        {"id": "shot-3", "type": "shot"},
        {"id": "zib", "type": "character"},
    ],
}
VOICE = {"asset_id": "voice-zib", "source_range": {"start": 2, "end": 5}}


class WarmExcerptTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            generation_admission, "managed_project", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.principal = SimpleNamespace(owner="owner")
        # Process-wide warm-up state: an app shut down by another test closes it.
        for reset in (
            generation_admission._CLOSING.clear,
            generation_admission._WARMED.clear,
            generation_admission._ACTIVE.clear,
        ):
            reset()
            self.addCleanup(reset)

    def test_each_excerpt_the_shots_reference_is_cut_once(self):
        value, assets, access = admission(
            PROJECT,
            {
                "shot-1": {
                    "audios": [VOICE],
                    "images": [{"asset_id": "pic"}],
                    # Video clips are not voice excerpts; their cut stays at pricing.
                    "videos": [
                        {"asset_id": "clip", "source_range": {"start": 0, "end": 3}}
                    ],
                },
                "shot-2": {
                    "audios": [
                        VOICE,
                        {
                            "asset_id": "voice-mia",
                            "source_range": {"start": 0, "end": 3},
                        },
                    ],
                    "guides": [
                        {"media_id": "clip", "source_range": {"start": 0, "end": 1}}
                    ],
                    "first_frame": None,
                },
                # A shot whose draft cannot be read yet is left to its price check.
                "shot-3": ValueError("no references yet"),
            },
        )
        self.assertEqual(value.warm_excerpts(self.principal, "story"), 2)
        self.assertEqual(
            assets.cuts,
            [("owner", "voice-zib", 2, 5), ("owner", "voice-mia", 0, 3)],
        )
        self.assertEqual(access.authorized, ["jobs:write", "assets:write"])
        self.assertEqual(
            assets.got,
            [("owner", "voice-zib", "story"), ("owner", "voice-mia", "story")],
        )

    def test_one_bad_excerpt_does_not_stop_the_others(self):
        value, assets, access = admission(
            PROJECT,
            {
                "shot-1": {
                    "audios": [
                        {"asset_id": "gone", "source_range": {"start": 0, "end": 3}},
                        VOICE,
                    ]
                },
                "shot-2": {},
                "shot-3": {},
            },
        )
        derive = assets.derive

        def refuse_gone(owner, asset_id, start, end):
            if asset_id == "gone":
                raise KeyError(asset_id)
            return derive(owner, asset_id, start, end)

        assets.derive = refuse_gone
        self.assertEqual(value.warm_excerpts(self.principal, "story"), 1)
        self.assertEqual(assets.cuts, [("owner", "voice-zib", 2, 5)])

    def test_shutdown_starts_no_new_cut_and_waits_for_the_running_one(self):
        self.addCleanup(generation_admission._CLOSING.clear)
        self.addCleanup(generation_admission._WARMED.clear)
        import threading

        release, cutting = threading.Event(), threading.Event()
        value, assets, _ = admission(
            PROJECT,
            {"shot-1": {"audios": [VOICE]}, "shot-2": {}, "shot-3": {}},
        )
        derive = assets.derive

        def slow(*args):
            cutting.set()
            release.wait(5)
            return derive(*args)

        assets.derive = slow
        value.repo = None
        self.assertTrue(
            generation_admission.warm_excerpts_soon(value, self.principal, "story")
        )
        self.assertTrue(cutting.wait(5))
        threading.Timer(0.05, release.set).start()
        generation_admission.drain_warmups(timeout=5)
        self.assertEqual(assets.cuts, [("owner", "voice-zib", 2, 5)])
        self.assertEqual(generation_admission._ACTIVE, set())
        self.assertFalse(
            generation_admission.warm_excerpts_soon(value, self.principal, "later")
        )

    def test_a_story_without_excerpts_cuts_nothing(self):
        value, assets, access = admission(
            PROJECT,
            {"shot-1": {"images": [{"asset_id": "pic"}]}, "shot-2": {}, "shot-3": {}},
        )
        self.assertEqual(value.warm_excerpts(self.principal, "story"), 0)
        self.assertEqual(assets.cuts, [])
        self.assertEqual(access.authorized, ["jobs:write"])

    def test_a_story_is_warmed_at_most_once_in_ten_minutes(self):
        started = []
        clock = [1000.0]

        class Thread:
            def __init__(self, target, args, **_kwargs):
                self.target, self.args = target, args

            def start(self):
                started.append(self.args[2])

        generation_admission._WARMED.clear()
        self.addCleanup(generation_admission._WARMED.clear)
        with (
            mock.patch.object(generation_admission.threading, "Thread", Thread),
            mock.patch.object(
                generation_admission.time, "monotonic", side_effect=lambda: clock[0]
            ),
        ):
            soon = generation_admission.warm_excerpts_soon
            self.assertTrue(soon(None, self.principal, "story"))
            self.assertFalse(soon(None, self.principal, "story"))
            self.assertTrue(soon(None, self.principal, "other"))
            clock[0] += generation_admission.WARM_EVERY_S
            self.assertTrue(soon(None, self.principal, "story"))
        self.assertEqual(started, ["story", "other", "story"])
        generation_admission._WARMED.clear()

    def test_a_failed_warm_up_is_only_logged(self):
        broken = SimpleNamespace(
            warm_excerpts=mock.Mock(side_effect=RuntimeError("storage down"))
        )
        with self.assertLogs("studio_platform.generation_admission", "INFO") as logs:
            generation_admission._warm(broken, self.principal, "story")
        line = [row for row in logs.output if "excerpt_warmup_skipped" in row][0]
        self.assertIn("error_type=RuntimeError", line)
        self.assertNotIn("storage down", line)

    def test_no_warm_up_beside_a_sqlite_database(self):
        sqlite = SimpleNamespace(
            repo=SimpleNamespace(
                engine=SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
            )
        )
        with mock.patch.object(generation_admission.threading, "Thread") as thread:
            self.assertFalse(
                generation_admission.warm_excerpts_soon(sqlite, self.principal, "s")
            )
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()

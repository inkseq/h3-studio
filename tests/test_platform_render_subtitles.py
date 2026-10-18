"""CPU-only Chinese glyph, timing and injection checks; no downloaded fonts."""
import copy
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PIL import Image, ImageChops

from studio_platform import media
from studio_platform.render_backend import CPURenderBackend, validate_render_request
from studio_platform.worker import BackendError, NotReady, SubmissionUncertain
import test_platform_render_backend as fixtures


PROFILE = "windows-yahei" if os.name == "nt" else "noto-cjk"
FONT = Path("C:/Windows/Fonts/msyh.ttc" if os.name == "nt" else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


def subtitle_payload():
    return {"recipe_id": "chapter-roughcut-v1", "request": {"duration": 2, "generate_audio": False,
        "export_crf": 18, "render": {"version": 3, "shots": [{"shot_id": "shot", "source_id": "clip", "frames": 48,
        "source_start_frame": 0}], "audio_tracks": [], "subtitles": {"preset": "shortdrama-zh-v1", "cues": [
        {"id": "cue", "start_frame": 12, "end_frame": 24, "text": "月光落在窗前\n她终于回来了"}]}}},
        "output_spec": {"width": 480, "height": 854, "fps": 24, "frame_count": 48}, "sources": {"clip": {
        "kind": "video", "simulation": False, "object": {"key": "owners/owner/assets/clip/video.mp4", "size_bytes": 123,
        "sha256": "a"*64, "content_type": "video/mp4"}}}}


class SubtitleValidationTests(unittest.TestCase):
    def test_v3_cli_cannot_register_ready_without_font_probe(self):
        from studio_platform import render_cli
        from studio_platform.settings import Settings
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory)/"data", auth_mode="local-test", render_enabled=True)
            with mock.patch.object(render_cli.Settings, "from_environment", return_value=settings), \
                 mock.patch.object(render_cli.CPURenderBackend, "assert_subtitle_ready", side_effect=NotReady("missing_test_font")) as probe, \
                 mock.patch.object(render_cli, "WorkerControl", side_effect=AssertionError("Must not register")) as control, redirect_stdout(io.StringIO()):
                self.assertEqual(render_cli.main(["--enabled", "--worker-id", "unready", "--instance-id", "fake-host",
                    "--work-dir", str(Path(directory)/"work"), "--contract-version", "3", "--once", "--confirmed-idle"]), 1)
                probe.assert_called_once()
                control.assert_not_called()

    def test_exact_v3_schema_and_legacy_no_silent_upgrade(self):
        payload = subtitle_payload()
        self.assertEqual(validate_render_request(payload)["frames"], 48)
        payload["request"]["render"]["subtitles"] = None
        validate_render_request(payload)
        for version in (1, 2):
            changed = copy.deepcopy(payload)
            changed["request"]["render"]["version"] = version
            with self.assertRaises(BackendError):
                validate_render_request(changed)
        for mutate in (lambda r: r.pop("subtitles"), lambda r: r.update(font_path="/tmp/untrusted.ttf"),
                       lambda r: r.update(filter="drawtext=unsafe")):
            changed = copy.deepcopy(payload)
            mutate(changed["request"]["render"])
            with self.assertRaises(BackendError):
                validate_render_request(changed)

    def test_ass_html_control_long_multiline_or_invalid_timing_is_rejected(self):
        for value in (r"{\pos(0,0)}劫持", r"\N", "<b>文字</b>", "一\r二", "一\x00二", "一\u202e二",
                      "一\u2028二", "一\u2029二", "字"*19, "一\n二\n三", "一\n", ""):
            payload = subtitle_payload()
            payload["request"]["render"]["subtitles"]["cues"][0]["text"] = value
            with self.subTest(value=repr(value)), self.assertRaises(BackendError):
                validate_render_request(payload)
        for mutate in (lambda c: c.update(start_frame=True), lambda c: c.update(start_frame=-1),
                       lambda c: c.update(end_frame=49), lambda c: c.update(end_frame=12),
                       lambda c: c.update(id="../escape"), lambda c: c.update(url="https://invalid.test")):
            payload = subtitle_payload()
            mutate(payload["request"]["render"]["subtitles"]["cues"][0])
            with self.assertRaises(BackendError):
                validate_render_request(payload)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and FONT.is_file(), "Existing reviewed CJK font + libass required")
class SubtitleRenderTests(unittest.TestCase):
    setUp = fixtures.RenderTests.setUp
    video = fixtures.RenderTests.video
    audio = fixtures.RenderTests.audio
    execute = fixtures.RenderTests.execute
    audio_samples = fixtures.RenderTests.audio_samples

    def caption_job(self, *, subtitles=True, audio=False, simulation=False):
        self.backend = CPURenderBackend(self.root / "renders", enabled=True, subtitle_font_profile=PROFILE)
        self.video("clip", "black", size="480x854", simulation=simulation)
        payload = subtitle_payload()
        payload["sources"] = copy.deepcopy(self.sources)
        if not subtitles:
            payload["request"]["render"]["subtitles"] = None
        if audio:
            self.audio("voice", seconds=3, amplitude=4000)
            payload["sources"]["voice"] = copy.deepcopy(self.sources["voice"])
            payload["request"]["generate_audio"] = True
            payload["request"]["render"]["audio_tracks"] = [{"source_id": "voice", "source_start": 0,
                "source_end": 2, "timeline_start": 0, "gain": .5}]
        return {"id": "caption-test", "owner_id": "owner", "request": payload}

    def exact_frame(self, path, index):
        raw = subprocess.run(["ffmpeg", "-v", "error", *media.input_options(path), "-i", str(path),
            "-vf", f"select=eq(n\\,{index})", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            check=True, capture_output=True, timeout=20).stdout
        with Image.open(io.BytesIO(raw)) as image:
            return image.convert("RGB")

    @staticmethod
    def white_box(frame):
        return frame.convert("L").point(lambda x: 255 if x > 190 else 0).getbbox()

    def test_chinese_two_lines_actual_pixels_safe_area_exact_frames_and_silent(self):
        job = self.caption_job()
        job["request"]["request"]["render"]["subtitles"]["cues"].append({"id": "one-frame", "start_frame": 25,
            "end_frame": 26, "text": "中"})
        result = self.execute(job)
        for index in (0, 11, 24, 26, 47):
            self.assertIsNone(self.white_box(self.exact_frame(result["video"], index)), index)
        for index in (12, 23, 25):
            bounds = self.white_box(self.exact_frame(result["video"], index))
            self.assertIsNotNone(bounds, index)
            self.assertGreaterEqual(bounds[0], 38)
            self.assertLessEqual(bounds[2], 442)
            self.assertGreater(bounds[1], 600)
            self.assertLessEqual(bounds[3], 854-102)
        frame = self.exact_frame(result["video"], 15)
        white = frame.convert("L").point(lambda x: 255 if x > 190 else 0)
        ys = [y for y in range(854) if white.crop((0, y, 480, y+1)).getbbox()]
        clusters = sum(i == 0 or y > ys[i-1]+1 for i, y in enumerate(ys))
        self.assertEqual(clusters, 2)
        self.assertFalse(any(s["codec_type"] == "audio" for s in media.probe(result["video"])["streams"]))
        self.assertEqual(set(result), {"video"})
        ass = (self.root / "renders" / "attempt-one" / "captions.ass").read_text(encoding="utf-8-sig")
        self.assertIn("月光落在窗前\\N她终于回来了", ass)
        self.assertEqual(ass.count("Dialogue:"), 2)

    def test_distinct_chinese_glyphs_are_not_the_same_missing_font_box(self):
        job = self.caption_job()
        job["request"]["request"]["render"]["subtitles"]["cues"] = [
            {"id": "a", "start_frame": 0, "end_frame": 12, "text": "中"},
            {"id": "b", "start_frame": 12, "end_frame": 24, "text": "文"}]
        result = self.execute(job)
        first, second = self.exact_frame(result["video"], 5), self.exact_frame(result["video"], 17)
        self.assertIsNotNone(self.white_box(first))
        self.assertIsNotNone(self.white_box(second))
        self.assertIsNotNone(ImageChops.difference(first, second).crop((38, 600, 442, 752)).getbbox())

    def test_burn_and_voice_coexist_and_simulation_marker_remains(self):
        job = self.caption_job(audio=True, simulation=True)
        result = self.execute(job)
        values = self.audio_samples(result["audio"])
        self.assertEqual(len(values), 64000)
        self.assertLessEqual(max(abs(x-2000) for x in values[16000:48000]), 2)
        frame = self.exact_frame(result["video"], 15)
        self.assertIsNotNone(self.white_box(frame.crop((0, 600, 480, 854))))
        for index in (0, 47):
            top = self.exact_frame(result["video"], index).crop((0, 0, 480, 106))
            self.assertGreater(sum(r > 170 and g > 150 and b < 100 for r, g, b in top.getdata()), 40)

    def test_missing_font_only_blocks_burn_before_copy_or_submission(self):
        job = self.caption_job()
        with mock.patch.object(self.backend, "subtitle_environment", side_effect=NotReady("missing_test_font")), \
             mock.patch.object(self.store, "open", side_effect=AssertionError("Must reject before copying")):
            with self.assertRaises(NotReady):
                self.backend.prepare(job, "font-missing", self.store, lambda: None)
        job["request"]["request"]["render"]["subtitles"] = None
        with mock.patch.object(self.backend, "subtitle_environment", side_effect=NotReady("No font needed")):
            result = self.execute(job, "off")
        self.assertIsNone(self.white_box(self.exact_frame(result["video"], 15)))

    def test_unknown_glyph_and_font_profile_are_refused_without_silent_fallback(self):
        job = self.caption_job()
        job["request"]["request"]["render"]["subtitles"]["cues"][0]["text"] = "\U0001f9d1"
        with self.assertRaisesRegex(BackendError, "glyph_unavailable"):
            self.backend.prepare(job, "unknown-glyph", self.store, lambda: None)
        for profile in ("/tmp/font.ttf", "https://font.invalid/x", "arial", None):
            with self.assertRaises(BackendError):
                CPURenderBackend(self.root / "bad", subtitle_font_profile=profile)

    def test_completed_caption_attempt_reconciles_without_rerendering(self):
        job = self.caption_job()
        prepared = self.backend.prepare(job, "captions-complete", self.store, lambda: None)
        self.backend.submit(prepared, "captions-complete")
        restarted = CPURenderBackend(self.root / "renders", enabled=True, subtitle_font_profile=PROFILE)
        with mock.patch.object(restarted, "_render", side_effect=AssertionError("Do not repeat")):
            self.assertEqual(restarted.reconcile("captions-complete").state, "succeeded")
            with self.assertRaises(SubmissionUncertain):
                restarted.submit(prepared, "captions-complete")


if __name__ == "__main__":
    unittest.main()

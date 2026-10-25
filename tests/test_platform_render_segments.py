"""Real CPU proof of ordered shot ranges and matching original audio; no network."""

from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import math
import shutil
import struct
import subprocess
import unittest
import wave

from studio_platform import media
from studio_platform.render_plans import compile_render
from studio_platform.shot_segments import source_binding
from test_platform_generated_audio import generated_story
from test_platform_render_plans import BODY
import test_platform_render_backend as legacy
import test_platform_render_v5 as v5


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required"
)
class RenderSegmentsTests(unittest.TestCase):
    setUp = legacy.RenderTests.setUp
    execute = legacy.RenderTests.execute
    mono = v5.RenderV5Tests.mono
    power = staticmethod(v5.RenderV5Tests.power)

    def sources_fixture(self):
        video = self.root / "original-ten-seconds.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:r=24:d=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1760:sample_rate=32000:duration=10",
                "-vf",
                ",".join(
                    f"drawbox=x=0:y=0:w=iw:h=ih:color={color}:t=fill:"
                    f"enable='gte(t,{start})*lt(t,{end})'"
                    for color, start, end in (
                        ("red", 1, 3),
                        ("blue", 5, 6),
                        ("yellow", 9, 10),
                    )
                ),
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-t",
                "10",
                str(video),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        wav, audio = self.root / "original.wav", self.root / "original.flac"
        samples = bytearray()
        for index in range(10 * 32000):
            frequency = 110
            for start, end, tone in ((1, 3, 440), (5, 6, 660), (9, 10, 880)):
                if start * 32000 <= index < end * 32000:
                    frequency = tone
                    break
            sample = round(5000 * math.sin(2 * math.pi * frequency * index / 32000))
            samples.extend(struct.pack("<hh", sample, sample))
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(samples)
        media.ffmpeg(["-i", wav, "-c:a", "flac", audio])
        for ident, path, kind, content_type in (
            ("clip", video, "video", "video/mp4"),
            ("voice", audio, "audio", "audio/flac"),
        ):
            with path.open("rb") as stream:
                info = self.store.write_new(
                    "owner",
                    ident,
                    stream,
                    filename=path.name,
                    content_type=content_type,
                )
            self.sources[ident] = {
                "kind": kind,
                "object": asdict(info),
                "simulation": False,
            }

    def source_hashes(self):
        result = {}
        for ident, source in self.sources.items():
            with self.store.open(source["object"]["key"]) as stream:
                result[ident] = hashlib.sha256(stream.read()).hexdigest()
        return result

    def assert_segments_render(self, ranges, expected_colors, expected_tones):
        self.sources_fixture()
        originals = self.source_hashes()
        project, track = generated_story()
        shot = project["entities"][2]
        video = next(item for item in project["entities"] if item["id"] == "clip")
        shot["data"].update(
            seconds=10,
            videoSegments=[{"start": start, "end": end} for start, end in ranges],
            videoSegmentsSource=source_binding(video),
        )
        track.update(start=0, end=10, gain=1)
        excluded = copy.deepcopy(shot)
        excluded.update(id="excluded", order=1)
        excluded["data"] = {"seconds": 10, "selectedAssetId": "unavailable"}
        project["entities"].append(excluded)
        project["journey"]["filmCuts"] = {
            "chapter-one": {"shotIds": [shot["id"]], "clip_sound_crossfade_s": 0}
        }
        calls = []

        def resolver(entity, kind):
            calls.append((entity["id"], kind))
            self.assertIn(entity["id"], {"clip", "voice"})
            return {
                **self.sources[entity["id"]],
                "duration": 10,
                "source_job_id": "job-one",
                "artifact_id": entity["data"].get("cloudArtifactId"),
            }

        original_project = copy.deepcopy(project)
        compiled, display, blockers, _ = compile_render(
            {**BODY, "aspect": "1:1"}, project, resolver
        )
        self.assertEqual(blockers, [])
        self.assertEqual(project, original_project)
        self.assertEqual(display["duration"], 4)
        self.assertCountEqual(calls, [("clip", "video"), ("voice", "audio")])
        self.assertEqual(set(compiled["sources"]), {"clip", "voice"})
        self.assertEqual(compiled["output_spec"]["frame_count"], 96)
        render = compiled["request"]["render"]
        self.assertEqual(render["version"], 5)
        self.assertEqual(
            [
                (
                    item["source_id"],
                    item["source_start_frame"],
                    item["source_frames"],
                    item["frames"],
                    item["playback_rate"],
                )
                for item in render["shots"]
            ],
            [
                ("clip", start * 24, (end - start) * 24, (end - start) * 24, 1)
                for start, end in ranges
            ],
        )
        timeline, expected_audio = 0, []
        for start, end in ranges:
            count = (end - start) * 32000
            expected_audio.append(
                ("voice", start * 32000, end * 32000, timeline, count)
            )
            timeline += count
        self.assertEqual(
            [
                (
                    item["source_id"],
                    item["source_start_sample"],
                    item["source_end_sample"],
                    item["timeline_start_sample"],
                    item["output_samples"],
                )
                for item in render["audio_tracks"]
            ],
            expected_audio,
        )
        self.assertTrue(
            all(
                item["fade_in_samples"] == item["fade_out_samples"] == 0
                and item["playback_rate"] == item["gain"] == 1
                for item in render["audio_tracks"]
            )
        )
        job = {"id": "segments-fixture", "owner_id": "owner", "request": compiled}
        original_request = copy.deepcopy(compiled)
        result = self.execute(job, "segments-fixture")
        self.assertEqual(job["request"], original_request)
        self.assertEqual(self.source_hashes(), originals)
        info = media.probe(result["video"])
        stream = next(item for item in info["streams"] if item["codec_type"] == "video")
        self.assertEqual((stream["nb_frames"], stream["r_frame_rate"]), ("96", "24/1"))
        self.assertAlmostEqual(float(stream["duration"]), 4, places=6)
        pixels = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(result["video"]),
                "-vf",
                "crop=2:2:(iw-2)/2:(ih-2)/2",
                "-an",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
        self.assertEqual(len(pixels), 96 * 12)
        cursor = 0
        for (start, end), color in zip(ranges, expected_colors):
            for frame in range(cursor, cursor + (end - start) * 24):
                red, green, blue = pixels[frame * 12 : frame * 12 + 3]
                if color == "red":
                    self.assertGreater(red, 200)
                    self.assertLess(max(green, blue), 30)
                elif color == "blue":
                    self.assertGreater(blue, 200)
                    self.assertLess(max(red, green), 30)
                else:
                    self.assertGreater(min(red, green), 200)
                    self.assertLess(blue, 30)
            cursor += (end - start) * 24
        samples = self.mono(result["audio"])
        self.assertEqual(len(samples), 128000)
        cursor = 0
        for (start, end), expected in zip(ranges, expected_tones):
            first, last = cursor + 0.2, cursor + (end - start) - 0.2
            wanted = self.power(samples, expected, first, last)
            self.assertGreater(wanted, 1_000_000)
            for unwanted in {110, 440, 660, 880, 1760} - {expected}:
                self.assertGreater(
                    wanted, 30 * self.power(samples, unwanted, first, last)
                )
            cursor += end - start

    def test_three_disjoint_ranges_render_96_frames_and_matching_original_audio(self):
        self.assert_segments_render(
            [(1, 3), (5, 6), (9, 10)], ["red", "blue", "yellow"], [440, 660, 880]
        )

    def test_nonchronological_repeated_ranges_keep_authored_order_and_audio(self):
        self.assert_segments_render(
            [(9, 10), (1, 3), (9, 10)], ["yellow", "red", "yellow"], [880, 440, 880]
        )


if __name__ == "__main__":
    unittest.main()

"""Versioned CPU export fixtures; synthetic local media, no generated footage."""

from __future__ import annotations

from array import array
import copy
from dataclasses import asdict
import math
import shutil
import struct
import subprocess
import unittest
from unittest import mock
import wave

from studio_platform import media
from studio_platform.render_backend import (
    BackendError,
    CPURenderBackend,
    SubmissionRejected,
    SubmissionUncertain,
    validate_render_request,
)
import test_platform_render_backend as legacy


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU tools required"
)
class RenderV5Tests(unittest.TestCase):
    setUp = legacy.RenderTests.setUp
    video = legacy.RenderTests.video
    job = legacy.RenderTests.job
    execute = legacy.RenderTests.execute

    def v5(self, shots, tracks=(), stage="chapter"):
        job = self.job(shots=shots, tracks=list(tracks), render_version=5)
        job["request"]["request"]["render"].update(stage=stage, subtitles=None)
        return job

    def tone(self, source_id, seconds, frequency, amplitude=5000):
        wav, flac = self.root / (source_id + ".wav"), self.root / (source_id + ".flac")
        samples = bytearray()
        for index in range(seconds * 32000):
            sample = round(
                amplitude * math.sin(2 * math.pi * frequency * index / 32000)
            )
            samples.extend(struct.pack("<hh", sample, sample))
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(samples)
        media.ffmpeg(["-i", wav, "-c:a", "flac", flac])
        with flac.open("rb") as stream:
            info = self.store.write_new(
                "owner",
                source_id,
                stream,
                filename=flac.name,
                content_type="audio/flac",
            )
        self.sources[source_id] = {
            "kind": "audio",
            "object": asdict(info),
            "simulation": False,
        }

    @staticmethod
    def shot(ident, source_frames, frames, rate, first=0):
        return {
            "shot_id": "shot-" + ident,
            "source_id": ident,
            "frames": frames,
            "source_start_frame": first,
            "source_frames": source_frames,
            "playback_rate": rate,
        }

    @staticmethod
    def track(
        ident, start, end, timeline, output, rate=1, gain=1, fade_in=0, fade_out=0
    ):
        return {
            "source_id": ident,
            "source_start_sample": start,
            "source_end_sample": end,
            "timeline_start_sample": timeline,
            "output_samples": output,
            "playback_rate": rate,
            "gain": gain,
            "fade_in_samples": fade_in,
            "fade_out_samples": fade_out,
        }

    def mono(self, path):
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "32000",
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        )
        values = array("h")
        values.frombytes(result.stdout)
        return values

    @staticmethod
    def rms(samples, first, last):
        value = samples[round(first * 32000) : round(last * 32000)]
        return math.sqrt(sum(sample * sample for sample in value) / len(value))

    @staticmethod
    def power(samples, frequency, first, last):
        value = samples[round(first * 32000) : round(last * 32000)]
        real = sum(
            sample * math.cos(2 * math.pi * frequency * i / 32000)
            for i, sample in enumerate(value)
        )
        imag = sum(
            sample * math.sin(2 * math.pi * frequency * i / 32000)
            for i, sample in enumerate(value)
        )
        return (real * real + imag * imag) / len(value) ** 2

    def test_combined_216_frame_fixture_pitch_samples_edges_and_detached_audio(self):
        from test_render_plans_v5 import fixture
        from test_platform_render_plans import BODY
        from studio_platform.render_plans import compile_render

        for ident, color, seconds in (
            ("C", "red", 4),
            ("A", "blue", 3),
            ("D", "yellow", 3),
        ):
            self.video(ident, color, seconds=seconds, size="256x256")
        for ident, seconds, frequency, amplitude in (
            ("c-sound", 4, 440, 5000),
            ("a-sound", 3, 660, 5000),
            ("d-detached", 3, 880, 5000),
            ("music", 9, 110, 1000),
        ):
            self.tone(ident, seconds, frequency, amplitude)
        project, catalog, calls, resolver = fixture()
        # Feed the same authoring fixture through the actual server compiler;
        # replace only its synthetic source metadata with these real fixtures.
        for artifact, ident, duration in (
            ("artifact-c-video", "C", 4),
            ("artifact-a-video", "A", 3),
            ("artifact-d-video", "D", 3),
            ("artifact-c-audio", "c-sound", 4),
            ("artifact-a-audio", "a-sound", 3),
            ("artifact-d-audio", "d-detached", 3),
            ("music-artifact", "music", 9),
        ):
            catalog[artifact].update(
                object=self.sources[ident]["object"],
                duration=duration,
                simulation=False,
            )
        for entity in project["entities"]:
            if entity["id"] in {"c", "a", "d"}:
                selected = entity["data"]["selectedVideoRange"]
                selected.update(start=1, end=selected["end"] + 1)
        for track in project["journey"]["soundTracks"]["chapter-one"]:
            if track["id"] in {"sound-c", "sound-a", "sound-d"}:
                track.update(start=1, end=track["end"] + 1, gain=0.7)
            elif track["id"] == "music-track":
                track["gain"] = 0.2
        compiled, display, blockers, _ = compile_render(
            {**BODY, "aspect": "1:1"}, project, resolver
        )
        self.assertEqual(blockers, [])
        self.assertEqual(display["duration"], 9)
        self.assertNotIn(("b-video", "video"), calls)
        self.assertNotIn(("b-audio", "audio"), calls)
        render = compiled["request"]["render"]
        self.assertEqual(
            [
                (s["source_start_frame"], s["source_frames"], s["frames"])
                for s in render["shots"]
            ],
            [(24, 72, 96), (24, 48, 24), (24, 48, 96)],
        )
        c, a, detached, music = render["audio_tracks"]
        self.assertEqual((c["fade_out_samples"], a["fade_in_samples"]), (8000, 8000))
        self.assertEqual(
            (detached["timeline_start_sample"], detached["output_samples"]),
            (160000, 64000),
        )
        self.assertEqual(
            (music["fade_in_samples"], music["fade_out_samples"]), (32000, 64000)
        )
        job = {"id": "job-combined-v5", "owner_id": "owner", "request": compiled}
        original_payload = copy.deepcopy(job["request"])
        result = self.execute(job, "combined-v5")
        self.assertEqual(job["request"], original_payload)
        info = media.probe(result["video"])
        self.assertEqual(
            next(s for s in info["streams"] if s["codec_type"] == "video")["nb_frames"],
            "216",
        )
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
        self.assertEqual(len(pixels), 216 * 12)
        for frame in range(216):
            red, green, blue = pixels[frame * 12 : frame * 12 + 3]
            if frame < 96:
                self.assertGreater(red, 200)
                self.assertLess(max(green, blue), 30)
            elif frame < 120:
                self.assertGreater(blue, 200)
                self.assertLess(max(red, green), 30)
            else:
                self.assertGreater(min(red, green), 200)
                self.assertLess(blue, 30)
        samples = self.mono(result["audio"])
        self.assertEqual(len(samples), 288000)
        self.assertGreater(
            self.power(samples, 440, 1.2, 1.6), 30 * self.power(samples, 330, 1.2, 1.6)
        )
        self.assertGreater(
            self.power(samples, 660, 4.4, 4.8), 30 * self.power(samples, 1320, 4.4, 4.8)
        )
        self.assertGreater(
            self.power(samples, 880, 5.2, 5.6), 30 * self.power(samples, 440, 5.2, 5.6)
        )
        self.assertLess(
            self.rms(samples, 3.95, 3.99), self.rms(samples, 3.5, 3.6) * 0.3
        )
        self.assertLess(
            self.rms(samples, 4.01, 4.04), self.rms(samples, 4.5, 4.6) * 0.3
        )
        self.assertLess(self.rms(samples, 8.7, 8.8), self.rms(samples, 7.1, 7.2) * 0.25)
        self.assertLess(
            self.power(samples, 880, 7.2, 7.6),
            self.power(samples, 880, 5.2, 5.6) / 1000,
        )

    def test_all_rates_half_up_frames_and_final_join_copy(self):
        self.video("red", "red", seconds=1, size="256x256")
        rates = ((0.5, 48), (0.75, 32), (1, 24), (1.25, 19), (1.5, 16), (2, 12))
        shots = [
            dict(self.shot("red", 24, frames, rate), shot_id=f"shot-{index}")
            for index, (rate, frames) in enumerate(rates)
        ]
        result = self.execute(self.v5(shots), "all-rates-v5")
        self.assertEqual(
            int(media.probe(result["video"])["streams"][0]["nb_frames"]), 151
        )
        # A v5 final stage reuses normalized chapters, without re-encoding them.
        joined = self.v5([self.shot("red", 24, 24, 1)], stage="film_concat")
        outputs, original = [], self.backend._run_process

        def run(*args, **kwargs):
            outputs.append(args[-1].name)
            return original(*args, **kwargs)

        with mock.patch.object(self.backend, "_run_process", side_effect=run):
            self.execute(joined, "concat-v5")
        self.assertEqual(outputs, ["joined.mp4"])
        rounding = ((0.5, 10), (0.75, 7), (1, 5), (1.25, 4), (1.5, 3), (2, 3))
        fractional = [
            dict(self.shot("red", 5, frames, rate, 12), shot_id=f"fraction-{index}")
            for index, (rate, frames) in enumerate(rounding)
        ]
        rounded = self.execute(self.v5(fractional), "fractional-v5")
        self.assertEqual(
            int(media.probe(rounded["video"])["streams"][0]["nb_frames"]), 32
        )

    def test_invalid_v5_values_and_unknown_fields_refuse(self):
        self.video("red", "red", seconds=1)
        self.tone("sound", 1, 440)
        job = self.v5(
            [self.shot("red", 24, 24, 1)], [self.track("sound", 0, 32000, 0, 32000)]
        )
        changes = [
            ("shot", "playback_rate", True),
            ("shot", "playback_rate", float("nan")),
            ("shot", "playback_rate", 0.4),
            ("shot", "source_frames", 23),
            ("track", "source_start_sample", 0.5),
            ("track", "output_samples", 64000),
            ("track", "fade_in_samples", 160001),
            ("track", "fade_in_samples", 32001),
            ("render", "stage", []),
            ("render", "stage", "other"),
        ]
        for kind, key, value in changes:
            with self.subTest(kind=kind, key=key, value=value):
                payload = copy.deepcopy(job["request"])
                render = payload["request"]["render"]
                target = (
                    render
                    if kind == "render"
                    else render["shots" if kind == "shot" else "audio_tracks"][0]
                )
                target[key] = value
                with self.assertRaises(BackendError):
                    validate_render_request(payload, owner_id="owner")
        payload = copy.deepcopy(job["request"])
        payload["request"]["render"]["audio_tracks"][0].update(
            fade_in_samples=20000, fade_out_samples=20000
        )
        with self.assertRaisesRegex(BackendError, "range_limit"):
            validate_render_request(payload)

    def test_short_original_flac_is_not_hidden_by_tempo_padding(self):
        self.video("red", "red", seconds=2)
        self.tone("sound", 1, 440)
        job = self.v5(
            [self.shot("red", 48, 96, 0.5)],
            [self.track("sound", 0, 64000, 0, 128000, 0.5)],
        )
        with mock.patch.object(
            self.backend, "_run_process", side_effect=AssertionError("No encoding")
        ):
            with self.assertRaisesRegex(BackendError, "audio_too_short"):
                self.backend.prepare(job, "short-v5", self.store, lambda: None)

    def test_single_source_frame_rounding_and_unknown_attempt_never_replays(self):
        self.video("red", "red", seconds=1, size="256x256")
        shots = [
            dict(
                self.shot("red", 1, 2 if rate == 0.5 else 1, rate, 23),
                shot_id=f"tiny-{index}",
            )
            for index, rate in enumerate((0.5, 0.75, 1, 1.25, 1.5, 2))
        ]
        job = self.v5(shots)
        result = self.execute(job, "tiny-v5")
        self.assertEqual(
            int(media.probe(result["video"])["streams"][0]["nb_frames"]), 7
        )
        prepared = self.backend.prepare(job, "unknown-v5", self.store, lambda: None)
        with mock.patch.object(self.backend, "_render", side_effect=legacy.Stopped()):
            with self.assertRaises(legacy.Stopped):
                self.backend.submit(prepared, "unknown-v5")
        restarted = CPURenderBackend(self.root / "renders", enabled=True)
        self.assertEqual(restarted.reconcile("unknown-v5").state, "unknown")
        with mock.patch.object(
            restarted, "_render", side_effect=AssertionError("No replay")
        ):
            with self.assertRaises(SubmissionUncertain):
                restarted.submit(prepared, "unknown-v5")

    def test_false_long_header_cannot_hide_short_decoded_video(self):
        self.video("red", "red", seconds=1, size="256x256")
        job = self.v5([self.shot("red", 48, 96, 0.5)])
        original = media.probe

        def exaggerated_header(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path.name == "input-000.mp4":
                info["format"]["duration"] = "2.000000"
                info["streams"][0]["duration"] = "2.000000"
            return info

        with mock.patch.object(media, "probe", side_effect=exaggerated_header):
            prepared = self.backend.prepare(
                job, "short-decoded-v5", self.store, lambda: None
            )
        with self.assertRaises(SubmissionRejected):
            self.backend.submit(prepared, "short-decoded-v5")
        directory = self.backend._directory("short-decoded-v5")
        self.assertTrue((directory / "source-000.framemd5").is_file())
        self.assertFalse((directory / "clip-000.mp4").exists())

    def test_false_long_flac_header_cannot_hide_one_missing_sample_before_atempo(self):
        self.video("red", "red", seconds=1, size="256x256")
        self.tone("sound", 1, 440)
        job = self.v5(
            [self.shot("red", 24, 48, 0.5)],
            [self.track("sound", 0, 32001, 0, 64000, 0.5)],
        )
        original = media.probe

        def exaggerated_header(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path.name.startswith("input-") and path.suffix == ".flac":
                info["format"]["duration"] = "2.000000"
                info["streams"][0]["duration"] = "2.000000"
            return info

        with mock.patch.object(media, "probe", side_effect=exaggerated_header):
            prepared = self.backend.prepare(
                job, "short-decoded-audio-v5", self.store, lambda: None
            )
            with self.assertRaises(SubmissionRejected):
                self.backend.submit(prepared, "short-decoded-audio-v5")
        directory = self.backend._directory("short-decoded-audio-v5")
        segment = directory / "audio-000.flac"
        self.assertEqual(
            round(float(original(segment)["format"]["duration"]) * 32000), 32000
        )
        self.assertFalse((directory / "result.flac").exists())


if __name__ == "__main__":
    unittest.main()

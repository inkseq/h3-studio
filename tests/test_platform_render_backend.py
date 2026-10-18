"""Real small CPU fixtures plus crash tests. Temporary state; no cloud/API calls."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from PIL import Image

from studio_platform import media
from studio_platform.render_backend import CPURenderBackend, RECIPE, validate_render_request
from studio_platform.storage import LocalObjectStore
from studio_platform.worker import BackendError, NotReady, SubmissionRejected, SubmissionUncertain


class Stopped(BaseException):
    pass


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Networking forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.store = LocalObjectStore(self.root / "objects")
        self.backend = CPURenderBackend(self.root / "renders", enabled=True)
        self.sources = {}

    def video(self, source_id, color, *, seconds=2, size="320x256", simulation=False, audio=True):
        path = self.root / (source_id+".mp4")
        args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=24:d={seconds}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=32000:duration={seconds}"]
        args += ["-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", str(seconds)]
        args += ["-c:a", "aac", "-ac", "2"] if audio else ["-an"]
        subprocess.run([*args, str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        with path.open("rb") as stream:
            info = self.store.write_new("owner", source_id, stream, filename=path.name, content_type="video/mp4")
        self.sources[source_id] = {"kind": "video", "object": asdict(info), "simulation": simulation}
        return source_id

    def audio(self, source_id, *, seconds=3, amplitude=4000):
        out = io.BytesIO()
        with wave.open(out, "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(struct.pack("<h", amplitude)*(seconds*32000*2))
        info = self.store.write_new("owner", source_id, io.BytesIO(out.getvalue()), filename=source_id+".wav", content_type="audio/wav")
        self.sources[source_id] = {"kind": "audio", "object": asdict(info), "simulation": False}
        return source_id

    def job(self, *, tracks=None, shots=None, render_version=1):
        if not self.sources:
            self.video("red", "red")
            self.video("blue", "blue")
        shots = shots or [{"shot_id": "shot-red", "source_id": "red", "frames": 24},
                          {"shot_id": "shot-blue", "source_id": "blue", "frames": 24}]
        tracks = tracks or []
        frames = sum(shot["frames"] for shot in shots)
        used = {item["source_id"] for item in [*shots, *tracks]}
        payload = {"recipe_id": RECIPE, "request": {"duration": frames/24, "generate_audio": bool(tracks), "export_crf": 18,
                   "render": {"version": render_version, "shots": shots, "audio_tracks": tracks}},
                   "output_spec": {"width": 256, "height": 256, "fps": 24, "frame_count": frames},
                   "sources": {key: copy.deepcopy(value) for key, value in self.sources.items() if key in used}}
        return {"id": "job-one", "owner_id": "owner", "request": payload}

    def execute(self, job, tag="attempt-one"):
        prepared = self.backend.prepare(job, tag, self.store, lambda: None)
        task_id = self.backend.submit(prepared, tag)
        self.assertEqual(self.backend.poll(tag, task_id).state, "succeeded")
        return self.backend.fetch(job, tag, task_id, self.root / "unused", lambda: None)

    def frame(self, video, at, filename):
        path = self.root / filename
        media.ffmpeg(["-ss", at, "-i", video, "-frames:v", "1", path])
        with Image.open(path) as image:
            return image.convert("RGB")

    def test_default_is_disabled_and_starts_nothing(self):
        path = self.root / "disabled"
        backend = CPURenderBackend(path)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No process")):
            with self.assertRaises(NotReady):
                backend.prepare({}, "attempt", self.store, lambda: None)
        self.assertFalse(path.exists())

    def test_order_letterbox_24fps_and_original_audio_discarded(self):
        job = self.job()
        output = self.execute(job)
        self.assertEqual(set(output), {"video"})
        streams = media.probe(output["video"])["streams"]
        self.assertFalse(any(stream["codec_type"] == "audio" for stream in streams))
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["nb_frames"], video["avg_frame_rate"]), (256, 256, "48", "24/1"))
        first, second = self.frame(output["video"], .25, "first.png"), self.frame(output["video"], 1.25, "second.png")
        self.assertGreater(first.getpixel((128, 128))[0], 200)
        self.assertGreater(second.getpixel((128, 128))[2], 200)
        self.assertLess(max(first.getpixel((128, 3))), 10)
        self.assertEqual(self.backend.actual_cost_resolver(job, "cpu-render-attempt-one"), 0)

    def test_source_too_short_fails_before_submission(self):
        job = self.job(shots=[{"shot_id": "one", "source_id": "red", "frames": 72}])
        with mock.patch.object(media, "run_media_process", wraps=media.run_media_process) as process, \
                mock.patch.object(self.backend, "_run_process", side_effect=AssertionError("transcode must not start")) as encoder:
            with self.assertRaisesRegex(BackendError, "video_too_short"):
                self.backend.prepare(job, "short", self.store, lambda: None)
        # Probe through the actual bounded launcher is permitted. Its OS argv
        # is an absolute executable on Windows and a Python exec wrapper on
        # Linux, so assert the tool contract rather than the old bare argv.
        self.assertGreater(process.call_count, 0)
        self.assertTrue(all(call.args[0][0] == "ffprobe" for call in process.call_args_list))
        encoder.assert_not_called()
        self.assertEqual(self.backend.reconcile("short").state, "unknown")

    def test_nonzero_source_frames_use_blue_middle_of_real_30fps_clip(self):
        path = self.root / "three-colors.mp4"
        args = ["ffmpeg", "-v", "error", "-nostdin"]
        for color in ("red", "blue", "lime"):
            args += ["-f", "lavfi", "-i", f"color=c={color}:s=320x256:r=30:d=1"]
        args += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                 "-an", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(path)]
        subprocess.run(args, check=True, capture_output=True, timeout=20)
        with path.open("rb") as stream:
            info = self.store.write_new("owner", "middle", stream, filename=path.name, content_type="video/mp4")
        self.sources["middle"] = {"kind": "video", "object": asdict(info), "simulation": False}
        job = self.job(shots=[{"shot_id": "middle-blue", "source_id": "middle", "frames": 24, "source_start_frame": 24}], render_version=2)
        result = self.execute(job, "middle-blue")
        video = next(s for s in media.probe(result["video"])["streams"] if s["codec_type"] == "video")
        self.assertEqual(video["nb_frames"], "24")
        for at, name in ((.02, "middle-first.png"), (.95, "middle-last.png")):
            color = self.frame(result["video"], at, name).getpixel((128, 128))
            self.assertGreater(color[2], 200)
            self.assertLess(color[0], 20)
            self.assertLess(color[1], 20)

    def test_v2_source_start_validation_and_actual_tail_limit(self):
        original = self.job(shots=[{"shot_id": "red", "source_id": "red", "frames": 24, "source_start_frame": 24}], render_version=2)
        for value in (True, -1, .5, 86400):
            job = copy.deepcopy(original)
            job["request"]["request"]["render"]["shots"][0]["source_start_frame"] = value
            with self.assertRaises(BackendError):
                validate_render_request(job["request"])
        old = copy.deepcopy(original)
        old["request"]["request"]["render"]["version"] = 1
        with self.assertRaises(BackendError):
            validate_render_request(old["request"])
        original["request"]["request"]["render"]["shots"][0]["source_start_frame"] = 25
        with self.assertRaisesRegex(BackendError, "video_too_short"):
            self.backend.prepare(original, "past-end", self.store, lambda: None)

    def test_independent_audio_timeline_gain_and_separate_flac(self):
        self.video("red", "red")
        self.audio("voice")
        job = self.job(shots=[{"shot_id": "red", "source_id": "red", "frames": 48}],
                       tracks=[{"source_id": "voice", "timeline_start": .5, "source_start": .25, "source_end": 1.25, "gain": .5}])
        outputs = self.execute(job)
        self.assertEqual(set(outputs), {"video", "audio"})
        self.assertTrue(any(s["codec_type"] == "audio" for s in media.probe(outputs["video"])["streams"]))
        audio = media.probe(outputs["audio"])
        self.assertEqual((audio["streams"][0]["sample_rate"], audio["streams"][0]["channels"]), ("32000", 2))
        raw = subprocess.run(["ffmpeg", "-v", "error", *media.input_options(outputs["audio"]), "-i", str(outputs["audio"]),
                              "-f", "s16le", "-acodec", "pcm_s16le", "-"], capture_output=True, check=True, timeout=20).stdout
        samples = struct.unpack("<"+"h"*(len(raw)//2), raw)
        def maximum(start, end):
            return max(abs(value) for value in samples[round(start*64000):round(end*64000)])
        self.assertEqual(maximum(.05, .4), 0)
        self.assertTrue(1900 <= maximum(.7, 1.2) <= 2100)
        self.assertEqual(maximum(1.65, 1.95), 0)

    def marked_flac(self, source_id, amplitudes):
        """Each value is one stereo sample, so boundaries are independently known."""
        wav = self.root / (source_id+".wav")
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(b"".join(struct.pack("<hh", value, value) for value in amplitudes))
        path = self.root / (source_id+".flac")
        media.ffmpeg(["-i", wav, "-c:a", "flac", path])
        with path.open("rb") as stream:
            info = self.store.write_new("owner", source_id, stream, filename=path.name, content_type="audio/flac")
        self.sources[source_id] = {"kind": "audio", "object": asdict(info), "simulation": False}

    def audio_samples(self, path):
        raw = subprocess.run(["ffmpeg", "-v", "error", *media.input_options(path), "-i", str(path),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"], check=True, capture_output=True, timeout=20).stdout
        return struct.unpack("<"+"h"*(len(raw)//2), raw)[::2]

    def test_generated_audio_real_two_second_inpoint_and_music_gains(self):
        from test_platform_generated_audio import generated_story
        from test_platform_render_plans import BODY
        from studio_platform.render_plans import compile_render
        # Color + a conflicting embedded tone; only the independent FLAC may mix.
        path = self.root / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=red:s=256x256:r=24:d=6", "-f", "lavfi", "-i",
            "sine=frequency=880:sample_rate=32000:duration=6", "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,2)*lt(t,5)'",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "6", str(path)], check=True, capture_output=True, timeout=20)
        with path.open("rb") as stream:
            info = self.store.write_new("owner", "clip", stream, filename=path.name, content_type="video/mp4")
        self.sources["clip"] = {"kind": "video", "object": asdict(info), "simulation": False}
        self.marked_flac("voice", [1000*(second+1) for second in range(6) for _ in range(32000)])
        self.audio("music", seconds=6, amplitude=1000)
        project, track = generated_story()
        shot = project["entities"][2]
        shot["data"].update(seconds=3, selectedVideoRange={"assetId": "clip", "fileId": "local-clip",
            "cloudAssetId": None, "cloudArtifactId": "artifact-video", "start": 2, "end": 5})
        music = copy.deepcopy(project["entities"][-1])
        music.update(id="music", title="Independent music")
        music["data"].update(fileId="local-music", cloudArtifactId=None)
        project["entities"].append(music)
        project["journey"]["soundTracks"]["chapter-one"].append({"assetId": "music", "fileId": "local-music",
            "start": 0, "end": 3, "gain": .2})
        def resolver(entity, kind):
            return {**self.sources[entity["id"]], "duration": 6, "source_job_id": "job-one",
                    "artifact_id": entity["data"].get("cloudArtifactId")}
        payload, _, blockers, _ = compile_render({**BODY, "aspect": "1:1"}, project, resolver)
        self.assertFalse(blockers)
        result = self.execute({"id": "linked-real", "owner_id": "owner", "request": payload})
        for index, at in enumerate((.05, 1.5, 2.95)):
            frame = self.frame(result["video"], at, f"linked-{index}.png")
            self.assertGreater(frame.getpixel((240, 240))[2], 200)
        samples = self.audio_samples(result["audio"])
        self.assertEqual(len(samples), 96000)
        for second, expected in enumerate((2300, 3000, 3700)):
            middle = samples[second*32000+8000:second*32000+16000]
            self.assertLessEqual(max(abs(value-expected) for value in middle), 2)

    def test_v2_sample_boundaries_do_not_accumulate_drift(self):
        from studio_platform.render_plans import frame_sample
        self.video("clip", "blue", seconds=2)
        marks = [0]*64000
        # Copy differently phased one/two-frame ranges from the same source.
        for sample in (frame_sample(1)+80, frame_sample(2)+80, frame_sample(4)+80):
            marks[sample:sample+16] = [4000]*16
        self.marked_flac("voice", marks)
        shots, tracks, expected, cursor = [], [], [], 0
        for index, (first, frames) in enumerate(((1, 1), (2, 2), (4, 1), (1, 1), (2, 2), (4, 1))):
            shots.append({"shot_id": f"shot-{index}", "source_id": "clip", "frames": frames, "source_start_frame": first})
            tracks.append({"source_id": "voice", "timeline_start": frame_sample(cursor)/32000,
                "source_start": frame_sample(first)/32000, "source_end": frame_sample(first+frames)/32000, "gain": .5})
            expected.append(frame_sample(cursor)+80)
            cursor += frames
        result = self.execute(self.job(shots=shots, tracks=tracks, render_version=2))
        values = self.audio_samples(result["audio"])
        self.assertLessEqual(abs(len(values)-frame_sample(cursor)), 1)
        starts = [i for i, value in enumerate(values) if value > 1900 and (i == 0 or values[i-1] <= 1900)]
        self.assertEqual(starts, expected)

    def test_short_clip_concat_has_exact_24fps_packet_timeline(self):
        from fractions import Fraction
        self.video("red", "red", seconds=1, audio=False)
        self.video("blue", "blue", seconds=1, audio=False)
        shots = [{"shot_id": f"shot-{index}", "source_id": "red" if index % 2 == 0 else "blue",
                  "frames": count, "source_start_frame": 0}
                 for index, count in enumerate((1, 2, 1, 1, 2, 1))]
        result = self.execute(self.job(shots=shots, render_version=2), tag="short-grid")
        info = media.probe(result["video"])
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual(Fraction(stream["avg_frame_rate"]), Fraction(24))
        self.assertEqual(int(stream["nb_frames"]), 8)
        self.assertAlmostEqual(float(stream["duration"]), 8/24, delta=1e-5)
        # MP4 movie/header duration may still round to its millisecond scale.
        # The actual video stream and each packet must retain the 24fps grid.
        self.assertAlmostEqual(float(info["format"]["duration"]), 8/24, delta=.001)
        packets = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(result["video"])],
            timeout=30))["packets"]
        self.assertEqual(len(packets), 8)
        for index, packet in enumerate(sorted(packets, key=lambda p: float(p["pts_time"]))):
            self.assertAlmostEqual(float(packet["pts_time"]), index/24, delta=1e-5)
            self.assertAlmostEqual(float(packet["duration_time"]), 1/24, delta=1e-5)

    def test_v2_short_flac_cannot_be_hidden_by_padding_even_if_header_probe_lies(self):
        self.video("clip", "red", seconds=2)
        self.marked_flac("voice", [3000]*31999)
        job = self.job(shots=[{"shot_id": "shot", "source_id": "clip", "frames": 24, "source_start_frame": 0}],
            tracks=[{"source_id": "voice", "timeline_start": 0, "source_start": 0, "source_end": 1, "gain": 1}], render_version=2)
        with self.assertRaisesRegex(BackendError, "audio_too_short"):
            self.backend.prepare(job, "short-probe", self.store, lambda: None)
        real_probe = media.probe
        def inflated(path):
            result = real_probe(path)
            if Path(path).suffix == ".flac" and not Path(path).name.startswith("audio-"):
                result["format"]["duration"] = "1.000000"
                result["streams"][0]["duration"] = "1.000000"
            return result
        with mock.patch.object(media, "probe", side_effect=inflated):
            prepared = self.backend.prepare(job, "decoded-short", self.store, lambda: None)
            with self.assertRaises(SubmissionRejected):
                self.backend.submit(prepared, "decoded-short")
        self.assertFalse((self.root / "renders" / "decoded-short" / "result.flac").exists())

    def test_simulation_mark_is_present_for_entire_mixed_source_film(self):
        self.video("red", "red", simulation=True)
        self.video("blue", "blue", simulation=False)
        output = self.execute(self.job())
        for index, at in enumerate((.1, .9, 1.1, 1.9)):
            frame = self.frame(output["video"], at, f"mark-{index}.png")
            pixels = frame.load()
            yellow = sum(pixels[x, y][0] > 170 and pixels[x, y][1] > 150 and pixels[x, y][2] < 100
                         for x in range(256) for y in range(42))
            self.assertGreater(yellow, 40)

    def test_lost_response_reconciles_same_output_without_transcoding_again(self):
        job = self.job()
        prepared = self.backend.prepare(job, "lost", self.store, lambda: None)
        task_id = self.backend.submit(prepared, "lost")
        restarted = CPURenderBackend(self.root / "renders", enabled=True)
        with mock.patch.object(restarted, "_render", side_effect=AssertionError("Blind repeat forbidden")):
            self.assertEqual(restarted.reconcile("lost", task_id).state, "succeeded")
            with self.assertRaises(SubmissionUncertain):
                restarted.submit(prepared, "lost")
            self.assertTrue(restarted.fetch(job, "lost", task_id, self.root, lambda: None)["video"].exists())

    def test_crash_after_submission_intent_stays_unknown_without_resubmit(self):
        job = self.job()
        prepared = self.backend.prepare(job, "crash", self.store, lambda: None)
        with mock.patch.object(self.backend, "_render", side_effect=Stopped()):
            with self.assertRaises(Stopped):
                self.backend.submit(prepared, "crash")
        restarted = CPURenderBackend(self.root / "renders", enabled=True)
        self.assertEqual(restarted.reconcile("crash").state, "unknown")
        with mock.patch.object(restarted, "_render", side_effect=AssertionError("No repeat")):
            with self.assertRaises(SubmissionUncertain):
                restarted.submit(prepared, "crash")
        self.assertFalse(restarted.cancel("crash", "cpu-render-crash"))

    def test_failed_process_is_terminal_and_not_replayed(self):
        job = self.job()
        prepared = self.backend.prepare(job, "failed", self.store, lambda: None)
        with mock.patch.object(self.backend, "_run_process", side_effect=BackendError("Synthetic failed process")):
            with self.assertRaises(SubmissionRejected):
                self.backend.submit(prepared, "failed")
        self.assertEqual(self.backend.reconcile("failed").state, "failed")
        with self.assertRaises(SubmissionUncertain):
            self.backend.submit(prepared, "failed")

    def test_source_ownership_hash_and_request_mutation_are_rejected(self):
        job = self.job()
        wrong_owner = copy.deepcopy(job)
        wrong_owner["owner_id"] = "other"
        with self.assertRaises(BackendError):
            self.backend.prepare(wrong_owner, "wrong-owner", self.store, lambda: None)
        corrupt = copy.deepcopy(job)
        corrupt["request"]["sources"]["red"]["object"]["sha256"] = "0"*64
        with self.assertRaises(Exception):
            self.backend.prepare(corrupt, "wrong-hash", self.store, lambda: None)
        self.backend.prepare(job, "immutable", self.store, lambda: None)
        changed = copy.deepcopy(job)
        changed["id"] = "different-job"
        with self.assertRaisesRegex(BackendError, "identity"):
            self.backend.prepare(changed, "immutable", self.store, lambda: None)

    def test_limits_reject_urls_excessive_counts_gain_and_audio_overrun(self):
        self.video("red", "red")
        self.audio("voice")
        job = self.job(shots=[{"shot_id": "red", "source_id": "red", "frames": 48}],
                       tracks=[{"source_id": "voice", "timeline_start": 0, "source_start": 0, "source_end": 1, "gain": 1}])
        mutations = [lambda p: p["sources"]["red"]["object"].update(key="https://example.invalid/media"),
                     lambda p: p["request"]["render"]["shots"].__imul__(51),
                     lambda p: p["request"]["render"]["audio_tracks"][0].update(gain=1.01),
                     lambda p: p["request"]["render"]["audio_tracks"][0].update(timeline_start=2),
                     lambda p: p["output_spec"].update(width=1920),
                     lambda p: p["sources"]["red"].update(simulation="false")]
        for mutation in mutations:
            bad = copy.deepcopy(job["request"])
            mutation(bad)
            with self.assertRaises(Exception):
                validate_render_request(bad, owner_id="owner")

    def test_very_small_output_limit_fails_instead_of_publishing_truncation(self):
        self.backend = CPURenderBackend(self.root / "renders", enabled=True, max_output_bytes=512)
        job = self.job()
        prepared = self.backend.prepare(job, "limit", self.store, lambda: None)
        with self.assertRaises(SubmissionRejected):
            self.backend.submit(prepared, "limit")
        self.assertEqual(self.backend.reconcile("limit").state, "failed")

    def test_cancel_during_heartbeat_stops_only_this_attempt(self):
        job = self.job()
        tag = "cancelled"
        def heartbeat():
            if tag in self.backend._active:
                self.assertTrue(self.backend.cancel(tag, "cpu-render-"+tag))
        prepared = self.backend.prepare(job, tag, self.store, heartbeat)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("Cancelled render must not launch FFmpeg")):
            task_id = self.backend.submit(prepared, tag)
        self.assertEqual(self.backend.reconcile(tag, task_id).state, "cancelled")
        self.assertFalse(self.backend.cancel("another", "cpu-render-another"))

    def test_process_timeout_kills_owned_handle_and_does_not_publish(self):
        directory = self.backend._directory("deadline", create=True)
        self.backend._active["deadline"] = {"process": None, "cancelled": threading.Event()}
        process = mock.Mock()
        process.poll.return_value = None
        process.kill.side_effect = lambda: setattr(process.poll, "return_value", -1)
        with mock.patch("subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(BackendError, "timeout"):
                self.backend._run_process([], directory, "deadline", lambda: None, time.monotonic()-1)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_root_capacity_counts_prepared_reservations_before_next_source_read(self):
        self.backend = CPURenderBackend(self.root / "renders", enabled=True,
                                       max_attempt_bytes=1024*1024, max_state_bytes=2*1024*1024)
        job = self.job()
        self.backend.prepare(job, "first", self.store, lambda: None)
        self.backend.prepare(job, "second", self.store, lambda: None)
        with mock.patch.object(self.store, "open", side_effect=AssertionError("Capacity must fail before source read")):
            with self.assertRaisesRegex(BackendError, "capacity"):
                self.backend.prepare(job, "third", self.store, lambda: None)
        restarted = CPURenderBackend(self.root / "renders", enabled=True,
                                    max_attempt_bytes=1024*1024, max_state_bytes=2*1024*1024)
        self.assertGreaterEqual(restarted._state_usage(), 2*1024*1024)

    def test_full_render_cache_stops_unsubmitted_job_instead_of_growing_retries(self):
        from sqlalchemy import func, select
        from studio_platform.repository import Repository, Scope, attempts
        from studio_platform.worker import WorkerRunner
        self.backend = CPURenderBackend(self.root / "renders", enabled=True,
            max_attempt_bytes=64*1024, max_state_bytes=128*1024)
        self.video("tiny", "blue", seconds=1, audio=False)
        request = self.job(shots=[{"shot_id": "tiny-shot", "source_id": "tiny", "frames": 24}])
        for tag in ("retained-first", "retained-second"):
            self.backend.prepare(request, tag, self.store, lambda: None)
        retained = {p: p.read_bytes() for tag in ("retained-first", "retained-second")
                    for p in (self.backend.state_dir / tag).iterdir()}
        clock = [1000.0]
        repo = Repository("sqlite:///"+str(self.root / "capacity.sqlite3").replace("\\", "/"), clock=lambda: clock[0])
        try:
            repo.create_schema()
            scope = Scope("cache-test", "owner", "project")
            plan = repo.create_plan(scope, request["request"],
                {"pool": "cpu-render", "backend": "cpu-render", "enabled": True,
                 "configuration_id": "cpu-render-v1", "expected_runtime_s": 1},
                expires_at=2000, estimated_cost_microusd=0)
            job = repo.create_job(scope, plan["id"], "same-cache-full-job")
            runner = WorkerRunner(repo, self.store, self.root / "worker", backend=self.backend,
                                  submission_guard=lambda job: True)
            with mock.patch.object(self.store, "open", side_effect=AssertionError("No source read with full cache")), \
                 mock.patch.object(self.backend, "submit", side_effect=AssertionError("No render submission with full cache")):
                self.assertEqual(runner.run_once("cpu-worker", "cpu-render")["state"], "failed")
                first = repo.get_job(scope, job["id"])
                self.assertEqual(first["error_code"], "render_cache_capacity_exhausted")
                self.assertEqual(first["result"]["actual_cost_microusd"], 0)
                self.assertEqual(first["result"]["billing_status"], "settled")
                clock[0] += 31
                self.assertEqual(runner.run_once("cpu-worker", "cpu-render")["state"], "idle")
            with repo.engine.connect() as connection:
                self.assertEqual(connection.execute(select(func.count()).select_from(attempts)).scalar_one(), 1)
                row = connection.execute(select(attempts)).mappings().one()
                self.assertIsNone(row["submission_started_at"])
                self.assertIsNone(row["upstream_task_id"])
            self.assertEqual(repo.get_job(scope, job["id"])["attempt_no"], 1)
            self.assertEqual({p: p.read_bytes() for p in retained}, retained)
            self.assertEqual(self.backend._state_usage(), 128*1024)
        finally:
            repo.close()

    def test_unknown_root_entry_is_rejected_without_recursive_scan_or_cleanup(self):
        job = self.job()
        self.backend.state_dir.mkdir()
        foreign = self.backend.state_dir / "foreign"
        foreign.mkdir()
        canary = foreign / "keep.txt"
        canary.write_text("unrelated preserved")
        with self.assertRaises(BackendError):
            self.backend.prepare(job, "blocked", self.store, lambda: None)
        self.assertEqual(canary.read_text(), "unrelated preserved")

    def test_slow_source_copy_checks_deadline_and_never_starts_encoder(self):
        job = self.job()
        clock = [10.0]
        self.backend.timeout_seconds = 1
        class SlowBody(io.BytesIO):
            def read(inner, size=-1):
                clock[0] += 2
                return super().read(size)
        with mock.patch.object(self.store, "open", return_value=SlowBody(b"fake partial input")):
            with mock.patch("studio_platform.render_backend.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(BackendError, "prepare_timeout"):
                    self.backend.prepare(job, "slow", self.store, lambda: None)
        self.assertEqual(self.backend._load(self.backend._directory("slow"))["phase"], "preparation_failed")

    def test_source_copy_heartbeat_can_cancel_this_preparation(self):
        job = self.job()
        clock = [10.0]
        counts = []
        class SlowBody(io.BytesIO):
            def read(inner, size=-1):
                clock[0] += 6
                return super().read(size)
        def heartbeat():
            counts.append(1)
            if len(counts) >= 2:
                self.assertTrue(self.backend.cancel("copy-cancel", "cpu-render-copy-cancel"))
        with mock.patch.object(self.store, "open", return_value=SlowBody(b"partial")):
            with mock.patch("studio_platform.render_backend.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(BackendError, "preparation_cancelled"):
                    self.backend.prepare(job, "copy-cancel", self.store, heartbeat)
        self.assertNotIn("copy-cancel", self.backend._preparing)


class RenderCLITests(unittest.TestCase):
    def test_default_does_not_open_db_storage_or_load_settings(self):
        from studio_platform import render_cli
        with mock.patch.object(render_cli.Settings, "from_environment", side_effect=AssertionError("No settings load")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(render_cli.main([]), 0)

    def test_enabled_cli_still_requires_render_setting(self):
        from studio_platform import render_cli
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"SIXNINE_RENDER_ENABLED": "0"}), mock.patch.object(render_cli, "Repository", side_effect=AssertionError("No DB")):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(render_cli.main(["--enabled", "--worker-id", "fake", "--instance-id", "fake-host",
                                                      "--work-dir", directory, "--once"]), 0)
                    self.assertEqual(json.loads(output.getvalue())["reason"], "render_flag_off")

    def test_once_registers_cpu_without_gpu_or_provider_calls_and_drains(self):
        from studio_platform import render_cli
        from studio_platform.settings import Settings
        from studio_platform.repository import Repository
        from studio_platform.control import WorkerControl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root / "data", auth_mode="local-test", render_enabled=True)
            with mock.patch.object(render_cli.Settings, "from_environment", return_value=settings):
                with mock.patch("subprocess.Popen", side_effect=AssertionError("Idle worker starts no FFmpeg")), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(render_cli.main(["--enabled", "--worker-id", "cpu-one", "--instance-id", "host-one",
                        "--work-dir", str(root / "work"), "--contract-version", "2", "--once", "--confirmed-idle"]), 0)
                    self.assertEqual(json.loads(output.getvalue())["state"], "idle")
            repository = Repository(settings.database_url)
            try:
                worker = WorkerControl(repository).get("cpu-one")
                self.assertEqual(worker["state"], "draining")
                self.assertEqual(worker["spec"]["physical_gpu_ids"], [])
                self.assertEqual(worker["provider"], "local-cpu")
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()

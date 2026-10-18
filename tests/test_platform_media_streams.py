"""Real CPU media tests: inspection and conversion must select the same tracks."""
from __future__ import annotations

import array
import copy
import hashlib
import math
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import unittest
from unittest import mock

from PIL import Image

from studio_platform import media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU ffmpeg/ffprobe required")
class MediaStreamSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sixnine-stream-selection-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def ffmpeg(self, arguments, *, capture=False):
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, arguments)],
            check=True, timeout=30, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return result.stdout

    def native_reference(self, frames=107):
        source = self.root / ("native-"+str(frames)+".mp4")
        self.ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=832x480:r=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000",
            "-frames:v", frames, "-t", frames/24, "-c:v", "libx264", "-threads", "1",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "32000", "-ac", "2", source])
        return source

    def test_real_native_107_frame_input_keeps_grid_and_passes_unchanged_execution_limit(self):
        from studio_platform.execution_policy import input_envelope_blockers
        from studio_platform.qualification_profiles import MULTIMODAL_INPUT_LIMITS
        source = self.native_reference()
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        output, metadata = media.normalize(source, media.inspect(source, "video"), self.root)
        self.assertEqual((metadata["frame_count"], metadata["fps"]), (107, 24))
        self.assertEqual(metadata["duration"], 107/24)
        self.assertEqual(metadata["duration_basis"], "verified_native_video_frames_v1")
        self.assertEqual(int(next(s for s in media.probe(output)["streams"] if s["codec_type"] == "video")["nb_frames"]), 107)
        compiled = {"recipe_id": "h3-base-ref2va-v1", "assets": {"clip": {"metadata": metadata}},
            "request": {"inputs": {"images": [], "videos": ["clip"], "audios": []}, "guides": [], "video_audio": {"clip": True}}}
        self.assertEqual(input_envelope_blockers(compiled, MULTIMODAL_INPUT_LIMITS), [])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_container_millisecond_rounding_of_real_stream_does_not_add_seventeen_frames(self):
        source = self.native_reference()
        original = source.read_bytes()
        probe = media.probe
        def rounded(path):
            data = probe(path)
            if Path(path) == source:
                # Real encoded tracks, with the coarse container duration that
                # some ffprobe/muxer versions report. Not a fabricated frame count.
                data["format"]["duration"] = "4.459"
            return data
        with mock.patch.object(media, "probe", side_effect=rounded):
            info = media.inspect(source, "video")
            self.assertEqual(info["container_duration"], 4.459)
            self.assertEqual(info["source_duration"], 107/24)
            self.assertLess(abs(info["duration_rounding_correction_seconds"]), .001)
            output, metadata = media.normalize(source, info, self.root)
        self.assertEqual(metadata["frame_count"], 107)
        self.assertEqual(int(next(s for s in probe(output)["streams"] if s["codec_type"] == "video")["nb_frames"]), 107)
        self.assertEqual(source.read_bytes(), original)

    def test_true_extra_frame_is_not_disguised_as_container_rounding(self):
        from studio_platform.execution_policy import input_envelope_blockers
        from studio_platform.qualification_profiles import MULTIMODAL_INPUT_LIMITS
        source = self.native_reference(108)
        info = media.inspect(source, "video")
        self.assertNotIn("duration_basis", info)
        _, metadata = media.normalize(source, info, self.root)
        self.assertEqual(metadata["frame_count"], 124)
        compiled = {"recipe_id": "h3-base-ref2va-v1", "assets": {"clip": {"metadata": metadata}},
            "request": {"inputs": {"images": [], "videos": ["clip"], "audios": []}, "guides": [], "video_audio": {"clip": True}}}
        self.assertTrue(any("总时长" in message for message in input_envelope_blockers(compiled, MULTIMODAL_INPUT_LIMITS)))

    def multitrack(self):
        source = self.root / "multitrack.mp4"
        # Legal first video, deliberately out-of-contract second video with
        # default disposition. Audio comes first in container index order.
        self.ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=256x256:r=24:d=3",
            "-f", "lavfi", "-i", "color=c=blue:s=6000x256:r=24:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=32000:duration=3",
            "-map", "2:a:0", "-map", "0:v:0", "-map", "1:v:0", "-map", "3:a:0",
            "-disposition:v:0", "0", "-disposition:v:1", "default",
            "-disposition:a:0", "0", "-disposition:a:1", "default",
            "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", source])
        return source

    def assert_first_sound(self, path):
        data = self.ffmpeg(["-i", path, "-map", "0:a:0", "-t", "0.3", "-ac", "1", "-ar", "32000",
                           "-f", "s16le", "pipe:1"], capture=True)
        samples = array.array("h")
        samples.frombytes(data)
        middle = samples[1600:8000]
        crossings = sum(before <= 0 < after for before, after in zip(middle, middle[1:]))
        self.assertAlmostEqual(crossings / (len(middle)/32000), 440, delta=15)

    def test_normalize_maps_first_inspected_video_and_audio_not_default_tracks(self):
        source = self.multitrack()
        info = media.inspect(source, "video")
        self.assertEqual((info["video_stream_index"], info["audio_stream_index"]), (1, 0))
        self.assertTrue(any("第一个" in value for value in info["notes"]))
        normalized, result = media.normalize(source, info, self.root)
        actual = media.probe(normalized)
        videos = [stream for stream in actual["streams"] if stream["codec_type"] == "video"]
        audios = [stream for stream in actual["streams"] if stream["codec_type"] == "audio"]
        self.assertEqual(len(videos), 1)
        self.assertEqual(len(audios), 1)
        self.assertEqual((videos[0]["width"], videos[0]["height"]), (256, 256))
        self.assertEqual((result["width"], result["height"]), (256, 256))
        self.assert_first_sound(normalized)
        self.assertEqual(len(media.probe(source)["streams"]), 4)

    def test_derived_container_is_reinspected_for_its_new_stream_indices(self):
        source = self.multitrack()
        source_info = media.inspect(source, "video")
        trimmed = media.derive(source, source_info, self.root, .5, 2.5)
        derived_info = media.inspect(trimmed, "video")
        self.assertEqual((derived_info["video_stream_index"], derived_info["audio_stream_index"]), (0, 1))
        self.assertEqual((derived_info["width"], derived_info["height"]), (256, 256))
        self.assert_first_sound(trimmed)
        output = self.root / "derived-model"
        output.mkdir()
        normalized, _ = media.normalize(trimmed, derived_info, output)
        self.assert_first_sound(normalized)

    def test_old_metadata_without_indices_reinspects_original_for_normalize_and_derive(self):
        source = self.multitrack()
        old = media.inspect(source, "video")
        del old["video_stream_index"], old["audio_stream_index"]
        with mock.patch.object(media, "inspect", wraps=media.inspect) as inspect:
            normalized, _ = media.normalize(source, old, self.root)
            inspect.assert_any_call(source, "video")
        self.assert_first_sound(normalized)
        with mock.patch.object(media, "inspect", wraps=media.inspect) as inspect:
            trimmed = media.derive(source, old, self.root, 0, 2)
            inspect.assert_any_call(source, "video")
        self.assert_first_sound(trimmed)

    def test_attached_picture_is_not_the_selected_real_video(self):
        cover = self.root / "cover.png"
        Image.new("RGB", (1024, 1024), "blue").save(cover)
        source = self.root / "cover-and-video.mp4"
        self.ffmpeg(["-i", cover, "-f", "lavfi", "-i", "color=c=red:s=256x256:r=24:d=3",
            "-map", "0:v:0", "-map", "1:v:0", "-c:v:0", "copy", "-c:v:1", "libx264",
            "-threads", "1", "-preset", "ultrafast", "-disposition:v:0", "attached_pic",
            "-pix_fmt", "yuv420p", source])
        streams = media.probe(source)["streams"]
        attached = [s for s in streams if s.get("disposition", {}).get("attached_pic")]
        self.assertEqual(len(attached), 1)
        info = media.inspect(source, "video")
        self.assertNotEqual(info["video_stream_index"], attached[0]["index"])
        output, _ = media.normalize(source, info, self.root)
        video = next(s for s in media.probe(output)["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (256, 256))

    def test_output_dimension_mismatch_is_rejected_after_real_encoding(self):
        source = self.multitrack()
        info = media.inspect(source, "video")
        probe = media.probe
        def changed(path):
            result = probe(path)
            if Path(path).name == "normalized.mp4":
                result = copy.deepcopy(result)
                next(s for s in result["streams"] if s["codec_type"] == "video")["width"] = 512
            return result
        with mock.patch.object(media, "probe", side_effect=changed):
            with self.assertRaisesRegex(media.MediaError, "尺寸"):
                media.normalize(source, info, self.root)

    def test_malformed_stored_stream_selection_is_not_silently_guessed(self):
        source = self.multitrack()
        info = media.inspect(source, "video")
        info["video_stream_index"] = True
        with mock.patch.object(media, "ffmpeg", side_effect=AssertionError("decoder must not start")):
            with self.assertRaises(media.MediaError):
                media.normalize(source, info, self.root)

    def rotated_video(self, degrees):
        coded = self.root / "phone-coded.mp4"
        source = self.root / ("phone-"+str(degrees)+".mp4")
        self.ffmpeg(["-f", "lavfi", "-i",
            "color=c=red:s=512x256:r=24:d=3,drawbox=x=256:y=0:w=256:h=256:color=blue:t=fill",
            "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p", coded])
        # Build a genuine ISO BMFF tkhd display matrix. FFmpeg 5.1 and 7.1
        # differ on whether `-metadata rotate` writes it, so do not let that
        # version-dependent fixture silently become an unrotated input.
        data = bytearray(coded.read_bytes())
        def box(kind, start=0, end=None):
            end = len(data) if end is None else end
            while start+8 <= end:
                size, current = struct.unpack_from(">I4s", data, start)
                self.assertGreaterEqual(size, 8)
                self.assertLessEqual(start+size, end)
                if current == kind:
                    return start, start+size
                start += size
            self.fail("synthetic MP4 box not found")
        moov, moov_end = box(b"moov")
        trak, trak_end = box(b"trak", moov+8, moov_end)
        tkhd, _ = box(b"tkhd", trak+8, trak_end)
        matrix_offset = tkhd + (48 if data[tkhd+8] == 0 else 60)
        angle = math.radians(degrees)
        cosine, sine = round(math.cos(angle)*65536), round(math.sin(angle)*65536)
        struct.pack_into(">9i", data, matrix_offset, cosine, -sine, 0, sine, cosine, 0, 0, 0, 1 << 30)
        source.write_bytes(data)
        return source

    def signature(self, path):
        return self.ffmpeg(["-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=2:2:flags=neighbor",
                           "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], capture=True)

    def test_real_phone_display_matrix_positive_and_negative_quarter_turn(self):
        for degrees in (90, -90):
            with self.subTest(degrees=degrees):
                source = self.rotated_video(degrees)
                info = media.inspect(source, "video")
                self.assertEqual(info["source_rotation_degrees"], degrees % 360)
                self.assertEqual((info["source_coded_width"], info["source_coded_height"]), (512, 256))
                self.assertEqual((info["width"], info["height"]), (256, 512))
                expected = self.signature(source)
                # A displayed portrait has two uniformly coloured rows, not
                # the coded landscape's red/blue columns.
                self.assertEqual(expected[:3], expected[3:6])
                self.assertEqual(expected[6:9], expected[9:12])
                self.assertNotEqual(expected[:3], expected[6:9])
                directory = self.root / ("rotation-"+str(degrees))
                directory.mkdir()
                output, normalized = media.normalize(source, info, directory)
                actual = media.inspect(output, "video")
                self.assertEqual((actual["width"], actual["height"]), (256, 512))
                self.assertEqual(actual["source_rotation_degrees"], 0)
                self.assertEqual((normalized["width"], normalized["height"]), (256, 512))
                self.assertLessEqual(max(abs(a-b) for a, b in zip(expected, self.signature(output))), 5)
                trimmed = media.derive(source, info, directory, .5, 2.5)
                derived = media.inspect(trimmed, "video")
                self.assertEqual(derived["source_rotation_degrees"], 0)
                self.assertEqual((derived["width"], derived["height"]), (256, 512))
                renormalized, _ = media.normalize(trimmed, derived, directory)
                self.assertLessEqual(max(abs(a-b) for a, b in zip(expected, self.signature(renormalized))), 5)
                self.assertEqual(media.inspect(source, "video")["source_rotation_degrees"], degrees % 360)

    def test_legacy_video_selection_without_rotation_reinspects_original(self):
        source = self.rotated_video(90)
        old = media.inspect(source, "video")
        del old["source_rotation_degrees"]
        old.update(width=512, height=256)
        with mock.patch.object(media, "inspect", wraps=media.inspect) as inspect:
            output, result = media.normalize(source, old, self.root)
            inspect.assert_any_call(source, "video")
        self.assertEqual((result["width"], result["height"]), (256, 512))
        self.assertEqual((media.inspect(output, "video")["width"], media.inspect(output, "video")["height"]), (256, 512))

    def test_arbitrary_display_rotation_is_rejected_without_becoming_zero(self):
        source = self.rotated_video(45)
        with self.assertRaisesRegex(media.MediaError, "旋转"):
            media.inspect(source, "video")


class NativeTimingBoundaryTests(unittest.TestCase):
    def test_only_proven_native_grid_with_at_most_one_millisecond_container_error_is_corrected(self):
        video = {"nb_frames": "107", "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "duration": "4.458333"}
        self.assertIsNotNone(media._native_video_timing(video, None, 107/24+.001))
        self.assertIsNone(media._native_video_timing(video, None, 107/24+.001002))
        for key, value in (("nb_frames", "108"), ("avg_frame_rate", "25/1"), ("r_frame_rate", "25/1"),
                ("duration", "4.46"), ("duration", "N/A")):
            with self.subTest(key=key, value=value):
                self.assertIsNone(media._native_video_timing({**video, key: value}, None, 4.459))
        for audio in ({"duration": str(107/24+.0001)}, {"duration": "4.48"}, {"duration": "N/A"}, {}):
            with self.subTest(audio=audio):
                self.assertIsNone(media._native_video_timing(video, audio, 4.459))
        self.assertIsNotNone(media._native_video_timing(video, {"duration": "4.448"}, 4.459))


if __name__ == "__main__":
    unittest.main()

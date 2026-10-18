import copy
import io
import json
from pathlib import Path
import unittest

from comfy_workflow import native_output_spec
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_compiler import (
    H3FL2VACompiler, compile_settings, control_schema, normalize_request)
from studio_platform.inference.wangp_contract import EngineManifest


class CompilerTests(unittest.TestCase):
    def request(self):
        return {"model": "MiniMax-H3-Base-BF16", "mode": "fl", "prompt": "Line one\nLine two",
                "duration": 5, "resolution": "480P", "aspect_ratio": "16:9", "seed": "4294967295"}

    def test_explicit_full_precision_50_step_mapping_and_native_frames(self):
        request = self.request()
        spec = native_output_spec(request)
        result = compile_settings(request, {}, spec, {})
        self.assertEqual(result["config"], "bf16,bf16")
        self.assertEqual(result["num_inference_steps"], 50)
        self.assertEqual((result["video_length"], result["force_fps"]), (124, "24"))
        self.assertEqual(result["prompt"], request["prompt"])
        self.assertEqual(result["multi_prompts_gen_type"], "FG")
        self.assertEqual(result["seed"], 2**32 - 1)
        self.assertEqual(result["repeat_generation"], 1)
        self.assertEqual(result["image_prompt_type"], "T")
        self.assertEqual(result["custom_settings"]["audio_refinement"], "none")
        self.assertEqual(normalize_request(request, {}, spec)["backend"], "wangp-local")
        self.assertNotIn("steps", request)

    def test_rejects_unimplemented_or_different_recipe_without_mutation(self):
        for changes in ({"steps": 20}, {"video_decode": "normal"}, {"video_tile_size": 512},
                        {"video_temporal_size": 64}, {"encoder_device": "cpu"},
                        {"generate_audio": False}, {"scheduler": "karras"}, {"sampler_name": "res_multistep"},
                        {"shift_audio": 4}, {"shift_video": 10}, {"guidance_scale": 2},
                        {"mode": "ref"}, {"denoise": .5}, {"guides": []}, {"export_crf": 22}):
            with self.subTest(changes=changes):
                request = {**self.request(), **changes}
                before = copy.deepcopy(request)
                with self.assertRaises(ValueError):
                    compile_settings(request, {}, native_output_spec(request), {})
                self.assertEqual(request, before)

    def test_image_roles_all_supported_combinations(self):
        for first, last, expected in (("a", None, "S"), (None, "b", "TE"), ("a", "b", "SE")):
            with self.subTest(first=first, last=last):
                request = {**self.request(), "inputs": {"first_frame": first, "last_frame": last}}
                ids = [key for key in (first, last) if key]
                metadata = {key: {"kind": "image"} for key in ids}
                handles = {key: "opaque-" + key for key in ids}
                settings = compile_settings(request, metadata, native_output_spec(request), handles)
                self.assertEqual(settings["image_prompt_type"], expected)
                self.assertEqual(settings["image_start"], handles.get(first))
                self.assertEqual(settings["image_end"], handles.get(last))

    def test_schema_uses_existing_public_convention(self):
        schema = control_schema()
        self.assertEqual(schema["steps"]["enum"], [50])
        self.assertEqual(schema["seed"]["default"], None)
        self.assertEqual(schema["seed"]["maximum_decimal"], "4294967295")
        self.assertEqual(schema["width"]["multipleOf"], 32)
        self.assertNotIn("video_temporal_size", schema)

    def test_snapshot_and_identity_fail_closed_before_staging(self):
        manifest = EngineManifest.from_dict(json.loads((Path(__file__).resolve().parents[1] / "deploy/wangp/manifest.json").read_text()))
        request = {**self.request(), "inputs": {"first_frame": "a"}}
        request = normalize_request(request, {"a": {"kind": "image"}}, native_output_spec(request))
        key = "owners/owner/assets/a/file.png"
        model = {"key": key, "sha256": "a" * 64, "size_bytes": 4}
        job = {"id": "job-1", "owner_id": "owner", "request_hash": "b" * 64,
               "execution_plan": {"engine_manifest_digest": manifest.digest},
               "request": {"request": request, "output_spec": native_output_spec(request),
                           "assets": {"a": {"model": model, "metadata": {"kind": "image"}}}}}
        uploads, beats = [], []
        def stage(item, source, *, heartbeat):
            heartbeat()
            uploads.append((item, source.read()))
            return item
        store = type("Store", (), {"open": lambda self, key: io.BytesIO(b"data")})()
        compiler = H3FL2VACompiler(manifest, stage)
        prepared = compiler(job, "attempt-1", store, lambda: beats.append(1))
        self.assertEqual(prepared.request_hash, job["request_hash"])
        self.assertEqual(len(beats), 2)
        self.assertEqual(uploads[0][1], b"data")
        self.assertEqual(prepared.settings["image_start"], prepared.inputs[0].handle)
        self.assertNotIn("owners/", prepared.settings_json)
        job["request"]["assets"]["a"]["model"]["key"] = "owners/other/assets/a/file.png"
        with self.assertRaisesRegex(BackendError, "owner_mismatch"):
            compiler(job, "attempt-1", store, lambda: None)
        self.assertEqual(len(uploads), 1)

    def test_output_grid_and_asset_metadata_cannot_drift(self):
        request = self.request()
        with self.assertRaisesRegex(ValueError, "output_spec"):
            compile_settings(request, {}, {**native_output_spec(request), "frames": 120}, {})
        request["inputs"] = {"first_frame": "a"}
        with self.assertRaisesRegex(ValueError, "must_be_image"):
            compile_settings(request, {"a": {"kind": "audio"}}, native_output_spec(request), {"a": "handle"})


if __name__ == "__main__":
    unittest.main()

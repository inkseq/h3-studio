"""Offline H3 control contract tests; no Comfy import, GPU, files or network calls.

The saved deployed node schema is used to check emitted native parameter and
link types, including guide chaining, sigma patches and tiled AV decode.
"""

import copy
import json
from pathlib import Path, PurePosixPath
import unittest

from comfy_workflow import (
    SAMPLER_NAMES,
    SCHEDULER_NAMES,
    REQUIRED_NODE_TYPES,
    build_workflow,
    controls_metadata,
    native_output_spec,
    validate_controls,
)
from test_comfy_workflow import fixture, find


class ControlTests(unittest.TestCase):
    def test_preset_canvas_scaling_and_old_768_dimensions(self):
        expected = {
            "21:9": (1536, 672),
            "16:9": (1344, 768),
            "4:3": (1024, 768),
            "1:1": (768, 768),
            "3:4": (768, 1024),
            "9:16": (768, 1344),
        }
        for aspect, size in expected.items():
            with self.subTest(aspect=aspect):
                spec = native_output_spec({"aspect_ratio": aspect})
                self.assertEqual((spec["width"], spec["height"]), size)
        for resolution, width, height in (
            ("480P", 832, 480),
            ("576P", 1024, 576),
            ("768P", 1344, 768),
        ):
            spec = native_output_spec(
                {"resolution": resolution, "aspect_ratio": "16:9"}
            )
            self.assertEqual((spec["width"], spec["height"]), (width, height))
            self.assertEqual(spec["frames"], 124)

    def test_actual_custom_dimensions_not_post_resize(self):
        request, uploads, filenames = fixture()
        request.update(resolution="custom", width=896, height=576)
        condition = find(
            build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
        )[0][1]
        self.assertEqual((condition["width"], condition["height"]), (896, 576))
        for width, height in (
            (128, 256),
            (257, 256),
            (1536, 1536),
            (256, 1536),
            (1568, 576),
        ):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                native_output_spec(
                    {"resolution": "custom", "width": width, "height": height}
                )
        for value in ("2K", "720P", False):
            with self.subTest(resolution=value), self.assertRaises(ValueError):
                native_output_spec({"resolution": value})

    def test_four_seconds_and_native_frame_snap(self):
        self.assertEqual(native_output_spec({"duration": 4})["frames"], 107)
        self.assertAlmostEqual(
            native_output_spec({"duration": 15})["actual_duration"], 362 / 24
        )

    def test_all_sampler_and_scheduler_options_reach_graph(self):
        request, uploads, filenames = fixture()
        for sampler in SAMPLER_NAMES:
            request["sampler_name"] = sampler
            graph = build_workflow(request, uploads, filenames)
            self.assertEqual(
                find(graph, "KSamplerSelect")[0][1]["sampler_name"], sampler
            )
        for scheduler in SCHEDULER_NAMES:
            request["scheduler"] = scheduler
            graph = build_workflow(request, uploads, filenames)
            self.assertEqual(
                find(graph, "BasicScheduler")[0][1]["scheduler"], scheduler
            )

    def test_controls_normalize_without_mutation_and_max_uint64(self):
        request, uploads, filenames = fixture()
        request.update(seed="18446744073709551615", steps=100, denoise=0.01)
        before = copy.deepcopy((request, uploads, filenames))
        controls = validate_controls(request, uploads, native_output_spec(request))
        graph = build_workflow(request, uploads, filenames)
        self.assertEqual(controls["seed"], 2**64 - 1)
        self.assertEqual(find(graph, "RandomNoise")[0][1]["noise_seed"], 2**64 - 1)
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["steps"], 100)
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["denoise"], 0.01)
        self.assertEqual((request, uploads, filenames), before)
        for value in ("18446744073709551616", "-1", "1.5", "1e8", "", None, True):
            request["seed"] = value
            with self.subTest(seed=value), self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)

    def test_video_soundtrack_toggle_renumbers_actual_conditioning(self):
        request, uploads, filenames = fixture()
        request["video_audio"] = {"v1": False}
        graph = build_workflow(request, uploads, filenames)
        condition = find(graph, "MiniMaxH3ReferenceToVideo")[0][1]
        self.assertNotIn("ref_video_audios.ref_video_audio_0", condition)
        self.assertIn("<Audio 1>", condition["prompt"])
        self.assertNotIn("<Audio 2>", condition["prompt"])
        request["prompt"] = "采用音频1和<Audio 1>。"
        self.assertIn(
            "<Audio 1>",
            find(
                build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
            )[0][1]["prompt"],
        )
        request["prompt"] = "Use <Audio 2>"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            build_workflow(request, uploads, filenames)
        request["prompt"] = "A scene"
        for mapping in ({"missing": True}, {"v1": 1}, []):
            request["video_audio"] = mapping
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)

    def test_default_sigma_graph_preserved_and_both_consumers_patch(self):
        request, uploads, filenames = fixture()
        before = build_workflow(request, uploads, filenames)
        request.update(shift_video=12, shift_audio=3)
        self.assertEqual(build_workflow(request, uploads, filenames), before)
        request.update(shift_video=10, shift_audio=None)
        graph = build_workflow(request, uploads, filenames)
        patch_id, patch = find(graph, "MiniMaxH3SigmaShift")[0]
        self.assertEqual((patch["shift_video"], patch["shift_audio"]), (10, 3))
        self.assertEqual(find(graph, "BasicGuider")[0][1]["model"], [patch_id, 0])
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["model"], [patch_id, 0])

    def test_ref_precision_encoder_and_tiled_decoders_reach_real_nodes(self):
        request, uploads, filenames = fixture()
        request.update(
            ref_image_size="match",
            encoder_device="cpu",
            video_decode="tiled",
            audio_decode="normal",
            video_tile_size=256,
            video_overlap=32,
            video_temporal_size=32,
            video_temporal_overlap=4,
            audio_tile_size=1024,
            audio_overlap=128,
        )
        graph = build_workflow(request, uploads, filenames)
        self.assertEqual(
            find(graph, "MiniMaxH3ReferenceToVideo")[0][1]["ref_image_size"], "match"
        )
        self.assertEqual(find(graph, "CLIPLoader")[0][1]["device"], "cpu")
        video = find(graph, "VAEDecodeTiled")[0][1]
        audio = find(graph, "VAEDecodeAudio")[0][1]
        self.assertEqual(
            (
                video["tile_size"],
                video["overlap"],
                video["temporal_size"],
                video["temporal_overlap"],
            ),
            (256, 32, 32, 4),
        )
        self.assertEqual(video["samples"], audio["samples"])
        self.assertFalse(find(graph, "VAEDecode"))
        self.assertFalse(find(graph, "VAEDecodeAudioTiled"))

    def test_audio_tiled_is_rejected_before_gpu_for_confirmed_h3_incompatibility(self):
        request, uploads, filenames = fixture()
        request["audio_decode"] = "tiled"
        with self.assertRaisesRegex(ValueError, "VAEDecodeAudioTiled.*不兼容"):
            build_workflow(request, uploads, filenames)
        metadata = controls_metadata()
        self.assertEqual(metadata["audio_decoder_modes"], ["normal"])
        self.assertIn("audio_tiled", metadata["unsupported"])

    def guide_fixture(self):
        request, uploads, filenames = fixture()
        request.update(duration=15, mode="fl", inputs={}, prompt="A woman waves.")
        uploads["gv"] = {
            "kind": "video",
            "source_duration": 3,
            "duration": 73 / 24,
            "fps": 24,
            "frame_count": 73,
            "has_audio": True,
        }
        filenames["gv"] = "h3-studio/guide.mp4"
        request["guides"] = [
            {"media_id": "i1", "time_seconds": 2.5},
            {"media_id": "gv", "time_seconds": 5, "use_audio": True},
            {"media_id": "a1", "time_seconds": 9},
        ]
        return request, uploads, filenames

    def test_image_video_audio_guides_chain_native_condition_not_start_latent(self):
        graph = build_workflow(*self.guide_fixture())
        base_id = find(graph, "MiniMaxH3ImageToVideo")[0][0]
        guides = find(graph, "MiniMaxH3AddGuide")
        self.assertEqual([node[1]["frame_idx"] for node in guides], [60, 120, 216])
        self.assertEqual(guides[0][1]["positive"], [base_id, 0])
        self.assertEqual(guides[1][1]["positive"], [guides[0][0], 0])
        self.assertEqual(guides[2][1]["positive"], [guides[1][0], 0])
        self.assertTrue(all(node[1]["latent"] == [base_id, 1] for node in guides))
        self.assertIn("image", guides[0][1])
        self.assertNotIn("audio", guides[0][1])
        self.assertIn("image", guides[1][1])
        self.assertIn("audio", guides[1][1])
        self.assertIn("audio_vae", guides[1][1])
        self.assertIn("audio", guides[2][1])
        self.assertNotIn("image", guides[2][1])
        self.assertEqual(
            find(graph, "BasicGuider")[0][1]["conditioning"], [guides[2][0], 0]
        )
        self.assertEqual(
            find(graph, "SamplerCustomAdvanced")[0][1]["latent_image"], [base_id, 1]
        )

    def test_guides_preserve_existing_refs_and_deliberate_asset_reuse(self):
        request, uploads, filenames = fixture()
        request["guides"] = [{"media_id": "i1", "time_seconds": 2}]
        graph = build_workflow(request, uploads, filenames)
        self.assertTrue(find(graph, "MiniMaxH3ReferenceToVideo"))
        self.assertTrue(find(graph, "MiniMaxH3AddGuide"))

    def test_guide_boundaries_reject_silent_native_or_export_tail_crops(self):
        request, uploads, filenames = self.guide_fixture()
        cases = [
            [{"media_id": "missing", "time_seconds": 1}],
            [{"media_id": "i1", "time_seconds": -1}],
            [{"media_id": "i1", "time_seconds": 15}],
            [{"media_id": "i1", "time_seconds": 14.999}],
            [{"media_id": "i1", "time_seconds": float("nan")}],
            [{"media_id": "i1", "use_audio": True}],
            [{"media_id": "gv", "time_seconds": 12}],
            [{"media_id": "a1", "time_seconds": 10.1}],
            [{"media_id": "i1", "time_seconds": 1, "strength": 0.5}],
            [{"media_id": "i1"}] * 9,
        ]
        for guides in cases:
            request["guides"] = guides
            with self.subTest(guides=guides), self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)
        request["guides"] = [{"media_id": "gv", "time_seconds": 1, "use_audio": True}]
        uploads["gv"]["has_audio"] = False
        with self.assertRaisesRegex(ValueError, "no soundtrack"):
            build_workflow(request, uploads, filenames)

    def test_invalid_engine_values_and_unknown_fake_controls_are_rejected(self):
        for key, value in (
            ("sampler_name", "not-real"),
            ("scheduler", "bad"),
            ("ref_image_size", "best"),
            ("denoise", 0),
            ("denoise", float("inf")),
            ("shift_video", 0),
            ("shift_audio", 101),
            ("steps", 101),
            ("steps", True),
            ("encoder_device", "cuda"),
            ("video_decode", "low"),
            ("audio_decode", "high"),
            ("video_tile_size", 65),
            ("audio_tile_size", 33),
            ("video_overlap", 512),
            ("audio_overlap", 512),
            ("video_temporal_overlap", 64),
            ("video_temporal_size", 9),
            ("export_crf", 52),
            ("negative_prompt", "ugly"),
            ("fps", 60),
            ("cfg", 8),
            ("reference_strength", 1),
        ):
            request, uploads, filenames = fixture()
            request[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)

    def test_metadata_enums_match_actual_deployed_engine(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "comfy-object-info.json").read_text(
                encoding="utf-8"
            )
        )
        metadata = controls_metadata()
        self.assertEqual(
            metadata["samplers"],
            schema["KSamplerSelect"]["input"]["required"]["sampler_name"][1]["options"],
        )
        self.assertEqual(
            metadata["schedulers"][1:],
            schema["BasicScheduler"]["input"]["required"]["scheduler"][1]["options"],
        )
        self.assertTrue(REQUIRED_NODE_TYPES <= set(schema))
        self.assertEqual(metadata["native_fps"], 24)
        self.assertIn("official-regenerate-2k", metadata["unavailable"])
        self.assertNotIn("cfg", metadata["ranges"])

    def test_high_quality_crf_reaches_raw_encoding_and_dynamic_schema(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "comfy-object-info.json").read_text(
                encoding="utf-8"
            )
        )
        request, uploads, filenames = fixture()
        original = build_workflow(request, uploads, filenames)
        request["export_crf"] = 18
        self.assertEqual(build_workflow(request, uploads, filenames), original)
        request["export_crf"] = 23
        self.assertEqual(build_workflow(request, uploads, filenames), original)
        for crf in (0, 7, 17):
            with self.subTest(crf=crf):
                request["export_crf"] = crf
                save = find(build_workflow(request, uploads, filenames), "SaveVideo")[
                    0
                ][1]
                self.assertEqual(save["format.codec.encoding"], "re-encode")
                self.assertEqual(save["format.codec.encoding.crf"], crf)
                # Resolve actual dynamic schema choices, rather than treating
                # dotted descendants as top-level SaveVideo fields.
                definition = schema["SaveVideo"]["input"]["required"]["format"]
                prefix = "format"
                for key in ("mp4", "h264", "re-encode"):
                    self.assertEqual(definition[0], "COMFY_DYNAMICCOMBO_V3")
                    branch = next(
                        item for item in definition[1]["options"] if item["key"] == key
                    )
                    self.assertEqual(save[prefix], key)
                    children = {
                        **branch["inputs"].get("required", {}),
                        **branch["inputs"].get("optional", {}),
                    }
                    child_name = (
                        "codec"
                        if key == "mp4"
                        else "encoding"
                        if key == "h264"
                        else "crf"
                    )
                    definition = children[child_name]
                    prefix += "." + child_name
                self.assertEqual(prefix, "format.codec.encoding.crf")
                self.assertEqual(definition[0], "FLOAT")
                self.assertGreaterEqual(save[prefix], definition[1]["min"])
                self.assertLessEqual(save[prefix], definition[1]["max"])

    def test_all_new_native_links_and_fields_match_actual_schema(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "comfy-object-info.json").read_text(
                encoding="utf-8"
            )
        )
        request, uploads, filenames = self.guide_fixture()
        request.update(
            shift_video=8,
            shift_audio=4,
            encoder_device="cpu",
            video_decode="tiled",
            audio_decode="normal",
        )
        graph = build_workflow(request, uploads, filenames)
        # These new nodes have no autogrow/dynamic child inputs, so validate
        # their exact input fields and source port types straight from schema.
        new_nodes = {
            "MiniMaxH3AddGuide",
            "MiniMaxH3SigmaShift",
            "VAEDecodeTiled",
            "VAEDecodeAudioTiled",
        }
        for node_id, node in graph.items():
            if node["class_type"] not in new_nodes:
                continue
            info = schema[node["class_type"]]
            fields = {
                **info["input"].get("required", {}),
                **info["input"].get("optional", {}),
            }
            self.assertFalse(set(node["inputs"]) - fields.keys())
            self.assertFalse(
                set(info["input"].get("required", {})) - node["inputs"].keys()
            )
            for name, value in node["inputs"].items():
                definition = fields[name]
                kind = definition[0]
                constraints = definition[1] if len(definition) > 1 else {}
                if isinstance(value, list):
                    source, port = value
                    self.assertLess(int(source), int(node_id))
                    self.assertEqual(
                        schema[graph[source]["class_type"]]["output"][port], kind
                    )
                elif kind in ("INT", "FLOAT"):
                    self.assertIsInstance(value, int if kind == "INT" else (int, float))
                    self.assertGreaterEqual(
                        value, constraints.get("min", -float("inf"))
                    )
                    self.assertLessEqual(value, constraints.get("max", float("inf")))

    def test_expert_visual_workflow_has_deployed_weights_and_coherent_links(self):
        root = Path(__file__).resolve().parents[1]
        workflow = json.loads(
            (root / "tests/fixtures/h3-ref-bf16-workflow.json").read_text(
                encoding="utf-8"
            )
        )
        schema = json.loads(
            (root / "comfy-object-info.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (root / "model_manifest.json").read_text(encoding="utf-8")
        )
        nodes = {node["id"]: node for node in workflow["nodes"]}
        model_names = {Path(item["path"]).name for item in manifest["files"]}
        self.assertTrue(any(node["type"] == "LoadImage" for node in nodes.values()))
        self.assertFalse(
            any(
                "Lora" in node["type"] or node["type"] == "ComfySwitchNode"
                for node in nodes.values()
            )
        )
        for node in nodes.values():
            self.assertTrue(node["type"] in schema or node["type"] == "MarkdownNote")
            named = node.get("widgets_values_named", {})
            for key in ("unet_name", "clip_name", "vae_name"):
                if key in named:
                    self.assertIn(named[key], model_names)
                    self.assertNotRegex(named[key], r"int8|nvfp4|turbo")
            if node["type"] == "LoadImage":
                # CI checks the template's portable reference syntax. Whether a
                # reference exists on the GPU belongs to live acceptance, not a
                # private historical job or this offline test.
                image_name = named.get("image")
                self.assertIsInstance(image_name, str)
                self.assertRegex(
                    image_name,
                    r"\A[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*\Z",
                )
                self.assertFalse(PurePosixPath(image_name).is_absolute())
                self.assertTrue(
                    all(part not in ("", ".", "..") for part in image_name.split("/"))
                )
                self.assertIn(
                    PurePosixPath(image_name).suffix.lower(),
                    (".png", ".jpg", ".jpeg", ".webp"),
                )
        for link_id, source, port, target, slot, kind in workflow["links"]:
            self.assertEqual(nodes[target]["inputs"][slot]["link"], link_id)
            self.assertIn(link_id, nodes[source]["outputs"][port]["links"])
            self.assertIn(kind, nodes[source]["outputs"][port]["type"].split(","))
            self.assertIn(kind, nodes[target]["inputs"][slot]["type"].split(","))
        model = next(node for node in nodes.values() if node["type"] == "UNETLoader")
        guider = next(node for node in nodes.values() if node["type"] == "BasicGuider")
        model_input = next(
            item["link"] for item in guider["inputs"] if item["name"] == "model"
        )
        model_link = next(link for link in workflow["links"] if link[0] == model_input)
        self.assertEqual(model_link[1], model["id"])


if __name__ == "__main__":
    unittest.main()

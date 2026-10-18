"""Offline structural and boundary tests for native H3 graphs (stdlib only)."""

import copy
import json
from pathlib import Path
import unittest

from comfy_workflow import (
    AUDIO_VAE,
    CLIP_NAME,
    DEFAULT_STEPS,
    DIFFUSION_FL,
    DIFFUSION_REF,
    VIDEO_VAE,
    build_workflow,
    native_output_spec,
)


def fixture():
    request = {
        "mode": "ref",
        "prompt": "@image1 is the character; use @video1 for motion and @audio1 for voice.",
        "duration": 5,
        "resolution": "768P",
        "aspect_ratio": "16:9",
        "generate_audio": True,
        "seed": 42,
        "_job_id": "offline-test",
        "inputs": {"images": ["i1"], "videos": ["v1"], "audios": ["a1"]},
    }
    uploads = {
        "i1": {"kind": "image", "width": 1024, "height": 1024},
        "v1": {
            "kind": "video",
            "source_duration": 5,
            "duration": 124 / 24,
            "fps": 24,
            "frame_count": 124,
            "has_audio": True,
        },
        "a1": {"kind": "audio", "duration": 5},
    }
    filenames = {
        "i1": "h3-studio/i1.png",
        "v1": "h3-studio/v1.mp4",
        "a1": "h3-studio/a1.wav",
    }
    return request, uploads, filenames


def find(graph, class_type):
    return [
        (node_id, node["inputs"])
        for node_id, node in graph.items()
        if node["class_type"] == class_type
    ]


OUTPUT_TYPES = {
    "UNETLoader": ("MODEL",),
    "CLIPLoader": ("CLIP",),
    "VAELoader": ("VAE",),
    "LoadImage": ("IMAGE", "MASK"),
    "LoadVideo": ("VIDEO",),
    "GetVideoComponents": ("IMAGE", "AUDIO", "FLOAT", "COMBO", "COMBO"),
    "LoadAudio": ("AUDIO",),
    "MiniMaxH3ReferenceToVideo": ("CONDITIONING", "LATENT"),
    "MiniMaxH3ImageToVideo": ("CONDITIONING", "LATENT"),
    "RandomNoise": ("NOISE",),
    "BasicGuider": ("GUIDER",),
    "KSamplerSelect": ("SAMPLER",),
    "BasicScheduler": ("SIGMAS",),
    "SamplerCustomAdvanced": ("LATENT", "LATENT"),
    "VAEDecode": ("IMAGE",),
    "VAEDecodeAudio": ("AUDIO",),
    "CreateVideo": ("VIDEO",),
    "SaveVideo": ("VIDEO",),
    "SaveAudioAdvanced": ("AUDIO",),
}
INPUT_TYPES = {
    "MiniMaxH3ReferenceToVideo": {"clip": "CLIP", "vae": "VAE", "audio_vae": "VAE"},
    "MiniMaxH3ImageToVideo": {
        "clip": "CLIP",
        "vae": "VAE",
        "first_frame": "IMAGE",
        "last_frame": "IMAGE",
    },
    "GetVideoComponents": {"video": "VIDEO"},
    "BasicGuider": {"model": "MODEL", "conditioning": "CONDITIONING"},
    "BasicScheduler": {"model": "MODEL"},
    "SamplerCustomAdvanced": {
        "noise": "NOISE",
        "guider": "GUIDER",
        "sampler": "SAMPLER",
        "sigmas": "SIGMAS",
        "latent_image": "LATENT",
    },
    "VAEDecode": {"samples": "LATENT", "vae": "VAE"},
    "VAEDecodeAudio": {"samples": "LATENT", "vae": "VAE"},
    "CreateVideo": {"images": "IMAGE", "audio": "AUDIO"},
    "SaveVideo": {"video": "VIDEO"},
    "SaveAudioAdvanced": {"audio": "AUDIO"},
}


class NativeGraphTests(unittest.TestCase):
    def assert_typed_graph(self, graph):
        json.dumps(graph, allow_nan=False)
        for node_id, node in graph.items():
            self.assertIn(node["class_type"], OUTPUT_TYPES)
            for name, value in node["inputs"].items():
                if not isinstance(value, list):
                    continue
                self.assertEqual(len(value), 2)
                source, port = value
                self.assertIn(source, graph)
                self.assertLess(int(source), int(node_id), "Graph must be acyclic")
                output_type = OUTPUT_TYPES[graph[source]["class_type"]][port]
                if name.startswith(("ref_images.", "ref_videos.")):
                    expected = "IMAGE"
                elif name.startswith(("ref_video_audios.", "ref_audios.")):
                    expected = "AUDIO"
                else:
                    expected = INPUT_TYPES[node["class_type"]][name]
                self.assertEqual(expected, output_type, f"Wrong type for {name}")

    def test_real_reference_graph_muxes_both_sampled_streams(self):
        graph = build_workflow(*fixture())
        self.assert_typed_graph(graph)
        condition = find(graph, "MiniMaxH3ReferenceToVideo")[0][1]
        self.assertEqual(condition["length"], 124)
        self.assertEqual((condition["width"], condition["height"]), (1344, 768))
        self.assertIn("ref_images.ref_image_0", condition)
        self.assertIn("ref_videos.ref_video_0", condition)
        self.assertIn("ref_video_audios.ref_video_audio_0", condition)
        self.assertIn("ref_audios.ref_audio_0", condition)
        self.assertIn("<Picture 1>", condition["prompt"])
        self.assertIn("<Video 1>", condition["prompt"])
        self.assertIn("<Audio 2>", condition["prompt"])
        video_decode = find(graph, "VAEDecode")[0][1]
        audio_decode = find(graph, "VAEDecodeAudio")[0][1]
        self.assertEqual(video_decode["samples"], audio_decode["samples"])
        self.assertIn("audio", find(graph, "CreateVideo")[0][1])
        saved = find(graph, "SaveVideo")[0][1]
        self.assertEqual(saved["filename_prefix"], "h3-studio/offline-test")
        self.assertEqual(saved["format.codec"], "h264")
        self.assertEqual(find(graph, "SaveAudioAdvanced")[0][1]["format"], "flac")

    def test_full_weights_no_lora_and_official_default_steps(self):
        graph = build_workflow(*fixture())
        self.assertEqual(find(graph, "UNETLoader")[0][1]["unet_name"], DIFFUSION_REF)
        self.assertEqual(find(graph, "CLIPLoader")[0][1]["clip_name"], CLIP_NAME)
        self.assertEqual(
            {x[1]["vae_name"] for x in find(graph, "VAELoader")}, {VIDEO_VAE, AUDIO_VAE}
        )
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["steps"], DEFAULT_STEPS)
        self.assertEqual(DEFAULT_STEPS, 20)
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["scheduler"], "beta")
        self.assertEqual(
            find(graph, "KSamplerSelect")[0][1]["sampler_name"], "res_multistep"
        )
        self.assertEqual(find(graph, "RandomNoise")[0][1]["noise_seed"], 42)
        self.assertFalse(any("Lora" in x["class_type"] for x in graph.values()))

    def test_first_and_last_frame_and_text_only(self):
        request, uploads, filenames = fixture()
        request.update(mode="fl", prompt="Slow camera push with ambient sound.")
        request["inputs"] = {"first_frame": "i1", "last_frame": "i1"}
        graph = build_workflow(request, uploads, filenames)
        self.assert_typed_graph(graph)
        condition = find(graph, "MiniMaxH3ImageToVideo")[0][1]
        self.assertIn("first_frame", condition)
        self.assertIn("last_frame", condition)
        self.assertEqual(find(graph, "UNETLoader")[0][1]["unet_name"], DIFFUSION_FL)
        self.assertEqual(find(graph, "BasicScheduler")[0][1]["scheduler"], "simple")
        request["inputs"] = {}
        self.assert_typed_graph(build_workflow(request, uploads, filenames))

    def test_silent_export_retains_audio_conditioning(self):
        request, uploads, filenames = fixture()
        request["generate_audio"] = False
        graph = build_workflow(request, uploads, filenames)
        self.assert_typed_graph(graph)
        self.assertIn(
            "ref_audios.ref_audio_0", find(graph, "MiniMaxH3ReferenceToVideo")[0][1]
        )
        self.assertNotIn("audio", find(graph, "CreateVideo")[0][1])
        self.assertFalse(find(graph, "VAEDecodeAudio"))

    def test_reference_order_and_soundtrack_ordinals(self):
        request, uploads, filenames = fixture()
        uploads.update(
            i2={"kind": "image"},
            v2=dict(uploads["v1"], has_audio=False),
            a2={"kind": "audio", "duration": 5},
        )
        filenames.update(i2="i2.png", v2="v2.mp4", a2="a2.wav")
        request["inputs"] = {
            "images": ["i2", "i1"],
            "videos": ["v2", "v1"],
            "audios": ["a2", "a1"],
        }
        request["prompt"] = (
            "@image1, @image2, @video1, @video2, @audio1 and @audio2; retain <Audio 1>."
        )
        graph = build_workflow(request, uploads, filenames)
        condition = find(graph, "MiniMaxH3ReferenceToVideo")[0][1]
        loader_id = condition["ref_images.ref_image_0"][0]
        self.assertEqual(graph[loader_id]["inputs"]["image"], "i2.png")
        self.assertNotIn("ref_video_audios.ref_video_audio_0", condition)
        self.assertIn("ref_video_audios.ref_video_audio_1", condition)
        self.assertIn("<Audio 2> and <Audio 3>", condition["prompt"])
        self.assertIn("retain <Audio 1>", condition["prompt"])

    def test_video_and_audio_totals_are_separate(self):
        request, uploads, filenames = fixture()
        request["inputs"]["videos"] = ["v1"] * 3
        request["inputs"]["audios"] = ["a1"] * 3
        self.assert_typed_graph(build_workflow(request, uploads, filenames))

    def test_chinese_ui_labels_use_displayed_native_ordinals(self):
        request, uploads, filenames = fixture()
        request["prompt"] = (
            "保持图片 1人物身份，以视频1为动作、音频 2为声音，保留<Audio 1>。"
        )
        condition = find(
            build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
        )[0][1]
        self.assertIn("<Picture 1>", condition["prompt"])
        self.assertIn("<Video 1>", condition["prompt"])
        self.assertIn("<Audio 2>", condition["prompt"])
        self.assertIn("保留<Audio 1>", condition["prompt"])

    def test_english_aliases_can_be_followed_by_chinese(self):
        request, uploads, filenames = fixture()
        request["prompt"] = (
            "@image1的人物与@picture1的脸，按@video1运动，采用@audio1声音。"
        )
        condition = find(
            build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
        )[0][1]
        self.assertEqual(
            condition["prompt"],
            "integrated_multimodal_description: <Picture 1>的人物与<Picture 1>的脸，按<Video 1>运动，采用<Audio 2>声音。",
        )
        request["prompt"] = "Keep @image1abc, @video1_2 and @audio12A as literal text."
        condition = find(
            build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
        )[0][1]
        self.assertIn("@image1abc, @video1_2 and @audio12A", condition["prompt"])

    def test_unavailable_alias_numbers_before_chinese_are_rejected(self):
        for alias in (
            "@image0的人物",
            "@image2的人物",
            "@picture12的脸",
            "@video2运动",
            "@audio2声音",
        ):
            with self.subTest(alias=alias):
                request, uploads, filenames = fixture()
                request["prompt"] = alias
                with self.assertRaisesRegex(ValueError, "unavailable @"):
                    build_workflow(request, uploads, filenames)

    def test_maximum_image_and_mixed_counts(self):
        request, uploads, filenames = fixture()
        request["prompt"] = "Generate the scene."
        request["inputs"] = {"images": ["i1"] * 9, "audios": ["a1"] * 3}
        condition = find(
            build_workflow(request, uploads, filenames), "MiniMaxH3ReferenceToVideo"
        )[0][1]
        self.assertIn("ref_images.ref_image_8", condition)
        request["inputs"]["videos"] = ["v1"]
        with self.assertRaisesRegex(ValueError, "12 files"):
            build_workflow(request, uploads, filenames)

    def test_explicit_padded_15_second_video_is_accepted(self):
        request, uploads, filenames = fixture()
        request["duration"] = 15
        uploads["v1"].update(source_duration=15, duration=362 / 24, frame_count=362)
        self.assert_typed_graph(build_workflow(request, uploads, filenames))
        self.assertAlmostEqual(native_output_spec(request)["actual_duration"], 362 / 24)

    def test_native_time_and_canvas_are_explicit(self):
        self.assertEqual(native_output_spec({"duration": 5})["frames"], 124)
        self.assertAlmostEqual(
            native_output_spec({"duration": 5})["actual_duration"], 124 / 24
        )
        self.assertEqual(native_output_spec({"aspect_ratio": "9:16"})["width"], 768)
        self.assertEqual(native_output_spec({"aspect_ratio": "9:16"})["height"], 1344)

    def test_rejections_do_not_crop_or_guess(self):
        cases = [
            ("mode", "unsupported"),
            ("resolution", "2K"),
            ("duration", 16),
            ("duration", float("nan")),
            ("steps", 0),
            ("seed", -1),
            ("seed", True),
            ("generate_audio", "false"),
            ("prompt", "  "),
            ("prompt", "Use @image9"),
            ("_job_id", "../escape"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                request, uploads, filenames = fixture()
                request[key] = value
                with self.assertRaises(ValueError):
                    build_workflow(request, uploads, filenames)
        for patch in (
            {"fps": 30},
            {"frame_count": 120},
            {"has_audio": None},
            {"frame_count": 141, "duration": 141 / 24},
            {"source_duration": 16},
        ):
            with self.subTest(patch=patch):
                request, uploads, filenames = fixture()
                uploads["v1"].update(patch)
                with self.assertRaises(ValueError):
                    build_workflow(request, uploads, filenames)

    def test_mode_type_paths_and_mutation_boundaries(self):
        for kind in ("missing", "audio"):
            request, uploads, filenames = fixture()
            uploads["i1"]["kind"] = kind
            with self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)
        for filename in ("../x.png", "/x.png", "C:\\x.png", "x\n.png"):
            request, uploads, filenames = fixture()
            filenames["i1"] = filename
            with self.assertRaises(ValueError):
                build_workflow(request, uploads, filenames)
        request, uploads, filenames = fixture()
        request["inputs"]["first_frame"] = "i1"
        with self.assertRaisesRegex(ValueError, "first/last"):
            build_workflow(request, uploads, filenames)
        request["inputs"].pop("first_frame")
        before = copy.deepcopy((request, uploads, filenames))
        build_workflow(request, uploads, filenames)
        self.assertEqual((request, uploads, filenames), before)

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[1] / "comfy-object-info.json").is_file(),
        "Cloud schema snapshot is absent",
    )
    def test_graph_matches_actual_cloud_schema_including_dynamic_inputs(self):
        """Validate real flattened schema; never import ComfyUI or request a GPU run.

        Model and media filename enumerations are intentionally checked only
        for the existence of their input fields: this snapshot was captured
        before model downloads and upload files were available on the worker.
        """
        info = json.loads(
            (Path(__file__).resolve().parents[1] / "comfy-object-info.json").read_text(
                encoding="utf-8"
            )
        )
        deferred_files = {
            ("UNETLoader", "unet_name"),
            ("CLIPLoader", "clip_name"),
            ("VAELoader", "vae_name"),
            ("LoadImage", "image"),
            ("LoadVideo", "file"),
            ("LoadAudio", "audio"),
        }

        def expand(schema, actual, prefix=""):
            fields, required = {}, set()
            for category in ("required", "optional"):
                for name, definition in schema.get(category, {}).items():
                    full_name = prefix + name
                    kind = definition[0]
                    metadata = definition[1] if len(definition) > 1 else {}
                    if kind == "COMFY_AUTOGROW_V3":
                        template = metadata["template"]
                        names = template.get("names") or [
                            template["prefix"] + str(i) for i in range(template["max"])
                        ]
                        inner = next(
                            iter(
                                template["input"]
                                .get("required", template["input"].get("optional", {}))
                                .values()
                            )
                        )
                        for index, child in enumerate(names):
                            child_name = full_name + "." + child
                            fields[child_name] = inner
                            if index < template.get("min", 0):
                                required.add(child_name)
                        continue
                    fields[full_name] = definition
                    if category == "required":
                        required.add(full_name)
                    if kind == "COMFY_DYNAMICCOMBO_V3" and full_name in actual:
                        selected = next(
                            (
                                option
                                for option in metadata["options"]
                                if option["key"] == actual[full_name]
                            ),
                            None,
                        )
                        self.assertIsNotNone(
                            selected, f"Invalid dynamic combo {full_name}"
                        )
                        children, child_required = expand(
                            selected["inputs"], actual, full_name + "."
                        )
                        fields.update(children)
                        required.update(child_required)
            return fields, required

        request, uploads, filenames = fixture()
        variants = [(request, uploads, filenames)]
        silent = copy.deepcopy(request)
        silent["generate_audio"] = False
        variants.append((silent, uploads, filenames))
        fl = copy.deepcopy(request)
        fl.update(
            mode="fl",
            prompt="A quiet scene.",
            inputs={"first_frame": "i1", "last_frame": "i1"},
        )
        variants.append((fl, uploads, filenames))
        for request, uploads, filenames in variants:
            graph = build_workflow(request, uploads, filenames)
            for node in graph.values():
                class_type, actual = node["class_type"], node["inputs"]
                self.assertIn(class_type, info)
                fields, required = expand(info[class_type]["input"], actual)
                self.assertFalse(
                    set(actual) - set(fields),
                    f"Unknown {class_type} inputs: {set(actual) - set(fields)}",
                )
                self.assertFalse(
                    required - set(actual),
                    f"Missing {class_type} inputs: {required - set(actual)}",
                )
                for name, value in actual.items():
                    kind = fields[name][0]
                    metadata = fields[name][1] if len(fields[name]) > 1 else {}
                    if isinstance(value, list):
                        self.assertEqual(
                            info[graph[value[0]]["class_type"]]["output"][value[1]],
                            kind,
                            f"{class_type}.{name} link type differs from cloud",
                        )
                    elif (class_type, name) in deferred_files:
                        self.assertIsInstance(value, str)
                    elif isinstance(kind, list):
                        self.assertIn(value, kind)
                    elif kind == "COMBO":
                        self.assertIn(value, metadata["options"])
                    elif kind in ("INT", "FLOAT"):
                        self.assertIsInstance(
                            value, int if kind == "INT" else (int, float)
                        )
                        self.assertGreaterEqual(
                            value, metadata.get("min", -float("inf"))
                        )
                        self.assertLessEqual(value, metadata.get("max", float("inf")))
                    elif kind == "STRING":
                        self.assertIsInstance(value, str)


if __name__ == "__main__":
    unittest.main()

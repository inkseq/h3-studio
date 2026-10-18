"""Offline REF qualification: real CPU media, owned immutable storage, fake Session."""

import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from comfy_workflow import native_output_spec
from studio_platform.assets import AssetService, AssetNotFound
from studio_platform.capabilities import compile_request
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_contract import (
    EngineManifest,
    InputDescriptor,
    canonical_json,
)
from studio_platform.inference.wangp_ref_compiler import (
    H3Ref2VACompiler,
    compile_settings,
    normalize_request,
)
from studio_platform.runtime_hosts.wangp_http import StagedInputs
from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs
from studio_platform.runtime_hosts.wangp_session import PinnedWanGPSession
from studio_platform.storage import LocalObjectStore
from test_platform_api import generation_request, png
from test_platform_repository import LedgerCase


def manifest(name="manifest-ref2va-candidate.json"):
    return EngineManifest.from_dict(
        json.loads(
            (Path(__file__).resolve().parents[1] / "deploy/wangp" / name).read_text()
        )
    )


def request():
    return {
        "model": "MiniMax-H3-Base-BF16",
        "mode": "ref",
        "prompt": "A red boat; steady camera.",
        "duration": 5,
        "resolution": "480P",
        "aspect_ratio": "16:9",
        "seed": "73",
        "inputs": {"images": ["i"], "videos": ["v"], "audios": ["a"]},
        "video_audio": {"v": False},
    }


def metadata():
    return {
        "i": {"kind": "image", "width": 512, "height": 512, "model_ready": True},
        "v": {
            "kind": "video",
            "width": 512,
            "height": 288,
            "model_ready": True,
            "fps": 24,
            "frame_count": 56,
            "duration": 56 / 24,
            "source_duration": 2,
            "has_audio": False,
        },
        "a": {
            "kind": "audio",
            "duration": 2,
            "sample_rate": 32000,
            "channels": 2,
            "model_ready": True,
        },
    }


class RefCompilerTests(unittest.TestCase):
    def test_exact_ref_roles_native_grid_and_base_controls_without_mutation(self):
        value, meta = request(), metadata()
        original = copy.deepcopy((value, meta))
        result = compile_settings(
            value, meta, native_output_spec(value), {k: "handle-" + k for k in meta}
        )
        self.assertEqual((value, meta), original)
        self.assertEqual(result["model_type"], "minimax_h3_ref2va")
        self.assertEqual(result["image_refs"], ["handle-i"])
        self.assertEqual(result["video_guide"], "handle-v")
        self.assertEqual(result["audio_guide"], "handle-a")
        self.assertEqual(
            (result["video_prompt_type"], result["audio_prompt_type"]), ("IV-U", "A")
        )
        self.assertEqual(
            (result["num_inference_steps"], result["config"], result["video_length"]),
            (50, "bf16,bf16", 124),
        )
        self.assertEqual(result["prompt"], value["prompt"])
        self.assertEqual(result["image_refs_relative_size"], 100)
        self.assertEqual(result["custom_settings"], {"audio_refinement": "none"})
        self.assertIsNone(result["image_start"])
        self.assertIsNone(result["video_guide2"])
        self.assertIsNone(result["audio_source"])

    def test_supported_first_probe_combinations_and_audio_requires_visual(self):
        for kinds, flags in (
            (["i"], ("I", "")),
            (["v"], ("V-U", "")),
            (["i", "a"], ("I", "A")),
            (["v", "a"], ("V-U", "A")),
        ):
            with self.subTest(kinds=kinds):
                value, meta = request(), metadata()
                value["inputs"] = {
                    plural: [key] if key in kinds else []
                    for plural, key in (
                        ("images", "i"),
                        ("videos", "v"),
                        ("audios", "a"),
                    )
                }
                value["video_audio"] = {"v": False} if "v" in kinds else {}
                meta = {k: meta[k] for k in kinds}
                result = compile_settings(
                    value, meta, native_output_spec(value), {k: "h-" + k for k in meta}
                )
                self.assertEqual(
                    (result["video_prompt_type"], result["audio_prompt_type"]), flags
                )
        value = request()
        value.update(inputs={"audios": ["a"]}, video_audio={})
        with self.assertRaisesRegex(ValueError, "requires_visual"):
            normalize_request(value, {"a": metadata()["a"]}, native_output_spec(value))

    def test_rejects_excess_controls_shapes_counts_soundtrack_and_unselected_media(
        self,
    ):
        modifications = [
            lambda r, m: r.update(steps=20),
            lambda r, m: r.update(encoder_device="cpu"),
            lambda r, m: r.update(guides=[]),
            lambda r, m: r.update(duration=6),
            lambda r, m: r.update(width=640),
            lambda r, m: r.update(height=640),
            lambda r, m: r.update(resolution="768P"),
            lambda r, m: r["inputs"].update(first_frame="i"),
            lambda r, m: r["inputs"]["images"].append("i"),
            lambda r, m: r["video_audio"].update(v=True),
            lambda r, m: m["v"].update(has_audio=True),
            lambda r, m: m["v"].update(source_duration=4),
            lambda r, m: m["v"].update(frame_count=90, duration=90 / 24),
            lambda r, m: m["a"].update(duration=4),
            lambda r, m: m["i"].update(width=1024),
            lambda r, m: m["i"].update(model_ready=False),
            lambda r, m: m["a"].update(duration=float("nan")),
            lambda r, m: m.update(extra=m["i"]),
        ]
        for change in modifications:
            value, meta = request(), metadata()
            change(value, meta)
            before = copy.deepcopy(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_request(value, meta, native_output_spec(value))
            self.assertEqual(value, before)

    def test_manifest_requires_exact_ref_weight_and_public_compile_preserves_recipe(
        self,
    ):
        ref = manifest()
        H3Ref2VACompiler(ref, None)
        with self.assertRaisesRegex(ValueError, "manifest_mismatch"):
            H3Ref2VACompiler(manifest("manifest.json"), None)
        changed = ref.document
        changed["components"]["transformer"]["files"][0]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "manifest_mismatch"):
            H3Ref2VACompiler(EngineManifest.from_dict(changed), None)
        body = generation_request(
            recipe_id="h3-base-ref2va-v1",
            inputs={"images": ["i"]},
            controls={"duration": 5, "resolution": "480P", "seed": "4294967295"},
        )
        compiled, fingerprint = compile_request(
            body,
            lambda _: {"asset_id": "i", "metadata": metadata()["i"], "model": {}},
            backend="wangp-worker",
        )
        self.assertEqual(compiled["recipe_id"], "h3-base-ref2va-v1")
        self.assertEqual(compiled["request"]["mode"], "ref")
        self.assertEqual(len(fingerprint), 64)

    def test_adapter_factory_selects_candidate_without_runtime_or_network(self):
        from studio_platform.inference.wangp_factory import create_backend

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ref = manifest()
            model = root / "manifest.json"
            model.write_text(ref.document_json)
            token = root / "token"
            token.write_text("synthetic-ref-test-token-" + "x" * 32)
            token.chmod(0o600)
            config = root / "runtime.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "enabled": True,
                        "slot_key": "ref-slot",
                        "configuration_id": "ref-config",
                        "manifest_file": str(model),
                        "token_file": str(token),
                        "runtime_incarnation": "a" * 32,
                    }
                )
            )
            config.chmod(0o600)
            slot = NS(
                runtime_config_file=str(config),
                endpoint="http://127.0.0.1:8199",
                spec=NS(
                    backend="wangp-worker",
                    model_id="MiniMax-H3-Base-BF16",
                    configuration_id="ref-config",
                    engine_manifest_digest=ref.digest,
                ),
            )
            with patch("httpx.Client", side_effect=AssertionError("no network")):
                backend = create_backend(slot, root)
            self.assertIsInstance(backend.compiler, H3Ref2VACompiler)
            self.assertIsNone(backend.transport._client)


class RefOwnedMediaTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.store = LocalObjectStore(self.root / "objects")
        self.assets = AssetService(
            self.repo.engine, self.store, self.root / "assets", tenant="test-tenant"
        )
        self.inputs = StagedInputs(self.root / "runtime-inputs")
        self.uploads = []

        def stage(item, source, *, heartbeat):
            self.inputs.save(item, source)
            self.uploads.append(item)
            return item

        self.compiler = H3Ref2VACompiler(manifest(), stage)

    def media(self, name, duration=2, *, sound=False):
        path = self.root / name
        args = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
        if path.suffix == ".wav":
            args += [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=32000",
                "-t",
                str(duration),
                "-ac",
                "2",
            ]
        else:
            args += ["-f", "lavfi", "-i", "color=red:size=512x288:rate=24"]
            if sound:
                args += [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:sample_rate=32000",
                    "-c:a",
                    "aac",
                    "-ac",
                    "2",
                ]
            args += ["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        subprocess.run(
            [*args, str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        return path

    def upload(self, path):
        with path.open("rb") as source:
            return self.assets.upload("superdan", "project-1", source, path.name)

    def job(self, assets):
        snapshots = {
            a["id"]: self.assets.model_snapshot("superdan", "project-1", a["id"])
            for a in assets
        }
        value = request()
        value["inputs"] = {
            plural: [
                key for key, s in snapshots.items() if s["metadata"]["kind"] == kind
            ]
            for plural, kind in (
                ("images", "image"),
                ("videos", "video"),
                ("audios", "audio"),
            )
        }
        value["video_audio"] = {key: False for key in value["inputs"]["videos"]}
        return {
            "id": "ref-job",
            "owner_id": "superdan",
            "request_hash": "a" * 64,
            "execution_plan": {"engine_manifest_digest": self.compiler.manifest.digest},
            "request": {
                "recipe_id": "h3-base-ref2va-v1",
                "request": value,
                "assets": snapshots,
                "output_spec": native_output_spec(value),
            },
        }

    def test_real_owned_three_kind_uploads_resolve_and_reach_correct_fake_session(self):
        image = self.assets.upload(
            "superdan", "project-1", io.BytesIO(png()), "boat.png"
        )
        video, audio = (
            self.upload(self.media("silent.mp4")),
            self.upload(self.media("sound.wav")),
        )
        job = self.job([image, video, audio])
        original = copy.deepcopy(job)
        prepared = self.compiler(job, "attempt-ref", self.store, lambda: None)
        settings = resolve_inputs(prepared, self.inputs, self.compiler.manifest)
        self.assertEqual(job, original)
        self.assertEqual(
            [item.kind for item in prepared.inputs], ["image", "video", "audio"]
        )
        paths = [
            *settings["image_refs"],
            settings["video_guide"],
            settings["audio_guide"],
        ]
        self.assertEqual([Path(p).suffix for p in paths], [".png", ".mp4", ".wav"])
        for path, item in zip(paths, prepared.inputs):
            self.assertTrue(Path(path).is_relative_to(self.inputs.root))
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(), item.sha256
            )
        calls = []

        class FakeSession:
            active_job = None

            def get_default_settings(self, model):
                calls.append(("defaults", model))
                return {"model_type": model, "num_inference_steps": 20}

            def submit_task(self, settings):
                calls.append(("submit", settings))
                return NS()

        facade = PinnedWanGPSession(
            FakeSession(), self.root, quiesce=lambda: None, worker_alive=lambda: False
        )
        facade.submit_task(settings)
        self.assertEqual(calls[0], ("defaults", "minimax_h3_ref2va"))
        self.assertEqual(calls[1][1]["num_inference_steps"], 50)
        self.assertEqual(calls[1][1]["video_prompt_type"], "IV-U")
        self.assertEqual(prepared.request_hash, job["request_hash"])
        self.assertEqual(len(self.uploads), 3)
        # Replay stages the same immutable objects; identity and bytes stay fixed.
        self.assertEqual(
            self.compiler(job, "attempt-ref", self.store, lambda: None), prepared
        )
        with self.assertRaisesRegex(ValueError, "manifest_binding"):
            resolve_inputs(prepared, self.inputs, manifest("manifest.json"))

    def test_foreign_owner_manifest_and_recipe_fail_before_any_upload(self):
        image = self.assets.upload(
            "superdan", "project-1", io.BytesIO(png()), "boat.png"
        )
        original = self.job([image])
        changes = [
            lambda j: j.update(owner_id="supervan"),
            lambda j: j["execution_plan"].update(engine_manifest_digest="b" * 64),
            lambda j: j["request"].update(recipe_id="h3-base-fl2va-v1"),
        ]
        for change in changes:
            job = copy.deepcopy(original)
            change(job)
            with self.assertRaises(BackendError):
                self.compiler(job, "attempt-ref", self.store, lambda: None)
        self.assertEqual(self.uploads, [])
        with self.assertRaises(AssetNotFound):
            self.assets.model_snapshot("supervan", "project-1", image["id"])

    def test_explicit_owned_selection_preserves_original_and_long_input_is_rejected(
        self,
    ):
        parent = self.upload(self.media("source.mp4", 4))
        old = self.assets.get("superdan", parent["id"])
        with self.assertRaisesRegex(BackendError, "selected_video"):
            self.compiler(self.job([parent]), "attempt-ref", self.store, lambda: None)
        with self.assertRaises(AssetNotFound):
            self.assets.derive("supervan", parent["id"], 1, 3)
        selected = self.assets.derive("superdan", parent["id"], 1, 3)
        job = self.job([selected])
        snapshot = job["request"]["assets"][selected["id"]]
        self.assertEqual(snapshot["parent_id"], parent["id"])
        self.assertEqual(snapshot["selection"], {"start": 1, "end": 3})
        prepared = self.compiler(job, "attempt-ref", self.store, lambda: None)
        self.assertTrue(
            Path(
                resolve_inputs(prepared, self.inputs, self.compiler.manifest)[
                    "video_guide"
                ]
            ).is_file()
        )
        self.assertEqual(self.assets.get("superdan", parent["id"]), old)

    def test_actual_soundtrack_and_typed_kind_tampering_fail_even_if_metadata_lies(
        self,
    ):
        asset = self.upload(self.media("with-audio.mp4", sound=True))
        job = self.job([asset])
        job["request"]["assets"][asset["id"]]["metadata"]["has_audio"] = False
        prepared = self.compiler(job, "attempt-ref", self.store, lambda: None)
        with self.assertRaisesRegex(ValueError, "media_probe_rejected"):
            resolve_inputs(prepared, self.inputs, self.compiler.manifest)
        wrong = replace(prepared, inputs=(replace(prepared.inputs[0], kind="image"),))
        with self.assertRaisesRegex(ValueError, "unbound_input"):
            resolve_inputs(wrong, self.inputs, self.compiler.manifest)

    def test_actual_audio_shape_hash_and_private_path_are_verified(self):
        path = self.media("long.wav", 4)
        body = path.read_bytes()
        item = InputDescriptor(
            "a", "handle-a", "audio", hashlib.sha256(body).hexdigest(), len(body)
        )
        self.inputs.save(item, io.BytesIO(body))
        with self.assertRaisesRegex(ValueError, "normalized_audio"):
            self.inputs.audio_path(item)
        with self.assertRaisesRegex(ValueError, "staged_input_mismatch"):
            self.inputs.resolve(replace(item, size_bytes=len(body) - 1))
        image = self.assets.upload(
            "superdan", "project-1", io.BytesIO(png()), "boat.png"
        )
        prepared = self.compiler(
            self.job([image]), "attempt-ref", self.store, lambda: None
        )
        settings = prepared.settings
        settings["image_refs"] = [str(path)]
        with self.assertRaisesRegex(ValueError, "unbound_input"):
            resolve_inputs(
                replace(prepared, settings_json=canonical_json(settings)),
                self.inputs,
                self.compiler.manifest,
            )


if __name__ == "__main__":
    unittest.main()

"""Offline recipe selection and inherited no-replay safety; no SDK/model calls."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "pro_pilot",
    Path(__file__).resolve().parents[1] / "tools" / "h3_pro6000_pilot_runtime.py",
)
pro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pro)


class FakeCUDA:
    def __getattr__(self, name):
        return lambda *args: 0


class FakeSession:
    def __init__(self, receipt=None, fail=False):
        self.receipt, self.fail = receipt, fail
        self.calls = []
        self.closes = 0

    def get_default_settings(self, model):
        return {"config": "int8,int8_convrot,lower_ram", "override_profile": 4}

    def submit_task(self, settings, callbacks=None):
        if self.receipt:
            assert json.loads(self.receipt.read_text())["state"] == "dispatch_intent"
        self.calls.append(copy.deepcopy(settings))
        if self.fail:
            raise ConnectionError("ambiguous_after_submit")
        return types.SimpleNamespace(done=True, result=lambda timeout=0: None)

    def close(self):
        self.closes += 1


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.torch = types.SimpleNamespace(cuda=FakeCUDA())
        self.task = {
            "id": "comparison",
            "mode": "fl",
            "steps": 20,
            "seed": 424242,
            "prompt": "synthetic scene",
            "resolution": "832x480",
            "frames": 124,
        }

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, result, output, task):
        path = output / (task["id"] + ".mp4")
        path.write_bytes(b"synthetic artifact")
        return [
            {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": pro.ProPilot("bf16").base.sha256(path),
            }
        ], {}

    def execute(self, pilot, session, tasks=None, audit=None):
        return pilot.base.run_tasks(
            session,
            tasks or [self.task],
            self.root,
            self.outputs,
            self.torch,
            time.time() + 3600,
            collect=self.collect,
            audit=audit,
        )

    def test_arm_mandatory_and_metadata_does_not_import_runtime(self):
        for bad in (None, "", "fp8", "pruned"):
            with self.assertRaisesRegex(ValueError, "explicit"):
                pro.assets_for(bad)
        with patch.object(
            pro.importlib,
            "import_module",
            side_effect=AssertionError("runtime imported"),
        ):
            for arm in ("bf16", "int8"):
                self.assertEqual(pro.asset_manifest(arm)["arm"], arm)
        result = subprocess.run(
            [sys.executable, str(Path(pro.__file__)), "assets", "--arm", "int8"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["arm"], "int8")

    def test_only_DiT_weights_differ_and_all_files_are_integrity_bound(self):
        bf, quant = pro.assets_for("bf16"), pro.assets_for("int8")
        self.assertEqual(bf[2:], quant[2:])
        self.assertTrue(all("pruned" not in path for path, _, _ in bf + quant))
        self.assertEqual([x[1] for x in bf[:2]], [66280486944, 66280486944])
        self.assertEqual([x[1] for x in quant[:2]], [34038903007, 34038903008])
        for path, size, digest in bf + quant:
            self.assertGreater(size, 0)
            self.assertRegex(digest, r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertTrue(bf[2][0].endswith("layer50_bf16.safetensors"))
        self.assertEqual(bf[3][0], "MiniMax-H3-video_vae_fp16.safetensors")

    def test_arm_instances_do_not_mutate_each_other_or_original_helper(self):
        bf, quant = pro.ProPilot("bf16"), pro.ProPilot("int8")
        self.assertNotEqual(
            bf.base.task_digest(self.task), quant.base.task_digest(self.task)
        )
        self.assertIn("-bf16-", bf.base.RECIPE)
        self.assertIn("-int8-", quant.base.RECIPE)
        source = Path(pro.__file__).with_name("h3_5090_pilot_runtime.py")
        original_spec = importlib.util.spec_from_file_location("original_check", source)
        original = importlib.util.module_from_spec(original_spec)
        original_spec.loader.exec_module(original)
        self.assertEqual(original.MODEL_TYPES["fl"], "minimax_h3_fl2va_pruned")

    def test_explicit_settings_override_pruned_defaults_and_freeze_companions(self):
        for arm in ("bf16", "int8"):
            pilot = pro.ProPilot(arm)
            config = pilot.runtime_config(self.root)
            self.assertEqual(config["transformer_quantization"], arm)
            self.assertEqual(config["text_encoder_quantization"], "bf16")
            self.assertEqual(config["profile"], 3)
            self.assertEqual(config["compile"], "")
            settings = pilot.base.settings_for(self.task)
            self.assertEqual(settings["model_type"], "minimax_h3_fl2va")
            self.assertEqual(
                settings["config"],
                "bf16,bf16" if arm == "bf16" else "bf16,bf16,lower_ram",
            )
            self.assertEqual(settings["override_profile"], 3)
            self.assertEqual(settings["activated_loras"], [])
            self.assertEqual(settings["skip_steps_cache_type"], "")
            self.assertEqual(settings["num_inference_steps"], 20)

    def test_durable_ambiguous_receipt_never_replays(self):
        pilot = pro.ProPilot("int8")
        receipt = self.root / "receipts/comparison.json"
        session = FakeSession(receipt, fail=True)
        with self.assertRaises(ConnectionError):
            self.execute(pilot, session)
        self.assertEqual(json.loads(receipt.read_text())["state"], "reconcile_required")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.execute(pilot, session)
        self.assertEqual(len(session.calls), 1)

    def test_completed_resume_and_changed_arm_are_not_new_submissions(self):
        pilot, session = pro.ProPilot("bf16"), FakeSession()
        self.execute(pilot, session)
        self.execute(pilot, session)
        self.assertEqual(len(session.calls), 1)
        with self.assertRaisesRegex(ValueError, "identity_changed"):
            self.execute(pro.ProPilot("int8"), session)
        self.assertEqual(len(session.calls), 1)
        (self.outputs / "comparison.mp4").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "output_changed"):
            self.execute(pilot, session)

    def test_effective_audit_failure_preserves_reconciliation_after_output_collection(
        self,
    ):
        pilot, session = pro.ProPilot("bf16"), FakeSession()

        def fail():
            raise ValueError("wrong_loaded_VAE")

        with self.assertRaisesRegex(ValueError, "wrong_loaded"):
            self.execute(pilot, session, audit=fail)
        self.assertEqual(
            json.loads((self.root / "receipts/comparison.json").read_text())["state"],
            "reconcile_required",
        )
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.execute(pilot, session)
        self.assertEqual(len(session.calls), 1)

    def test_FL_REF_switch_releases_previous_pipeline(self):
        pilot, session = pro.ProPilot("int8"), FakeSession()
        image = self.root / "reference.png"
        image.write_bytes(b"synthetic")
        ref = {
            **self.task,
            "id": "reference",
            "mode": "ref",
            "steps": 50,
            "inputs": {
                "image": {"path": str(image), "sha256": pilot.base.sha256(image)}
            },
        }
        self.execute(pilot, session, [self.task, ref])
        self.assertEqual(session.closes, 1)
        self.assertEqual(
            [s["model_type"] for s in session.calls], list(pro.MODEL_TYPES.values())
        )
        self.assertEqual([s["num_inference_steps"] for s in session.calls], [20, 50])
        self.assertTrue(
            all(
                s["config"] == pro.config_for("int8") and s["override_profile"] == 3
                for s in session.calls
            )
        )

    def test_memory_pressure_defers_without_lowering_shared_host_gate(self):
        pilot = pro.ProPilot("bf16")
        data = {
            "effective_available_ram_bytes": 127 * 1024**3,
            "gpu_free_bytes": 94 * 1024**3,
        }
        with patch.object(pilot.base, "resource_admission", return_value=data):
            self.assertEqual(
                pilot.resource_admission(self.torch, enforce=False)[
                    "required_available_ram_gib"
                ],
                128,
            )
            with self.assertRaisesRegex(ValueError, "headroom_insufficient"):
                pilot.resource_admission(self.torch)
        self.assertEqual(pro.MIN_AVAILABLE_RAM_GIB, 128)

    def test_video_fixture_must_have_56_actual_frames_and_cannot_silently_shorten(self):
        pilot = pro.ProPilot("bf16")
        path = self.root / "aligned.mp4"
        path.write_bytes(b"synthetic frame-count fixture")
        task = {
            **self.task,
            "mode": "ref",
            "inputs": {"video": {"path": str(path), "sha256": pilot.base.sha256(path)}},
        }
        metadata = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 832,
                    "height": 480,
                    "avg_frame_rate": "24/1",
                }
            ],
            "format": {"duration": "2.333333"},
        }
        for frames in ("56", "48", "39", "N/A", None):
            with (
                self.subTest(frames=frames),
                patch.object(
                    pilot.base,
                    "command",
                    side_effect=[
                        json.dumps(metadata),
                        json.dumps({"streams": [{"nb_read_frames": frames}]}),
                    ],
                ) as command,
            ):
                if frames == "56":
                    pilot.verify_input_media(task)
                    self.assertIn("-count_frames", command.call_args_list[-1].args[0])
                else:
                    with self.assertRaisesRegex(ValueError, "56_decoded_frames"):
                        pilot.verify_input_media(task)
        task["inputs"]["video"]["sha256"] = "0" * 64
        with patch.object(
            pilot.base, "command", side_effect=AssertionError("probe before hash")
        ):
            with self.assertRaisesRegex(ValueError, "input_hash_mismatch"):
                pilot.verify_input_media(task)

    def fake_device(
        self,
        name="NVIDIA RTX PRO 6000 Blackwell Server Edition",
        count=1,
        memory=95 * 1024**3,
    ):
        cuda = types.SimpleNamespace(
            is_available=lambda: True,
            device_count=lambda: count,
            get_device_name=lambda i: name,
            get_device_capability=lambda i: (12, 0),
            get_device_properties=lambda i: types.SimpleNamespace(
                total_memory=memory, uuid="GPU-12345678-1234-1234-1234-123456789abc"
            ),
        )
        return types.SimpleNamespace(cuda=cuda)

    def test_device_scope_accepts_one_visible_PRO_and_rejects_other_cards_slices_or_two_visible(
        self,
    ):
        self.assertEqual(
            pro.validate_device(self.fake_device())[2],
            "GPU-12345678-1234-1234-1234-123456789abc",
        )
        for device in (
            self.fake_device(count=2),
            self.fake_device(name="NVIDIA GeForce RTX 5090"),
            self.fake_device(memory=48 * 1024**3),
            self.fake_device(name="NVIDIA RTX 6000 Ada"),
        ):
            with self.assertRaises(ValueError):
                pro.validate_device(device)

    def fake_runtime(self, pilot):
        config = pilot.runtime_config(self.root)
        attention = types.SimpleNamespace(
            **(
                {"q_proj": object(), "k_proj": object(), "v_proj": object()}
                if pilot.arm == "bf16"
                else {"qkv_proj": object()}
            )
        )
        module = types.SimpleNamespace(
            server_config=config.copy(),
            default_profile_video=3,
            loaded_profile=3,
            loaded_config=pro.config_for(pilot.arm),
            transformer_type=pro.MODEL_TYPES["fl"],
            transformer_quantization=pilot.arm,
            text_encoder_quantization="bf16",
            int8_backend=types.SimpleNamespace(
                _backend="kitchen" if pilot.arm == "int8" else "pytorch"
            ),
            preload_mode=lambda kind: "default",
            wan_model=types.SimpleNamespace(
                transformer=types.SimpleNamespace(
                    h3_checkpoint_info={
                        "compressed_modulation": False,
                        "time_embed_dim": 2688,
                    },
                    split_linear_modules_map={"qkv_proj": {}}
                    if pilot.arm == "bf16"
                    else None,
                    blocks=[types.SimpleNamespace(attn=attention)],
                )
            ),
        )
        return module, config

    def test_loaded_audit_rejects_pruning_changed_profile_or_silent_global_precision(
        self,
    ):
        pilot = pro.ProPilot("bf16")
        module, config = self.fake_runtime(pilot)
        audit = pilot.runtime_audit(module, config, loaded=True)
        self.assertEqual(audit["task_override_profile"], 3)
        for field, value in (
            ("loaded_profile", 4),
            ("loaded_config", "int8,int8_convrot,lower_ram"),
        ):
            changed = copy.deepcopy(module)
            setattr(changed, field, value)
            with self.assertRaisesRegex(ValueError, "loaded_settings"):
                pilot.runtime_audit(changed, config, loaded=True)
        module.wan_model.transformer.h3_checkpoint_info["compressed_modulation"] = True
        with self.assertRaisesRegex(ValueError, "not_unpruned"):
            pilot.runtime_audit(module, config, loaded=True)
        module.server_config["text_encoder_quantization"] = "int8"
        with self.assertRaisesRegex(ValueError, "effective_pilot"):
            pilot.runtime_audit(module, config)

    def test_kernel_fallback_cannot_be_reported_as_requested_backend(self):
        for arm, expected in (("bf16", "pytorch"), ("int8", "kitchen")):
            pilot = pro.ProPilot(arm)
            module, config = self.fake_runtime(pilot)
            self.assertEqual(
                pilot.runtime_audit(module, config)["effective_int8_backend"], expected
            )
            for fallback in (None, "triton", "pytorch" if arm == "int8" else "kitchen"):
                module.int8_backend._backend = fallback
                with self.assertRaisesRegex(ValueError, "kernel_backend_changed"):
                    pilot.runtime_audit(module, config)

    def test_BF16_layout_fix_has_new_identity_and_checks_loaded_structure(self):
        bf = pro.ProPilot("bf16")
        quant = pro.ProPilot("int8")
        self.assertEqual(
            bf.base.RECIPE, "h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2"
        )
        self.assertEqual(
            quant.base.RECIPE, "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1"
        )
        current_digest = bf.base.task_digest(self.task)
        with patch.object(
            bf.base, "RECIPE", "h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-lowram-v1"
        ):
            self.assertNotEqual(current_digest, bf.base.task_digest(self.task))
        for pilot, path in (
            (bf, "interleaved_split"),
            (quant, "grouped_convrot_fused"),
        ):
            module, config = self.fake_runtime(pilot)
            self.assertEqual(
                pilot.runtime_audit(module, config, loaded=True)["loaded_qkv_path"],
                path,
            )
            module.wan_model.transformer.blocks[0].attn = types.SimpleNamespace(
                qkv_proj=object()
            )
            if pilot.arm == "bf16":
                with self.assertRaisesRegex(ValueError, "QKV_layout"):
                    pilot.runtime_audit(module, config, loaded=True)
        # Two-head checkpoint row labels: a contiguous-thirds split scrambles
        # native interleaved Q/K/V, while the upstream split maps preserve heads.
        native = ["q0", "k0", "v0", "q1", "k1", "v1"]
        expected = [["q0", "q1"], ["k0", "k1"], ["v0", "v1"]]
        self.assertEqual([native[index::3] for index in range(3)], expected)
        self.assertNotEqual(
            [native[start : start + 2] for start in range(0, 6, 2)], expected
        )
        self.assertTrue(
            bf.audit_definitions(self.fake_definitions(bf))["fl"]["qkv_splitting"]
        )
        self.assertFalse(
            quant.audit_definitions(self.fake_definitions(quant))["fl"]["qkv_splitting"]
        )

    def fake_definitions(self, pilot, *, vae=None, architecture=None):
        def definition(model_type):
            return {
                "architecture": architecture or model_type,
                "qkv_splitting": True,
                "URLs": [
                    "https://example/" + f[0]
                    for f in (
                        pro.MODEL_FILES["bf16"][
                            0 if model_type.endswith("fl2va") else 1
                        ],
                        pro.MODEL_FILES["int8"][
                            0 if model_type.endswith("fl2va") else 1
                        ],
                    )
                ],
            }

        groups = [
            {
                "bf16": {
                    "text_encoder_URLs": ["https://example/" + pro.SHARED_ASSETS[0][0]]
                }
            },
            {"bf16": {"video_vae_file": vae or pro.SHARED_ASSETS[1][0]}},
            {"lower_ram": {"qkv_splitting": False}},
        ]

        def filename(model_type, quantization, dtype_policy, model_def=None, URLs=None):
            choices = URLs if URLs is not None else model_def["URLs"]
            return next(v for v in choices if quantization in v.rsplit("/", 1)[-1])

        module = types.SimpleNamespace(
            transformer_quantization=pilot.arm,
            text_encoder_quantization="bf16",
            get_model_config_groups=lambda m, d: groups,
            get_model_filename=filename,
            model_config_groups=types.SimpleNamespace(
                selected_model_configs=lambda gs, config: (
                    (i, key, gs[i][key]) for i, key in enumerate(config.split(","))
                )
            ),
        )
        return types.SimpleNamespace(
            get_model_def=definition,
            _ensure_runtime=lambda: types.SimpleNamespace(module=module),
        )

    def test_definition_checks_require_explicit_original_VAE_and_full_model_types(self):
        for arm in ("bf16", "int8"):
            pilot = pro.ProPilot(arm)
            result = pilot.audit_definitions(self.fake_definitions(pilot))
            self.assertEqual(set(result), {"fl", "ref"})
            with self.assertRaisesRegex(ValueError, "FP16_video_VAE"):
                pilot.audit_definitions(
                    self.fake_definitions(
                        pilot,
                        vae="minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors",
                    )
                )
            with self.assertRaisesRegex(ValueError, "architecture_mismatch"):
                pilot.audit_definitions(
                    self.fake_definitions(pilot, architecture="minimax_h3_fl2va_pruned")
                )

    def test_actual_loader_paths_must_be_the_hashed_cache_not_a_same_named_copy(self):
        pilot = pro.ProPilot("bf16")
        models = self.root / "models"
        for name, _, _ in pro.assets_for("bf16"):
            path = models / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                b"tiny path fixture; full-hash verifier is tested separately"
            )
        session = self.fake_definitions(pilot)
        module = session._ensure_runtime().module
        module.fl = types.SimpleNamespace(
            locate_file=lambda name: models / name,
            get_local_model_filename=lambda url, extra_paths=None: (
                models / (extra_paths or "") / url.rsplit("/", 1)[-1]
            ),
        )
        observed = pilot.audit_definitions(session, models, {"fl"})
        self.assertIn("resolved_asset_paths", observed["fl"])
        self.assertNotIn("resolved_asset_paths", observed["ref"])
        other = self.root / "unverified-copy.safetensors"
        other.write_bytes(b"unverified")
        module.fl.get_local_model_filename = lambda *a, **k: other
        with self.assertRaisesRegex(ValueError, "effective_asset_path_mismatch"):
            pilot.audit_definitions(session, models, {"fl"})


if __name__ == "__main__":
    unittest.main()

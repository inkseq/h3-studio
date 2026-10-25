"""CPU-only script preview, CAS, isolation, uncertainty and native plan checks."""

import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from sqlalchemy import select
from comfy_workflow import native_output_spec
from studio_platform import script_windows as core
from studio_platform.script_window_timing import schedule
from studio_platform.inference import wangp_full_compiler as compiler
from studio_platform.inference.wangp_contract import (
    InputDescriptor,
    PreparedRequest,
    canonical_json,
)
from studio_platform.runtime_catalog import engine_manifest, get_profile
from studio_platform.auth import Principal
from studio_platform.repository import jobs, Conflict
from studio_platform.inference.native_outputs import native_delivery_spec
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.api import create_app
from studio_platform.settings import Settings
from fastapi.testclient import TestClient
import test_platform_api as fixtures

PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
TEXT = "A woman crosses a street. (S1) <d>[Chinese] 等我回来。</d> She smiles. (S1) <d>[Chinese] 一定。</d>"


class Model:
    def __init__(self):
        self.calls = 0
        self.hook = None
        self.failure = None

    def generate(self, model, messages, **kwargs):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.failure:
            raise self.failure
        value = json.loads(messages[0]["text"])
        seconds = value["explicit_window_seconds"] or int(
            next(iter(value["schedules"]))
        )
        timing = value["schedules"][str(seconds)]
        windows = [
            {
                "index": i,
                "description": f"The woman continues action {i}.",
                "dialogue_ids": [],
            }
            for i in range(len(timing["windows"]))
        ]
        windows[0]["dialogue_ids"] = [line["id"] for line in value["dialogue"]]
        return {"text": json.dumps({"window_seconds": seconds, "windows": windows})}


class PreviewTests(unittest.TestCase):
    setUp_base = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login

    def setUp(self):
        self.setUp_base()
        self.login()
        self.model = Model()
        self.app.state.script_windows.client = self.model
        self.project = fixtures.project()
        self.project["entities"][-1]["data"].update(seconds=15, prompt=TEXT)
        response = self.client.post("/v1/projects", json={"project": self.project})
        self.assertEqual(response.status_code, 201, response.text)
        self.version = response.json()["version"]
        self.url = "/v1/projects/story-one/shots/shot-one/script-windows"

    def preview(self, key="preview-one", **body):
        return self.client.post(
            self.url + "/preview",
            json={"expected_version": self.version, **body},
            headers={"Idempotency-Key": key},
        )

    def edit(self, actions, version=None):
        return self.client.post(
            "/v1/projects/story-one/actions",
            json={"expected_version": version or self.version, "actions": actions},
        )

    def test_preview_confirm_clear_and_no_generation(self):
        result = self.preview(window_seconds=5)
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(value["status"], "ready")
        self.assertEqual(len(value["script_windows"]["windows"]), 4)
        self.assertEqual(value["timing"]["output_frames"], 357)
        read = self.client.get(self.url + "/previews/" + value["preview_id"])
        self.assertEqual(read.json(), value)
        confirmed = self.edit(
            [
                {
                    "op": "shot.script_windows.confirm",
                    "shot_id": "shot-one",
                    "preview_id": value["preview_id"],
                }
            ]
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(self.preview(window_seconds=5).json(), value)
        current = self.client.get("/v1/projects/story-one").json()["project"]
        self.assertEqual(core.source_hash(current, "shot-one"), value["source_hash"])
        self.assertEqual(self.model.calls, 1)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs)).all(), [])
        cleared = self.edit(
            [{"op": "shot.script_windows.clear", "shot_id": "shot-one"}],
            version=confirmed.json()["version"],
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertNotIn(
            "script_windows", cleared.json()["project"]["entities"][-1]["data"]
        )

    def test_stale_cas_before_call_and_during_call(self):
        response = self.preview(expected_version=100)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.model.calls, 0)

        def change():
            result = self.edit(
                [
                    {
                        "op": "entity.update",
                        "entity_id": "shot-one",
                        "patch": {"data": {"prompt": "Changed"}},
                    }
                ]
            )
            self.assertEqual(result.status_code, 200, result.text)

        self.model.hook = change
        value = self.preview().json()
        self.assertEqual(value["status"], "stale")
        result = self.edit(
            [
                {
                    "op": "shot.script_windows.confirm",
                    "shot_id": "shot-one",
                    "preview_id": value["preview_id"],
                }
            ],
            version=2,
        )
        self.assertEqual(result.status_code, 409)

    def test_unknown_never_retried_and_mismatch_refused(self):
        self.model.failure = TimeoutError("sensitive upstream value")
        value = self.preview().json()
        self.assertEqual(value["status"], "unknown")
        self.assertNotIn("sensitive", json.dumps(value))
        self.assertEqual(self.preview().json(), value)
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.preview(window_seconds=6).status_code, 409)

    def test_pending_crash_recovery_never_retries(self):
        with patch.object(
            self.app.state.repository,
            "put_document",
            side_effect=RuntimeError("crash after call"),
        ):
            with self.assertRaises(RuntimeError):
                self.preview()
        self.assertEqual(self.preview().json()["status"], "pending")
        self.assertEqual(self.model.calls, 1)

    def test_rate_limit_replay_and_owner_isolation(self):
        for i in range(3):
            self.assertEqual(self.preview(key=f"preview-{i}").status_code, 200)
        self.assertEqual(self.preview(key="fourth").status_code, 429)
        first = self.preview(key="preview-0").json()
        self.assertEqual(self.model.calls, 3)
        self.login("supervan")
        self.assertEqual(self.preview(key="foreign").status_code, 404)
        self.assertEqual(
            self.client.get(self.url + "/previews/" + first["preview_id"]).status_code,
            404,
        )
        self.assertEqual(self.model.calls, 3)

    def test_invalid_model_and_invalid_length_do_not_retry(self):
        self.assertEqual(self.preview(window_seconds=11).status_code, 422)
        self.assertEqual(self.model.calls, 0)
        with patch.object(
            self.model, "generate", return_value={"text": "{bad json"}
        ) as call:
            self.assertEqual(self.preview().json()["status"], "invalid")
            self.assertEqual(self.preview().json()["status"], "invalid")
            self.assertEqual(call.call_count, 1)

    def test_reentrant_same_key_returns_pending_without_second_call(self):
        def replay():
            result = self.preview()
            self.assertEqual(result.json()["status"], "pending", result.text)

        self.model.hook = replay
        self.assertEqual(self.preview().json()["status"], "ready")
        self.assertEqual(self.model.calls, 1)

    def test_trashed_project_and_read_only_key_cannot_call_model(self):
        from test_platform_guided import GuidedTests

        _, headers = GuidedTests.key(self, scopes=["projects:read"])
        response = self.client.post(
            self.url + "/preview",
            json={"expected_version": 1},
            headers={**headers, "Idempotency-Key": "read-only"},
        )
        self.assertIn(response.status_code, (403, 404))
        trash = self.client.post(
            "/v1/trash/actions",
            json={
                "action": "trash",
                "items": [
                    {
                        "kind": "project",
                        "project_id": "story-one",
                        "id": "story-one",
                        "expected_version": 1,
                    }
                ],
            },
            headers={"Idempotency-Key": "trash-one"},
        )
        self.assertEqual(trash.status_code, 200, trash.text)
        self.assertIn(self.preview().status_code, (404, 409))
        self.assertEqual(self.model.calls, 0)

    def test_discovery(self):
        value = self.client.get("/v1/guided-schema").json()
        self.assertIn("shot.script_windows.confirm", value["operation_schemas"])
        self.assertIn("script_windows", value)

    def test_generation_receipt_forgery_and_stale_refused(self):
        value = self.preview().json()
        self.edit(
            [
                {
                    "op": "shot.script_windows.confirm",
                    "shot_id": "shot-one",
                    "preview_id": value["preview_id"],
                }
            ]
        ).raise_for_status()
        project = self.client.get("/v1/projects/story-one").json()["project"]
        principal = Principal("superdan", "browser-test")
        service = self.app.state.script_windows
        self.assertEqual(
            service.generation(principal, project, "shot-one")["preview_id"],
            value["preview_id"],
        )
        forged = copy.deepcopy(project)
        forged["entities"][-1]["data"]["script_windows"]["windows"][0][
            "description"
        ] = "Injected"
        with self.assertRaises(Conflict):
            service.generation(principal, forged, "shot-one")
        # Use the real HTTP generation preflight for stale documents.
        self.edit(
            [
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {"data": {"prompt": "Different scene"}},
                }
            ],
            version=2,
        ).raise_for_status()
        self.assertNotEqual(
            core.source_hash(
                self.client.get("/v1/projects/story-one").json()["project"], "shot-one"
            ),
            value["source_hash"],
        )
        self.assertTrue(project["entities"][-1]["data"]["script_windows"])
        with self.assertRaises(Conflict):
            service.generation(
                principal,
                self.client.get("/v1/projects/story-one").json()["project"],
                "shot-one",
            )


class PureTests(unittest.TestCase):
    def test_dialogue_ids_exact_once_order_and_source_hash(self):
        project = fixtures.project()
        project["entities"][-1]["data"].update(seconds=15, prompt=TEXT)
        original = core.source(project, "shot-one", {})
        value = {
            "window_seconds": 5,
            "windows": [
                {
                    "index": i,
                    "description": f"Visual beat {i}.",
                    "dialogue_ids": ["line-1", "line-2"] if i == 0 else [],
                }
                for i in range(4)
            ],
        }
        split, _, prompts = core.validate_result(json.dumps(value), original, 5)
        self.assertEqual("\n\n".join(prompts).count("<d>"), 2)
        for ids in (
            ["line-1"],
            ["line-1", "line-1"],
            ["line-2", "line-1"],
            ["line-1", "line-2", "foreign"],
        ):
            broken = copy.deepcopy(value)
            broken["windows"][0]["dialogue_ids"] = ids
            with self.assertRaises(ValueError):
                core.validate_result(json.dumps(broken), original, 5)
        for text in (
            'A paragraph\n\nAnother.',
            '"Say hello"',
            '[/duration 10s]',
            '(S1) <d>[Chinese] extra</d>',
        ):
            broken = copy.deepcopy(value)
            broken["windows"][0]["description"] = text
            with self.assertRaises(ValueError):
                core.validate_result(json.dumps(broken), original)
        old = core.source_hash(project, "shot-one")
        project["entities"][-1]["data"]["script_windows"] = split
        project["entities"][-1]["version"] += 1
        self.assertEqual(old, core.source_hash(project, "shot-one"))
        project["entities"][-1]["data"]["seconds"] = 14
        self.assertNotEqual(old, core.source_hash(project, "shot-one"))

    def test_direct_and_legacy_compile_cannot_bypass_confirmation(self):
        from studio_platform.capabilities import compile_request

        request = fixtures.generation_request(
            deployment_profile_id=PROFILE,
            controls={
                "duration": 15,
                "window_seconds": 5,
                "script_window_prompts": ["a", "b", "c", "d"],
            },
        )
        with self.assertRaisesRegex(ValueError, "script_windows_unconfirmed"):
            compile_request(request, lambda _: None, backend="wangp-worker")

    def test_native_short_tail_and_non_divisible(self):
        timing = schedule(362, 5)
        self.assertEqual(
            [
                (w["generation_frames"], w["overlap_frames"], w["output_frames"])
                for w in timing["windows"]
            ],
            [(124, 0, 124), (124, 18, 106), (124, 18, 106), (107, 86, 21)],
        )
        self.assertEqual(
            schedule(native_output_spec({"duration": 11})["frames"], 6)["windows"][-1][
                "end_frame"
            ],
            285,
        )
        with self.assertRaises(ValueError):
            schedule(192, 10)

    def test_compiler_round_trip_and_no_segments_regression(self):
        for mode in ("fl", "ref"):
            for windows in (False, True):
                prompts = [f"Visual beat {i}." for i in range(4)]
                request = {
                    "model": get_profile(PROFILE)["model_id"],
                    "mode": mode,
                    "prompt": "\n\n".join(prompts) if windows else "Legacy prompt",
                    "duration": 15,
                    "seed": "42",
                    "resolution": "480P",
                    "inputs": {"images": [], "videos": [], "audios": []},
                }
                if windows:
                    request.update(window_seconds=5, script_window_prompts=prompts)
                metadata, handles, descriptors = {}, {}, ()
                if mode == "ref":
                    request["inputs"]["images"] = ["ref-image"]
                    metadata = {
                        "ref-image": {
                            "kind": "image",
                            "model_ready": True,
                            "width": 832,
                            "height": 480,
                        }
                    }
                    handles = {"ref-image": "h-image"}
                    descriptors = (
                        InputDescriptor("ref-image", "h-image", "image", "b" * 64, 4),
                    )
                output = native_output_spec(request)
                settings = compiler.compile_settings(
                    request, metadata, output, handles, PROFILE
                )
                self.assertEqual(
                    settings["multi_prompts_gen_type"], "PW" if windows else "FG"
                )
                self.assertEqual(settings["num_inference_steps"], 5)
                self.assertEqual(
                    settings["prompt"].count(compiler.FILM_FRAME), 4 if windows else 1
                )
                settings["output_filename"] = "sixnine-test"
                binding = engine_manifest(PROFILE, mode)
                prepared = PreparedRequest(
                    "job",
                    "test",
                    "a" * 64,
                    binding.digest,
                    canonical_json(settings),
                    canonical_json(output),
                    True,
                    descriptors,
                )
                compiler.validate_prepared(prepared, binding)
                compiled = {
                    "request": request,
                    "output_spec": output,
                    "recipe_id": binding.document["generation_recipe_id"],
                    "deployment_profile_id": PROFILE,
                    "assets": {k: {"metadata": v} for k, v in metadata.items()},
                }
                delivery = native_delivery_spec(compiled)
                self.assertEqual(delivery["frame_count"], 357 if windows else 362)
                self.assertEqual(delivery["requested_duration_s"], 15)
                with tempfile.TemporaryDirectory() as temporary:
                    journal = ReceiptJournal(
                        Path(temporary) / "operations.sqlite",
                        slot_key="slot-one",
                        manifest_digest=binding.digest,
                        create=True,
                    )
                    journal.claim(prepared, "abc")
                    journal.save_delivery_context(prepared)
                    context = journal.delivery_context(prepared.operation_id)
                    self.assertEqual(
                        context["output_spec"]["frames"], delivery["frame_count"]
                    )
                    journal.save_delivery_context(prepared)
                if windows:
                    bad = copy.deepcopy(settings)
                    bad["prompt"] = bad["prompt"].replace("\n\n", "\n", 1)
                    with self.assertRaises(ValueError):
                        compiler.validate_prepared(
                            replace(prepared, settings_json=canonical_json(bad)),
                            binding,
                        )


class AdmissionTests(unittest.TestCase):
    def test_http_preview_confirm_preflight_keeps_original_job_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            model = Model()
            app = create_app(
                Settings(
                    Path(temporary),
                    auth_mode="local-test",
                    execution_backend="wangp-worker",
                    default_deployment_profile_id=PROFILE,
                    operator_admin_owners=("superdan",),
                    operator_capacity_owners=("superdan",),
                ),
                story_chat_client=model,
            )
            with TestClient(app) as client:
                client.post(
                    "/api/auth/login", json={"username": "superdan"}
                ).raise_for_status()
                project = fixtures.project()
                project["entities"][-1]["data"].update(
                    seconds=15,
                    prompt=TEXT,
                    h3={
                        "deploymentProfileId": PROFILE,
                        "recipeId": "h3-base-fl2va-v1",
                        "controls": {"seed": "42", "duration": 15},
                    },
                )
                created = client.post("/v1/projects", json={"project": project})
                self.assertEqual(created.status_code, 201, created.text)
                prefix = "/v1/projects/story-one/shots/shot-one"
                preview = client.post(
                    prefix + "/script-windows/preview",
                    json={"expected_version": 1, "window_seconds": 5},
                    headers={"Idempotency-Key": "preview"},
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["status"], "ready", preview.text)
                confirm = client.post(
                    "/v1/projects/story-one/actions",
                    json={
                        "expected_version": 1,
                        "actions": [
                            {
                                "op": "shot.script_windows.confirm",
                                "shot_id": "shot-one",
                                "preview_id": preview.json()["preview_id"],
                            }
                        ],
                    },
                )
                self.assertEqual(confirm.status_code, 200, confirm.text)
                plan = client.post(
                    prefix + "/generation-plans",
                    json={"expected_version": confirm.json()["version"]},
                )
                self.assertEqual(plan.status_code, 201, plan.text)
                request = plan.json()["effective_request"]
                self.assertEqual(request["duration"], 15)
                self.assertEqual(request["steps"], 5)
                self.assertEqual(len(request["script_window_prompts"]), 4)
                self.assertEqual(request["prompt"].count("<d>"), 2)
                self.assertEqual(model.calls, 1)
                with app.state.repository.engine.connect() as conn:
                    self.assertEqual(conn.execute(select(jobs)).all(), [])

"""Exact bounded multimodal HTTP payloads, fake transport, no API credentials."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.auth import Principal
from studio_platform.quick_chat_assistant import QuickChatAssistant, model_schema, MAX_PART_BYTES
from studio_platform.google_chat import ChatError


class FakeClient:
    def __init__(self, response=None, error=None, before=None):
        self.requests = []
        self.error, self.before = error, before
        self.response = response or {"reply": "完整建议。", "intent": "propose", "proposed_card": {
            "title": "广告片", "prompt": "A cup on the desk, close-up.", "controls": {"duration": 10}}}

    def _request(self, method, path, body):
        self.requests.append((method, path, copy.deepcopy(body)))
        if self.before:
            self.before()
        if self.error:
            raise self.error
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(self.response, ensure_ascii=False)}]}}],
            "modelVersion": "fake-transport", "usageMetadata": {"promptTokenCount": 42, "secret_extra": "omitted"}}


class FakeStore:
    def __init__(self, objects):
        self.objects = objects

    def open(self, key):
        return io.BytesIO(self.objects[key])


def receipt(kind, raw, ident=None, **extra):
    ident = ident or "asset-"+kind
    sha = hashlib.sha256(raw).hexdigest()
    return {"asset_id": ident, "project_id": "hidden-project", "kind": kind, "status": "ready",
        "original": {"sha256": sha}, "metadata": {"model_ready": True, "source_duration": 4.0},
        "model": {"key": "model-"+kind, "size_bytes": len(raw), "sha256": sha}, **extra}


def context(assets):
    return {"owner": "superdan", "assets": {a["asset_id"]: a for a in assets}, "bindings": [
        {"binding_id": "binding-"+a["kind"], "asset_id": a["asset_id"], "enabled": True,
         "slot": {"image": "images", "video": "videos", "audio": "audios"}[a["kind"]], "purpose": "reference"} for a in assets],
        "related_card": {"id": "revision-fixed", "prompt": "A cup on the desk.", "controls": {"duration": 5}, "input_hash": "a"*64},
        "lineage": [{"id": "revision-parent", "input_hash": "b"*64}]}


class AssistantPayloadTests(unittest.TestCase):
    def test_exact_model_payload_image_video_audio_and_context(self):
        assets = [receipt(kind, kind.encode()+b"-normalized") for kind in ("image", "video", "audio")]
        store = FakeStore({a["model"]["key"]: a["kind"].encode()+b"-normalized" for a in assets})
        input_context = context(assets)
        persisted = []
        input_context["record_manifest"] = lambda value: persisted.append(copy.deepcopy(value))
        client = FakeClient(before=lambda: self.assertEqual(len(persisted[-1]), 3))
        adapter = QuickChatAssistant(None, store, client=client)
        result = adapter.complete("gemini-3.8-flash", [{"role": "user", "text": "做10秒特写"}], input_context, {"controls": {"duration": 5}})
        method, path, body = client.requests[0]
        self.assertEqual((method, path), ("POST", "/v1beta/models/gemini-3.8-flash:generateContent"))
        inline = [p["inlineData"] for p in body["contents"][-1]["parts"] if "inlineData" in p]
        self.assertEqual([v["mimeType"] for v in inline], ["image/png", "video/mp4", "audio/wav"])
        self.assertEqual(base64.b64decode(inline[1]["data"]), b"video-normalized")
        self.assertIn("A cup on the desk.", json.dumps(body))
        self.assertIn("revision-fixed", json.dumps(body))
        self.assertFalse(any(v["sent"] for v in persisted[-1]))
        self.assertTrue(all(v["delivery_confirmed"] for v in result["media_input_manifest"]))
        self.assertEqual(result["card"]["controls"]["duration"], 10)
        self.assertEqual(result["usage"], {"promptTokenCount": 42})

    def test_gemma_unsupported_audio_does_not_fall_back_to_text_or_other_model(self):
        asset = receipt("audio", b"wav-payload")
        client = FakeClient()
        input_context = context([asset])
        persisted = []
        input_context["record_manifest"] = lambda value: persisted.append(copy.deepcopy(value))
        adapter = QuickChatAssistant(None, FakeStore({"model-audio": b"wav-payload"}), client=client)
        with self.assertRaises(ChatError) as error:
            adapter.complete("gemma-4-31b-it", [{"role": "user", "text": "听声音"}], input_context, {})
        self.assertEqual(error.exception.code, "assistant_media_unsupported")
        self.assertEqual(client.requests, [])
        self.assertEqual(persisted[-1][0]["reason"], "unsupported_model_input")

    def test_media_integrity_limit_and_unselected_long_media_reject_before_request(self):
        for case in ("hash", "size", "long"):
            with self.subTest(case=case):
                asset = receipt("video", b"mp4")
                if case == "hash":
                    asset["model"]["sha256"] = "a"*64
                elif case == "size":
                    asset["model"]["size_bytes"] = MAX_PART_BYTES+1
                else:
                    asset["metadata"]["model_ready"] = False
                client = FakeClient()
                adapter = QuickChatAssistant(None, FakeStore({"model-video": b"mp4"}), client=client)
                with self.assertRaises(ChatError):
                    adapter.complete("gemini-3.8-flash", [{"role": "user", "text": "做视频"}], context([asset]), {})
                self.assertEqual(client.requests, [])

    def test_timeout_keeps_prepared_manifest_not_claimed_delivery(self):
        asset = receipt("image", b"png")
        input_context = context([asset])
        persisted = []
        input_context["record_manifest"] = lambda value: persisted.append(copy.deepcopy(value))
        client = FakeClient(error=ChatError("upstream_timeout", "lost response"))
        adapter = QuickChatAssistant(None, FakeStore({"model-image": b"png"}), client=client)
        with self.assertRaises(ChatError):
            adapter.complete("gemini-3.8-flash", [{"role": "user", "text": "做视频"}], input_context, {})
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(persisted[-1][0]["reason"], "prepared_for_request")
        self.assertFalse(persisted[-1][0]["sent"])
        self.assertFalse(persisted[-1][0]["delivery_confirmed"])

    def test_schema_distinguishes_implementation_from_actual_verification(self):
        enabled = model_schema(enabled=True)
        self.assertTrue(enabled[0]["inputs"]["video"]["enabled"])
        self.assertFalse(enabled[0]["inputs"]["video"]["verified"])
        self.assertFalse(enabled[1]["inputs"]["video"]["implemented"])
        self.assertFalse(any(c["enabled"] for row in model_schema() for c in row["inputs"].values()))

    def test_arbitrary_execution_proposal_and_bad_json_reject(self):
        for response in ({"reply": "bad", "intent": "propose", "proposed_card": {"prompt": "valid", "api_key": "fake"}},
                         {"reply": "bad", "intent": "discuss", "proposed_card": {"prompt": "bad"}}):
            client = FakeClient(response=response)
            adapter = QuickChatAssistant(None, FakeStore({}), client=client)
            with self.assertRaises(ChatError) as error:
                adapter.complete("gemini-3.8-flash", [{"role": "user", "text": "讨论"}], context([]), {})
            self.assertEqual(error.exception.code, "assistant_invalid_response")
            self.assertEqual(len(client.requests), 1)

    def test_bound_inline_size_no_file_uri_or_remote_urls(self):
        client = FakeClient()
        adapter = QuickChatAssistant(None, FakeStore({}), client=client)
        with self.assertRaises(ChatError):
            adapter.complete("other-model", [{"role": "user", "text": "hello"}], context([]), {})
        self.assertEqual(client.requests, [])


class AdapterDatabaseReceiptTests(unittest.TestCase):
    def test_real_png_receipt_and_request_manifest_persist_before_fake_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Settings(Path(temporary), auth_mode="local-test"))
            repo, service = app.state.repository, app.state.quick_chat
            try:
                principal = Principal("superdan", "browser")
                sid = service.create_session(principal, {}, "create")["session"]["id"]
                image = io.BytesIO()
                Image.new("RGB", (512, 512), "red").save(image, "PNG")
                image.seek(0)
                asset = service.upload(principal, sid, image, "red.png", "reference")
                binding = service.materials(principal, sid)["bindings"][0]
                selected = {k: binding[k] for k in ("binding_id", "version", "asset_id", "kind", "slot", "enabled")}
                selected.update(slot="first_frame", enabled=True)
                service.put_materials(principal, sid, {"expected_version": 1, "bindings": [selected]}, "bind")
                def before():
                    from sqlalchemy import select
                    from studio_platform.quick_chat import objects
                    with repo.engine.connect() as conn:
                        turn = conn.execute(select(objects).where(objects.c.kind == "turn")).mappings().one()["payload"]
                    self.assertEqual(turn["status"], "running")
                    self.assertEqual(turn["assistant_run"]["media_input_manifest"][0]["asset_id"], asset["asset_id"])
                    self.assertFalse(turn["assistant_run"]["media_input_manifest"][0]["delivery_confirmed"])
                client = FakeClient(response={"reply": "图像讨论。", "intent": "discuss", "proposed_card": None}, before=before)
                service.assistant = QuickChatAssistant(app.state.assets, app.state.storage, client=client)
                service.assistant_enabled = True
                turn = service.create_turn(principal, sid, {"expected_version": 2, "text": "先讨论这张图片", "model_id": "gemini-3.8-flash", "assistant_mode": "discuss"}, "turn")
                self.assertEqual(turn["status"], "completed")
                self.assertTrue(turn["assistant_run"]["media_input_manifest"][0]["delivery_confirmed"])
                self.assertIsNone(turn["card_id"])
            finally:
                repo.engine.dispose()


if __name__ == "__main__":
    unittest.main()

"""Streamed story chat turns (#1083): scripted model and fake SSE; no network."""

import json
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.google_chat import ChatError, GoogleChatClient
from studio_platform.settings import Settings
from studio_platform.story_chat_stream import reply_so_far, sse, stream_turn
from test_story_chat import CAST, ScriptedModel


def events(text):
    found = []
    for block in text.split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or lines[0].startswith(":"):
            continue
        name = lines[0].removeprefix("event: ")
        found.append((name, json.loads(lines[1].removeprefix("data: "))))
    return found


class StreamingModel(ScriptedModel):
    """Feeds the plan to on_text in small pieces, like the real adapter."""

    def generate_stream(self, *args, on_text=None, **kwargs):
        result = self.generate(*args, **kwargs)
        for end in range(8, len(result["text"]) + 8, 8):
            on_text(result["text"][:end])
        return result


class ReplyExtractionTests(unittest.TestCase):
    def test_partial_reply_is_read_safely(self):
        full = json.dumps({"reply": 'Line "one"\nCafé ☕ done', "chips": []})
        seen = [reply_so_far(full[:end]) for end in range(len(full) + 1)]
        self.assertIsNone(seen[0])
        self.assertEqual(seen[-1], 'Line "one"\nCafé ☕ done')
        # Every prefix is a prefix of the final text: no half escapes.
        for value in seen:
            if value is not None:
                self.assertTrue('Line "one"\nCafé ☕ done'.startswith(value), value)

    def test_no_reply_key_or_non_string(self):
        self.assertIsNone(reply_so_far('{"chips": []}'))
        self.assertIsNone(reply_so_far('{"reply": 3}'))

    def test_stream_turn_maps_errors_and_keepalive(self):
        from fastapi import HTTPException

        def failing(progress):
            progress("progress", step="thinking")
            raise HTTPException(429, {"code": "story_chat_turn_limit"})

        found = events("".join(stream_turn(failing)))
        self.assertEqual(found[0], ("progress", {"step": "thinking"}))
        self.assertEqual(
            found[1],
            ("error", {"status": 429, "detail": {"code": "story_chat_turn_limit"}}),
        )

        def crashing(progress):
            raise RuntimeError("PRIVATE details")

        text = "".join(stream_turn(crashing))
        self.assertNotIn("PRIVATE", text)
        self.assertIn("story_chat_failed", text)
        self.assertEqual(sse("x", {"a": "é"}), 'event: x\ndata: {"a":"é"}\n\n')


class StoryChatStreamTests(unittest.TestCase):
    model_class = StreamingModel

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            Path(self.tmp.name), auth_mode="local-test", story_chat_enabled=True
        )
        self.model = self.model_class()
        self.app = create_app(settings, story_chat_client=self.model)
        self.addCleanup(self.app.state.repository.engine.dispose)
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login", json={"username": "superdan"})
        chat = self.client.post("/v1/story-chats", json={"language": "en"})
        self.project = chat.json()["project_id"]

    def stream(self, body):
        with self.client.stream(
            "POST", f"/v1/story-chats/{self.project}/turns/stream", json=body
        ) as response:
            return response.status_code, response.headers, response.read().decode()

    def test_stream_sends_reply_then_the_same_stored_result(self):
        self.model.plans.append(CAST)
        status, headers, text = self.stream({"turn_id": "turn-0001", "text": "Hi"})
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        found = events(text)
        names = [name for name, _ in found]
        self.assertEqual(names[0], "progress")
        self.assertEqual(names[-2:], ["progress", "done"])
        self.assertEqual(found[-2][1], {"step": "applying"})
        if self.model_class is StreamingModel:
            replies = [data["text"] for name, data in found if name == "reply"]
            self.assertTrue(replies)
            self.assertEqual(replies[-1], CAST["reply"])
        done = found[-1][1]
        self.assertEqual(done["turn"]["turn_id"], "turn-0001")
        self.assertEqual(len(self.model.calls), 1)
        # The plan was applied once; a plain replay returns the stored turn.
        replay = self.client.post(
            f"/v1/story-chats/{self.project}/turns",
            json={"turn_id": "turn-0001", "text": "Hi"},
        ).json()
        self.assertEqual(replay["turn"], done["turn"])
        self.assertEqual(len(self.model.calls), 1)

    def test_invalid_body_fails_before_the_stream_starts(self):
        status, _, text = self.stream({"turn_id": "bad id", "text": "Hi"})
        self.assertEqual(status, 422)
        self.assertNotIn("event:", text)

    def test_model_error_arrives_as_an_error_event_and_writes_nothing(self):
        self.model.plans.append(ChatError("upstream_timeout", "timeout", 502))
        status, _, text = self.stream({"turn_id": "turn-0001", "text": "Hi"})
        self.assertEqual(status, 200)
        name, data = events(text)[-1]
        self.assertEqual(name, "error")
        self.assertEqual(data["status"], 502)
        self.assertEqual(data["detail"]["code"], "assistant_upstream_timeout")
        chat = self.client.get(f"/v1/story-chats/{self.project}").json()
        self.assertEqual(chat.get("turns", chat.get("chat", {}).get("turns", 0)), 0)


class StoryChatStreamWithoutStreamingClientTests(StoryChatStreamTests):
    """A client without generate_stream still works: progress steps only."""

    model_class = ScriptedModel


class GoogleStreamTests(unittest.TestCase):
    def client(self, chunks, status=200):
        body = "".join("data: " + json.dumps(c) + "\r\n\r\n" for c in chunks)

        def handler(request):
            self.assertEqual(request.url.params["alt"], "sse")
            self.assertTrue(request.url.path.endswith(":streamGenerateContent"))
            return httpx.Response(status, text=body)

        config = type(
            "C",
            (),
            {"base_url": "https://generativelanguage.googleapis.com", "api_key": "k"},
        )()
        return GoogleChatClient(
            loader=lambda: config, transport=httpx.MockTransport(handler)
        )

    def test_streamed_text_matches_generate_shape(self):
        chunks = [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "think", "thought": True}]}}
                ]
            },
            {"candidates": [{"content": {"parts": [{"text": '{"reply": "Hel'}]}}]},
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": 'lo"}'}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
                "modelVersion": "v",
            },
        ]
        seen = []
        result = self.client(chunks).generate_stream(
            "gemini-3.8-flash",
            [{"role": "user", "text": "x"}],
            "",
            json_mode=True,
            on_text=seen.append,
        )
        self.assertEqual(result["text"], '{"reply": "Hello"}')
        self.assertEqual(seen, ['{"reply": "Hel', '{"reply": "Hello"}'])
        self.assertEqual(result["finish_reason"], "STOP")
        self.assertEqual(result["usage"]["candidatesTokenCount"], 4)

    def test_http_error_and_empty_stream(self):
        with self.assertRaises(ChatError) as caught:
            self.client([], status=429).generate_stream(
                "gemini-3.8-flash", [{"role": "user", "text": "x"}], ""
            )
        self.assertEqual(caught.exception.code, "google_http_429")
        with self.assertRaises(ChatError) as caught:
            self.client([{"candidates": []}]).generate_stream(
                "gemini-3.8-flash", [{"role": "user", "text": "x"}], ""
            )
        self.assertEqual(caught.exception.code, "empty_or_blocked")


if __name__ == "__main__":
    unittest.main()

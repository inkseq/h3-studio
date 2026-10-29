"""Mock transport only: profile selection, bounded requests and secret-safe errors."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from studio_platform import inky_providers as providers


class InkyProvidersTests(unittest.TestCase):
    source = {
        "shot_action": "Rain falls.",
        "scene_script": "Rain at a station.",
        "characters": [],
        "place": "Station",
        "props": [],
        "lang": "en",
        "seconds": 5,
    }

    def test_exact_profile_and_authorized_missing_openai_origin(self):
        with patch.object(providers, "load_api", return_value=object()) as load:
            providers.config("openai")
            load.assert_called_once_with("openai", profile="openai--inkseq")
        requests = []

        def endpoint(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": '{"example":"Rain falls."}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 400, "completion_tokens": 40},
                },
            )

        client = providers.Terra(
            loader=lambda _: SimpleNamespace(base_url=None, api_key="FAKE"),
            transport=httpx.MockTransport(endpoint),
        )
        result = client(self.source, "vague_action")
        # 400 input tokens at the 1.25 cache-write rate + 40 output tokens at 5.
        self.assertEqual(result["actual_micros"], 500 + 40 * 5)
        self.assertEqual(
            str(requests[0].url), "https://api.openai.com/v1/chat/completions"
        )
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "gpt-6.1-terra")
        self.assertEqual(body["max_completion_tokens"], 1024)
        self.assertEqual(body["reasoning_effort"], "low")
        for key in ("provider", "usage", "reasoning", "max_tokens", "tools"):
            self.assertNotIn(key, body)

    def test_wrong_origin_does_not_receive_a_key_and_has_no_fallback(self):
        endpoint = Mock()
        for service, origin in (
            ("typesafe", None),
            ("openai", "https://other.invalid"),
        ):
            client = providers.Client(
                service,
                loader=lambda _: SimpleNamespace(base_url=origin, api_key="SECRET"),
                transport=httpx.MockTransport(endpoint),
            )
            with self.assertRaisesRegex(
                providers.InkyProviderError, "^inky_profile_invalid$"
            ):
                client.post("/ignored", {})
        endpoint.assert_not_called()

    def test_redirect_and_exception_body_are_never_retried_or_exposed(self):
        requests = []

        def endpoint(request):
            requests.append(request)
            return httpx.Response(
                307, headers={"Location": "https://other.invalid/SECRET"}, text="SECRET"
            )

        client = providers.Jev(
            loader=lambda _: SimpleNamespace(
                base_url=providers.TYPE_ORIGIN, api_key="FAKE"
            ),
            transport=httpx.MockTransport(endpoint),
        )
        with self.assertRaisesRegex(
            providers.InkyProviderError, "^inky_provider_http$"
        ):
            client(self.source)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("assets", json.loads(requests[0].content))

    def test_missing_or_invalid_token_counts_are_unknown(self):
        for value in (
            None,
            {},
            {"prompt_tokens": 3},
            {"prompt_tokens": -1, "completion_tokens": 1},
            {"prompt_tokens": True, "completion_tokens": 1},
            {"prompt_tokens": "3", "completion_tokens": 1},
        ):
            self.assertIsNone(providers.token_cost(value))
        self.assertEqual(
            providers.token_cost({"prompt_tokens": 0, "completion_tokens": 0}), 0
        )
        self.assertEqual(
            providers.token_cost({"prompt_tokens": 10, "completion_tokens": 2}), 23
        )
        # A 2048-token first cache write is 2560 micro-USD; one token rounds up.
        self.assertEqual(
            providers.token_cost({"prompt_tokens": 2048, "completion_tokens": 0}), 2560
        )
        self.assertEqual(
            providers.token_cost({"prompt_tokens": 1, "completion_tokens": 0}), 2
        )

    def test_truncated_output_keeps_known_cost(self):
        result = {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
        }
        client = providers.Terra(
            loader=lambda _: SimpleNamespace(base_url=None, api_key="FAKE"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result)),
        )
        self.assertEqual(
            client(self.source, "vague_action"),
            {"text": None, "actual_micros": 625 + 100 * 5},
        )

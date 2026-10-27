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

    def test_exact_profile_and_authorized_missing_openrouter_origin(self):
        with patch.object(providers, "load_api", return_value=object()) as load:
            providers.config("openrouter")
            load.assert_called_once_with(
                "openrouter", profile="openrouter--inkseq-chat"
            )
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
                    "usage": {"cost": 0.0006001},
                },
            )

        client = providers.Terra(
            loader=lambda _: SimpleNamespace(base_url=None, api_key="FAKE"),
            transport=httpx.MockTransport(endpoint),
        )
        result = client(self.source, "vague_action")
        self.assertEqual(result["actual_micros"], 601)
        self.assertEqual(
            str(requests[0].url), "https://openrouter.ai/api/v1/chat/completions"
        )
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "openai/gpt-6.1-terra")
        self.assertEqual(body["max_tokens"], 1024)
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertEqual(body["provider"]["max_price"]["request"], 0)
        self.assertNotIn("tools", body)

    def test_wrong_origin_does_not_receive_a_key_and_has_no_fallback(self):
        endpoint = Mock()
        for service, origin in (
            ("typesafe", None),
            ("openrouter", "https://other.invalid"),
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

    def test_missing_or_invalid_cost_is_unknown(self):
        for value in (None, True, -1, "NaN", float("inf"), {}, "secret"):
            self.assertIsNone(providers.cost_micros(value))
        self.assertEqual(providers.cost_micros(0), 0)
        self.assertEqual(providers.cost_micros("0.0000001"), 1)

    def test_truncated_output_keeps_known_cost(self):
        result = {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"cost": "0.001"},
        }
        client = providers.Terra(
            loader=lambda _: SimpleNamespace(base_url=None, api_key="FAKE"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result)),
        )
        self.assertEqual(
            client(self.source, "vague_action"), {"text": None, "actual_micros": 1000}
        )

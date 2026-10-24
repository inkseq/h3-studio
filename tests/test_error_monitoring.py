"""Closed content-free events, using an in-memory transport; never Sentry IO."""

import copy
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import error_monitoring
from studio_platform.settings import Settings


SECRET = "story-prompt-email@example.test-token-cookie-query"


class ErrorMonitoringPrivacyTests(unittest.TestCase):
    def test_nested_event_rebuilt_without_content_or_hints(self):
        frames = [
            {
                "filename": "/app/studio_platform/story_chat_images.py",
                "function": "request_image",
                "lineno": 150,
                "vars": {"prompt": SECRET},
                "context_line": SECRET,
                "pre_context": [SECRET],
                "post_context": [SECRET],
                "abs_path": SECRET,
            },
            {"filename": SECRET, "function": SECRET, "lineno": 4},
        ]
        original = {
            "event_id": "a" * 32,
            "environment": "staging",
            "server_name": SECRET,
            "request": {"url": SECRET, "cookies": SECRET, "data": SECRET},
            "contexts": {"trace": {"data": {"story": SECRET}}},
            "breadcrumbs": {"values": [{"message": SECRET}]},
            "message": SECRET,
            "logentry": {"message": SECRET, "params": [SECRET]},
            "user": {"email": SECRET, "id": SECRET},
            "transaction": SECRET,
            "tags": {SECRET: SECRET},
            "extra": {"nested": {"exception": SECRET}},
            "exception": {
                "values": [
                    {
                        "type": "ExceptionGroup",
                        "value": SECRET,
                        "stacktrace": {"frames": frames},
                        "mechanism": {
                            "type": SECRET,
                            "data": {"text": SECRET},
                            "handled": False,
                        },
                    },
                    {
                        "type": "ValueError",
                        "value": SECRET,
                        "stacktrace": {"frames": frames},
                    },
                ]
            },
        }
        before = copy.deepcopy(original)
        hint = {"attachments": [SECRET], "exc_info": SECRET}
        event = error_monitoring.sanitized_event(original, hint)
        self.assertNotIn(SECRET, json.dumps(event))
        self.assertNotIn("attachments", hint)
        self.assertEqual(original, before)
        self.assertEqual(
            set(event), {"event_id", "platform", "level", "environment", "exception"}
        )
        self.assertEqual(
            event["exception"]["values"][1]["stacktrace"]["frames"],
            [
                {
                    "filename": "studio_platform/story_chat_images.py",
                    "lineno": 150,
                    "in_app": True,
                }
            ],
        )

    def test_drop_non_exceptions_and_malformed_nested_metadata(self):
        for value in (
            None,
            [],
            {"message": SECRET},
            {"type": "transaction"},
            {"exception": {"values": SECRET}},
            {"exception": {"values": []}},
        ):
            self.assertIsNone(error_monitoring.sanitized_event(value))
        self.assertEqual(
            error_monitoring.sanitized_event(
                {
                    "exception": {
                        "values": [
                            {
                                "type": {"secret": SECRET},
                                "value": SECRET,
                                "stacktrace": {"frames": [None, SECRET]},
                            },
                        ]
                    }
                }
            )["exception"]["values"],
            [{"type": "Error", "value": "[message omitted]"}],
        )
        self.assertEqual(
            error_monitoring.sanitized_event(
                {
                    "exception": {
                        "values": [
                            {"type": "PrivatePromptWithoutSpaces", "value": SECRET},
                        ]
                    }
                }
            )["exception"]["values"][0]["type"],
            "Error",
        )

    def test_disabled_masks_global_without_transport_or_global_initialization(self):
        with patch("sentry_sdk.init") as initialize:
            for dsn, environment in (
                ("", "production"),
                (error_monitoring.DSN, ""),
                (error_monitoring.DSN, "local"),
            ):
                client = error_monitoring.create_client(
                    SimpleNamespace(sentry_dsn=dsn, sentry_environment=environment)
                )
                self.addCleanup(client.close)
                self.assertIsNone(client.transport)
                self.assertTrue(
                    client.is_active()
                )  # Prevent fallback to another app/global.
                self.assertEqual(client.integrations, {})
            initialize.assert_not_called()

    def test_only_explicit_target_derives_environment_and_destination_is_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings.from_environment().sentry_environment, "")
            os.environ["INKSEQ_SENTRY_DSN"] = error_monitoring.DSN
            self.assertEqual(Settings.from_environment().sentry_environment, "")
            for target in ("staging", "production"):
                os.environ["INKSEQ_DEPLOYMENT_TARGET"] = target
                settings = Settings.from_environment()
                self.assertEqual(settings.sentry_environment, target)
            for dsn in (
                SECRET,
                error_monitoring.DSN + "?prompt=" + SECRET,
                error_monitoring.DSN.replace("ingest.us.sentry.io", "example.test"),
            ):
                os.environ["INKSEQ_SENTRY_DSN"] = dsn
                with self.assertRaisesRegex(ValueError, "monitoring"):
                    Settings.from_environment()

    def test_sdk_real_exception_envelope_excludes_request_scope_and_logging(self):
        import sentry_sdk
        from fastapi.testclient import TestClient
        from sentry_sdk.transport import Transport

        class MemoryTransport(Transport):
            def __init__(self):
                super().__init__()
                self.envelopes = []

            def capture_envelope(self, envelope):
                self.envelopes.append(envelope)

        transport = MemoryTransport()
        with (
            patch(
                "sentry_sdk.client.make_transport",
                side_effect=lambda options: transport if options["dsn"] else None,
            ),
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            app = error_monitoring.monitored_app(
                SimpleNamespace(
                    sentry_dsn=error_monitoring.DSN, sentry_environment="staging"
                )
            )
            client = app.state.error_monitoring_client
            self.assertEqual(set(client.integrations), {"starlette", "fastapi"})
            self.assertFalse(client.options["send_default_pii"])
            self.assertFalse(client.options["include_local_variables"])
            self.assertEqual(client.options["max_request_body_size"], "never")
            self.assertEqual(client.options["traces_sample_rate"], 0)

            @app.post("/fixture/{identity}")
            async def fixture(identity: str):
                sentry_sdk.set_context("story", {"prompt": SECRET})
                sentry_sdk.set_user({"id": SECRET, "email": SECRET})
                sentry_sdk.set_extra("prompt", SECRET)
                sentry_sdk.add_breadcrumb(message=SECRET)
                sentry_sdk.add_attachment(bytes=SECRET.encode(), filename="prompt.txt")
                with self.assertLogs(level="ERROR"):
                    logging.error(SECRET)
                raise ExceptionGroup(SECRET, [ValueError(SECRET), RuntimeError(SECRET)])

            with TestClient(app, raise_server_exceptions=False) as browser:
                response = browser.post(
                    "/fixture/" + SECRET + "?prompt=" + SECRET,
                    json={"prompt": SECRET},
                    headers={
                        "cookie": "session=" + SECRET,
                        "authorization": "Bearer " + SECRET,
                        "baggage": "sentry-transaction=" + SECRET,
                        "sentry-trace": "a" * 32 + "-" + "b" * 16 + "-1",
                    },
                )
            self.assertEqual(response.status_code, 500)
            sentry_sdk.capture_message(SECRET)
            sentry_sdk.flush()
            events = [
                item for envelope in transport.envelopes for item in envelope.items
            ]
            self.assertTrue(events)
            self.assertTrue(all(item.type == "event" for item in events))
            outbound = b"\n".join(
                envelope.serialize() for envelope in transport.envelopes
            )
            self.assertNotIn(SECRET.encode(), outbound)
            event = events[0].get_event()
            self.assertEqual(event["environment"], "staging")
            self.assertEqual(
                set(event) - {"event_id"},
                {"platform", "level", "environment", "exception"},
            )

    def test_enabled_disabled_concurrent_apps_foreign_global_and_lifespan_close(self):
        import sentry_sdk
        from fastapi.testclient import TestClient
        from sentry_sdk.transport import Transport

        transports = []
        overlap = threading.Barrier(2)
        observed_clients = []

        class MemoryTransport(Transport):
            def __init__(self, options):
                super().__init__(options)
                self.envelopes = []
                self.killed = False

            def capture_envelope(self, envelope):
                self.envelopes.append(envelope)

            def kill(self):
                self.killed = True

        def transport(options):
            if not options["dsn"]:
                return None
            result = MemoryTransport(options)
            transports.append(result)
            return result

        def app(environment, dsn=error_monitoring.DSN, **kwargs):
            result = error_monitoring.monitored_app(
                SimpleNamespace(sentry_dsn=dsn, sentry_environment=environment),
                **kwargs,
            )

            @result.get("/error")
            async def error():
                raise RuntimeError(SECRET)

            @result.get("/overlap")
            async def overlapping_error():
                expected = result.state.error_monitoring_client
                observed_clients.append((expected, sentry_sdk.get_client()))
                # Both apps must be inside their scopes before either resumes.
                # Separate TestClient portals run concurrently, not just two
                # live lifespans with sequential requests.
                await asyncio.to_thread(overlap.wait, timeout=5)
                await asyncio.sleep(0)
                observed_clients.append((expected, sentry_sdk.get_client()))
                raise RuntimeError(SECRET)

            return result

        with (
            patch("sentry_sdk.client.make_transport", side_effect=transport),
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            global_scope = sentry_sdk.get_global_scope()
            previous = global_scope.client
            foreign = error_monitoring.create_client(
                SimpleNamespace(
                    sentry_dsn=error_monitoring.DSN, sentry_environment="production"
                )
            )
            global_scope.set_client(foreign)
            self.addCleanup(global_scope.set_client, previous)
            self.addCleanup(foreign.close)
            foreign_transport = foreign.transport
            enabled = app("staging")
            other = app("production")
            active_transport = enabled.state.error_monitoring_client.transport
            other_transport = other.state.error_monitoring_client.transport
            disabled = [app("local"), app(""), app("production", dsn="")]
            self.assertIs(global_scope.client, foreign)
            with TestClient(other, raise_server_exceptions=False) as b:
                with TestClient(enabled, raise_server_exceptions=False) as a:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(a.get, "/overlap")
                        second = pool.submit(b.get, "/overlap")
                        self.assertEqual(first.result(timeout=10).status_code, 500)
                        self.assertEqual(second.result(timeout=10).status_code, 500)
                    self.assertEqual(len(observed_clients), 4)
                    for expected, actual in observed_clients:
                        self.assertIs(actual, expected)
                    for source, environment in (
                        (active_transport, "staging"),
                        (other_transport, "production"),
                    ):
                        events = [
                            item.get_event()
                            for envelope in source.envelopes
                            for item in envelope.items
                        ]
                        self.assertTrue(events)
                        self.assertEqual(
                            {event["environment"] for event in events}, {environment}
                        )
                    self.assertEqual(a.get("/error").status_code, 500)
                    count = len(active_transport.envelopes)
                    self.assertGreater(count, 0)
                    for local in disabled:
                        with TestClient(
                            local, raise_server_exceptions=False
                        ) as browser:
                            self.assertEqual(browser.get("/error").status_code, 500)
                        self.assertEqual(len(active_transport.envelopes), count)
                        self.assertEqual(len(foreign_transport.envelopes), 0)
                    self.assertEqual(b.get("/error").status_code, 500)
                    self.assertEqual(a.get("/error").status_code, 500)
                self.assertTrue(active_transport.killed)
                self.assertFalse(other_transport.killed)
                before = len(other_transport.envelopes)
                self.assertEqual(b.get("/error").status_code, 500)
                self.assertGreater(len(other_transport.envelopes), before)
            self.assertTrue(other_transport.killed)
            self.assertIs(global_scope.client, foreign)
            self.assertEqual(len(foreign_transport.envelopes), 0)
            # Re-entering the same app refreshes only its closed client.
            with TestClient(enabled, raise_server_exceptions=False) as a:
                reopened = enabled.state.error_monitoring_client.transport
                self.assertIsNot(reopened, active_transport)
                self.assertEqual(a.get("/error").status_code, 500)
            self.assertTrue(reopened.killed)
            self.assertTrue(reopened.envelopes)

            # Startup failure still closes its own client; no event escapes to
            # the existing foreign client or another app.
            @asynccontextmanager
            async def fails_startup(app):
                raise RuntimeError(SECRET)
                yield

            failing = app("staging", lifespan=fails_startup)
            failed_transport = failing.state.error_monitoring_client.transport
            with self.assertRaises(RuntimeError):
                with TestClient(failing):
                    pass
            self.assertTrue(failed_transport.killed)
            self.assertEqual(len(foreign_transport.envelopes), 0)
            for observed in transports:
                outbound = b"\n".join(
                    envelope.serialize() for envelope in observed.envelopes
                )
                self.assertNotIn(SECRET.encode(), outbound)


if __name__ == "__main__":
    unittest.main()

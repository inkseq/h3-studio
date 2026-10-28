"""Model-call tracing with a fake Langfuse client: no network, no credentials."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import langfuse_config, llm_tracing, protected_config


def valid():
    return {
        "profile": langfuse_config.PROFILE,
        "base_url": "https://us.cloud.langfuse.com",
        "public_key": "pk-lf-FAKE0000000000000000",
        "secret_key": "sk-lf-FAKE0000000000000000",
    }


class Observation:
    def __init__(self, log, name, fields):
        self.log, self.name, self.fields = log, name, fields

    def __enter__(self):
        self.log.append(("start", self.name, self.fields))
        return self

    def __exit__(self, *exc):
        self.log.append(("end", self.name))
        return False

    def update(self, **fields):
        self.log.append(("update", self.name, fields))


class FakeClient:
    def __init__(self, fail=False):
        self.log, self.fail = [], fail

    def start_as_current_observation(self, *, name, as_type="span", **fields):
        if self.fail:
            raise RuntimeError("exporter down")
        return Observation(self.log, name, {"as_type": as_type, **fields})


class TracingTests(unittest.TestCase):
    def tearDown(self):
        llm_tracing._client = None

    def test_off_without_configuration_and_body_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            llm_tracing.configure(
                SimpleNamespace(langfuse_config_file=None, data_dir=directory)
            )
        self.assertFalse(llm_tracing.enabled())
        with llm_tracing.generation("x", model="m", input="hi") as traced:
            traced.update(output="ignored")
            ran = True
        self.assertTrue(ran)

    def test_bad_credential_file_leaves_tracing_off(self):
        settings = SimpleNamespace(
            langfuse_config_file=Path("/nonexistent/langfuse"), sentry_environment=""
        )
        with self.assertLogs("studio_platform.llm_tracing", "WARNING"):
            llm_tracing.configure(settings)
        self.assertFalse(llm_tracing.enabled())

    def test_generation_records_model_input_output_and_usage(self):
        llm_tracing._client = client = FakeClient()
        with llm_tracing.generation(
            "gemini-generate", model="gemini-3.8-flash", input="hello"
        ) as traced:
            traced.update(output="world", usage_details={"input": 3, "output": 1})
        start, update, end = client.log
        self.assertEqual(start[1], "gemini-generate")
        self.assertEqual(start[2]["as_type"], "generation")
        self.assertEqual(start[2]["model"], "gemini-3.8-flash")
        self.assertEqual(update[2]["usage_details"], {"input": 3, "output": 1})
        self.assertEqual(end, ("end", "gemini-generate"))

    def test_errors_are_marked_and_reraised(self):
        llm_tracing._client = client = FakeClient()
        error = ValueError("boom")
        error.code = "empty_or_blocked"
        with self.assertRaises(ValueError):
            with llm_tracing.observe("story-chat-turn"):
                raise error
        update = [row for row in client.log if row[0] == "update"][0]
        self.assertEqual(
            update[2], {"level": "ERROR", "status_message": "empty_or_blocked"}
        )
        self.assertEqual(client.log[-1], ("end", "story-chat-turn"))

    def test_tracing_failure_never_breaks_the_request(self):
        llm_tracing._client = FakeClient(fail=True)
        with self.assertLogs("studio_platform.llm_tracing", "WARNING"):
            with llm_tracing.generation("x", model="m") as traced:
                traced.update(output="still fine")
                result = 42
        self.assertEqual(result, 42)

    def test_people_are_hashed_and_text_is_bounded(self):
        self.assertEqual(llm_tracing.person("qa-user"), llm_tracing.person("qa-user"))
        self.assertNotIn("qa-user", llm_tracing.person("qa-user"))
        self.assertIsNone(llm_tracing.person(""))
        self.assertEqual(len(llm_tracing.clip("x" * 10, 4)), 5)
        self.assertEqual(llm_tracing.clip({"a": "é"}), '{"a": "é"}')


class LangfuseConfigTests(unittest.TestCase):
    def test_closed_contract_and_secret_safe_repr(self):
        credential = langfuse_config.validate(valid())
        self.assertNotIn("sk-lf", repr(credential))
        for field, value in (
            ("profile", "langfuse--other"),
            ("base_url", "https://evil.example"),
            ("public_key", "sk-lf-FAKE0000000000000000"),
            ("secret_key", "sk-lf-FAKE 000000000000000"),
            ("secret_key", "sk-lf-x"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(langfuse_config.LangfuseConfigError),
            ):
                langfuse_config.validate({**valid(), field: value})
        with self.assertRaises(langfuse_config.LangfuseConfigError):
            langfuse_config.validate({**valid(), "extra": 1})

    def test_reads_a_protected_file_and_refuses_duplicates(self):
        def protected(path):
            real = path.stat()
            return SimpleNamespace(
                st_mode=0o100440,
                st_uid=0,
                st_nlink=1,
                st_size=real.st_size,
                st_dev=real.st_dev,
                st_ino=real.st_ino,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "langfuse"
            path.write_text(json.dumps(valid()))
            with patch.object(
                protected_config.os, "fstat", return_value=protected(path)
            ):
                self.assertEqual(
                    langfuse_config.runtime_config(path).base_url,
                    "https://us.cloud.langfuse.com",
                )
            path.write_text('{"profile": 1, "profile": 2}')
            with patch.object(
                protected_config.os, "fstat", return_value=protected(path)
            ):
                with self.assertRaises(langfuse_config.LangfuseConfigError):
                    langfuse_config.runtime_config(path)
        with self.assertRaises(langfuse_config.LangfuseConfigError):
            langfuse_config.runtime_config("relative/path")


if __name__ == "__main__":
    unittest.main()

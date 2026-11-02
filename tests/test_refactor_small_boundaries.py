"""Local failure characterization for unchanged media and demonstration paths."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from studio_platform import media
from studio_platform.assets import AssetService
from studio_platform.render_backend import validate_render_request
from studio_platform.worker import BackendError, MockBackend, SubmissionRejected


class SmallModuleBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_mock_process_timeout_leaves_no_receipt_and_unknown_original(self):
        backend = MockBackend(self.root, enabled=True)
        with (
            patch.object(
                subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 180)
            ),
            self.assertRaisesRegex(SubmissionRejected, "mock_cpu_generation_failed"),
        ):
            backend.submit({"shape": (256, 256, 1, False)}, "original")
        self.assertFalse((self.root / "original.json").exists())
        self.assertEqual(backend.reconcile("original").state, "unknown")

    def test_mock_corrupt_receipt_is_not_success_and_is_never_replaced(self):
        backend = MockBackend(self.root, enabled=True)
        receipt = self.root / "original.json"
        original = json.dumps({"task_id": "mock-foreign", "simulation": True})
        receipt.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(BackendError, "invalid_mock_receipt"):
            backend.reconcile("original")
        self.assertEqual(receipt.read_text(encoding="utf-8"), original)

    def derivation(self):
        service = AssetService.__new__(AssetService)
        service.staging_dir = self.root
        service.max_bytes = 1024
        service.journal = SimpleNamespace(save=Mock())
        service.store = Mock()
        source = b"original immutable source"
        receipt = {
            "id": "a" * 32,
            "suffix": ".mp4",
            "asset": {"selection": {"start": 0, "end": 2}},
            "derivation": {
                "input_name": "derivation-input.mp4",
                "original": {
                    "size_bytes": len(source),
                    "sha256": hashlib.sha256(source).hexdigest(),
                },
                "metadata": {},
                "phase": "planned",
            },
        }
        directory = service._stage(receipt)
        directory.mkdir()
        borrowed = directory / "derivation-input.mp4"
        borrowed.write_bytes(source)
        return service, receipt, directory, borrowed

    def test_empty_derivation_preserves_borrowed_source_and_original_phase(self):
        service, receipt, directory, borrowed = self.derivation()
        output = directory / "trimmed.mp4"
        output.write_bytes(b"")
        before = copy.deepcopy(receipt)
        with (
            patch.object(media, "derive", return_value=output),
            self.assertRaises(media.MediaError),
        ):
            service._prepare_derivation(receipt)
        self.assertEqual(receipt, before)
        self.assertEqual(borrowed.read_bytes(), b"original immutable source")
        service.journal.save.assert_not_called()
        service.store.open.assert_not_called()

    def test_derivation_save_failure_keeps_both_original_and_published_staging(self):
        service, receipt, directory, borrowed = self.derivation()
        output = directory / "trimmed.mp4"
        output.write_bytes(b"derived bytes")
        service.journal.save.side_effect = OSError("local journal unavailable")
        with (
            patch.object(media, "derive", return_value=output),
            self.assertRaisesRegex(OSError, "local journal unavailable"),
        ):
            service._prepare_derivation(receipt)
        self.assertEqual(borrowed.read_bytes(), b"original immutable source")
        self.assertEqual((directory / "source.mp4").read_bytes(), b"derived bytes")
        self.assertEqual(receipt["derivation"]["phase"], "ready")
        service.journal.save.assert_called_once_with(receipt)
        service.store.open.assert_not_called()

    @staticmethod
    def render_payload():
        return {
            "recipe_id": "chapter-roughcut-v1",
            "request": {
                "duration": 1,
                "generate_audio": False,
                "export_crf": 18,
                "render": {
                    "version": 1,
                    "shots": [{"shot_id": "shot", "source_id": "source", "frames": 24}],
                    "audio_tracks": [],
                },
            },
            "output_spec": {"width": 256, "height": 256, "fps": 24, "frame_count": 24},
            "sources": {
                "source": {
                    "kind": "video",
                    "simulation": False,
                    "object": {
                        "size_bytes": 16,
                        "sha256": "a" * 64,
                        "content_type": "video/mp4",
                        "key": "owners/owner/original/source.mp4",
                    },
                }
            },
        }

    def test_render_input_limits_accept_exact_cap_and_reject_one_beyond(self):
        payload = self.render_payload()
        before = copy.deepcopy(payload)
        self.assertEqual(
            validate_render_request(
                payload, max_source_bytes=16, max_total_input_bytes=16
            )["frames"],
            24,
        )
        for arguments, code in (
            ({"max_source_bytes": 15}, "render_source_size_limit"),
            ({"max_total_input_bytes": 15}, "render_total_input_size_limit"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(BackendError, code):
                validate_render_request(payload, **arguments)
        self.assertEqual(payload, before)

    def test_render_nan_duration_refuses_without_mutating_payload(self):
        payload = self.render_payload()
        payload["request"]["duration"] = float("nan")
        with self.assertRaisesRegex(BackendError, "render_duration_mismatch"):
            validate_render_request(payload)
        self.assertEqual(payload["request"]["render"]["shots"][0]["frames"], 24)


class SmallModuleImportCompatibilityTests(unittest.TestCase):
    def test_worker_and_render_exports_are_the_same_original_contract_objects(self):
        from studio_platform import render_backend, render_request, worker
        from studio_platform.inference import mock, protocol

        self.assertIs(worker.MockBackend, mock.MockBackend)
        self.assertEqual(worker.MockBackend.__module__, "studio_platform.worker")
        for name in (
            "NotReady",
            "Outcome",
            "SubmissionRejected",
            "SubmissionUncertain",
            "TAG",
        ):
            self.assertIs(getattr(worker, name), getattr(protocol, name))
        self.assertIs(render_backend.BackendError, protocol.BackendError)
        for name in (
            "RECIPE",
            "MIMES",
            "SOURCE_ID",
            "validate_render_request",
            "validate_subtitles",
        ):
            self.assertIs(getattr(render_backend, name), getattr(render_request, name))

    def test_projection_error_keeps_original_public_identity(self):
        from studio_platform import (
            quick_chat_migration,
            quick_chat_migration_projection,
        )

        self.assertIs(
            quick_chat_migration.MigrationError,
            quick_chat_migration_projection.MigrationError,
        )
        self.assertEqual(
            quick_chat_migration.MigrationError.__module__,
            "studio_platform.quick_chat_migration",
        )
        self.assertIs(
            quick_chat_migration._group_shots,
            quick_chat_migration_projection._group_shots,
        )


if __name__ == "__main__":
    unittest.main()

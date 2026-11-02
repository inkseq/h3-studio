"""Explicit local CPU simulation adapter; no provider calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from ..media import ffmpeg
from ..providers.registry import MOCK
from .outputs import _shape
from .protocol import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
    TAG,
)


class MockBackend:
    """Local CPU demonstration, not an AI model or video benchmark."""

    def __init__(self, state_dir, *, enabled=False):
        self.enabled = bool(enabled)
        self.state_dir = Path(state_dir).resolve()
        self.slot_key = (
            "mock-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()
        )
        self.kind = MOCK.id

    def prepare(self, job, tag, store, heartbeat):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        return dict(shape=_shape(job), tag=tag)

    def submit(self, prepared, tag):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        receipt = self.state_dir / (tag + ".json")
        if receipt.exists():
            raise SubmissionUncertain("mock_submission_already_recorded")
        width, height, duration, audio = prepared["shape"]
        video = self.state_dir / (tag + ".mp4")
        # Pillow bundles its default font. Do not depend on a host Fontconfig or
        # silently omit the watermark when an OS font cannot be found.
        from PIL import Image, ImageDraw, ImageFont

        frame = Image.new("RGB", (width, height), (23, 32, 66))
        draw = ImageDraw.Draw(frame)
        for label, size, y, color in (
            ("SIMULATION", max(18, width // 14), height // 2, "yellow"),
            ("CPU demo - NOT H3", max(12, width // 24), height - 40, "white"),
        ):
            font = ImageFont.load_default(size=size)
            box = draw.textbbox((0, 0), label, font=font)
            draw.text(
                ((width - (box[2] - box[0])) / 2, y), label, fill=color, font=font
            )
        image = self.state_dir / (tag + "-simulation.png")
        frame.save(image)
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "24",
            "-i",
            str(image),
        ]
        if audio:
            command += [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=32000:duration={duration}",
            ]
        command += [
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
        command += ["-c:a", "aac", "-ar", "32000", "-ac", "2"] if audio else ["-an"]
        command += ["-movflags", "+faststart", str(video)]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=180)
            if audio:
                ffmpeg(
                    [
                        "-i",
                        video,
                        "-vn",
                        "-c:a",
                        "flac",
                        self.state_dir / (tag + ".flac"),
                    ]
                )
            task_id = "mock-" + tag
            payload = {
                "task_id": task_id,
                "tag": tag,
                "simulation": True,
                "audio": audio,
            }
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return task_id
        except (OSError, subprocess.SubprocessError, ValueError):
            raise SubmissionRejected("mock_cpu_generation_failed") from None

    def reconcile(self, tag, task_id=None):
        if not TAG.fullmatch(tag):
            raise BackendError("invalid_attempt_tag")
        if task_id is not None and task_id != "mock-" + tag:
            raise BackendError("mock_task_identity_mismatch")
        receipt = self.state_dir / (tag + ".json")
        if not receipt.exists():
            return Outcome("unknown")
        try:
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if result["task_id"] != "mock-" + tag or result["simulation"] is not True:
                raise ValueError
        except (ValueError, KeyError):
            raise BackendError("invalid_mock_receipt") from None
        return Outcome("succeeded", result["task_id"], 0)

    def poll(self, tag, task_id):
        return self.reconcile(tag, task_id)

    def cancel(self, tag, task_id):
        # CPU submit finishes synchronously: already completed results are retained.
        return False

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        outcome = self.reconcile(tag, task_id)
        if outcome.state != "succeeded":
            raise BackendError("mock_output_unknown")
        files = {"video": self.state_dir / (tag + ".mp4")}
        if _shape(job)[3]:
            files["audio"] = self.state_dir / (tag + ".flac")
        return files


# Retain historical class identity for imports and serialized references.
MockBackend.__module__ = "studio_platform.worker"

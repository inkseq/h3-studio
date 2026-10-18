"""Durable, bounded first/last and Ref2VA qualification on an existing endpoint.

The caller owns the instance lock, deadline/budget, and SSH lifecycle. No cloud
creation occurs here. Once submission may have happened, retries only reconcile
and collect that same tag. Qualification is inference/shape proof, not quality.
"""

from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path

from .media import ffmpeg, inspect, probe
from .repository import request_hash
from .worker import SubmissionRejected

REFERENCE_VIDEO_DURATION = 107 / 24
# One AAC frame at the fixture's 32kHz rate plus MP4 millisecond rounding.
# Applies only to this controller-owned fixture, never user input admission.
FIXTURE_CONTAINER_DURATION_TOLERANCE = 1024 / 32000 + 0.001


class BoundedInputSmoke:
    name = ""
    mode = ""

    def __init__(
        self, backend, clock, save, verify, *, can_submit=None, collection_context=None
    ):
        self.backend, self.clock, self.save, self.verify = backend, clock, save, verify
        self.can_submit = can_submit or (lambda: None)
        self.collection_context = collection_context or (lambda: nullcontext(True))

    def request(self, tag):
        raise NotImplementedError

    def prepare(self, directory, target, bootstrap):
        raise NotImplementedError

    def tick(self, directory, bootstrap):
        from .lium_bootstrap import BootError
        from .media import MediaError, MediaBusy
        from .worker import BackendError
        from comfy_workflow import build_workflow

        directory = Path(directory)
        target = directory / self.name
        target.mkdir(exist_ok=True)
        receipt = target / "state.json"
        tag = self.name + "-" + bootstrap["tag"]
        request = self.request(tag)
        digest = request_hash(request)
        identity = {
            "bootstrap": bootstrap["identity"],
            "fl_outputs": bootstrap["evidence"]["outputs"],
            "profile": self.name,
            "request_hash": digest,
        }
        state = (
            json.loads(receipt.read_text())
            if receipt.exists()
            else {"identity": identity, "tag": tag, "phase": "pending"}
        )
        if state.get("identity") != identity or state.get("tag") != tag:
            raise BootError("multimodal_qualification_identity_conflict")
        if state["phase"] == "qualified":
            evidence = state.get("evidence", {})
            if (
                evidence.get("qualification_profile") != self.name
                or evidence.get("request_hash") != digest
                or not evidence.get("input_evidence")
            ):
                raise BootError("multimodal_qualification_evidence_invalid")
            return {"state": "qualified", "evidence": evidence}
        if state["phase"] == "failed":
            return {"state": "qualification_failed", "qualification_stage": self.name}
        if not state.get("submission_started"):
            blocked = self.can_submit()
            if blocked:
                return {"state": blocked, "qualification_stage": self.name}
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {
                    "state": "qualification_upstream_busy",
                    "qualification_stage": self.name,
                }
            metadata, names, inputs = {}, {}, {}
            try:
                paths = self.prepare(directory, target, bootstrap)
                for ident, (path, kind) in paths.items():
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.stat().st_size > 512 * 1024**2
                    ):
                        raise BootError("multimodal_qualification_input_unavailable")
                    meta = self.fixture_metadata(path, kind)
                    self.validate_fixture(ident, meta)
                    metadata[ident] = meta
                    inputs[ident] = {
                        "filename": path.name,
                        "size_bytes": path.stat().st_size,
                        "sha256": self.file_hash(path),
                        "metadata": meta,
                    }
            except MediaBusy:
                return {
                    "state": "qualification_collection_waiting",
                    "qualification_stage": self.name,
                }
            except (
                BootError,
                MediaError,
                ValueError,
                KeyError,
                StopIteration,
                ZeroDivisionError,
            ):
                return self.fail(
                    receipt, state, "fixture_preparation_or_validation_failed"
                )
            # Preparation/probing can take time; re-check the deadline and stop
            # signal before uploading and again before the sole inference POST.
            blocked = self.can_submit()
            if blocked:
                return {"state": blocked, "qualification_stage": self.name}
            for ident, (path, kind) in paths.items():
                name = tag + "-" + ident + path.suffix
                with path.open("rb") as source:
                    uploaded = self.backend._json(
                        "POST",
                        "/upload/image",
                        files={"image": (name, source, "application/octet-stream")},
                        data={
                            "type": "input",
                            "overwrite": "true",
                            "subfolder": "sixnine-qualification",
                        },
                    )
                if (
                    uploaded.get("name") != name
                    or uploaded.get("subfolder") != "sixnine-qualification"
                    or uploaded.get("type", "input") != "input"
                ):
                    raise BootError("multimodal_qualification_upload_mismatch")
                names[ident] = "sixnine-qualification/" + name
            graph = build_workflow(request, metadata, names)
            blocked = self.can_submit()
            if blocked:
                return {"state": blocked, "qualification_stage": self.name}
            state.update(
                submission_started=self.clock(),
                phase="submitting",
                input_evidence=inputs,
            )
            self.save(receipt, state)  # Durable before inference, including lost reply.
            try:
                state["task_id"] = self.backend.submit(graph, tag)
                state["phase"] = "running"
                self.save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "failed"
                self.save(receipt, state)
                return {
                    "state": "qualification_failed",
                    "qualification_stage": self.name,
                }
            except Exception:
                return {
                    "state": "qualification_submission_unknown",
                    "qualification_stage": self.name,
                }
        task = state.get("task_id")
        result = self.backend.poll(tag, task) if task else self.backend.reconcile(tag)
        if result.task_id and not task:
            state["task_id"] = task = result.task_id
            self.save(receipt, state)
        if result.state in {"failed", "cancelled"}:
            state["phase"] = "failed"
            self.save(receipt, state)
            return {"state": "qualification_failed", "qualification_stage": self.name}
        if result.state != "succeeded" or not task:
            return {
                "state": "qualification_running"
                if result.state == "running"
                else "qualification_submission_unknown",
                "qualification_stage": self.name,
            }
        with self.collection_context() as acquired:
            if not acquired:
                return {
                    "state": "qualification_collection_waiting",
                    "qualification_stage": self.name,
                }
            try:
                paths = self.backend.fetch(
                    {"request": {"request": request}}, tag, task, target, lambda: None
                )
            except BackendError as error:
                if str(error) == "comfy_save_outputs_missing":
                    return self.fail(
                        receipt, state, "completed_inference_outputs_missing"
                    )
                raise
            try:
                evidence = self.verify(paths, request)
            except MediaBusy:
                return {
                    "state": "qualification_collection_waiting",
                    "qualification_stage": self.name,
                }
            except (BootError, BackendError, MediaError):
                return self.fail(receipt, state, "collected_output_validation_failed")
        evidence.update(
            scope=self.name + "_single_host_inference_not_quality_or_all_controls",
            qualification_profile=self.name,
            request_hash=digest,
            input_evidence=state["input_evidence"],
            elapsed_wall_seconds=self.clock() - state["submission_started"],
            completed_at=self.clock(),
        )
        state.update(phase="qualified", evidence=evidence)
        self.save(receipt, state)
        return {"state": "qualified", "evidence": evidence}

    def fail(self, receipt, state, reason):
        state.update(phase="failed", failure=reason)
        self.save(receipt, state)
        return {"state": "qualification_failed", "qualification_stage": self.name}

    @staticmethod
    def fixture_metadata(path, kind):
        meta = inspect(path, kind)
        if kind == "video":
            stream = next(
                s for s in probe(path)["streams"] if s.get("codec_type") == "video"
            )
            numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
            fps, frames = numerator / denominator, int(stream["nb_frames"])
            # Keep the observed container duration as separate evidence. MP4
            # muxers can round to milliseconds or retain one AAC padding frame;
            # the actual reference video is exactly the decoded frame grid.
            meta.update(
                source_duration=meta["duration"],
                duration=frames / fps,
                fps=fps,
                frame_count=frames,
            )
        return meta

    @staticmethod
    def validate_fixture(ident, meta):
        from .lium_bootstrap import BootError

        kind = meta.get("kind")
        valid = meta.get("model_ready", True) is True
        if kind == "image":
            valid = valid and meta.get("width") == 2048 and meta.get("height") == 2048
        elif kind == "video":
            valid = valid and (
                meta.get("width"),
                meta.get("height"),
                meta.get("fps"),
                meta.get("frame_count"),
            ) == (832, 480, 24, 107)
            observed = meta.get("source_duration", meta.get("duration"))
            valid = (
                valid
                and abs(meta.get("duration", 0) - REFERENCE_VIDEO_DURATION) <= 1e-6
                and type(observed) in (int, float)
                and math.isfinite(observed)
                and abs(observed - REFERENCE_VIDEO_DURATION)
                <= FIXTURE_CONTAINER_DURATION_TOLERANCE
                and meta.get("has_audio") is True
            )
        elif kind == "audio":
            valid = (
                valid
                and abs(meta.get("duration", 0) - 4.45) <= 1e-6
                and meta.get("has_audio") is True
            )
        else:
            valid = False
        if not valid:
            raise BootError("multimodal_qualification_fixture_shape_mismatch")

    @staticmethod
    def file_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def image(path, *, last=False):
        from PIL import Image, ImageDraw

        if not path.exists():
            canvas = Image.new("RGB", (2048, 2048), "navy")
            draw = ImageDraw.Draw(canvas)
            draw.rectangle(
                (512 if not last else 704, 512, 1536 if not last else 1728, 1536),
                fill="red",
            )
            draw.ellipse(
                (832 if not last else 1024, 832, 1216 if not last else 1408, 1216),
                fill="white",
            )
            canvas.save(path)


class FirstLastSmoke(BoundedInputSmoke):
    name = "firstlast4-768p-5s-v1"
    mode = "fl"

    def request(self, tag):
        return {
            "mode": "fl",
            "prompt": "A red object moves slowly right across a navy background, gentle ambient sound.",
            "duration": 5,
            "resolution": "768P",
            "aspect_ratio": "16:9",
            "steps": 4,
            "seed": "34567",
            "generate_audio": True,
            "video_decode": "tiled",
            "encoder_device": "cpu",
            "_job_id": tag,
            "inputs": {"first_frame": "first", "last_frame": "last"},
        }

    def prepare(self, directory, target, bootstrap):
        first, last = target / "first.png", target / "last.png"
        self.image(first)
        self.image(last, last=True)
        return {"first": (first, "image"), "last": (last, "image")}


class BoundedReferenceSmoke(BoundedInputSmoke):
    name = "ref4-bounded-768p-5s-v1"
    mode = "ref"

    def request(self, tag):
        return {
            "mode": "ref",
            "prompt": "A red ceramic teapot on a wooden table, matching reference motion and gentle ambient sound.",
            "duration": 5,
            "resolution": "768P",
            "aspect_ratio": "16:9",
            "steps": 4,
            "seed": "23456",
            "generate_audio": True,
            "video_decode": "tiled",
            "encoder_device": "cpu",
            "_job_id": tag,
            "inputs": {
                "images": ["own-image"],
                "videos": ["fl-video"],
                "audios": ["fl-audio"],
            },
            "video_audio": {"fl-video": True},
            "guides": [
                {"media_id": "own-image", "time_seconds": 1, "use_audio": False}
            ],
        }

    def prepare(self, directory, target, bootstrap):
        from .lium_bootstrap import BootError

        source_video, source_audio = directory / "raw.mp4", directory / "raw.flac"
        for kind, source in (("video", source_video), ("audio", source_audio)):
            evidence = bootstrap["evidence"]["outputs"][kind]
            if (
                evidence.get("filename") != source.name
                or source.is_symlink()
                or not source.is_file()
                or source.stat().st_size != evidence.get("size_bytes")
                or self.file_hash(source) != evidence.get("sha256")
            ):
                raise BootError("reference_source_integrity_mismatch")
        image, video, audio = (
            target / "own-pattern.png",
            target / "reference-480p.mp4",
            target / "reference-audio.wav",
        )
        self.image(image)
        # Production FL50 is 768p/5.17s. Normalize to the independently recorded
        # reference envelope; do not silently expand it by reusing a larger clip.
        if not video.exists():
            temporary = target / "reference-480p.partial.mp4"
            ffmpeg(
                [
                    "-i",
                    source_video,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-vf",
                    "scale=832:480,fps=24",
                    "-frames:v",
                    "107",
                    "-t",
                    str(107 / 24),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "32000",
                    "-ac",
                    "2",
                    temporary,
                ]
            )
            temporary.replace(video)
        if not audio.exists():
            temporary = target / "reference-audio.partial.wav"
            ffmpeg(
                [
                    "-i",
                    source_audio,
                    "-vn",
                    "-t",
                    "4.45",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "32000",
                    "-ac",
                    "2",
                    temporary,
                ]
            )
            temporary.replace(audio)
        return {
            "own-image": (image, "image"),
            "fl-video": (video, "video"),
            "fl-audio": (audio, "audio"),
        }

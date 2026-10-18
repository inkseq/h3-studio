"""One bounded Ref2VA qualification using our own FL2VA outputs as inputs.

No rental, scheduling, policy widening or credential access. Caller owns an idle
dedicated Comfy endpoint and the outer per-intent lock. This is a recipe smoke,
not exhaustive control, quality, or model-authenticity verification.
"""

import hashlib
import json
from pathlib import Path

from .media import ffmpeg, inspect, probe
from .worker import SubmissionRejected


class ReferenceSmoke:
    def __init__(
        self,
        backend,
        clock,
        save,
        verify,
        *,
        profile="smoke",
        can_submit=None,
        collection_context=None,
    ):
        if profile not in {"smoke", "full50_768p_5s", "ref4-bounded-768p-5s-v1"}:
            raise ValueError("unsupported_reference_qualification_profile")
        self.profile = profile
        self.backend, self.clock, self.save, self.verify = backend, clock, save, verify
        self.can_submit, self.collection_context = can_submit, collection_context

    def tick(self, directory, bootstrap):
        if self.profile == "ref4-bounded-768p-5s-v1":
            from .lium_multimodal_smoke import BoundedReferenceSmoke

            return BoundedReferenceSmoke(
                self.backend,
                self.clock,
                self.save,
                self.verify,
                can_submit=self.can_submit,
                collection_context=self.collection_context,
            ).tick(directory, bootstrap)
        from .lium_bootstrap import BootError
        from comfy_workflow import build_workflow

        directory = Path(directory)
        full = self.profile == "full50_768p_5s"
        target = directory / ("reference-full-smoke" if full else "reference-smoke")
        target.mkdir(exist_ok=True)
        receipt = target / "state.json"
        tag = ("ref-full-" if full else "ref-") + bootstrap["tag"]
        identity = {
            "bootstrap": bootstrap["identity"],
            "fl_outputs": bootstrap["evidence"]["outputs"],
            "qualification_profile": self.profile,
        }
        state = (
            json.loads(receipt.read_text())
            if receipt.exists()
            else {"identity": identity, "tag": tag, "phase": "pending"}
        )
        if state.get("identity") != identity:
            raise BootError("reference_qualification_identity_conflict")
        if state["phase"] == "qualified":
            return {"state": "qualified", "evidence": state["evidence"]}
        if state["phase"] == "failed":
            return {"state": "reference_qualification_failed"}
        request = {
            "mode": "ref",
            "prompt": "A red ceramic teapot on a wooden table, matching the reference motion and gentle ambient sound.",
            "duration": 5 if full else 4,
            "resolution": "768P" if full else "480P",
            "aspect_ratio": "16:9",
            "steps": 50 if full else 4,
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
        if not state.get("submission_started"):
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {"state": "reference_qualification_upstream_busy"}
            from PIL import Image, ImageDraw

            image = target / "own-pattern.png"
            if not image.exists():
                # Procedural owned fixture; not a photograph or generated-quality claim.
                canvas = Image.new("RGB", (512, 512), "navy")
                draw = ImageDraw.Draw(canvas)
                draw.rectangle((128, 128, 384, 384), fill="red")
                draw.ellipse((208, 208, 304, 304), fill="white")
                canvas.save(image)
            source_video = directory / "raw.mp4"
            source_audio = directory / "raw.flac"
            for kind, source in (("video", source_video), ("audio", source_audio)):
                evidence = bootstrap["evidence"]["outputs"][kind]
                if (
                    evidence.get("filename") != source.name
                    or source.is_symlink()
                    or not source.is_file()
                    or source.stat().st_size != evidence.get("size_bytes")
                    or self._hash(source) != evidence.get("sha256")
                ):
                    raise BootError("reference_source_integrity_mismatch")
            audio = target / "own-fl-audio.wav"
            if not audio.exists():
                ffmpeg(
                    [
                        "-i",
                        source_audio,
                        "-vn",
                        "-c:a",
                        "pcm_s16le",
                        "-ar",
                        "32000",
                        "-ac",
                        "2",
                        audio,
                    ]
                )
            paths = {"own-image": image, "fl-video": source_video, "fl-audio": audio}
            metadata, names, inputs = {}, {}, {}
            for ident, path in paths.items():
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.stat().st_size > 512 * 1024**2
                ):
                    raise BootError("reference_qualification_input_unavailable")
                kind = {"own-image": "image", "fl-video": "video", "fl-audio": "audio"}[
                    ident
                ]
                metadata[ident] = inspect(path, kind)
                if kind == "video":
                    stream = next(
                        x
                        for x in probe(path)["streams"]
                        if x.get("codec_type") == "video"
                    )
                    numerator, denominator = map(
                        int, stream["avg_frame_rate"].split("/")
                    )
                    metadata[ident].update(
                        fps=numerator / denominator,
                        frame_count=int(stream["nb_frames"]),
                    )
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
                    raise BootError("reference_qualification_upload_mismatch")
                names[ident] = "sixnine-qualification/" + name
                inputs[ident] = {
                    "filename": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": self._hash(path),
                    "metadata": metadata[ident],
                }
            graph = build_workflow(request, metadata, names)
            state.update(
                submission_started=self.clock(),
                phase="submitting",
                input_evidence=inputs,
            )
            self.save(receipt, state)
            try:
                state["task_id"] = self.backend.submit(graph, tag)
                state["phase"] = "running"
                self.save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "failed"
                self.save(receipt, state)
                return {"state": "reference_qualification_failed"}
            except Exception:
                return {"state": "reference_submission_unknown"}
        task = state.get("task_id")
        result = self.backend.poll(tag, task) if task else self.backend.reconcile(tag)
        if result.task_id and not task:
            state["task_id"] = task = result.task_id
            self.save(receipt, state)
        if result.state in {"failed", "cancelled"}:
            state["phase"] = "failed"
            self.save(receipt, state)
            return {"state": "reference_qualification_failed"}
        if result.state != "succeeded" or not task:
            return {
                "state": "reference_running"
                if result.state == "running"
                else "reference_submission_unknown"
            }
        paths = self.backend.fetch(
            {"request": {"request": request}}, tag, task, target, lambda: None
        )
        evidence = self.verify(paths, request)
        evidence.update(
            scope="single_host_ref2va_image_video_audio_and_image_guide_smoke_not_all_controls",
            qualification_profile=self.profile,
            input_evidence=state["input_evidence"],
            elapsed_wall_seconds=self.clock() - state["submission_started"],
            completed_at=self.clock(),
        )
        state.update(phase="qualified", evidence=evidence)
        self.save(receipt, state)
        return {"state": "qualified", "evidence": evidence}

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

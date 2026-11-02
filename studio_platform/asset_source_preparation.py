"""Prepare an asset receipt's borrowed source without owning its journal or lock."""

from __future__ import annotations

import os

from . import media
from .storage import IntegrityError, _copy, _sync_directory


class AssetSourcePreparation:
    def _prepare_artifact_frame(self, receipt):
        from .artifact_frames import extract

        spec = receipt["frame_extraction"]
        directory = self._stage(receipt)
        directory.mkdir(mode=0o700, exist_ok=True)
        if spec["phase"] != "ready":
            source_path = directory / "artifact-input.mp4"
            original = spec["original"]
            if source_path.exists():
                source_path = self._file(receipt, source_path.name)
                count, checksum = self._digest(source_path)
                if (count, checksum) != (original["size_bytes"], original["sha256"]):
                    raise IntegrityError("截帧源文件副本校验失败；原视频保持不变")
            else:
                receiving = directory / "receiving"
                if receiving.exists():
                    self._file(receipt, "receiving").unlink()
                actual = self.store.stat(original["key"])
                if (actual.size_bytes, actual.content_type) != (
                    original["size_bytes"],
                    original["content_type"],
                ):
                    raise IntegrityError("截帧源对象与核验记录不符；原视频保持不变")
                with (
                    self.store.open(original["key"]) as source,
                    receiving.open("xb") as target,
                ):
                    count, _ = _copy(source, target, self.max_bytes, original["sha256"])
                    target.flush()
                    os.fsync(target.fileno())
                if count != original["size_bytes"]:
                    raise IntegrityError("截帧源文件副本不完整")
                receiving.rename(source_path)
                _sync_directory(directory)
            if (directory / "extracted.png").exists():
                self._file(receipt, "extracted.png")
            target, measured = extract(
                source_path,
                directory,
                receipt["asset"]["selection"],
                max_output_bytes=self.max_bytes,
            )
            target = self._file(receipt, target.name)
            count, checksum = self._digest(target)
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            destination = directory / "source.png"
            if destination.exists():
                self._file(receipt, destination.name)
            target.replace(destination)
            _sync_directory(directory)
            receipt["asset"]["selection"].update(measured)
            receipt.update(size_bytes=count, sha256=checksum)
            spec["phase"] = "ready"
            self.journal.save(receipt)
        # Only the verified borrowed copy is removed, never the source artifact.
        input_path = directory / "artifact-input.mp4"
        if input_path.exists():
            self._file(receipt, input_path.name).unlink()
            _sync_directory(directory)

    def _prepare_derivation(self, receipt):
        spec = receipt["derivation"]
        directory = self._stage(receipt)
        directory.mkdir(mode=0o700, exist_ok=True)
        if spec["phase"] != "ready":
            source_path = directory / spec["input_name"]
            original = spec["original"]
            if source_path.exists():
                source_path = self._file(receipt, spec["input_name"])
                count, checksum = self._digest(source_path)
                if count != original["size_bytes"] or checksum != original["sha256"]:
                    raise IntegrityError("裁剪源文件副本校验失败；原始存储对象保持不变")
            else:
                # A partial receiving file is not treated as a complete source.
                # It is safe to retry this read of the existing immutable object.
                receiving = directory / "receiving"
                if receiving.exists():
                    self._file(receipt, "receiving").unlink()
                with (
                    self.store.open(original["key"]) as source,
                    receiving.open("xb") as target,
                ):
                    count, _ = _copy(source, target, self.max_bytes, original["sha256"])
                    target.flush()
                    os.fsync(target.fileno())
                if count != original["size_bytes"]:
                    raise IntegrityError("裁剪源文件副本不完整")
                receiving.rename(source_path)
                _sync_directory(directory)
            selected = receipt["asset"]["selection"]
            # All paths are private server-owned staging entries. A retry may
            # overwrite only its unfinished derived output, never an original.
            expected_name = "trimmed" + receipt["suffix"]
            if (directory / expected_name).exists():
                self._file(receipt, expected_name)
            target = media.derive(
                source_path,
                spec["metadata"],
                directory,
                selected["start"],
                selected["end"],
                max_output_bytes=self.max_bytes,
            )
            target = self._file(receipt, target.name)
            count, checksum = self._digest(target)
            if not count:
                raise media.MediaError("裁剪素材为空")
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            destination = directory / ("source" + receipt["suffix"])
            if destination.exists():
                # A crash between publication and receipt persistence may leave
                # the previous exact derived file. Never replace a linked path.
                self._file(receipt, destination.name)
            target.replace(destination)
            _sync_directory(directory)
            receipt.update(size_bytes=count, sha256=checksum)
            spec["phase"] = "ready"
            self.journal.save(receipt)
        # The source remains in its verified object store; removing this borrowed
        # processing copy keeps the normal 4*max_bytes reservation sufficient.
        input_path = directory / spec["input_name"]
        if input_path.exists():
            self._file(receipt, spec["input_name"]).unlink()
            _sync_directory(directory)

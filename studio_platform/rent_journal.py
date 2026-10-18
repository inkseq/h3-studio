"""Private durable rent markers; no credentials, request bodies or media."""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import stat
import uuid


class RentJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("rent_journal_absolute_path_required")

    def _path(self, tag):
        if not isinstance(tag, str) or str(uuid.UUID(tag)) != tag:
            raise ValueError("rent_journal_invalid_tag")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError("rent_journal_linked_directory")
        info = self.directory.stat()
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("rent_journal_directory_not_private")
        return self.directory / (tag + ".json")

    @staticmethod
    def _validate(value, tag):
        fields = {"version", "tag", "phase"}
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("tag") != tag
        ):
            raise ValueError("rent_journal_invalid")
        phase = value.get("phase")
        if (
            phase in {"post_started", "confirmed", "quarantined", "rejected"}
            and "executor_id" in value
        ):
            fields.add("executor_id")
        if phase in {"confirmed", "quarantined"}:
            fields.add("instance_id")
        if "absolute_ttl" in value and phase in {
            "post_started",
            "confirmed",
            "quarantined",
            "rejected",
        }:
            fields.add("absolute_ttl")
            RentJournal._validate_ttl(value["absolute_ttl"])
        if (
            phase
            not in {
                "checking",
                "not_submitted",
                "post_started",
                "confirmed",
                "quarantined",
                "rejected",
            }
            or set(value) != fields
        ):
            raise ValueError("rent_journal_invalid")
        for key in ("executor_id", "instance_id"):
            if key in value and (
                not isinstance(value[key], str)
                or str(uuid.UUID(value[key])) != value[key]
            ):
                raise ValueError("rent_journal_invalid_id")
        if (
            "instance_id" in value
            and value.get("absolute_ttl", {}).get("instance_id") is not None
            and value["absolute_ttl"]["instance_id"] != value["instance_id"]
        ):
            raise ValueError("rent_journal_invalid_ttl_identity")
        return value

    @staticmethod
    def _validate_ttl(value):
        fields = {
            "version",
            "created_at",
            "hard_deadline",
            "requested_hours",
            "deadline",
            "effective_deadline",
            "instance_id",
            "provider_created_at",
            "attempts",
        }
        numeric = lambda v: (
            type(v) in (int, float) and math.isfinite(v) and 0 <= v < 253402300799
        )
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["requested_hours"]) is not int
            or not 1 <= value["requested_hours"] <= 720
            or not all(
                numeric(value[k])
                for k in (
                    "created_at",
                    "hard_deadline",
                    "deadline",
                    "effective_deadline",
                )
            )
            or value["deadline"]
            != min(
                value["created_at"] + value["requested_hours"] * 3600,
                value["hard_deadline"],
            )
            or not value["created_at"]
            < value["effective_deadline"]
            <= value["deadline"]
            or not isinstance(value["attempts"], list)
            or len(value["attempts"]) > 2
        ):
            raise ValueError("rent_journal_invalid_ttl")
        if value["instance_id"] is not None:
            if str(uuid.UUID(value["instance_id"])) != value["instance_id"]:
                raise ValueError("rent_journal_invalid_ttl_identity")
            if (
                not numeric(value["provider_created_at"])
                or abs(value["provider_created_at"] - value["created_at"]) > 300
            ):
                raise ValueError("rent_journal_invalid_ttl_identity")
        elif value["provider_created_at"] is not None or value["attempts"]:
            raise ValueError("rent_journal_invalid_ttl_identity")
        for index, attempt in enumerate(value["attempts"]):
            if (
                not isinstance(attempt, dict)
                or set(attempt)
                != {"target", "started_at", "status", "acknowledged", "confirmed"}
                or not numeric(attempt["target"])
                or not numeric(attempt["started_at"])
                or not value["created_at"] < attempt["target"] <= value["deadline"]
                or attempt["status"] not in ("PENDING", "RUNNING")
                or type(attempt["acknowledged"]) is not bool
                or type(attempt["confirmed"]) is not bool
            ):
                raise ValueError("rent_journal_invalid_ttl_attempt")
            if index and (
                not value["attempts"][0]["acknowledged"]
                or not value["attempts"][0]["confirmed"]
                or value["attempts"][0]["status"] != "PENDING"
                or attempt["status"] != "RUNNING"
                or attempt["target"] > value["attempts"][0]["target"]
            ):
                raise ValueError("rent_journal_invalid_ttl_correction")
        return value

    def read(self, tag):
        path = self._path(tag)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ValueError("rent_journal_file_not_private")
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError("rent_journal_too_large")
        return self._validate(json.loads(raw), tag)

    def save(
        self, tag, phase, *, executor_id=None, instance_id=None, absolute_ttl=None
    ):
        # Creation acknowledgements must not overwrite a concurrent TTL intent
        # recovered from an earlier lost rent response.
        with self.ttl_lock(tag):
            self._save(
                tag,
                phase,
                executor_id=executor_id,
                instance_id=instance_id,
                absolute_ttl=absolute_ttl,
            )

    def _save(
        self, tag, phase, *, executor_id=None, instance_id=None, absolute_ttl=None
    ):
        path = self._path(tag)
        prior = self.read(tag)
        if (
            prior is None
            and phase != "checking"
            or prior is not None
            and (prior["phase"], phase)
            not in {
                ("checking", "not_submitted"),
                ("checking", "post_started"),
                ("post_started", "confirmed"),
                ("post_started", "rejected"),
                ("post_started", "quarantined"),
            }
        ):
            raise ValueError("rent_journal_transition_refused")
        value = {"version": 1, "tag": tag, "phase": phase}
        if prior and "absolute_ttl" in prior:
            if absolute_ttl is not None and absolute_ttl != prior["absolute_ttl"]:
                raise ValueError("rent_journal_ttl_conflict")
            value["absolute_ttl"] = prior["absolute_ttl"]
        elif absolute_ttl is not None:
            if phase != "post_started":
                raise ValueError("rent_journal_ttl_binding_requires_new_rent")
            value["absolute_ttl"] = absolute_ttl
        for key, candidate in (
            ("executor_id", executor_id),
            ("instance_id", instance_id),
        ):
            if candidate is not None:
                value[key] = str(uuid.UUID(candidate))
        self._validate(value, tag)
        if (
            prior
            and "executor_id" in prior
            and value.get("executor_id") != prior["executor_id"]
        ):
            raise ValueError("rent_journal_executor_conflict")
        self._write(path, value, prior is not None)

    def _write(self, path, value, replace):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 4096:
            raise ValueError("rent_journal_too_large")
        tag = value["tag"]
        target = (
            self.directory / (tag + "-" + str(uuid.uuid4()) + ".next")
            if replace
            else path
        )
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(target, path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def ttl_lock(self, tag):
        """One process owns scheduling; a crashed owner leaves its durable intent."""
        path = self._path(tag).with_suffix(".ttl-lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ValueError("rent_journal_lock_not_private")
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def update_ttl(self, tag, value):
        """Caller holds ttl_lock. Only an already-bound rent may be updated."""
        path, prior = self._path(tag), self.read(tag)
        if not prior or "absolute_ttl" not in prior:
            raise ValueError("rent_journal_ttl_binding_missing")
        old = prior["absolute_ttl"]
        self._validate_ttl(value)
        if (
            any(
                value[k] != old[k]
                for k in (
                    "version",
                    "created_at",
                    "hard_deadline",
                    "requested_hours",
                    "deadline",
                )
            )
            or value["effective_deadline"] > old["effective_deadline"]
            or old["instance_id"] is not None
            and any(value[k] != old[k] for k in ("instance_id", "provider_created_at"))
            or len(value["attempts"]) < len(old["attempts"])
        ):
            raise ValueError("rent_journal_ttl_extension_refused")
        for before, after in zip(old["attempts"], value["attempts"]):
            if (
                any(before[k] != after[k] for k in ("target", "started_at", "status"))
                or before["acknowledged"]
                and not after["acknowledged"]
                or before["confirmed"]
                and not after["confirmed"]
            ):
                raise ValueError("rent_journal_ttl_replay_refused")
        self._write(path, {**prior, "absolute_ttl": value}, True)

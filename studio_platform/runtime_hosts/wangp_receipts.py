"""Durable subordinate execution journal; never a business scheduler.

The state directory must be private to the runtime service. An explicit create
flag distinguishes a new slot from a missing/lost journal on an existing slot.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time

from ..inference.protocol import BackendError, NotReady
from ..inference.wangp_contract import (
    ArtifactDescriptor,
    OperationReceipt,
    PreparedRequest,
    TERMINAL,
    canonical_json,
    _identifier,
    _sha,
)


def checked_directory(path: Path, *, create=False) -> Path:
    """Reject links/reparse points in every existing ancestor, without resolving."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise BackendError("wangp_unsafe_directory")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if not create:
                raise BackendError("wangp_directory_missing") from None
            item.mkdir(mode=0o700)
            info = item.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise BackendError("wangp_unsafe_directory")
    return path


def _regular(path: Path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_nlink != 1
    ):
        raise BackendError("wangp_unsafe_file")
    return info


@contextmanager
def checked_reader(path: Path, root: Path):
    """Open a contained regular file and bind checks to the opened descriptor."""
    root = checked_directory(root)
    path = Path(path).absolute()
    if ".." in path.parts:
        raise BackendError("wangp_output_outside_root")
    try:
        path.relative_to(root)
    except ValueError:
        raise BackendError("wangp_output_outside_root") from None
    checked_directory(path.parent)
    before = _regular(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise BackendError("wangp_file_replaced")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
            after = os.fstat(fd)
            current = _regular(path)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise BackendError("wangp_file_changed")
    finally:
        os.close(fd)


def sync_directory(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


VERIFICATION_RECEIPT = "runtime-verification.json"
MAX_VERIFICATION_RECEIPT = 1024 * 1024


def _verification_identity(value, *, manifest_digest, slot_key, pid, incarnation=None):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "state",
            "manifest_digest",
            "slot_key",
            "pid",
            "incarnation",
            "verified_at",
            "evidence",
        }
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("state") != "runtime_files_verified"
        or value.get("manifest_digest") != manifest_digest
        or value.get("slot_key") != slot_key
        or type(value.get("pid")) is not int
        or value["pid"] != pid
        or pid <= 0
        or not isinstance(value.get("incarnation"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["incarnation"])
        or incarnation is not None
        and value["incarnation"] != incarnation
        or type(value.get("verified_at")) not in (int, float)
        or not math.isfinite(value["verified_at"])
        or value["verified_at"] <= 0
        or not isinstance(value.get("evidence"), dict)
        or value["evidence"].get("manifest_digest") != manifest_digest
        or value["evidence"].get("inference_verified") is not False
    ):
        raise ValueError("verification_receipt_mismatch")
    return value


def write_verification_receipt(state, host, evidence):
    """Called only after full verification while this process owns the slot.

    This receipt is an observation, never a reusable verification cache. A new
    launcher always rehashes before publishing evidence for its new incarnation.
    """
    state = checked_directory(state)
    value = _verification_identity(
        {
            "version": 1,
            "state": "runtime_files_verified",
            "manifest_digest": host.manifest.digest,
            "slot_key": host.journal.slot_key,
            "pid": os.getpid(),
            "incarnation": host.incarnation,
            "verified_at": time.time(),
            "evidence": evidence,
        },
        manifest_digest=host.manifest.digest,
        slot_key=host.journal.slot_key,
        pid=os.getpid(),
        incarnation=host.incarnation,
    )
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_VERIFICATION_RECEIPT:
        raise ValueError("verification_receipt_mismatch")
    descriptor, name = tempfile.mkstemp(
        prefix=".runtime-verification-", suffix=".tmp", dir=state
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state / VERIFICATION_RECEIPT)
        sync_directory(state)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def read_verification_receipt(
    state, *, manifest_digest, slot_key, pid, incarnation=None
):
    """Consume only the exact owned child's private receipt, never trust a path alone."""
    try:
        state = checked_directory(state)
        with checked_reader(state / VERIFICATION_RECEIPT, state) as stream:
            if os.name != "nt" and os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError()
            raw = stream.read(MAX_VERIFICATION_RECEIPT + 1)
        if len(raw) > MAX_VERIFICATION_RECEIPT:
            raise ValueError()
        return _verification_identity(
            json.loads(raw),
            manifest_digest=manifest_digest,
            slot_key=slot_key,
            pid=pid,
            incarnation=incarnation,
        )
    except Exception:
        raise ValueError("verification_receipt_mismatch") from None


class ReceiptJournal:
    # fmt: off
    """Separate SQLite connections serialize admission, including separate callers.

The process lock is acquired by the host, not a read-only journal consumer.
No request settings, prompts, credentials or runtime error strings are persisted.
"""
    # fmt: on

    def __init__(
        self, path: Path, *, slot_key: str, manifest_digest: str, create=False
    ):
        _identifier(slot_key)
        _sha(manifest_digest)
        self.path = Path(path).absolute()
        self.slot_key, self.manifest_digest = slot_key, manifest_digest
        self._host_lock = None
        self._lock_guard = threading.Lock()
        checked_directory(self.path.parent, create=create)
        if create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        elif not self.path.exists():
            raise NotReady("wangp_journal_missing")
        info = _regular(self.path)
        self._file_identity = (info.st_dev, info.st_ino)
        try:
            with self._connection() as db:
                if create:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript("""
                        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        CREATE TABLE operations (
                            operation_id TEXT PRIMARY KEY,
                            slot_key TEXT NOT NULL,
                            state TEXT NOT NULL,
                            hold_reason TEXT NOT NULL,
                            incarnation TEXT NOT NULL,
                            receipt TEXT NOT NULL
                        );
                        CREATE UNIQUE INDEX one_active_slot ON operations(slot_key)
                            WHERE state NOT IN ('succeeded','failed','cancelled');
                    """)
                    db.executemany(
                        "INSERT INTO metadata VALUES (?,?)",
                        [
                            ("version", "1"),
                            ("slot_key", slot_key),
                            ("manifest_digest", manifest_digest),
                        ],
                    )
                meta = dict(db.execute("SELECT key,value FROM metadata"))
                if meta != {
                    "version": "1",
                    "slot_key": slot_key,
                    "manifest_digest": manifest_digest,
                }:
                    raise NotReady("wangp_journal_identity_mismatch")
            if create:
                sync_directory(self.path.parent)
        except sqlite3.DatabaseError:
            raise NotReady("wangp_journal_invalid") from None

    @contextmanager
    def _connection(self):
        try:
            checked_directory(self.path.parent)
            info = _regular(self.path)
            if (info.st_dev, info.st_ino) != self._file_identity:
                raise NotReady("wangp_journal_replaced")
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        except (OSError, sqlite3.Error):
            raise NotReady("wangp_journal_unavailable") from None
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def acquire_host(self):
        with self._lock_guard:
            if self._host_lock is not None:
                raise NotReady("wangp_host_already_owned")
            path = self.path.with_name(self.path.name + ".host-lock")
            checked_directory(path.parent)
            if path.exists():
                _regular(path)
            fd = os.open(
                path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            handle = os.fdopen(fd, "r+b")
            try:
                opened = os.fstat(fd)
                current = _regular(path)
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise NotReady("wangp_host_lock_replaced")
                if opened.st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BackendError):
                handle.close()
                raise NotReady("wangp_host_slot_owned") from None
            self._host_lock = handle

    def release_host(self):
        with self._lock_guard:
            if self._host_lock is not None:
                handle, self._host_lock = self._host_lock, None
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    @staticmethod
    def _get(db, op):
        row = db.execute(
            "SELECT receipt FROM operations WHERE operation_id=?", (op,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            value.pop('_delivery_context', None)
            value.pop('_delivery', None)
            return OperationReceipt.from_dict(value)
        except (ValueError, TypeError, KeyError):
            raise NotReady("wangp_receipt_invalid") from None

    @staticmethod
    def _put(db, receipt):
        payload = receipt.to_dict()
        previous = db.execute(
            'SELECT receipt FROM operations WHERE operation_id=?',
            (receipt.operation_id,),
        ).fetchone()
        if previous is not None:
            old = json.loads(previous[0])
            for name in ('_delivery_context', '_delivery'):
                if name in old:
                    payload[name] = old[name]
        db.execute(
            """INSERT INTO operations VALUES (?,?,?,?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET state=excluded.state,
            hold_reason=excluded.hold_reason, receipt=excluded.receipt""",
            (
                receipt.operation_id,
                receipt.slot_key,
                receipt.state,
                receipt.hold_reason,
                receipt.incarnation,
                canonical_json(payload),
            ),
        )

    def get(self, op: str) -> OperationReceipt | None:
        _identifier(op)
        with self._connection() as db:
            return self._get(db, op)

    def claim(self, prepared: PreparedRequest, incarnation: str):
        _identifier(incarnation)
        with self._connection() as db:
            existing = self._get(db, prepared.operation_id)
            if existing is not None:
                if existing.identity_digest != prepared.identity_digest:
                    existing = replace(existing, hold_reason="wangp_identity_conflict")
                    self._put(db, existing)
                return existing, False
            if prepared.manifest_digest != self.manifest_digest:
                raise NotReady("wangp_manifest_mismatch")
            if db.execute(
                "SELECT 1 FROM operations WHERE state NOT IN ('succeeded','failed','cancelled') OR hold_reason!='' LIMIT 1"
            ).fetchone():
                raise NotReady("wangp_slot_obligation_pending")
            receipt = OperationReceipt(
                prepared.operation_id,
                prepared.job_id,
                prepared.attempt_tag,
                prepared.request_hash,
                prepared.manifest_digest,
                prepared.identity_digest,
                self.slot_key,
                incarnation,
                "prepared",
                prepared.generate_audio,
            )
            self._put(db, receipt)
            return receipt, True

    def transition(
        self,
        op: str,
        *,
        expected,
        state: str,
        stop_proven=False,
        reason="",
        artifacts: tuple[ArtifactDescriptor, ...] = (),
    ):
        with self._connection() as db:
            receipt = self._get(db, op)
            if receipt is None:
                raise NotReady("wangp_operation_missing")
            if receipt.state not in expected or receipt.hold_reason:
                return receipt
            if receipt.state in TERMINAL:
                raise BackendError("wangp_terminal_immutable")
            updated = replace(
                receipt,
                state=state,
                stop_proven=stop_proven,
                reason=reason,
                artifacts=artifacts,
            )
            self._put(db, updated)
            return updated

    def save_delivery_context(self, prepared):
        """Keep only original geometry in the existing record; never a new prompt."""
        with self._connection() as db:
            receipt = self._get(db, prepared.operation_id)
            if receipt is None or receipt.identity_digest != prepared.identity_digest:
                raise NotReady('wangp_delivery_identity_mismatch')
            raw = json.loads(
                db.execute(
                    'SELECT receipt FROM operations WHERE operation_id=?',
                    (prepared.operation_id,),
                ).fetchone()[0]
            )
            context = {'output_spec': json.loads(prepared.output_spec_json)}
            if '_delivery_context' in raw and raw['_delivery_context'] != context:
                raise NotReady('wangp_delivery_context_mismatch')
            raw['_delivery_context'] = context
            db.execute(
                'UPDATE operations SET receipt=? WHERE operation_id=?',
                (canonical_json(raw), prepared.operation_id),
            )

    def delivery_context(self, op):
        _identifier(op)
        with self._connection() as db:
            row = db.execute(
                'SELECT receipt FROM operations WHERE operation_id=?', (op,)
            ).fetchone()
            value = json.loads(row[0]).get('_delivery_context') if row else None
            if (
                type(value) is not dict
                or set(value) != {'output_spec'}
                or type(value['output_spec']) is not dict
            ):
                raise NotReady('wangp_delivery_context_missing')
            return value

    def delivery(self, op):
        from ..inference.wangp_delivery import validate_delivery_receipt

        _identifier(op)
        with self._connection() as db:
            row = db.execute(
                'SELECT receipt FROM operations WHERE operation_id=?', (op,)
            ).fetchone()
            value = json.loads(row[0]).get('_delivery') if row else None
            return validate_delivery_receipt(value) if value is not None else None

    def save_delivery(self, op, value):
        from ..inference.wangp_delivery import validate_delivery_receipt

        value = validate_delivery_receipt(value)
        with self._connection() as db:
            receipt = self._get(db, op)
            if (
                receipt is None
                or receipt.state != 'succeeded'
                or not receipt.stop_proven
                or receipt.hold_reason
            ):
                raise NotReady('wangp_delivery_original_output_unavailable')
            identity = {name: getattr(receipt, name) for name in value['identity']}
            if (
                value['identity'] != identity
                or value['generate_audio'] is not receipt.generate_audio
            ):
                raise NotReady('wangp_delivery_identity_mismatch')
            raw = json.loads(
                db.execute(
                    'SELECT receipt FROM operations WHERE operation_id=?', (op,)
                ).fetchone()[0]
            )
            old = raw.get('_delivery')
            if old is not None:
                old = validate_delivery_receipt(old)
                if old['fingerprint'] != value['fingerprint']:
                    raise NotReady('wangp_delivery_fingerprint_mismatch')
                if old['roles'] and value['roles'] != old['roles']:
                    raise NotReady('wangp_delivery_roles_changed')
                for kind, prior in old['objects'].items():
                    current = value['objects'].get(kind)
                    if current is None or any(
                        current[name] != prior[name]
                        for name in ('key', 'storage_binding')
                    ):
                        raise NotReady('wangp_delivery_storage_binding_changed')
            raw['_delivery'] = value
            db.execute(
                'UPDATE operations SET receipt=? WHERE operation_id=?',
                (canonical_json(raw), op),
            )
            return value

    def request_cancel(self, op: str):
        with self._connection() as db:
            receipt = self._get(db, op)
            if receipt is None or receipt.state in TERMINAL:
                return receipt
            receipt = replace(receipt, cancel_requested=True)
            self._put(db, receipt)
            return receipt

    def recover(self, incarnation: str):
        """Old in-memory handles are gone; preserve uncertainty without replay."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT operation_id FROM operations WHERE incarnation!=? AND state NOT IN ('succeeded','failed','cancelled')",
                (incarnation,),
            ).fetchall()
            for (op,) in rows:
                receipt = self._get(db, op)
                self._put(
                    db, replace(receipt, state="unknown", reason="wangp_host_restarted")
                )

    def has_obligations(self):
        with self._connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM operations WHERE state NOT IN ('succeeded','failed','cancelled') OR hold_reason!='' LIMIT 1"
                ).fetchone()
                is not None
            )

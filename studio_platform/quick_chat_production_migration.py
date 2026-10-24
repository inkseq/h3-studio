"""Closed operator production entry for #481; no route, schema or new store.

The host must qualify the manifest and reader receipts against real promotions.
Hashes here detect drift; self-supplied JSON is not proof of owner authorization.
The staging entry remains independent and cannot be used with production settings.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
import time

from .quick_chat_migration import _apply, require
from .repository import canonical, request_hash

ROW_KEYS = {
    "index",
    "owner",
    "session_id",
    "project_id",
    "session_version",
    "project_version",
    "source_hash",
    "project_hash",
    "target_hash",
    "session_row_hash",
    "raw_children_hash",
    "counts",
    "archive_payload_bytes",
}
READER_FLAGS = {
    "story_chat_archive",
    "versions_alias",
    "works_archive",
    "legacy_write_fence",
    "permanent_purge_guard",
}
SOURCE_FILES = (
    "studio_platform/quick_chat_migration.py",
    "studio_platform/quick_chat_production_migration.py",
    "tools/apply_approved_quick_chat_stories.py",
)


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)


def _hash(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % length, value)


def _counts(value):
    return (
        isinstance(value, dict)
        and 0 < len(value) <= 32
        and all(
            isinstance(key, str)
            and re.fullmatch(r"[a-z_]{1,64}", key)
            and type(number) is int
            and 0 <= number <= 20000
            for key, number in value.items()
        )
    )


def load_manifest(raw, expected_sha256):
    """Read the unchanged private review manifest; no runtime fields are adopted."""
    require(
        isinstance(raw, bytes)
        and 0 < len(raw) <= 131072
        and _hash(expected_sha256)
        and hashlib.sha256(raw).hexdigest() == expected_sha256,
        "production_manifest_hash_mismatch",
    )
    value = json.loads(raw)
    require(
        isinstance(value, dict)
        and value.get("environment") == "production"
        and value.get("origin") == "https://inkseq.com"
        and type(value.get("batch_size")) is int
        and value["batch_size"] == 17
        and value.get("automatic_retry") is False
        and value.get("production_apply_performed") is False
        and value.get("owner_pre_authorization_comment_id") == 6369552441
        and value.get("claude_number_acceptance_comment_id") == 6370536037,
        "production_approval_scope_invalid",
    )
    source = value.get("source_receipt")
    require(
        isinstance(source, dict)
        and _hash(source.get("sha256"))
        and _hash(source.get("snapshot_hash"))
        and _id(source.get("original_command_id")),
        "production_original_receipt_invalid",
    )
    ready, excluded = value.get("ready"), value.get("excluded")
    require(
        isinstance(ready, list)
        and len(ready) == 17
        and isinstance(excluded, list)
        and len(excluded) == 23,
        "production_closed_manifest_required",
    )
    seen, projects = set(), set()
    for index, row in enumerate(ready, 1):
        require(
            isinstance(row, dict) and set(row) == ROW_KEYS,
            "production_manifest_row_invalid",
        )
        require(
            type(row["index"]) is int
            and row["index"] == index
            and all(_id(row[key]) for key in ("owner", "session_id", "project_id"))
            and all(
                _hash(row[key])
                for key in (
                    "source_hash",
                    "project_hash",
                    "target_hash",
                    "session_row_hash",
                    "raw_children_hash",
                )
            )
            and all(
                type(row[key]) is int and row[key] >= 1
                for key in ("session_version", "project_version")
            )
            and type(row["archive_payload_bytes"]) is int
            and 0 < row["archive_payload_bytes"] <= 8 * 1024**2
            and _counts(row["counts"]),
            "production_manifest_row_invalid",
        )
        identity = row["owner"], row["session_id"]
        project = row["owner"], row["project_id"]
        require(
            identity not in seen and project not in projects,
            "production_manifest_duplicate",
        )
        seen.add(identity)
        projects.add(project)
    require(
        dict(Counter(row["owner"] for row in ready)) == value.get("per_owner")
        and sorted(value["per_owner"].values()) == [4, 13],
        "production_manifest_census_mismatch",
    )
    for row in excluded:
        require(
            isinstance(row, dict)
            and all(_id(row.get(key)) for key in ("owner", "session_id", "project_id")),
            "production_manifest_exclusion_invalid",
        )
        identity = row["owner"], row["session_id"]
        require(identity not in seen, "production_manifest_duplicate")
        seen.add(identity)
    return value


def validate_production_readers(
    value, *, manifest_sha256, tenant_id, actor_user_id, operation_id, now
):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "environment",
            "origin",
            "tenant_id",
            "actor_user_id",
            "operation_id",
            "batch_operation_id",
            "manifest_sha256",
            "checked_at",
            "current",
            "rollback",
            "source_sha256",
        }
        and value["environment"] == "production"
        and value["origin"] == "https://inkseq.com"
        and value["tenant_id"] == tenant_id
        and value["actor_user_id"] == actor_user_id
        and value["operation_id"] == operation_id
        and value["manifest_sha256"] == manifest_sha256
        and _id(value["batch_operation_id"])
        and type(value["checked_at"]) in (int, float)
        and math.isfinite(value["checked_at"])
        and 0 <= now - value["checked_at"] <= 900,
        "production_reader_proof_invalid",
    )
    pins = value["source_sha256"]
    require(
        isinstance(pins, dict)
        and set(pins) == set(SOURCE_FILES)
        and all(_hash(pin) for pin in pins.values()),
        "production_entry_source_unverified",
    )
    for name in ("current", "rollback"):
        reader = value[name]
        require(
            isinstance(reader, dict)
            and set(reader)
            == READER_FLAGS
            | {
                "platform_sha",
                "frontend_sha",
                "image_id",
                "archive_contract",
                "promotion_receipt_sha256",
                "self_check_id",
            }
            and all(_hash(reader[key], 40) for key in ("platform_sha", "frontend_sha"))
            and isinstance(reader["image_id"], str)
            and re.fullmatch(r"sha256:[a-f0-9]{64}", reader["image_id"])
            and type(reader["archive_contract"]) is int
            and reader["archive_contract"] == 2
            and all(
                type(reader[key]) is int and reader[key] == 1 for key in READER_FLAGS
            )
            and _hash(reader["promotion_receipt_sha256"])
            and _id(reader["self_check_id"]),
            "production_compatible_readers_required",
        )
    require(
        value["current"]["platform_sha"] != value["rollback"]["platform_sha"],
        "production_reader_rollback_not_distinct",
    )
    return canonical(value)


@dataclass(frozen=True)
class _ProductionGuard:
    row: dict
    readers: dict
    approval: dict

    def check(self, closure, analysis):
        row = self.row
        require(
            closure["session"]["id"] == row["session_id"]
            and closure["session"]["owner"] == row["owner"]
            and closure["session"]["version"] == row["session_version"]
            and closure["project"]["document_id"] == row["project_id"]
            and request_hash(closure["project"]["payload"]) == row["project_hash"]
            and request_hash(closure["session"]) == row["session_row_hash"]
            and request_hash(
                sorted(closure["objects"], key=lambda item: (item["kind"], item["id"]))
            )
            == row["raw_children_hash"]
            and request_hash(analysis["project"]) == row["target_hash"]
            and analysis["counts"] == row["counts"]
            and analysis["archive_payload_bytes"] == row["archive_payload_bytes"],
            "production_approved_row_changed",
        )


def apply_production_approved(
    connection,
    settings,
    owner,
    session_id,
    *,
    manifest_raw,
    manifest_sha256,
    actor_user_id,
    operation_id,
    reader_evidence,
    now=None,
):
    """Exactly one approved session. Existing/uncertain outcomes are never replayed."""
    require(
        settings.deployment_target == "production"
        and settings.public_origin == "https://inkseq.com",
        "production_environment_required",
    )
    require(
        _id(operation_id) and _id(actor_user_id),
        "production_operation_identity_required",
    )
    manifest = load_manifest(manifest_raw, manifest_sha256)
    rows = [
        row
        for row in manifest["ready"]
        if (row["owner"], row["session_id"]) == (owner, session_id)
    ]
    require(len(rows) == 1, "production_session_not_approved")
    at = time.time() if now is None else now
    readers = validate_production_readers(
        reader_evidence,
        manifest_sha256=manifest_sha256,
        tenant_id=settings.tenant_id,
        actor_user_id=actor_user_id,
        operation_id=operation_id,
        now=at,
    )
    row = rows[0]
    approval = {
        "manifest_sha256": manifest_sha256,
        "snapshot_hash": manifest["source_receipt"]["snapshot_hash"],
        "original_receipt_sha256": manifest["source_receipt"]["sha256"],
        "number_review_comment_id": manifest["claude_number_acceptance_comment_id"],
        "operation_id": operation_id,
        "batch_operation_id": readers["batch_operation_id"],
        "manifest_index": row["index"],
    }
    return _apply(
        connection,
        settings,
        owner,
        session_id,
        source_hash=row["source_hash"],
        project_version=row["project_version"],
        actor_user_id=actor_user_id,
        reader_evidence=None,
        now=at,
        production_guard=_ProductionGuard(row, readers, approval),
    )

"""Portable hosted-user metadata; never authentication responses or tokens."""

import json
import math
import time
from datetime import datetime

FILE = "workos-users.json"
MAX_BYTES = 16 * 1024 * 1024
MAX_USERS = 100000
PROFILE_FIELDS = (
    "id",
    "email",
    "email_verified",
    "first_name",
    "last_name",
    "external_id",
    "created_at",
    "updated_at",
)


def _string(value, maximum, *, optional=False):
    return (
        value is None
        and optional
        or (
            isinstance(value, str)
            and (0 if optional else 1) <= len(value) <= maximum
            and not any(ord(c) < 32 for c in value)
        )
    )


def validate(value):
    common = {"version", "provider", "client_id", "observed_at", "status", "users"}
    if (
        type(value) is not dict
        or set(value) != common
        or value["version"] != 1
        or type(value["version"]) is not int
        or value["provider"] != "workos"
        or not _string(value["client_id"], 200)
        or type(value["observed_at"]) not in (int, float)
        or not math.isfinite(value["observed_at"])
        or value["observed_at"] <= 0
        or value["status"] not in ("complete", "unavailable")
        or type(value["users"]) is not list
        or len(value["users"]) > MAX_USERS
        or value["status"] == "unavailable"
        and value["users"]
    ):
        raise ValueError("workos_backup_invalid")
    seen = set()
    for row in value["users"]:
        if (
            type(row) is not dict
            or set(row) != set(PROFILE_FIELDS) | {"identities"}
            or not _string(row["id"], 200)
            or row["id"] in seen
            or not _string(row["email"], 320)
            or type(row["email_verified"]) is not bool
            or any(
                not _string(row[field], 500, optional=True)
                for field in PROFILE_FIELDS[3:]
            )
            or type(row["identities"]) is not list
            or len(row["identities"]) > 100
        ):
            raise ValueError("workos_backup_invalid")
        seen.add(row["id"])
        for identity in row["identities"]:
            if (
                type(identity) is not dict
                or set(identity) != {"provider", "idp_id"}
                or not _string(identity["provider"], 100)
                or not _string(identity["idp_id"], 500)
            ):
                raise ValueError("workos_backup_invalid")
    return value


def snapshot(
    credentials, *, client_factory=None, clock=time.time, monotonic=time.monotonic
):
    """An unavailable provider must not suppress the durable business DB backup.

    The existing daily runner reports this coverage failure after confirming
    the business backup remotely. No upstream exception text is retained.
    """
    from .workos_config import validate_credentials

    credentials = validate_credentials(credentials)
    packet = dict(
        version=1,
        provider="workos",
        client_id=credentials["client_id"],
        observed_at=clock(),
        status="unavailable",
        users=[],
    )
    client = None
    deadline = monotonic() + 60
    try:
        if client_factory is None:
            from workos import WorkOSClient

            client_factory = WorkOSClient
        client = client_factory(
            **credentials,
            base_url="https://api.workos.com",
            request_timeout=5,
        )
        options = {"timeout": 5, "max_retries": 0}
        page = client.user_management.list_users(
            limit=100,
            order="asc",
            request_options=options,
        )
        records, seen = [], set()
        encoded_bytes = len(
            json.dumps(dict(packet, status="complete"), ensure_ascii=False).encode()
        )
        for user in page.auto_paging_iter():
            if len(records) >= MAX_USERS or monotonic() >= deadline:
                raise ValueError("workos_backup_limit")
            record = {field: getattr(user, field, None) for field in PROFILE_FIELDS}
            for field in ("created_at", "updated_at"):
                if isinstance(record[field], datetime):
                    record[field] = record[field].isoformat()
            identities = client.user_management.get_user_identities(
                user.id,
                request_options=options,
            )
            if len(identities) > 100:
                raise ValueError("workos_backup_limit")
            record["identities"] = [
                {"provider": identity.provider, "idp_id": identity.idp_id}
                for identity in identities
            ]
            validate(dict(packet, status="complete", users=[record]))
            if record["id"] in seen:
                raise ValueError("workos_backup_invalid")
            encoded_bytes += len(json.dumps(record, ensure_ascii=False).encode()) + (
                2 if records else 0
            )
            if encoded_bytes > MAX_BYTES:
                raise ValueError("workos_backup_limit")
            seen.add(record["id"])
            records.append(record)
        if monotonic() >= deadline:
            raise ValueError("workos_backup_limit")
        packet = validate(dict(packet, status="complete", users=records))
        if len(json.dumps(packet, ensure_ascii=False).encode()) > MAX_BYTES:
            raise ValueError("workos_backup_limit")
        return packet
    except Exception:
        return validate(dict(packet, status="unavailable", users=[]))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass  # Provider cleanup cannot undo the saved business snapshot.


def inspect_file(directory):
    from .backup import _digest, _regular
    from .workos_config import unique_pairs

    path = _regular(directory / FILE, max_bytes=MAX_BYTES)
    packet = validate(json.loads(path.read_bytes(), object_pairs_hook=unique_pairs))
    return dict(
        file=FILE,
        sha256=_digest(path),
        size_bytes=path.stat().st_size,
        status=packet["status"],
        user_count=len(packet["users"]),
    )


def read(directory, evidence):
    from .workos_config import unique_pairs

    if type(evidence) is not dict or inspect_file(directory) != evidence:
        raise ValueError("workos_backup_evidence_invalid")
    return validate(
        json.loads((directory / FILE).read_bytes(), object_pairs_hook=unique_pairs)
    )

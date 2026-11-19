"""Physical roles owned by the original asset receipt (not model inputs)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

ASSET_OBJECT_ROLES = ("original", "model", "thumb", "preview")


def _together(function, roles):
    """{role: result or the exception it raised}, every call finished."""
    if not roles:
        return {}
    with ThreadPoolExecutor(len(roles)) as pool:
        waiting = {role: pool.submit(function, role) for role in roles}
    results = {}
    for role, item in waiting.items():
        try:
            results[role] = item.result()
        except Exception as error:
            results[role] = error
    return results


def write_objects(service, receipt, *, interrupted=False):
    """Store the receipt's objects: the original, then its copies together.

    One object is a conditional put, a head and a read-back, about 2 s to the
    object store from the app, and a picture has four: 6 to 8 s one after
    another (staging, 2026-11-19, #1301). The original still goes first and
    alone: while its outcome is unknown nothing else is written. Its copies
    (model, thumb, preview) then go together, in the order of record
    `_write_object` keeps for one object: every intent is saved before any
    put, every put's outcome before any read-back, and what was read back
    before the receipt says verified. Threads only talk to the store; the
    receipt is changed and saved by the caller's thread. An object that needs a
    multipart session keeps every object on the one-by-one path.
    """
    roles = [role for role in ASSET_OBJECT_ROLES if role in receipt["objects"]]
    if "original" in roles:
        service._write_object(receipt, "original", interrupted=interrupted)
    _write_copies(service, receipt, [r for r in roles if r != "original"], interrupted)


def _write_copies(service, receipt, roles, interrupted):
    from .storage import (
        IntegrityError,
        ObjectAlreadyExists,
        StorageWriteUncertain,
        make_object_key,
    )

    objects, save = receipt["objects"], service.journal.save
    paths = {}
    for role in roles:
        spec = objects[role]
        if spec["phase"] == "verified":
            continue
        paths[role] = service._file(receipt, spec["filename"])
        size, checksum = service._digest(paths[role])
        if size != spec["size_bytes"] or checksum != spec["sha256"]:
            raise IntegrityError("素材暂存副本已变化，不会提交不同内容")
        if service.multipart and size > service.store.config.max_single_put_bytes:
            for each in roles:
                service._write_object(receipt, each, interrupted=interrupted)
            return

    def read_back(names):
        # Unknown outcomes only read back the existing key. Never new-key retry.
        read = _together(lambda role: service._verify_stored(objects[role]), names)
        changed = False
        for role in names:
            if not isinstance(read[role], Exception) and role in paths:
                receipt["asset"][role] = asdict(read[role])
                objects[role]["phase"] = "verified"
                changed = True
        if changed:
            save(receipt)
        for role in names:
            if isinstance(read[role], Exception):
                raise read[role]

    planned = [role for role in paths if objects[role]["phase"] == "planned"]
    # A copy whose earlier outcome is unknown is settled before a new one is put.
    read_back([role for role in roles if role not in planned])
    if not planned:
        return
    for role in planned:
        objects[role].update(
            key=make_object_key(
                receipt["owner"], receipt["id"], objects[role]["filename"]
            ),
            phase="putting",
        )
    save(receipt)  # Intents committed before the first possible object write.

    def put(role):
        spec = objects[role]
        try:
            with paths[role].open("rb") as source:
                service.store.put(
                    spec["key"],
                    source,
                    content_type=spec["content_type"],
                    max_bytes=service.max_bytes,
                    expected_sha256=spec["sha256"],
                )
        except ObjectAlreadyExists:
            pass

    outcome = _together(put, planned)
    unknown = [role for role in planned if outcome[role] is not None]
    for role in planned:
        objects[role]["phase"] = "put_unknown" if role in unknown else "verifying"
    save(receipt)
    if unknown:
        raise StorageWriteUncertain(objects[unknown[0]]["key"]) from None
    read_back(planned)

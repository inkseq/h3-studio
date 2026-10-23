"""One-step "undo last round" for story chat (owner, 2026-10-23).

Before a turn writes, the story as it was (without its chat transcript) is kept in one
side document per story, so the story itself never grows. Undo restores it only while the
story is still exactly the version that turn produced; any later edit, picture or rename
makes the undo unavailable instead of overwriting newer work. Only the latest turn can be
undone, and a successful undo forgets its snapshot.
"""

from __future__ import annotations

import copy

from .repository import Conflict, NotFound, Scope

KIND = "story_chat_undo"


def _scope(app, principal):
    return Scope(
        app.state.settings.tenant_id,
        principal.owner,
        "__story_chat_undo",
        principal.actor_id,
    )


def _current(app, principal, project_id):
    try:
        return app.state.repository.get_document(
            _scope(app, principal), KIND, project_id
        )
    except NotFound:
        return None


def _put(app, principal, project_id, payload):
    for _attempt in range(2):
        row = _current(app, principal, project_id)
        try:
            app.state.repository.put_document(
                _scope(app, principal),
                KIND,
                project_id,
                payload,
                expected_version=row["version"] if row else 0,
            )
            return
        except Conflict:
            continue


def remember(app, principal, project_id, turn_id, version, project):
    """Keep the story before this turn; the turn must then land at version + 1."""
    before = copy.deepcopy(project)
    before.get("journey", {}).pop("storyChat", None)
    _put(
        app,
        principal,
        project_id,
        {"turn_id": turn_id, "version": version, "project": before},
    )


def forget(app, principal, project_id):
    _put(app, principal, project_id, {"turn_id": None})


def available(app, principal, row):
    """The saved snapshot if the story is still exactly what its turn produced."""
    saved = _current(app, principal, row["payload"]["id"])
    if not saved:
        return None
    value = saved["payload"]
    messages = (
        row["payload"].get("journey", {}).get("storyChat", {}).get("messages", [])
    )
    last = messages[-1] if messages else None
    if (
        not value.get("turn_id")
        or last is None
        or last.get("role") != "assistant"
        or last.get("turn_id") != value["turn_id"]
        or row["version"] != value.get("version", -2) + 1
    ):
        return None
    return value


def restored(saved, chat):
    """The saved story with the current transcript minus the undone turn."""
    project = copy.deepcopy(saved["project"])
    value = copy.deepcopy(chat)
    value["messages"] = [
        m for m in value.get("messages", []) if m.get("turn_id") != saved["turn_id"]
    ]
    value["turns"] = max(0, value.get("turns", 0) - 1)
    project.setdefault("journey", {})["storyChat"] = value
    return project

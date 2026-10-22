"""Chat-first story building (#422): a chat that edits one ordinary project.

A story chat owns no table, job, asset, budget or provider. Its transcript lives in
``project.journey.storyChat`` and its cards are ordinary character/location/prop/shot
entities. Every write is one existing ``Guided.mutate`` (CAS, ownership, activity).
The model only returns a bounded JSON plan; this module validates it and turns it
into existing project actions. Paid work is never created here: a generation
proposal is advice the user confirms through the existing generation routes.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timezone

MODEL = "gemini-3.8-flash"
MAX_TEXT_CHARS = 4000
MAX_TURNS = 200
HISTORY_MESSAGES = 24
HISTORY_CHARS = 1500
MAX_ACTIONS = 40
MAX_SHOTS = 60
MAX_CARDS = 60
SECONDS = (2, 10)
KINDS = {"character": "character", "place": "location", "prop": "prop"}
TURN_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
LANGUAGES = {"zh", "en"}

INSTRUCTION = """You are Inkseq's story assistant. You help one person turn an idea into a short
film by building its cards with them: characters, places, props and shots.

Work like a good director's assistant:
- Ask at most one or two short questions per turn, only when the answer changes the film
  (who, where, mood, length). If the idea is clear enough, build cards right away.
- Keep everything the person did not ask to change. Edit cards in place; do not recreate them.
- Shots are 2-10 seconds each (default 5). A 30-second film is about six shots. Each shot text
  describes what we SEE and HEAR in that moment, concretely, in one or two sentences.
- Every shot lists the characters in it (cast), its place, and any props, by card reference.
- Character/place/prop descriptions are visual and stable (look, clothing, colours, era), so
  every shot that uses them looks consistent.
- Never claim you generated, rendered, charged or saved anything. Video generation costs
  credits and the person confirms it themselves. Use propose_generate ONLY in a turn where
  the person asks to make/generate the video or says the story is ready; then say they can
  review the cost and confirm. In other turns do not mention confirming or generating;
  instead, when the shots look complete, one chip may offer to make the video.
- Reply in the person's interface language (given with each message) unless they clearly
  write whole sentences in another language. Short words like "ok" do not change it. Keep replies short and friendly; no lists of the cards you
  made (the cards are shown next to the chat).

Return ONLY one JSON object, no code fences:
{"reply": string, "chips": [up to 3 short suggested next messages],
 "actions": [ ...tools... ]}

Tools (use existing card ids exactly as given; new cards use "new:<short-name>" refs that later
actions in the same turn may reuse):
{"tool":"set_title","title":string,"logline":string?}
{"tool":"upsert_character","ref":string,"name":string,"description":string}
{"tool":"upsert_place","ref":string,"name":string,"description":string}
{"tool":"upsert_prop","ref":string,"name":string,"description":string}
{"tool":"upsert_shot","ref":string,"text":string,"seconds":number?,"cast":[refs]?,
 "place":ref|null?,"props":[refs]?,"after":ref|null?}
   (new shots go at the end unless "after" names a shot; "after":null puts it first)
{"tool":"remove_card","ref":string}
{"tool":"propose_generate","shots":[refs] or "all"}
"""


class StoryChatError(Exception):
    def __init__(self, code, status=422, **detail):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value, limit, *, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise StoryChatError("story_chat_plan_invalid")
    return value.strip()[:limit]


def new_story(project_id, title, language):
    """A normal schema-4 project with one chapter and scene for the chat's shots."""
    chapter = "chapter-" + uuid.uuid5(uuid.NAMESPACE_URL, project_id + ":chapter").hex
    scene = "scene-" + uuid.uuid5(uuid.NAMESPACE_URL, project_id + ":scene").hex
    entity = dict(description="", version=1, status="draft", order=0)
    return {
        "schemaVersion": 4,
        "id": project_id,
        "title": title,
        "logline": "",
        "entities": [
            {
                **entity,
                "id": chapter,
                "type": "chapter",
                "parentId": None,
                "title": "Story" if language == "en" else "故事",
                "data": {},
            },
            {
                **entity,
                "id": scene,
                "type": "scene",
                "parentId": chapter,
                "title": "Scene 1" if language == "en" else "场景 1",
                "data": {"script": ""},
            },
        ],
        "links": [],
        "jobs": [],
        "journey": {
            "stage": 2,
            "storyChat": {
                "version": 1,
                "language": language,
                "sceneId": scene,
                "turns": 0,
                "messages": [],
            },
        },
        "layout": {"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
        "updatedAt": now_iso(),
    }


def chat_of(project):
    chat = project.get("journey", {}).get("storyChat")
    if not isinstance(chat, dict) or chat.get("version") != 1:
        raise StoryChatError("story_chat_not_found", 404)
    return chat


def shots_of(project, chat):
    scene = chat.get("sceneId")
    shots = [
        e for e in project["entities"] if e["type"] == "shot" and e["parentId"] == scene
    ]
    return sorted(shots, key=lambda e: (e.get("order", 0), e["id"]))


def _image(entity):
    data = entity.get("data", {})
    looks = data.get("looks") or []
    for look in looks:
        if isinstance(look, dict) and look.get("imageIds"):
            return look["imageIds"][0]
    versions = data.get("versions") or []
    for version in versions:
        if isinstance(version, dict) and version.get("imageIds"):
            return version["imageIds"][0]
    if data.get("imageIds"):
        return data["imageIds"][0]
    gallery = data.get("gallery") or []
    return gallery[0] if gallery and isinstance(gallery[0], str) else None


def cards(project, chat):
    """Compact, UI-shaped view of the story's cards; entity IDs are the card IDs."""

    def profile(entity):
        return {
            "id": entity["id"],
            "name": entity.get("title", ""),
            "text": entity.get("description", ""),
            "image_entity_id": _image(entity),
            "version": entity["version"],
        }

    roots = [e for e in project["entities"] if e["parentId"] is None]
    result = {
        "characters": [profile(e) for e in roots if e["type"] == "character"],
        "places": [profile(e) for e in roots if e["type"] == "location"],
        "props": [profile(e) for e in roots if e["type"] == "prop"],
        "shots": [],
    }
    start = 0.0
    for index, shot in enumerate(shots_of(project, chat)):
        data = shot.get("data", {})
        board = data.get("storyboard") or {}
        seconds = data.get("seconds", 5)
        seconds = seconds if type(seconds) in (int, float) else 5
        result["shots"].append(
            {
                "id": shot["id"],
                "order": index + 1,
                "text": data.get("prompt", shot.get("description", "")),
                "seconds": seconds,
                "start": round(start, 3),
                "end": round(start + seconds, 3),
                "cast": [
                    c if isinstance(c, str) else c.get("characterId")
                    for c in data.get("cast", [])
                ],
                "place": (board.get("place") or {}).get("locationId"),
                "props": list(board.get("propIds", [])),
                "selected_artifact_id": data.get("cloudArtifactId")
                or data.get("selectedArtifactId"),
                "version": shot["version"],
            }
        )
        start += seconds
    return result


def _parse(text):
    """The model's JSON plan; tolerate a code fence, nothing else."""
    raw = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    try:
        value = json.loads(raw)
    except ValueError:
        # Prose without a plan is still a useful reply; nothing is edited.
        return {"reply": text.strip()[:MAX_TEXT_CHARS], "chips": [], "actions": []}
    if not isinstance(value, dict):
        raise StoryChatError("story_chat_plan_invalid", 502)
    reply = value.get("reply")
    actions = value.get("actions", [])
    chips = value.get("chips", [])
    if (
        not isinstance(reply, str)
        or not isinstance(actions, list)
        or not isinstance(chips, list)
    ):
        raise StoryChatError("story_chat_plan_invalid", 502)
    if len(actions) > MAX_ACTIONS:
        raise StoryChatError("story_chat_plan_too_large", 502)
    return {
        "reply": reply.strip()[:MAX_TEXT_CHARS],
        "chips": [c.strip()[:80] for c in chips if isinstance(c, str) and c.strip()][
            :3
        ],
        "actions": actions,
    }


def _seconds(value):
    if type(value) not in (int, float):
        value = 5
    return min(max(value, SECONDS[0]), SECONDS[1])


def _unique(ids):
    if len(set(ids)) != len(ids):
        raise StoryChatError("story_chat_plan_invalid", 502)
    return ids


def snapshot(project):
    """Versions of what the model saw, to detect a concurrent edit of the same card."""
    versions = {e["id"]: e["version"] for e in project["entities"]}
    versions["project"] = (project.get("title"), project.get("logline", ""))
    return versions


def apply_plan(project, chat, plan):
    """Validate the plan against the current project and return existing actions.

    Raises StoryChatError for a plan that names a card that no longer exists, so a
    plan computed before a concurrent user edit is never applied to the wrong card.
    """
    lookup = {e["id"]: e for e in project["entities"]}
    refs = {}
    actions, changes = [], []
    proposal = None
    scene = chat["sceneId"]
    shots = [s["id"] for s in shots_of(project, chat)]
    removed = set()
    touched = set()  # existing cards (and "project" for the title) this plan writes

    def resolve(ref, kinds):
        if not isinstance(ref, str) or not ref:
            raise StoryChatError("story_chat_plan_invalid", 502)
        ident = refs.get(ref, ref)
        entity = lookup.get(ident)
        if entity is None or entity["type"] not in kinds or ident in removed:
            raise StoryChatError("story_chat_card_missing", 409, ref=ref)
        return ident

    def new_id(ref, kind):
        if not isinstance(ref, str) or not ref.startswith("new:") or ref in refs:
            raise StoryChatError("story_chat_plan_invalid", 502)
        ident = kind + "-" + uuid.uuid4().hex
        refs[ref] = ident
        return ident

    for item in plan["actions"]:
        if not isinstance(item, dict):
            raise StoryChatError("story_chat_plan_invalid", 502)
        tool = item.get("tool")
        if tool == "set_title":
            patch = {"title": _text(item.get("title"), 160)}
            if "logline" in item:
                patch["logline"] = _text(item.get("logline"), 2000, required=False)
            actions.append({"op": "project.update", "patch": patch})
            touched.add("project")
            changes.append({"kind": "title", "op": "update"})
        elif tool in {"upsert_character", "upsert_place", "upsert_prop"}:
            kind = KINDS[tool.removeprefix("upsert_")]
            name = _text(item.get("name"), 160)
            text = _text(item.get("description"), 4000, required=False)
            ref = item.get("ref")
            if isinstance(ref, str) and ref.startswith("new:") and ref not in refs:
                if (
                    sum(e["type"] in KINDS.values() for e in lookup.values())
                    >= MAX_CARDS
                ):
                    raise StoryChatError("story_chat_card_limit", 422)
                ident = new_id(ref, kind)
                entity = {
                    "id": ident,
                    "type": kind,
                    "parentId": None,
                    "title": name,
                    "description": text,
                    "data": {},
                }
                actions.append({"op": "entity.create", "entity": entity})
                lookup[ident] = {**entity, "version": 1}
                changes.append({"kind": kind, "op": "create", "id": ident})
            else:
                ident = resolve(ref, {kind})
                touched.add(ident)
                actions.append(
                    {
                        "op": "entity.update",
                        "entity_id": ident,
                        "patch": {"title": name, "description": text},
                    }
                )
                changes.append({"kind": kind, "op": "update", "id": ident})
        elif tool == "upsert_shot":
            ref = item.get("ref")
            creating = (
                isinstance(ref, str) and ref.startswith("new:") and ref not in refs
            )
            # A new shot gets defaults; an existing shot changes only the fields given,
            # so a text-only edit keeps its length, cast, place and props.
            data = {}
            if creating or "text" in item:
                data["prompt"] = _text(item.get("text"), 4000)
            if creating or "seconds" in item:
                data["seconds"] = _seconds(item.get("seconds", 5))
            if creating or "cast" in item:
                # The binding shape Works reads; an empty lookId uses the default look.
                data["cast"] = [
                    {"characterId": ident, "lookId": ""}
                    for ident in _unique(
                        [resolve(r, {"character"}) for r in item.get("cast") or []]
                    )
                ]

            board = {}
            if creating or "place" in item:
                place = item.get("place")
                board["place"] = (
                    {"locationId": resolve(place, {"location"})} if place else None
                )
            if creating or "props" in item:
                board["propIds"] = _unique(
                    [resolve(r, {"prop"}) for r in item.get("props") or []]
                )
            if creating:
                if len(shots) >= MAX_SHOTS:
                    raise StoryChatError("story_chat_shot_limit", 422)
                ident = new_id(ref, "shot")
                data["storyboard"] = {
                    "version": 1,
                    "mode": "ref",
                    "quality": "draft",
                    **board,
                }
                entity = {
                    "id": ident,
                    "type": "shot",
                    "parentId": scene,
                    "title": "",
                    "description": data["prompt"],
                    "data": data,
                }
                actions.append({"op": "entity.create", "entity": entity})
                lookup[ident] = {**entity, "version": 1}
                position = len(shots)
                if "after" in item:
                    after = item["after"]
                    position = (
                        0
                        if after is None
                        else shots.index(resolve(after, {"shot"})) + 1
                    )
                shots.insert(position, ident)
                changes.append({"kind": "shot", "op": "create", "id": ident})
            else:
                ident = resolve(ref, {"shot"})
                current = lookup[ident].get("data", {})
                if board:
                    # Keep the user's explicit generation choices (mode, frames, refs).
                    existing = current.get("storyboard") or {}
                    data["storyboard"] = {
                        **existing,
                        **board,
                        "version": 1,
                        "mode": existing.get("mode", "ref"),
                    }
                patch = {"data": data} if data else {}
                if "prompt" in data:
                    patch["description"] = data["prompt"]
                if patch:
                    actions.append(
                        {"op": "entity.update", "entity_id": ident, "patch": patch}
                    )
                    lookup[ident] = {**lookup[ident], "data": {**current, **data}}
                if "after" in item:
                    after = item["after"]
                    shots.remove(ident)
                    position = (
                        0
                        if after is None
                        else shots.index(resolve(after, {"shot"})) + 1
                    )
                    shots.insert(position, ident)
                touched.add(ident)
                changes.append({"kind": "shot", "op": "update", "id": ident})
        elif tool == "remove_card":
            ident = resolve(item.get("ref"), {"character", "location", "prop", "shot"})
            actions.append(
                {"op": "entity.delete", "entity_id": ident, "cascade": False}
            )
            removed.add(ident)
            touched.add(ident)
            if ident in shots:
                shots.remove(ident)
            changes.append({"kind": lookup[ident]["type"], "op": "delete", "id": ident})
        elif tool == "propose_generate":
            wanted = item.get("shots")
            ids = (
                shots
                if wanted == "all"
                else [resolve(r, {"shot"}) for r in wanted or []]
            )
            if ids:
                proposal = {"kind": "generate", "shot_ids": list(dict.fromkeys(ids))}
        else:
            raise StoryChatError("story_chat_plan_invalid", 502)
    # Guided.delete_entity already removes deleted cards from cast, place and props
    # against the evolving document, so no separate cleanup patch is emitted here.
    # Shot order is explicit so Works plays them in the chat's order.
    for index, shot_id in enumerate(shots):
        current = lookup[shot_id].get("order")
        if shot_id in refs.values():
            for action in actions:
                if (
                    action["op"] == "entity.create"
                    and action["entity"]["id"] == shot_id
                ):
                    action["entity"]["order"] = index
        elif current != index:
            touched.add(shot_id)
            actions.append(
                {"op": "entity.update", "entity_id": shot_id, "patch": {"order": index}}
            )
    if proposal:
        proposal["shot_ids"] = [s for s in proposal["shot_ids"] if s not in removed]
        proposal = proposal if proposal["shot_ids"] else None
    return actions, changes, proposal, touched


def prompt_messages(project, chat, text):
    """History plus the current cards, as alternating user/model turns."""
    history = []
    for message in chat.get("messages", [])[-HISTORY_MESSAGES:]:
        role = "user" if message.get("role") == "user" else "model"
        body = message.get("text", "")[:HISTORY_CHARS] or "…"
        if history and history[-1]["role"] == role:
            history[-1]["text"] += "\n" + body
        else:
            history.append({"role": role, "text": body})
    if history and history[0]["role"] != "user":
        history.pop(0)
    state = {
        "title": project.get("title"),
        "logline": project.get("logline", ""),
        "cards": cards(project, chat),
    }
    language = "Chinese" if chat.get("language") == "zh" else "English"
    current = (
        f"Interface language: {language}.\n"
        "Current story cards (JSON, ids are exact):\n"
        + json.dumps(state, ensure_ascii=False)[:30000]
        + "\n\nPerson says:\n"
        + text
    )
    if history and history[-1]["role"] == "user":
        history[-1]["text"] += "\n" + current
    else:
        history.append({"role": "user", "text": current})
    return history


def append_turn(chat, turn_id, text, plan, changes, proposal):
    value = copy.deepcopy(chat)
    at = now_iso()
    value["turns"] = value.get("turns", 0) + 1
    value["messages"] = value.get("messages", []) + [
        {
            "id": turn_id + "-u",
            "turn_id": turn_id,
            "role": "user",
            "text": text,
            "at": at,
        },
        {
            "id": turn_id + "-a",
            "turn_id": turn_id,
            "role": "assistant",
            "text": plan["reply"],
            "chips": plan["chips"],
            "changes": changes,
            "proposal": proposal,
            "at": at,
        },
    ]
    return value


def stored_turn(chat, turn_id):
    found = [m for m in chat.get("messages", []) if m.get("turn_id") == turn_id]
    return found or None

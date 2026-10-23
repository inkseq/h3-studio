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
import hashlib
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
{"tool":"propose_generate","shots":[refs] or ["all"]}
{"tool":"propose_new_story","idea":string}
   (when the person asks for a different, unrelated story: do NOT change this story's cards;
   propose a new story with their idea and say it will open in a new chat. Only rework this
   story when they clearly want to change or replace it)
{"tool":"propose_image","ref":string}
   (only when the person asks to draw or redraw a character, place or prop picture; the
   person confirms it and it costs credits: say they can review and confirm it, never that
   you started or made it. You cannot
   see existing pictures; describe the change in the card's description first if needed)
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


def _story_key(entity, lookup):
    """Chapter → scene → shot reading order, as Works plays the story."""
    path = []
    seen = set()
    while entity is not None and entity["id"] not in seen:
        seen.add(entity["id"])
        order = entity.get("order", 0)
        path.append((order if type(order) in (int, float) else 0, entity["id"]))
        entity = lookup.get(entity.get("parentId"))
    return tuple(reversed(path))


def shots_of(project, chat):
    """Every shot of the story in reading order. A chat-made story has one scene; an
    existing story opened in the chat (#422) may have several chapters and scenes."""
    lookup = {e["id"]: e for e in project["entities"]}
    shots = [e for e in project["entities"] if e["type"] == "shot"]
    return sorted(shots, key=lambda e: _story_key(e, lookup))


def last_scene(project):
    """The scene new shots join when an existing story is opened in the chat."""
    lookup = {e["id"]: e for e in project["entities"]}
    scenes = [e for e in project["entities"] if e["type"] == "scene"]
    return max(scenes, key=lambda e: _story_key(e, lookup))["id"] if scenes else None


def open_actions(project, language):
    """Attach an empty chat to an existing story; add a chapter and scene if it has none."""
    actions = []
    scene = last_scene(project)
    if scene is None:
        # Reuse the story's last chapter if it has one; new ids never collide.
        lookup = {e["id"]: e for e in project["entities"]}
        chapters = [e for e in project["entities"] if e["type"] == "chapter"]
        base = new_story(project["id"], "", language)["entities"]
        if chapters:
            chapter_id = max(chapters, key=lambda e: _story_key(e, lookup))["id"]
        else:
            chapter_id = "chapter-" + uuid.uuid4().hex
            actions.append(
                {
                    "op": "entity.create",
                    "entity": {
                        **{k: v for k, v in base[0].items() if k != "version"},
                        "id": chapter_id,
                    },
                }
            )
        scene = "scene-" + uuid.uuid4().hex
        actions.append(
            {
                "op": "entity.create",
                "entity": {
                    **{k: v for k, v in base[1].items() if k != "version"},
                    "id": scene,
                    "parentId": chapter_id,
                    "order": len(
                        [
                            e
                            for e in project["entities"]
                            if e["type"] == "scene" and e["parentId"] == chapter_id
                        ]
                    ),
                },
            }
        )
    chat = {
        "version": 1,
        "language": language,
        "sceneId": scene,
        "turns": 0,
        "messages": [],
        "opened": True,
    }
    return actions + [{"op": "journey.update", "patch": {"storyChat": chat}}]


def _image(entity):
    data = entity.get("data", {})
    for field, default in (
        ("looks", "defaultLookId"),
        ("versions", "defaultVersionId"),
    ):
        variants = [v for v in data.get(field) or [] if isinstance(v, dict)]
        selected = [v for v in variants if v.get("id") == data.get(default)]
        for variant in selected + variants:
            if variant.get("imageIds"):
                return variant["imageIds"][0]
    if data.get("imageIds"):
        return data["imageIds"][0]
    gallery = data.get("gallery") or []
    return gallery[0] if gallery and isinstance(gallery[0], str) else None


def cards(project, chat):
    """Compact, UI-shaped view of the story's cards; entity IDs are the card IDs."""

    lookup = {e["id"]: e for e in project["entities"]}

    def profile(entity):
        image = _image(entity)
        path = (lookup.get(image) or {}).get("data", {}).get("cloudContentPath")
        return {
            "id": entity["id"],
            "name": entity.get("title", ""),
            "text": entity.get("description", ""),
            "image_entity_id": image,
            "image_url": path
            if isinstance(path, str) and path.startswith("/v1/assets/")
            else None,
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


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def profile_data(kind):
    """The same empty profile shapes the Works storyboard editor creates."""
    if kind == "character":
        return {
            "looks": [
                {
                    "id": "main",
                    "name": "默认造型",
                    "description": "",
                    "gallery": {},
                    "imageIds": [],
                    "version": 1,
                }
            ],
            "defaultLookId": "main",
            "voices": [],
            "mainVoiceId": "",
        }
    if kind == "location":
        return {
            "versions": [
                {
                    "id": "main",
                    "name": "默认",
                    "description": "",
                    "imageIds": [],
                    "version": 1,
                }
            ],
            "defaultVersionId": "main",
            "ambient": None,
        }
    return {"imageIds": []}


def snapshot(project):
    """Content digests of what the model saw, to detect a concurrent edit of the same card.

    Bound to the card's actual content, not its nested version: a full-project browser
    save can change a title or description without bumping that version.
    """
    digests = {
        e["id"]: _digest(
            {
                k: e.get(k)
                for k in ("type", "parentId", "title", "description", "order", "data")
            }
        )
        for e in project["entities"]
    }
    digests["project"] = _digest([project.get("title"), project.get("logline", "")])
    return digests


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
    # Shots created or moved this turn; their scene follows their position.
    placed = set()
    # Created without "after": the chat's scene unless that breaks reading order.
    appended = set()
    pictures = []  # cards the person may confirm a picture for (advice only)
    new_story = None  # an idea for a separate story the person may start (advice only)
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
                    "data": profile_data(kind),
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
                # Its scene is settled after ordering, from its neighbours.
                placed.add(ident)
                if "after" not in item:
                    appended.add(ident)
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
                    placed.add(ident)
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
        elif tool == "propose_new_story":
            new_story = _text(item.get("idea"), 1000)
        elif tool == "propose_image":
            ident = resolve(item.get("ref"), {"character", "location", "prop"})
            pictures.append(ident)
        elif tool == "propose_generate":
            wanted = item.get("shots")
            if wanted == ["all"]:
                wanted = "all"
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
    # A placed shot keeps its scene (an appended one takes the chat's scene) unless that
    # would break chapter/scene reading order against its neighbours; only then does it
    # join the scene of the shot before it, or of the next unmoved shot when it is first.
    # A plan that restates the existing order therefore moves nothing between scenes.
    def scene_key(scene_id):
        entity = lookup.get(scene_id)
        return _story_key(entity, lookup) if entity else ()

    parents = {}
    for index, shot_id in enumerate(shots):
        if shot_id not in placed:
            parents[shot_id] = lookup[shot_id]["parentId"]
            continue
        before = next((parents[s] for s in reversed(shots[:index])), None)
        following = next(
            (lookup[s]["parentId"] for s in shots[index + 1 :] if s not in placed), None
        )
        created = shot_id in refs.values()
        keep = (
            None if created and shot_id not in appended else lookup[shot_id]["parentId"]
        )
        if (
            keep is not None
            and (before is None or scene_key(keep) >= scene_key(before))
            and (following is None or scene_key(keep) <= scene_key(following))
        ):
            parents[shot_id] = keep
        else:
            parents[shot_id] = before or following or scene
    for index, shot_id in enumerate(shots):
        current = lookup[shot_id].get("order")
        if shot_id in refs.values():
            for action in actions:
                if (
                    action["op"] == "entity.create"
                    and action["entity"]["id"] == shot_id
                ):
                    action["entity"]["order"] = index
                    action["entity"]["parentId"] = parents[shot_id]
        else:
            patch = {}
            if current != index:
                patch["order"] = index
            if parents[shot_id] != lookup[shot_id]["parentId"]:
                patch["parentId"] = parents[shot_id]
            if patch:
                touched.add(shot_id)
                actions.append(
                    {"op": "entity.update", "entity_id": shot_id, "patch": patch}
                )
    if proposal:
        proposal["shot_ids"] = [s for s in proposal["shot_ids"] if s not in removed]
        proposal = proposal if proposal["shot_ids"] else None
    if new_story:
        proposal = {
            **(proposal or {"kind": "new_story", "shot_ids": []}),
            "new_story": new_story,
        }
    pictures = [c for c in dict.fromkeys(pictures) if c not in removed][:4]
    if pictures:
        # Picture proposals ride on the same advice-only proposal field.
        proposal = {
            **(proposal or {"kind": "generate", "shot_ids": []}),
            "picture_card_ids": pictures,
        }
        if not proposal["shot_ids"]:
            proposal["kind"] = "image"
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

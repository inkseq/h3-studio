"""Discoverable editing contract. No credentials, model calls or storage access."""

from copy import deepcopy

from .render_limits import MAX_AUDIO_TRACKS
from .project_validation import ROLES, TYPES
from .script_windows import contract as script_windows_contract

ACTION_FIELDS = {
    "library.rename": {"target", "title"},
    "library.tag": {"targets", "tags"},
    "library.file": {"profile_id", "entity_ids", "targets", "variant_id"},
    "storyboard.bulk": {"shot_ids", "operation", "parent_id"},
    "storyboard.defaults": {"value"},
    "storyboard.restore": {"shot_id", "artifact_id"},
    "storyboard.star": {"shot_id", "artifact_id", "starred", "title"},
    "storyboard.select": {"shot_id", "artifact_id"},
    "storyboard.fork_profile": {"profile_id", "shot_ids", "new_profile_id", "title"},
    "entity.create": {"entity"},
    "entity.update": {"entity_id", "patch"},
    "entity.delete": {"entity_id", "cascade"},
    "link.create": {"link"},
    "link.delete": {"link_id"},
    "project.update": {"patch"},
    "journey.update": {"patch"},
    "layout.update": {"patch"},
    "asset.attach": {
        "asset_id",
        "entity_id",
        "title",
        "parent_id",
        "shot_id",
        "role",
        "select",
    },
    "artifact.adopt": {
        "artifact_id",
        "entity_id",
        "title",
        "parent_id",
        "shot_id",
        "select",
    },
    "shot.select": {"shot_id", "entity_id"},
    "shot.trim": {"shot_id", "start", "end"},
    "shot.script_windows.confirm": {"shot_id", "preview_id"},
    "shot.script_windows.clear": {"shot_id"},
    "shot.segments": {"shot_id", "segments"},
    "shot.configure_generation": {
        "shot_id",
        "recipe_id",
        "prompt",
        "controls",
        "inputs",
        "deployment_profile_id",
        "tier",
        "mode",
    },
    "captions.set": {"chapter_id", "cues"},
    "captions.confirm": {"chapter_id", "reviewed"},
    "sound.set": {"chapter_id", "tracks", "mode"},
    "sound.generated": {"shot_id", "gain"},
}
ACTION_REQUIRED = {
    "library.rename": {"target", "title"},
    "library.tag": {"targets", "tags"},
    "library.file": {"profile_id"},
    "storyboard.bulk": {"shot_ids", "operation"},
    "storyboard.defaults": {"value"},
    "storyboard.restore": {"shot_id", "artifact_id"},
    "storyboard.star": {"shot_id", "artifact_id"},
    "storyboard.select": {"shot_id", "artifact_id"},
    "storyboard.fork_profile": {"profile_id", "shot_ids", "new_profile_id"},
    "entity.create": {"entity"},
    "entity.update": {"entity_id", "patch"},
    "entity.delete": {"entity_id"},
    "link.create": {"link"},
    "link.delete": {"link_id"},
    "project.update": {"patch"},
    "journey.update": {"patch"},
    "layout.update": {"patch"},
    "asset.attach": {"asset_id"},
    "artifact.adopt": {"artifact_id"},
    "shot.select": {"shot_id"},
    "shot.trim": {"shot_id", "start", "end"},
    "shot.script_windows.confirm": {"shot_id", "preview_id"},
    "shot.script_windows.clear": {"shot_id"},
    "shot.segments": {"shot_id", "segments"},
    "shot.configure_generation": {"shot_id"},
    "captions.set": {"chapter_id", "cues"},
    "captions.confirm": {"chapter_id", "reviewed"},
    "sound.set": {"chapter_id", "tracks"},
    "sound.generated": {"shot_id"},
}


class ActionShapeError(ValueError):
    """Public guidance made only from closed operation and field names."""


def validate_action_fields(action):
    if not isinstance(action, dict):
        raise ActionShapeError(
            "每个操作必须为对象；查看 /v1/guided-schema 的 operation_schemas"
        )
    op = action.get("op")
    if not isinstance(op, str) or op not in ACTION_FIELDS:
        raise ActionShapeError(
            "未知编辑操作或缺少op；GET /v1/guided-schema，选择operation_schemas中的操作名"
        )
    missing = ACTION_REQUIRED[op] - action.keys()
    if missing:
        raise ActionShapeError(
            f"{op} 缺少必填字段：{', '.join(sorted(missing))}；查看 /v1/guided-schema 的 operation_schemas.{op}"
        )
    if action.keys() - (ACTION_FIELDS[op] | {"op"}):
        raise ActionShapeError(
            f"{op} 包含不支持的字段；允许字段：{', '.join(sorted(ACTION_FIELDS[op]))}；查看 /v1/guided-schema"
        )


ID_SCHEMA = {
    "type": "string",
    "pattern": "^[A-Za-z0-9_-]{1,160}$",
    "minLength": 1,
    "maxLength": 160,
}
TITLE = {"type": "string", "maxLength": 160}
TEXT = {"type": "string", "maxLength": 24000}
OBJECT = {"type": "object", "additionalProperties": True}
TIME = {"type": "number", "minimum": 0, "maximum": 360000}
SEGMENTS = {
    "type": "array",
    "maxItems": 50,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["start", "end"],
        "properties": {"start": TIME, "end": TIME},
    },
    "description": "Ordered original-source seconds, inward aligned to 24fps; overlaps/repeats keep their order. [] explicitly clears. Requires a current source binding and trusted duration; use shot.segments to bind automatically.",
}
SEGMENT_SOURCE = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["assetId", "fileId", "cloudAssetId", "cloudArtifactId"],
    "properties": {
        "assetId": ID_SCHEMA,
        **{
            key: {"type": ["string", "null"], "minLength": 1, "maxLength": 200}
            for key in ("fileId", "cloudAssetId", "cloudArtifactId")
        },
    },
    "description": "Bound to the chosen video identity. Cannot be rebound while ranges remain; clear explicitly before selecting another take.",
}
PARENT = {
    "anyOf": [ID_SCHEMA, {"type": "null"}],
    "description": "Existing parent entity ID; null means project-wide.",
}
ENTITY_PROPERTIES = {
    "id": {
        **ID_SCHEMA,
        "description": "Optional stable ID; otherwise generated. IDs are unique within the story.",
    },
    "type": {"type": "string", "enum": sorted(TYPES)},
    "parentId": PARENT,
    "title": {**TITLE, "default": ""},
    "description": {**TEXT, "default": ""},
    "status": {
        "type": "string",
        "enum": ["draft", "review", "ready"],
        "default": "draft",
    },
    "order": {
        "type": "integer",
        "minimum": 0,
        "description": "Defaults to sibling count; display order only.",
    },
    "data": {
        **OBJECT,
        "description": "Create replaces defaults when supplied. A shot defaults to seconds=5,prompt=''; generation defaults to recipe='video'. Include desired defaults in supplied data.",
        "properties": {
            "seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
            "clipSound": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "Shot only. Newly selected takes default on and bind validated same-job FLAC through actions. Off persists across takes and suppresses attached generated sound only. Absent on legacy shots preserves existing tracks; no backfill. Independent mute/review/detach and slow-rate guards remain.",
            },
            "videoSegments": SEGMENTS,
            "videoSegmentsSource": SEGMENT_SOURCE,
            "prompt": TEXT,
            "script": {"type": "string", "maxLength": 120000},
            "h3": {
                **OBJECT,
                "description": "Draft controls only. GET /v1/capabilities and use generation-plans for actual supported H3 controls and limits.",
            },
            "recipe": {
                "type": "string",
                "enum": ["video", "image", "music"],
                "description": "Required for generation entities; image/music are draft plans, not connected generation services.",
            },
        },
    },
}
ENTITY_PATCH = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        k: v for k, v in ENTITY_PROPERTIES.items() if k not in {"id", "type"}
    },
}
ENTITY_PATCH["properties"]["data"] = {
    **ENTITY_PROPERTIES["data"],
    "description": "Merges one level into current data. Nested h3/other objects replace their whole value; read current data first. Entity version increments.",
}


def operation_schemas():
    from .capabilities import RECIPES, control_schema
    from .quality_tiers import recipe_alias

    recipe_ids = list(RECIPES) + [recipe_alias(mode) for mode in ("fl", "ref")]
    tier = {"enum": ["fast", "standard", "refine"]}
    mode = {"enum": ["fl", "ref"]}

    source_range = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end"],
                "properties": {"start": TIME, "end": TIME},
            },
        ],
        "description": "Original source seconds; 2..15 second selection within inspected source. null clears; omitted preserves a matching existing binding.",
    }

    def media_item(kind):
        roles = {
            "images": ["reference", "identity"],
            "videos": ["motion", "reference"],
            "audios": ["audio", "reference"],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {
                "asset_id": ID_SCHEMA,
                "purpose": {"enum": roles[kind]},
                "source_range": source_range,
                **({"include_audio": {"type": "boolean"}} if kind == "videos" else {}),
            },
        }

    editable_controls = {
        k: v
        for k, v in control_schema().items()
        if k not in {"guides", "video_audio"} and v.get("available") is not False
    }
    draft_inputs = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **{
                kind: {
                    "type": "array",
                    "maxItems": 9 if kind == "images" else 3,
                    "items": media_item(kind),
                }
                for kind in ("images", "videos", "audios")
            },
            **{
                kind: {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["asset_id"],
                            "properties": {"asset_id": ID_SCHEMA},
                        },
                    ]
                }
                for kind in ("first_frame", "last_frame")
            },
            "guides": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["media_id", "time_seconds"],
                    "properties": {
                        "media_id": ID_SCHEMA,
                        "time_seconds": TIME,
                        "use_audio": {"type": "boolean"},
                        "source_range": source_range,
                    },
                },
            },
        },
    }
    attach = {
        "entity_id": ID_SCHEMA,
        "title": TITLE,
        "parent_id": PARENT,
        "shot_id": ID_SCHEMA,
        "select": {
            "type": "boolean",
            "default": False,
            "description": "True requires shot_id and image/video media; explicitly selects this media for that shot.",
        },
    }
    props = {
        "library.rename": {
            "target": {
                "type": "object",
                "required": ["kind", "id"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"enum": ["entity", "asset", "artifact"]},
                    "id": ID_SCHEMA,
                },
            },
            "title": {"type": "string", "minLength": 1},
        },
        "library.tag": {
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "id"],
                    "properties": {
                        "kind": {"enum": ["entity", "asset", "artifact"]},
                        "id": ID_SCHEMA,
                    },
                },
            },
            "tags": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
        "library.file": {
            "profile_id": ID_SCHEMA,
            "entity_ids": {"type": "array", "uniqueItems": True, "items": ID_SCHEMA},
            "variant_id": ID_SCHEMA,
            "targets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind", "id"],
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"enum": ["entity", "asset", "artifact"]},
                        "id": ID_SCHEMA,
                    },
                },
            },
        },
        "storyboard.bulk": {
            "shot_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": ID_SCHEMA,
            },
            "operation": {"enum": ["draft", "final", "move"]},
            "parent_id": ID_SCHEMA,
        },
        "storyboard.defaults": {
            "value": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "recipe_id": {"enum": recipe_ids},
                    "deployment_profile_id": {"type": ["string", "null"]},
                    "tier": tier,
                    "mode": mode,
                    "controls": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            k: v
                            for k, v in editable_controls.items()
                            if k not in {"duration", "seed"}
                        },
                    },
                },
            }
        },
        "storyboard.restore": {"shot_id": ID_SCHEMA, "artifact_id": ID_SCHEMA},
        "storyboard.star": {
            "shot_id": ID_SCHEMA,
            "artifact_id": ID_SCHEMA,
            "starred": {"type": "boolean"},
            "title": {"type": "string"},
        },
        "storyboard.select": {"shot_id": ID_SCHEMA, "artifact_id": ID_SCHEMA},
        "storyboard.fork_profile": {
            "profile_id": ID_SCHEMA,
            "new_profile_id": ID_SCHEMA,
            "title": TITLE,
            "shot_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": ID_SCHEMA,
            },
        },
        "entity.create": {
            "entity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": ENTITY_PROPERTIES,
            }
        },
        "entity.update": {"entity_id": ID_SCHEMA, "patch": ENTITY_PATCH},
        "entity.delete": {
            "entity_id": ID_SCHEMA,
            "cascade": {"type": "boolean", "default": False},
        },
        "link.create": {
            "link": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "target", "role"],
                "properties": {
                    "id": ID_SCHEMA,
                    "source": ID_SCHEMA,
                    "target": ID_SCHEMA,
                    "role": {"enum": sorted(ROLES)},
                },
            }
        },
        "link.delete": {"link_id": ID_SCHEMA},
        "project.update": {
            "patch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"title": {**TITLE, "minLength": 1}, "logline": TEXT},
            }
        },
        "journey.update": {
            "patch": {
                **OBJECT,
                "description": "Merges top-level fields. Nested objects/arrays replace existing fields. Brief/sound/soundTracks/captionTracks must be objects; final document validation applies.",
            }
        },
        "layout.update": {
            "patch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "positions": {
                        "type": "object",
                        "description": "Keys must be existing entity IDs; replaces the whole positions map.",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["x", "y"],
                            "properties": {
                                "x": {
                                    "type": "number",
                                    "minimum": -10000000,
                                    "maximum": 10000000,
                                },
                                "y": {
                                    "type": "number",
                                    "minimum": -10000000,
                                    "maximum": 10000000,
                                },
                            },
                        },
                    },
                    "viewport": {
                        "type": "object",
                        "required": ["x", "y", "zoom"],
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "zoom": {"type": "number", "minimum": 0.05, "maximum": 10},
                        },
                    },
                },
            }
        },
        "asset.attach": {
            **attach,
            "asset_id": {
                "type": "string",
                "description": "Exact ready asset receipt ID from POST /v1/assets; same owner and project required.",
            },
            "role": {
                "enum": sorted(ROLES),
                "description": "Optional compatible reference link when shot_id is supplied. Without shot_id no link is created.",
            },
        },
        "artifact.adopt": {
            **attach,
            "artifact_id": {
                **ID_SCHEMA,
                "description": "Exact artifact ID from GET /v1/jobs/{job_id}/artifacts; same owner/project and succeeded job required.",
            },
        },
        "shot.select": {
            "shot_id": ID_SCHEMA,
            "entity_id": {
                "anyOf": [ID_SCHEMA, {"const": ""}, {"type": "null"}],
                "description": "Existing image/video entity ID. Omit, null or empty string to clear the selection.",
            },
        },
        "shot.script_windows.confirm": {"shot_id": ID_SCHEMA, "preview_id": ID_SCHEMA},
        "shot.script_windows.clear": {"shot_id": ID_SCHEMA},
        "shot.trim": {"shot_id": ID_SCHEMA, "start": TIME, "end": TIME},
        "shot.segments": {"shot_id": ID_SCHEMA, "segments": SEGMENTS},
        "shot.configure_generation": {
            "shot_id": ID_SCHEMA,
            "recipe_id": {"enum": recipe_ids},
            "tier": tier,
            "mode": mode,
            "deployment_profile_id": {
                "type": ["string", "null"],
                "description": "Use the neutral quality alias from capabilities, or tier and mode. Raw deployment IDs require a human administrator.",
            },
            "prompt": {"type": "string", "maxLength": 12000},
            "controls": {
                "type": "object",
                "additionalProperties": False,
                "properties": editable_controls,
            },
            "inputs": draft_inputs,
        },
        "captions.set": {
            "chapter_id": ID_SCHEMA,
            "cues": {
                "type": "array",
                "maxItems": 500,
                "items": {
                    "type": "object",
                    "required": ["id", "start", "end", "text"],
                    "properties": {
                        "id": ID_SCHEMA,
                        "start": TIME,
                        "end": TIME,
                        "text": {"type": "string", "maxLength": 2000},
                    },
                },
            },
        },
        "captions.confirm": {"chapter_id": ID_SCHEMA, "reviewed": {"const": True}},
        "sound.set": {
            "chapter_id": ID_SCHEMA,
            "mode": {"enum": ["silent", "dialogue", "music", "mixed"]},
            "tracks": {
                "type": "array",
                "maxItems": MAX_AUDIO_TRACKS,
                "items": {
                    **OBJECT,
                    "description": "Editable audio track. Rendering additionally requires a valid same-project audio entity/receipt, matching fileId, bounded source range and gain; muted drafts may keep incomplete bindings.",
                    "properties": {
                        "id": ID_SCHEMA,
                        "assetId": ID_SCHEMA,
                        "fileId": {"type": "string"},
                        "shotId": ID_SCHEMA,
                        "role": {"type": "string"},
                        "offset": {"type": "number"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "gain": {"type": "number"},
                        "muted": {"type": "boolean"},
                        "needsReview": {"type": "boolean"},
                    },
                },
            },
        },
        "sound.generated": {
            "shot_id": ID_SCHEMA,
            "gain": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
        },
    }
    rules = {
        "library.rename": [
            "Change mutable owner project display title only; receipt filenames and accepted inputs remain unchanged."
        ],
        "library.tag": [
            "Replace tags on authorized same-project library targets under original project CAS; no storage rewrite."
        ],
        "library.file": [
            "File existing same-story media/receipts into a profile without copying bytes. Optional look/location version image slot accepts images only; ordinary audio/video filing does not select voice/ambient generation inputs. Original files remain retained."
        ],
        "storyboard.bulk": [
            "One project CAS changes all selected shots or none. Draft/final is intent only; move requires an existing scene. No enqueue or deletion."
        ],
        "storyboard.defaults": [
            "Replace story defaults; omitted shot overrides keep inheriting. Duration and seed remain per shot."
        ],
        "storyboard.restore": [
            "Only an original server-snapshotted recipe is restorable. Unknown history is refused. Exact effective inputs replace implicit current profile compilation; source assets, context and accepted jobs remain retained."
        ],
        "storyboard.star": [
            "Owner project annotation only; never changes the immutable generation recipe."
        ],
        "storyboard.select": [
            "Uses original artifact.adopt(select=true) with succeeded same-owner/project receipt checks. Does not generate or replace any accepted job."
        ],
        "storyboard.fork_profile": [
            "Copies an existing project-local character/location/prop and rewires selected shots in one CAS. Other stories, original profile and unselected shots are unchanged. Cross-story usage is not claimed."
        ],
        "entity.create": [
            "chapter has no parent; scene requires an existing chapter parent; shot requires an existing scene parent. Other entities may be project-wide or children of chapter/scene/shot.",
            "Creation/ready status only edits the document; no model runs.",
        ],
        "entity.update": [
            "id/type/version cannot be patched. Final hierarchy and media bindings must validate."
        ],
        "entity.delete": [
            "Children require cascade=true. References are reconciled; server files and generation jobs are retained. This does not cancel jobs."
        ],
        "link.create": [
            "source and target must exist. See link_role_rules for allowed types. Duplicate edges and dependency cycles are rejected.",
            "firstFrame/lastFrame/motion/audio links to generation entities require recipe=video; dependency links only target generation entities.",
        ],
        "link.delete": ["link_id must exist in this story."],
        "project.update": ["Title must contain non-whitespace text."],
        "journey.update": [
            "Read current journey before replacing nested arrays. Updating a draft is not execution or approval authority."
        ],
        "layout.update": [
            "Layout must remain valid against the current document. Changes do not submit generation."
        ],
        "asset.attach": [
            "Requires assets:read in addition to story editing scopes. Upload requires assets:write.",
            "Existing receipt attachment is reused; conflicting entity_id is rejected. Selection and links do not generate media.",
        ],
        "artifact.adopt": [
            "Requires jobs:read in addition to story editing scopes. Video/image can be candidates; audio is attached to the library.",
            "No role field on this action. Use link.create separately for compatible references.",
        ],
        "shot.select": [
            "Selection is an existing image/video entity in this story; it is not an asset receipt ID."
        ],
        "shot.script_windows.confirm": [
            "Copies an owner/shot-scoped ready preview, checks source hash and normal CAS; never calls the model."
        ],
        "shot.script_windows.clear": [
            "Explicitly removes the confirmed split; does not generate or change prior takes."
        ],
        "shot.trim": [
            "Shot must already select a video entity. Requires 0 <= start < end <= 360000. Saves a range binding; rendering checks real media duration and whether it matches the current file."
        ],
        "shot.segments": [
            "Binds ordered ranges to the currently chosen owned video, checks trusted source duration and snaps inward to the existing 24fps grid. Does not change the original file or generation duration.",
            "[] explicitly clears. Clear then select another take in the same CAS batch; a silent source change or omitted ranges in a full save is refused. Idempotent replay returns the original receipt before resolving media again.",
        ],
        "shot.configure_generation": [
            "Edits the same saved shot used by the website. Omitted fields/slots are preserved; controls merge per field. Empty arrays clear that slot; first/last null clears that slot. Mode changes never remove incompatible inputs.",
            "All asset_id and guide media_id values are ready upload receipt IDs, not document entity IDs. Requires assets:read when referencing receipts. The server checks owner/project, media kind and source ranges; no generation or derivation occurs during this edit.",
            "A provided slot replaces its direct links; inherited character references stay intact. Matching link IDs, omitted selections and omitted video audio switches are preserved. Guide selections are preserved when receipt/time still match. Files and result candidates are never deleted.",
            "Read GET /v1/projects/{project_id}/shots/{shot_id}/generation-draft. POST the same prefix /generation-plans with expected_version (project version) and optional capabilities_version to preflight the saved draft. Selected ranges require assets:write for CPU derivatives; plans never submit a model job.",
        ],
        "captions.set": [
            "Replaces the chapter caption track and removes its previous confirmation. Cue IDs must be unique; combined text <=100000 characters.",
            "Editable drafts may overlap or be out of chapter bounds; confirmation/export apply stricter checks.",
        ],
        "captions.confirm": [
            "Requires 1..500 cues, nonempty safe text, start<end, no overlaps after millisecond rounding, and bounds within the chapter's 24fps timeline.",
            "reviewed=true is an explicit human/authorized review assertion; timeline or caption changes invalidate the confirmation.",
        ],
        "sound.set": [
            "Replaces the chapter track array. Empty tracks=[] is valid. mode changes project journey.sound.mode.",
            "Manual render needs 0 <= gain <=1, start<end within real audio duration, nonnegative timeline offset and matching fileId. This saves a draft, not an audio render.",
        ],
        "sound.generated": [
            "Shot must select an adopted generated video, and library must contain exactly one audio/flac artifact from the same succeeded job.",
            "A conflicting unmuted manual track for that audio is rejected. Reuses/replaces the active generated track for that shot.",
        ],
    }
    return {
        op: {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", *sorted(ACTION_REQUIRED[op])],
            "properties": {"op": {"const": op}, **deepcopy(props[op])},
            "required_scopes": [
                "projects:read",
                "projects:write",
                *(
                    ["assets:read", "jobs:read"]
                    if op == "storyboard.restore"
                    else ["assets:read"]
                    if op in {"asset.attach", "shot.configure_generation"}
                    else ["jobs:read"]
                    if op in {"artifact.adopt", "storyboard.select", "storyboard.star"}
                    else []
                ),
            ],
            "constraints": rules[op],
        }
        for op in ACTION_FIELDS
    }


def examples():
    """No paid generation; later examples use the story built by the first."""
    return [
        {
            "id": "create-story-structure",
            "required_scopes": ["projects:create", "projects:read", "projects:write"],
            "description": "Create a new story, then its chapter, scene and five-second shot. Key must allow all projects to create a new one.",
            "steps": [
                {
                    "method": "POST",
                    "path": "/v1/projects",
                    "headers": {"Idempotency-Key": "example-story-create-1"},
                    "json": {
                        "title": "雨夜来信",
                        "logline": "林岚在车站收到一封明天寄出的信。",
                    },
                    "save_response_as": "story",
                },
                {
                    "method": "POST",
                    "path": "/v1/projects/${story.id}/actions",
                    "headers": {"Idempotency-Key": "example-story-structure-1"},
                    "json": {
                        "expected_version": "${story.version}",
                        "actions": [
                            {
                                "op": "entity.create",
                                "entity": {
                                    "id": "chapter-one",
                                    "type": "chapter",
                                    "title": "第一章：来信",
                                },
                            },
                            {
                                "op": "entity.create",
                                "entity": {
                                    "id": "scene-one",
                                    "type": "scene",
                                    "parentId": "chapter-one",
                                    "title": "雨夜车站",
                                    "data": {"script": "林岚停在站台灯下，打开信封。"},
                                },
                            },
                            {
                                "op": "entity.create",
                                "entity": {
                                    "id": "shot-one",
                                    "type": "shot",
                                    "parentId": "scene-one",
                                    "title": "拆信特写",
                                    "data": {
                                        "seconds": 5,
                                        "prompt": "近景，林岚在暖黄色站台灯下打开信封，雨水从帽檐滴落。",
                                    },
                                },
                            },
                        ],
                    },
                    "save_response_as": "structured_story",
                },
                {"method": "GET", "path": "/v1/projects/${story.id}"},
            ],
        },
        {
            "id": "upload-image-attach-select",
            "required_scopes": [
                "projects:read",
                "projects:write",
                "assets:read",
                "assets:write",
            ],
            "description": "Use story.id/shot-one from the first example. Upload an actual image, add a first-frame reference and explicitly select it as the shot's image candidate; no video is generated.",
            "steps": [
                {
                    "method": "POST",
                    "path": "/v1/assets",
                    "multipart": {
                        "fields": {
                            "client_project_id": "${story.id}",
                            "client_asset_id": "example-portrait-1",
                        },
                        "file": {
                            "field": "file",
                            "local_path": "${portrait_file}",
                            "filename": "portrait.png",
                            "content_type": "image/png",
                        },
                    },
                    "save_response_as": "image_upload",
                    "require": "status=ready before attachment; otherwise GET /v1/assets/${image_upload.asset_id} to inspect status",
                },
                {
                    "method": "GET",
                    "path": "/v1/projects/${story.id}/meta",
                    "save_response_as": "current_story",
                },
                {
                    "method": "POST",
                    "path": "/v1/projects/${story.id}/actions",
                    "headers": {"Idempotency-Key": "example-portrait-attach-1"},
                    "json": {
                        "expected_version": "${current_story.version}",
                        "actions": [
                            {
                                "op": "asset.attach",
                                "asset_id": "${image_upload.asset_id}",
                                "entity_id": "portrait-reference",
                                "title": "林岚首帧参考",
                                "shot_id": "shot-one",
                                "role": "firstFrame",
                                "select": True,
                            }
                        ],
                    },
                },
                {
                    "method": "GET",
                    "path": "/v1/projects/${story.id}/entities?type=image",
                },
            ],
        },
        {
            "id": "audio-and-reviewed-subtitles",
            "required_scopes": [
                "projects:read",
                "projects:write",
                "assets:read",
                "assets:write",
            ],
            "description": "Use story.id/chapter-one/shot-one from the first example. Supply an actual audio file of at least two seconds. Review the supplied subtitle before reviewed=true. This edits the mix and exports SRT; it does not render video or generate music.",
            "steps": [
                {
                    "method": "POST",
                    "path": "/v1/assets",
                    "multipart": {
                        "fields": {
                            "client_project_id": "${story.id}",
                            "client_asset_id": "example-voice-1",
                        },
                        "file": {
                            "field": "file",
                            "local_path": "${voice_file}",
                            "filename": "voice.wav",
                            "content_type": "audio/wav",
                        },
                    },
                    "save_response_as": "audio_upload",
                    "require": "status=ready and verified duration>=2 seconds",
                },
                {
                    "method": "GET",
                    "path": "/v1/projects/${story.id}/meta",
                    "save_response_as": "current_story",
                },
                {
                    "method": "POST",
                    "path": "/v1/projects/${story.id}/actions",
                    "headers": {"Idempotency-Key": "example-sound-captions-1"},
                    "json": {
                        "expected_version": "${current_story.version}",
                        "actions": [
                            {
                                "op": "asset.attach",
                                "asset_id": "${audio_upload.asset_id}",
                                "entity_id": "voice-reference",
                                "title": "林岚对白",
                            },
                            {
                                "op": "sound.set",
                                "chapter_id": "chapter-one",
                                "mode": "dialogue",
                                "tracks": [
                                    {
                                        "id": "voice-track",
                                        "assetId": "voice-reference",
                                        "fileId": "cloud_asset_${audio_upload.asset_id}",
                                        "shotId": "shot-one",
                                        "role": "dialogue",
                                        "offset": 0,
                                        "start": 0,
                                        "end": 2,
                                        "gain": 0.7,
                                        "muted": False,
                                    }
                                ],
                            },
                            {
                                "op": "captions.set",
                                "chapter_id": "chapter-one",
                                "cues": [
                                    {
                                        "id": "cue-one",
                                        "start": 0,
                                        "end": 2,
                                        "text": "这封信，来自明天。",
                                    }
                                ],
                            },
                            {
                                "op": "captions.confirm",
                                "chapter_id": "chapter-one",
                                "reviewed": True,
                            },
                        ],
                    },
                },
                {
                    "method": "GET",
                    "path": "/v1/projects/${story.id}/chapters/chapter-one/subtitles.srt",
                },
            ],
        },
    ]


def contract():
    from .storyboard import contract as storyboard_contract
    from .result_collection import contract as collection_contract

    return {
        "schema_revision": 3,
        "result_collection": collection_contract(),
        "operation_schemas": operation_schemas(),
        "script_windows": script_windows_contract(),
        "request": {
            "method": "POST",
            "path": "/v1/projects/{project_id}/actions",
            "content_type": "application/json",
            "required": ["expected_version", "actions"],
            "expected_version": {"type": "integer", "minimum": 1},
            "actions": {"type": "array", "minItems": 1, "maxItems": 200},
            "authorization": "Bearer API key; never send provider credentials. projects:write requires projects:read.",
            "idempotency": "Use a stable Idempotency-Key (1..160 letters/digits/underscore/hyphen) for the same logical request; never reuse it with a different body.",
            "atomicity": "All actions execute in order in one transaction. One failure rolls back the whole document edit. Response is the updated id/version/project envelope.",
        },
        "link_role_rules": {
            role: {
                "source_types": sorted(types),
                "target_types": ["generation"]
                if role == "dependency"
                else ["shot", "generation"],
            }
            for role, types in ROLES.items()
        },
        "examples": examples(),
        "example_variables": "Examples 2/3 use the story created by example 1. Replace ${name.field} with the saved JSON response field; exact whole-value replacements retain JSON types (version must be an integer). Use real local files for portrait_file/voice_file. Generate fresh stable idempotency/client_asset IDs per new logical run.",
        "recovery": {
            "422": "Correct missing/unsupported fields using operation_schemas; nothing in the action batch was committed.",
            "404": "Check the selected story, entity/receipt ID, ownership and required key scopes; do not guess another owner's IDs.",
            "409": "Fetch the latest project and reconcile changes. For an unknown write outcome, first replay the identical request with its original idempotency key; do not change expected_version during that replay.",
        },
        "storyboard": storyboard_contract(),
        "validation_note": "These schemas describe supported editing inputs and stricter execution prerequisites separately. Final project validation remains authoritative. Draft acceptance is not generation/render readiness.",
    }

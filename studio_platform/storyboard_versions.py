"""Storyboard authoring on existing projects, plans and immutable job receipts.

No execution queue or financial authority is owned here. Historical requests
without an authoring snapshot remain explicitly unknown.
"""

import copy
import json
import secrets
import uuid

from sqlalchemy import insert, select, update
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .repository import (
    Scope,
    Conflict,
    NotFound,
    documents,
    jobs,
    artifacts,
    plans,
    request_hash,
)
from .content_lifecycle import owner_reference_gate, assert_project_writable
from .generation_draft import shot_entity, validate_controls_patch
from .capabilities import version_matches


class StoryboardPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=1)
    shot_ids: list[StrictStr] = Field(min_length=1)
    copies: StrictInt = Field(ge=1)
    capabilities_version: StrictStr | None = None


class StoryboardPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=0)
    defaults: dict


def contract():
    return {
        "versions": "/v1/projects/{project_id}/shots/{shot_id}/versions?limit=20&offset=0",
        "quality": {
            "quality_flags": ["burned_subtitles"],
            "quality_checks": "burned_subtitles: status, detector; flagged checks also include confidence and times_s. No OCR text. Legacy takes are not_checked; unavailable/timeout/failed is not a clean result.",
            "behavior": "Advisory only: keep, manually regenerate or trim. Never automatic re-roll or additional charging. Conservative CPU OCR checks exact normalized dialogue, bottom-centred stable text for >=1s and background motion; no warning does not guarantee subtitle-free video.",
        },
        "profile_usage": "/v1/projects/{project_id}/storyboard/profiles",
        "defaults": {
            "account": "/v1/storyboard/preferences",
            "story": "storyboard.defaults action",
            "precedence": ["global", "story", "shot"],
            "shot_only": ["duration", "seed"],
            "copies_limit": "Original /v1/quick-chat/preferences copies_max; default 4, owner editable.",
        },
        "preflight": {
            "method": "POST",
            "path": "/v1/projects/{project_id}/storyboard/preflights",
            "body": {"expected_version": 1, "shot_ids": ["SHOT"], "copies": 2},
            "header": "Idempotency-Key",
            "read": "/v1/projects/{project_id}/storyboard/preflights/{id}",
            "admission": "No job is submitted. Explicitly send ready original plan_ids to POST /v1/batches after whole-batch confirmation.",
            "recovery": "Same key/body returns original seeds and plan IDs; GET observes without advancing. Pending/unknown never starts a replacement. closed_preflight=true permits explicit abandonment of this unpaid operation only.",
        },
        "recipes": "New server preflights retain immutable authoring_recipe and authoring_context in original plans/jobs. Old versions without evidence return recipe_status=unknown. Restoring uses exact effective inputs and separately preserves original profile provenance; no mutable-profile reconstruction.",
        "actions": [
            "storyboard.bulk",
            "storyboard.defaults",
            "storyboard.restore",
            "storyboard.star",
            "storyboard.select",
            "storyboard.fork_profile",
        ],
        "limits": "Original scope/pause, editable spending, model controls, byte quotas and accounting apply. Quality labels do not override a selected model. Profile usage is project-local; global search is #238. Restorable deletion/undo is #236.",
    }


def validate_defaults(value):
    if not isinstance(value, dict) or set(value) - {
        "recipe_id",
        "deployment_profile_id",
        "controls",
    }:
        raise ValueError("Invalid storyboard defaults / 分镜默认设置无效")
    from .capabilities import RECIPES

    if "recipe_id" in value and value["recipe_id"] not in RECIPES:
        raise ValueError("Invalid recipe / 配方无效")
    if value.get("deployment_profile_id") is not None:
        from .runtime_catalog import get_profile

        get_profile(value["deployment_profile_id"])
    controls = value.get("controls", {})
    validate_controls_patch(controls)
    if set(controls) & {"duration", "seed"}:
        raise ValueError("Duration and seed belong to each shot / 时长和种子须逐镜设置")
    return copy.deepcopy(value)


def preferences(repo, tenant, owner, *, conn=None):
    if conn is None:
        with repo.engine.connect() as connection:
            return preferences(repo, tenant, owner, conn=connection)
    row = (
        conn.execute(
            select(documents).where(
                repo._scope(documents, Scope(tenant, owner, "__preferences")),
                documents.c.kind == "storyboard_preferences",
                documents.c.document_id == "defaults",
            )
        )
        .mappings()
        .first()
    )
    return {
        "version": row["version"] if row else 0,
        "defaults": row["payload"] if row else {},
    }


def effective_project(project, shot_id, defaults):
    """Pure rendering copy; source fences always use the original document."""
    value = copy.deepcopy(project)
    shot = shot_entity(value, shot_id)
    h3 = shot["data"].setdefault("h3", {})
    shot_defaults = {
        **({"recipe_id": h3["recipeId"]} if "recipeId" in h3 else {}),
        **(
            {"deployment_profile_id": h3["deploymentProfileId"]}
            if "deploymentProfileId" in h3
            else {}
        ),
        "controls": copy.deepcopy(h3.get("controls", {})),
    }
    story = project.get("journey", {}).get("storyboard", {}).get("defaults", {})
    stored = {
        "global": copy.deepcopy(defaults),
        "story": copy.deepcopy(story),
        "shot": shot_defaults,
    }
    if h3.get("explicitReferences"):
        defaults, story = {}, {}
    inherited, provenance = {}, {}
    for label, layer in (("global", defaults), ("story", story)):
        for key, item in layer.items():
            if key == "controls":
                for field, control in item.items():
                    inherited.setdefault("controls", {})[field] = copy.deepcopy(control)
                    provenance["controls." + field] = label
            else:
                inherited[key] = copy.deepcopy(item)
                provenance[key] = label
    for source, target in (
        ("recipe_id", "recipeId"),
        ("deployment_profile_id", "deploymentProfileId"),
    ):
        if target in h3:
            inherited[source] = h3[target]
            provenance[source] = "shot"
        elif source in inherited:
            h3[target] = inherited[source]
    own = h3.get("controls", {})
    for field, control in own.items():
        inherited.setdefault("controls", {})[field] = copy.deepcopy(control)
        provenance["controls." + field] = "shot"
    h3["controls"] = copy.deepcopy(inherited.get("controls", {}))
    provenance["controls.duration"] = provenance["controls.seed"] = "shot"
    return value, {"effective": inherited, "provenance": provenance, **stored}


def check_admission_source(conn, repo, scope, compiled):
    # fmt: off
    """Final fence under the existing owner write gate, before any reservation.

Only new server-snapshotted plans opt in. Original accepted job replay occurs
before this check; neither old plans nor accepted jobs are rewritten.
"""
    # fmt: on
    if "film_source_ref" in compiled:
        from .film_runs import validate_film_source

        validate_film_source(conn, repo, scope, compiled)
        return
    if "authoring_defaults_version" not in compiled:
        return
    if (
        preferences(repo, scope.tenant_id, scope.owner_id, conn=conn)["version"]
        != compiled["authoring_defaults_version"]
    ):
        raise Conflict("storyboard_defaults_changed")
    row = (
        conn.execute(
            select(documents).where(
                repo._scope(
                    documents, Scope(scope.tenant_id, scope.owner_id, "__projects")
                ),
                documents.c.kind == "project",
                documents.c.document_id == scope.project_id,
            )
        )
        .mappings()
        .first()
    )
    from .source_snapshot import source_snapshot, validate_source_ref

    ref = compiled["client_ref"]
    if (
        not row
        or not validate_source_ref(row["payload"], ref)
        or source_snapshot(row["payload"], ref["shot_id"])
        != compiled["server_source_hash"]
    ):
        raise Conflict("shot_version_conflict")


def apply_action(project, action, resolved=None):
    from .guided import entity_by_id, new_entity

    op = action["op"]
    if op == "storyboard.defaults":
        project.setdefault("journey", {}).setdefault("storyboard", {})["defaults"] = (
            validate_defaults(action["value"])
        )
        return
    if op in {"storyboard.star", "storyboard.restore"}:
        shot = shot_entity(project, action["shot_id"])
        if op == "storyboard.star":
            if (
                not ({"starred", "title"} & action.keys())
                or ("starred" in action and type(action["starred"]) is not bool)
                or ("title" in action and not isinstance(action["title"], str))
            ):
                raise ValueError("Starred must be boolean / 星标须为布尔值")
            if "starred" in action:
                project.setdefault("journey", {}).setdefault(
                    "storyboardStars", {}
                ).setdefault(shot["id"], {})[action["artifact_id"]] = action["starred"]
            if "title" in action:
                project.setdefault("journey", {}).setdefault(
                    "storyboardTitles", {}
                ).setdefault(shot["id"], {})[action["artifact_id"]] = action["title"]
            return
        from .generation_draft import configure

        recipe, sources, context = resolved
        # Retain the old authoring context as provenance, but don't recompile
        # today's mutable profiles over a restored immutable effective recipe.
        shot["data"].pop("storyboard", None)
        shot["data"]["restoredRecipeContext"] = {
            "artifact_id": action["artifact_id"],
            "original_authoring_context": copy.deepcopy(context),
        }
        shot["data"]["h3"] = {"explicitReferences": True}
        shot["data"]["referenceRanges"] = {}
        configure(project, {"shot_id": shot["id"], **recipe}, sources)
        if "duration" in recipe["controls"]:
            shot["data"]["seconds"] = recipe["controls"]["duration"]
        return
    ids = action.get("shot_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("Select distinct shots / 请选择不重复的镜头")
    selected = [shot_entity(project, ident) for ident in ids]
    if op == "storyboard.bulk":
        operation = action.get("operation")
        if operation not in {"draft", "final", "move"}:
            raise ValueError("Invalid bulk operation / 批量操作无效")
        if operation == "move":
            parent = entity_by_id(project, action.get("parent_id"))
            if parent["type"] != "scene":
                raise ValueError("Destination must be a scene / 目标须为场景")
            order = (
                max(
                    (
                        e["order"]
                        for e in project["entities"]
                        if e["parentId"] == parent["id"]
                    ),
                    default=-1,
                )
                + 1
            )
        for index, shot in enumerate(selected):
            if operation == "move":
                shot.update(parentId=parent["id"], order=order + index)
            else:
                if "storyboard" in shot["data"]:
                    shot["data"]["storyboard"]["quality"] = operation
                    # Standard is 480P and HD is 768P (CR-20261025-65). The tier
                    # label alone never reached the request, so set the shot's
                    # own resolution with it.
                    shot["data"].setdefault("h3", {}).setdefault("controls", {})[
                        "resolution"
                    ] = "480P" if operation == "draft" else "768P"
                else:
                    # A label must not opt an exact/legacy recipe into compilation.
                    shot["data"]["qualityIntent"] = operation
            shot["version"] += 1
        return
    if op != "storyboard.fork_profile":
        raise ValueError("Unknown storyboard action / 分镜操作未知")
    original = entity_by_id(project, action["profile_id"])
    if original["type"] not in {"character", "location", "prop"}:
        raise ValueError("Not a reusable profile / 不是可复用档案")
    fork = copy.deepcopy(original)
    fork.update(id=action["new_profile_id"], version=1)
    if "title" in action:
        fork["title"] = action["title"]
    new_entity(project, {k: v for k, v in fork.items() if k != "version"})
    for shot in selected:
        # Materialize inherited bindings only on the selected shots, leaving
        # their scene, original profile and every unselected shot unchanged.
        lookup = {e["id"]: e for e in project["entities"]}
        scene = lookup.get(shot.get("parentId"), {})
        data = shot["data"]
        if original["type"] == "character":
            cast = copy.deepcopy(
                data.get("cast", scene.get("data", {}).get("cast", []))
            )
            for index, actor in enumerate(cast):
                if (
                    isinstance(actor, dict)
                    and actor.get("characterId") == original["id"]
                ):
                    actor["characterId"] = fork["id"]
                elif actor == original["id"]:
                    cast[index] = fork["id"]
            data["cast"] = cast
            data.setdefault("h3", {})["explicitCast"] = True
        elif original["type"] == "location":
            sb = data.get("storyboard")
            if sb is not None:
                place = copy.deepcopy(
                    sb.get(
                        "place",
                        scene.get("data", {}).get("storyboard", {}).get("place"),
                    )
                )
                if place and place.get("locationId") == original["id"]:
                    place["locationId"] = fork["id"]
                    sb["place"] = place
            if (
                data.get("locationId", scene.get("data", {}).get("locationId"))
                == original["id"]
            ):
                data["locationId"] = fork["id"]
        else:
            sb = data.get("storyboard", {})
            sb["propIds"] = [
                fork["id"] if i == original["id"] else i for i in sb.get("propIds", [])
            ]
        for link in project["links"]:
            if link["target"] == shot["id"] and link["source"] == original["id"]:
                link["source"] = fork["id"]
        shot["version"] += 1


def migration_source_shots(conn, tenant, owner, project, shot, project_version):
    """Alias only the exact original shots in an owner-bound adoption receipt.

    Editable entity metadata is an index hint, never authority over job history.
    Ordinary shots (including historical source links) keep their own versions.
    """
    marker = shot.get("data", {}).get("migration")
    if not isinstance(marker, dict) or marker.get("canonical") is not True:
        return (shot["id"],)

    def need(condition):
        if not condition:
            raise Conflict("story_migration_binding_invalid")

    session = marker.get("source_session_id")
    receipt_id = marker.get("receipt_id")
    need(isinstance(session, str) and session and receipt_id == session)
    row = (
        conn.execute(
            select(documents).where(
                documents.c.tenant_id == tenant,
                documents.c.owner_id == owner,
                documents.c.project_id == project["id"],
                documents.c.kind == "quick_chat_story_migration",
                documents.c.document_id == receipt_id,
            )
        )
        .mappings()
        .first()
    )
    need(row is not None and row["version"] == 1)
    receipt = row["payload"]
    need(isinstance(receipt, dict))
    need(
        type(receipt.get("contract")) is int
        and receipt["contract"] == 1
        and type(receipt.get("mapping_version")) is int
        and receipt["mapping_version"] == 2
        and receipt.get("status") == "completed"
        and "rollback" not in receipt
        and receipt.get("tenant_id") == tenant
        and receipt.get("owner_id") == owner
        and receipt.get("project_id") == project["id"]
        and receipt.get("source_session_id") == session
        and receipt.get("receipt_id") == receipt_id
        and receipt.get("archive_strategy") == "receipt_archive"
        and type(receipt.get("after_project_version")) is int
        and 1 <= receipt["after_project_version"] <= project_version
    )
    canonical = receipt.get("canonical_shots")
    need(isinstance(canonical, dict) and isinstance(canonical.get(shot["id"]), dict))
    entry = canonical[shot["id"]]
    sources, card_id = entry.get("source_shot_ids"), entry.get("card_id")
    need(
        isinstance(sources, list)
        and bool(sources)
        and all(isinstance(s, str) and s for s in sources)
        and len(sources) == len(set(sources))
        and shot["id"] in sources
        and isinstance(card_id, str)
        and bool(card_id)
    )
    need(
        marker
        == {
            "canonical": True,
            "source_shot_ids": sources,
            "source_session_id": session,
            "receipt_id": receipt_id,
        }
    )
    mapping = receipt.get("mapping")
    need(isinstance(mapping, dict))
    originals = {}
    for item_id, item in mapping.items():
        need(isinstance(item, dict))
        if item.get("card_id") == card_id:
            source = item.get("shot_id")
            need(
                isinstance(source, str)
                and source not in originals
                and isinstance(item_id, str)
                and bool(item_id)
                and isinstance(item.get("revision_id"), str)
                and bool(item["revision_id"])
            )
            originals[source] = (item_id, item.get("revision_id"))
    need(set(originals) == set(sources))
    entities = {entity["id"]: entity for entity in project["entities"]}
    before = receipt.get("before_project")
    archived = receipt.get("archived_shots")
    need(isinstance(before, dict) and before.get("id") == project["id"])
    need(receipt.get("before_project_hash") == request_hash(before))
    need(isinstance(before.get("entities"), list) and isinstance(archived, dict))
    need(
        all(
            isinstance(e, dict)
            and isinstance(e.get("id"), str)
            and isinstance(e.get("data"), dict)
            for e in before["entities"]
        )
    )
    previous = {entity["id"]: entity for entity in before["entities"]}
    need(len(previous) == len(before["entities"]))
    for source in sources:
        if source == shot["id"]:
            entity = entities.get(source)
        else:
            archive = archived.get(source)
            need(source not in entities and isinstance(archive, dict))
            entity = archive.get("entity")
            need(isinstance(entity, dict) and entity == previous.get(source))
            need(
                isinstance(archive.get("projection_hash"), str)
                and len(archive["projection_hash"]) == 64
                and all(c in '0123456789abcdef' for c in archive["projection_hash"])
            )
        need(entity is not None and entity.get("type") == "shot")
        item_id, revision_id = originals[source]
        projection = entity.get("data", {}).get("quickChatProjection")
        need(isinstance(projection, dict))
        need(
            projection.get("session_id") == session
            and projection.get("item_id") == item_id
            and projection.get("revision_id") == revision_id
        )
    return tuple(sources)


class StoryboardVersions:
    def __init__(self, app):
        self.app = app
        self.repo, self.settings = app.state.repository, app.state.settings
        self.admission = app.state.generation_admission
        self.access = self.admission.access
        self.tenant = self.settings.tenant_id

    def artifact_recipe(self, principal, project_id, shot_id, artifact_id):
        record = self.access.project(principal, project_id)
        project = record["payload"]
        shot = shot_entity(project, shot_id)
        row = self.access.artifact(principal, artifact_id)
        job = self.access.job(principal, row["job_id"])
        with self.repo.engine.connect() as conn:
            source_ids = migration_source_shots(
                conn, self.tenant, principal.owner, project, shot, record["version"]
            )
        if (
            job["project_id"] != project_id
            or job["request"].get("client_ref", {}).get("shot_id") not in source_ids
        ):
            raise NotFound("artifact_not_found")
        return {
            "recipe": copy.deepcopy(job["request"].get("authoring_recipe")),
            "context": copy.deepcopy(job["request"].get("authoring_context")),
        }

    def resolve_action(self, principal, project_id, action):
        snapshot = self.artifact_recipe(
            principal, project_id, action["shot_id"], action["artifact_id"]
        )
        recipe = snapshot["recipe"]
        if action["op"] == "storyboard.star":
            return None
        if recipe is None:
            raise Conflict("historical_recipe_unknown")
        from .generation_draft import prepare_patch

        sources = prepare_patch(
            recipe,
            lambda ident: self.app.state.guided.resolve(
                principal, project_id, {"op": "asset.attach", "asset_id": ident}
            ),
        )
        return recipe, sources, snapshot["context"]

    def versions(self, principal, project_id, shot_id, limit=20, offset=0):
        record = self.access.project(principal, project_id, "jobs:read")
        project = record["payload"]
        shot = shot_entity(project, shot_id)
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("Invalid page / 分页无效")
        with self.repo.engine.connect() as conn:
            source_ids = migration_source_shots(
                conn, self.tenant, principal.owner, project, shot, record["version"]
            )
            where = (
                jobs.c.tenant_id == self.tenant,
                jobs.c.owner_id == principal.owner,
                jobs.c.project_id == project_id,
                jobs.c.request["client_ref"]["shot_id"].as_string().in_(source_ids),
                jobs.c.status == "succeeded",
                artifacts.c.metadata["kind"].as_string().in_(("video", "image")),
            )
            rows = list(
                conn.execute(
                    select(
                        artifacts.c.id,
                        artifacts.c.job_id,
                        artifacts.c.metadata,
                        jobs.c.request,
                        jobs.c.created_at,
                    )
                    .join(jobs, artifacts.c.job_id == jobs.c.id)
                    .where(*where)
                    .order_by(jobs.c.created_at.desc(), artifacts.c.id)
                    .limit(limit + 1)
                    .offset(offset)
                ).mappings()
            )
            from .universal_trash import active_target

            trash = {
                r["id"]: active_target(
                    conn,
                    self.tenant,
                    principal.owner,
                    {"kind": "artifact", "project_id": project_id, "id": r["id"]},
                )
                for r in rows
            }
        values = []
        from .media_renditions import public_artifact_renditions
        from .subtitle_quality import public_quality

        for row in rows[:limit]:
            if row["metadata"].get("content_deleted"):
                continue
            entity = next(
                (
                    e
                    for e in project["entities"]
                    if e["data"].get("cloudArtifactId") == row["id"]
                ),
                None,
            )
            recipe = row["request"].get("authoring_recipe")
            values.append(
                {
                    "id": row["id"],
                    "entity_id": entity["id"] if entity else None,
                    "job_id": row["job_id"],
                    "source_shot_id": row["request"]["client_ref"]["shot_id"],
                    "aliased": row["request"]["client_ref"]["shot_id"] != shot_id,
                    "trash_state": trash[row["id"]]["payload"]["state"]
                    if trash[row["id"]]
                    else "active",
                    "trash_version": trash[row["id"]]["version"]
                    if trash[row["id"]]
                    else record["version"],
                    "recipe_status": "known" if recipe is not None else "unknown",
                    "recipe": recipe,
                    "authoring_context": row["request"].get("authoring_context"),
                    "starred": project.get("journey", {})
                    .get("storyboardStars", {})
                    .get(shot_id, {})
                    .get(row["id"], False),
                    "title": project.get("journey", {})
                    .get("storyboardTitles", {})
                    .get(shot_id, {})
                    .get(row["id"], entity.get("title", "") if entity else ""),
                    "annotation_version": record["version"],
                    "selected": bool(
                        entity and shot["data"].get("selectedAssetId") == entity["id"]
                    ),
                    "created_at": row["created_at"],
                    "kind": row["metadata"].get("kind"),
                    **public_quality(row["metadata"]),
                    "content_url": f'/v1/artifacts/{row["id"]}/content',
                    **public_artifact_renditions(row),
                }
            )
        setting = preferences(self.repo, self.tenant, principal.owner)
        _, defaults = effective_project(project, shot_id, setting["defaults"])
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "project_version": record["version"],
            "versions": values,
            "ordering": "newest_first",
            "next_offset": offset + limit if len(rows) > limit else None,
            "defaults": defaults,
        }

    def get_preferences(self, principal):
        if not principal.allows("__preferences", "projects:read"):
            raise NotFound("preferences_not_found")
        return {
            **preferences(self.repo, self.tenant, principal.owner),
            "copies_max": self.app.state.quick_chat.preferences(principal)[
                "copies_max"
            ],
            "copies_preferences_url": "/v1/quick-chat/preferences",
        }

    def profiles(self, principal, project_id):
        record = self.access.project(principal, project_id)
        project = record["payload"]
        lookup = {e["id"]: e for e in project["entities"]}
        usage = {
            e["id"]: []
            for e in project["entities"]
            if e["type"] in {"character", "location", "prop"}
        }
        for shot in (e for e in project["entities"] if e["type"] == "shot"):
            data = shot["data"]
            scene = lookup.get(shot.get("parentId"), {}).get("data", {})
            sb = data.get("storyboard")
            if sb is not None:
                bindings = data.get("cast", scene.get("cast", []))
                ids = {
                    v.get("characterId") if isinstance(v, dict) else v for v in bindings
                }
                place = sb.get("place")
                if "place" not in sb:
                    place = {
                        "locationId": data.get("locationId") or scene.get("locationId")
                    }
                if place:
                    ids.add(place.get("locationId"))
                ids.update(sb.get("propIds", []))
            elif data.get("h3", {}).get("explicitReferences"):
                ids = set()  # Historical provenance is not a live profile binding.
            else:
                bindings = list(data.get("cast", []))
                if not data.get("h3", {}).get("explicitCast"):
                    bindings += scene.get("cast", [])
                ids = {
                    v.get("characterId") if isinstance(v, dict) else v for v in bindings
                }
                ids.add(data.get("locationId") or scene.get("locationId"))
                ids.update(
                    v["source"] for v in project["links"] if v["target"] == shot["id"]
                )
            for ident in ids & usage.keys():
                usage[ident].append(shot["id"])
        return {
            "project_id": project_id,
            "project_version": record["version"],
            "usage_scope": "project",
            "profiles": [
                {
                    "id": ident,
                    "type": lookup[ident]["type"],
                    "title": lookup[ident]["title"],
                    "shot_ids": ids,
                    "shot_count": len(ids),
                }
                for ident, ids in usage.items()
            ],
        }

    def set_preferences(self, principal, body, key):
        from .quick_chat import key_check

        key_check(key)
        if not principal.allows("__preferences", "projects:write"):
            raise NotFound("preferences_not_found")
        if (
            not isinstance(body, dict)
            or set(body) != {"expected_version", "defaults"}
            or type(body["expected_version"]) is not int
        ):
            raise ValueError(
                "Expected version and defaults are required / 需要版本和默认设置"
            )
        scope = Scope(self.tenant, principal.owner, "__preferences")
        receipt_id = request_hash({"actor": principal.actor_id, "key": key})
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            old = self._document(
                conn, scope, "storyboard_preferences_receipt", receipt_id
            )
            if old:
                if old["payload"]["fingerprint"] != request_hash(body):
                    raise Conflict("edit_idempotency_conflict")
                return old["payload"]["response"]
            from .quality_authoring import normalize

            selected, _ = normalize(
                self.settings, self.repo, principal, body["defaults"], partial=True
            )
            value = validate_defaults(selected)
            current = preferences(self.repo, self.tenant, principal.owner, conn=conn)
            if current["version"] != body["expected_version"]:
                raise Conflict("document_version_conflict")
            response = {"version": current["version"] + 1, "defaults": value}
            self._put(
                conn,
                scope,
                "storyboard_preferences",
                "defaults",
                value,
                response["version"],
                current["version"] != 0,
            )
            self._put(
                conn,
                scope,
                "storyboard_preferences_receipt",
                receipt_id,
                {"fingerprint": request_hash(body), "response": response},
                1,
            )
        return response

    def _document(self, conn, scope, kind, ident):
        return (
            conn.execute(
                select(documents).where(
                    self.repo._scope(documents, scope),
                    documents.c.kind == kind,
                    documents.c.document_id == ident,
                )
            )
            .mappings()
            .first()
        )

    def _put(self, conn, scope, kind, ident, payload, version=1, exists=False):
        values = dict(payload=payload, version=version, updated_at=self.repo.clock())
        if exists:
            conn.execute(
                update(documents)
                .where(
                    self.repo._scope(documents, scope),
                    documents.c.kind == kind,
                    documents.c.document_id == ident,
                )
                .values(**values)
            )
        else:
            conn.execute(
                insert(documents).values(
                    tenant_id=scope.tenant_id,
                    owner_id=scope.owner_id,
                    project_id=scope.project_id,
                    kind=kind,
                    document_id=ident,
                    **values,
                )
            )

    def get_preflight(self, principal, project_id, ident):
        self.access.project(principal, project_id, "jobs:write")
        scope = Scope(self.tenant, principal.owner, project_id)
        with self.repo.engine.connect() as conn:
            row = self._document(conn, scope, "storyboard_preflight", ident)
        if not row:
            raise NotFound("storyboard_preflight_not_found")
        return self._public_preflight(row["payload"], principal)

    @staticmethod
    def _public_preflight(value, principal=None):
        from .credits_pricing import aggregate_quotes, user_estimate

        items = copy.deepcopy(value["items"])
        complete = all(item.get("plan") for item in items)
        credit_summary = aggregate_quotes(
            [item.get("plan") for item in items], complete=complete
        )
        hide_dollars = bool(credit_summary) and (
            principal is None or principal.machine or principal.role != "admin"
        )
        if hide_dollars:
            for item in items:
                if item.get("plan"):
                    item["plan"]["estimate"] = user_estimate(
                        item["plan"]["estimate"], hide_dollars=True
                    )
        costs = [
            item["plan"]["estimate"].get("cost_microusd")
            for item in items
            if item.get("plan")
        ]
        # Runtime is an immutable allowance, never a guessed queue ETA.
        runtimes = [item.get("machine_seconds") for item in items]
        return {
            "id": value["id"],
            "project_id": value["project_id"],
            "project_version": value["project_version"],
            "status": "blocked"
            if value.get("blocked")
            else "completed"
            if complete
            else "pending",
            "items": items,
            **credit_summary,
            "request_hash": value["fingerprint"],
            "no_generation_submitted": True,
            **(
                {"blocked": value["blocked"], "closed_preflight": True}
                if value.get("blocked")
                else {}
            ),
            "ready": not value.get("blocked", False)
            and complete
            and all(item["plan"]["status"] == "ready" for item in items),
            "estimate": user_estimate(
                {
                    "cost_microusd": sum(costs)
                    if complete and all(type(c) is int for c in costs)
                    else None,
                    "machine_seconds": sum(runtimes)
                    if complete and all(type(v) in (int, float) for v in runtimes)
                    else None,
                    "kind": "budget_reservation",
                    "final_bill": False,
                    "runtime_kind": "planned_machine_allowance",
                    "eta_seconds": None,
                },
                hide_dollars=hide_dollars,
            ),
        }

    def preflight(self, principal, project_id, body, key):
        from .quick_chat import key_check
        from .generation_admission import reject_managed

        key_check(key)
        if (
            not isinstance(body, dict)
            or set(body)
            - {"expected_version", "shot_ids", "copies", "capabilities_version"}
            or type(body.get("expected_version")) is not int
            or type(body.get("copies")) is not int
            or body["copies"] < 1
            or not isinstance(body.get("shot_ids"), list)
            or not body["shot_ids"]
            or any(not isinstance(i, str) for i in body["shot_ids"])
            or len(set(body["shot_ids"])) != len(body["shot_ids"])
        ):
            raise ValueError("Invalid storyboard preflight / 分镜预检参数无效")
        record = self.access.project(principal, project_id, "jobs:write")
        reject_managed(record["payload"])
        scope = Scope(self.tenant, principal.owner, project_id)
        ident = "storyboard-" + request_hash(
            {
                "tenant": self.tenant,
                "owner": principal.owner,
                "project": project_id,
                "actor": principal.actor_id,
                "key": key,
            }
        )
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            assert_project_writable(conn, self.tenant, principal.owner, project_id)
            old = self._document(conn, scope, "storyboard_preflight", ident)
            if old:
                if old["payload"]["fingerprint"] != request_hash(body):
                    raise Conflict("edit_idempotency_conflict")
                value = old["payload"]
                if value.get("blocked"):
                    return self._public_preflight(value, principal)
            else:
                project_row = self._document(
                    conn,
                    Scope(self.tenant, principal.owner, "__projects"),
                    "project",
                    project_id,
                )
                if (
                    not project_row
                    or project_row["version"] != body["expected_version"]
                ):
                    raise Conflict("document_version_conflict")
                project = project_row["payload"]
                caps = self.admission.capabilities_provider()
                if not version_matches(
                    body.get("capabilities_version", caps["capabilities_version"]),
                    caps["capabilities_version"],
                ):
                    raise Conflict("capabilities_changed")
                self.app.state.quick_chat._copies_allowed(
                    principal, body["copies"], conn
                )
                setting = preferences(
                    self.repo, self.tenant, principal.owner, conn=conn
                )
                # Bound materialization using the existing configured byte quota,
                # not a second hidden business copies limit.
                minimum = sum(
                    len(
                        json.dumps(
                            {
                                "shot_id": ident,
                                "index": 0,
                                "seed": "0",
                                "plan_id": "0" * 36,
                                "plan": None,
                            },
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                    )
                    for ident in body["shot_ids"]
                )
                if body["copies"] * minimum > self.settings.max_project_bytes:
                    raise ValueError(
                        "Preflight exceeds configured document byte quota / 预检超出配置的文档字节限额"
                    )
                from .generation_draft import read_draft

                items = []
                for shot_id in body["shot_ids"]:
                    render, _ = effective_project(project, shot_id, setting["defaults"])
                    draft, _ = read_draft(render, shot_id, capabilities=caps)
                    if draft.get("deployment_profile_id"):
                        # Current explicit deployment profiles are all native
                        # WangP, validated through their actual compiler schema.
                        from .inference.wangp_profile_dispatch import control_schema
                        from .capabilities import RECIPES

                        schema = control_schema(
                            draft["deployment_profile_id"], RECIPES[draft["recipe_id"]]
                        )["seed"]
                        seed_max = int(schema["maximum_decimal"])
                    else:
                        if self.settings.execution_backend == "wangp-worker":
                            from .inference.wangp_compiler import control_schema

                            seed_max = int(control_schema()["seed"]["maximum_decimal"])
                        else:
                            seed_max = (1 << 64) - 1
                    bits = seed_max.bit_length()
                    explicit = draft["controls"].get("seed")
                    if explicit is not None and not 0 <= int(explicit) <= seed_max:
                        raise ValueError(
                            "Seed exceeds selected runtime domain / 种子超出当前运行时范围"
                        )
                    chosen = set()
                    for index in range(body["copies"]):
                        seed = (
                            int(explicit)
                            if index == 0 and explicit is not None
                            else secrets.randbits(bits) % (seed_max + 1)
                        )
                        while seed in chosen:
                            seed = (seed + 1) % (seed_max + 1)
                        chosen.add(seed)
                        items.append(
                            {
                                "shot_id": shot_id,
                                "index": index,
                                "seed": str(seed),
                                "plan_id": str(
                                    uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        ident + ":" + shot_id + ":" + str(index),
                                    )
                                ),
                                "plan": None,
                            }
                        )
                value = {
                    "id": ident,
                    "project_id": project_id,
                    "project_version": project_row["version"],
                    "fingerprint": request_hash(body),
                    "settings_version": setting["version"],
                    "items": items,
                }
                if (
                    len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                    > self.settings.max_project_bytes
                ):
                    raise ValueError(
                        "Preflight exceeds configured document byte quota / 预检超出配置的文档字节限额"
                    )
                self._put(conn, scope, "storyboard_preflight", ident, value)
        # Each plan is authoritative in the original plan table. A crash between
        # plan commit and projection commit recovers that exact ID, never redraws.
        for item in value["items"]:
            if item.get("plan"):
                continue
            try:
                plan = self.repo.get_plan(scope, item["plan_id"])
                result = self.admission.planning.retained_plan(plan, principal)
            except NotFound:
                current = self.access.project(principal, project_id, "jobs:write")
                try:
                    if (
                        preferences(self.repo, self.tenant, principal.owner)["version"]
                        != value["settings_version"]
                    ):
                        raise Conflict("storyboard_defaults_changed")
                    result = self.admission.preflight(
                        principal,
                        current["payload"],
                        item["shot_id"],
                        expected_version=value["project_version"],
                        seed_override=item["seed"],
                        plan_id=item["plan_id"],
                    )
                except (Conflict, ValueError) as error:
                    # Only deterministic validation/source conflicts close an
                    # unpaid preflight. Storage/network/runtime unknowns retain
                    # their original operation for explicit reconciliation.
                    if isinstance(error, Conflict) and str(error) not in {
                        "storyboard_defaults_changed",
                        "document_version_conflict",
                        "shot_version_conflict",
                    }:
                        raise
                    with self.repo.transaction() as conn:
                        owner_reference_gate(conn, self.tenant, principal.owner)
                        latest = self._document(
                            conn, scope, "storyboard_preflight", ident
                        )
                        payload = copy.deepcopy(latest["payload"])
                        payload["blocked"] = (
                            "source_changed"
                            if isinstance(error, Conflict)
                            else "invalid_generation_input"
                        )
                        self._put(
                            conn,
                            scope,
                            "storyboard_preflight",
                            ident,
                            payload,
                            latest["version"] + 1,
                            True,
                        )
                    return self.get_preflight(principal, project_id, ident)
                plan = self.repo.get_plan(scope, item["plan_id"])
            runtime = (
                plan["execution_plan"].get("expected_runtime_s")
                if plan["execution_plan"].get("quote_known") is True
                else None
            )
            with self.repo.transaction() as conn:
                owner_reference_gate(conn, self.tenant, principal.owner)
                latest = self._document(conn, scope, "storyboard_preflight", ident)
                payload = copy.deepcopy(latest["payload"])
                target = next(
                    v for v in payload["items"] if v["plan_id"] == item["plan_id"]
                )
                target.update(plan=result, machine_seconds=runtime)
                if (
                    len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    > self.settings.max_project_bytes
                ):
                    payload = copy.deepcopy(latest["payload"])
                    payload["blocked"] = "preflight_document_size_exceeded"
                self._put(
                    conn,
                    scope,
                    "storyboard_preflight",
                    ident,
                    payload,
                    latest["version"] + 1,
                    True,
                )
                if payload.get("blocked"):
                    return self._public_preflight(payload, principal)
        return self.get_preflight(principal, project_id, ident)


def register_routes(app):
    from fastapi import Header, Query, Request

    service = StoryboardVersions(app)
    app.state.storyboard_versions = service

    @app.get("/v1/storyboard/preferences")
    def get_preferences(request: Request):
        return service.get_preferences(request.state.principal)

    @app.patch("/v1/storyboard/preferences")
    def set_preferences(
        request: Request,
        body: StoryboardPreferencesRequest,
        idempotency_key: str = Header(...),
    ):
        value = body.model_dump(exclude_unset=True)
        try:
            return service.set_preferences(
                request.state.principal, value, idempotency_key
            )
        except Conflict as error:
            if str(error) == "document_version_conflict":
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {
                        "detail": {
                            "code": "document_version_conflict",
                            "resource": "storyboard_preferences",
                            "no_mutation_committed": True,
                            "request_hash": request_hash(value),
                            "message": "Defaults changed; reload before editing. / 默认设置已改变，请刷新后编辑。",
                        }
                    },
                    status_code=409,
                )
            raise

    @app.get("/v1/projects/{project_id}/shots/{shot_id}/versions")
    def versions(
        project_id: str,
        shot_id: str,
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return service.versions(
            request.state.principal, project_id, shot_id, limit, offset
        )

    @app.get("/v1/projects/{project_id}/storyboard/profiles")
    def profiles(project_id: str, request: Request):
        return service.profiles(request.state.principal, project_id)

    @app.post("/v1/projects/{project_id}/storyboard/preflights")
    def preflight(
        project_id: str,
        request: Request,
        body: StoryboardPreflightRequest,
        idempotency_key: str = Header(...),
    ):
        body = body.model_dump(exclude_unset=True)
        try:
            return service.preflight(
                request.state.principal, project_id, body, idempotency_key
            )
        except Conflict as error:
            if str(error) == "document_version_conflict":
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {
                        "detail": {
                            "code": "document_version_conflict",
                            "project_id": project_id,
                            "no_mutation_committed": True,
                            "request_hash": request_hash(body),
                            "message": "Story changed; reload before a new preflight. / 故事已更改，请刷新后重新预检。",
                        }
                    },
                    status_code=409,
                )
            raise

    @app.get("/v1/projects/{project_id}/storyboard/preflights/{ident}")
    def get_preflight(project_id: str, ident: str, request: Request):
        return service.get_preflight(request.state.principal, project_id, ident)

"""Complete persisted-reference closure for an original Quick Chat cleanup.

Caller holds the tenant/owner reference gate. No storage IO or schema creation.
Unknown historical storage identities are retained rather than guessed.
"""
from __future__ import annotations

from .asset_objects import ASSET_OBJECT_ROLES

import hashlib
import json
import re

from sqlalchemy import inspect, select

from .content_lifecycle import reference_ids
from .repository import artifacts, attempts, documents, jobs, plans, registered_workers
from .storage import key_belongs_to


TERMINAL = {"succeeded", "failed", "cancelled"}


def rows_if_present(connection, table):
    return list(connection.execute(select(table)).mappings()) if inspect(connection).has_table(table.name) else []


def history_blocker(connection, service, owner, session):
    from .quick_chat import objects
    from .assets import asset_table
    from .storage_asset_journal import receipts
    from .artifact_writer import write_receipts
    project = session["payload"]["project_id"]
    # Original owner reference gate also protects import recovery before uploads exist.
    for row in connection.execute(select(documents).where(documents.c.tenant_id==service.tenant,
            documents.c.owner_id==owner,documents.c.project_id=="__library",
            documents.c.kind=="library_import")).mappings():
        payload=row["payload"]; request=payload.get("request",{})
        if payload.get("state") not in {"completed","blocked"} and project in {
                request.get("source",{}).get("project_id"),request.get("target_project_id")}:
            return "authoring_operation_unresolved"
    # The hidden projection is immutable to generic authoring. Include every
    # historical execution/revision, not only the latest card or displayed job.
    children = list(connection.execute(select(objects).where(objects.c.tenant == service.tenant,
        objects.c.owner == owner, objects.c.session_id == session["id"])).mappings())
    target_jobs = list(connection.execute(select(jobs).where(jobs.c.tenant_id == service.tenant,
        jobs.c.owner_id == owner, jobs.c.project_id == project)).mappings())
    linked = {row["payload"].get("job_id") for row in children if row["kind"] == "execution"}
    for ident in linked - {None} - {row["id"] for row in target_jobs}:
        row = connection.execute(select(jobs).where(jobs.c.id == ident,
            jobs.c.tenant_id == service.tenant, jobs.c.owner_id == owner)).mappings().first()
        if row is None:
            return "job_binding_unknown"
        target_jobs.append(row)
    ids = {row["id"] for row in target_jobs}
    if any(row["status"] not in TERMINAL or row["lease_worker_id"] is not None
           or row["lease_expires_at"] is not None for row in target_jobs):
        return "jobs_active_or_uncertain"
    if ids:
        previous = connection.execute(select(attempts).where(attempts.c.job_id.in_(ids))).mappings()
        if any((row["submission_started_at"] is not None or row["upstream_task_id"] is not None)
               and row["upstream_stopped"] != 1 for row in previous):
            return "upstream_stop_unconfirmed"
        if connection.execute(select(registered_workers.c.id).where(
                registered_workers.c.current_job_id.in_(ids), registered_workers.c.state != "retired")).first():
            return "worker_obligation_unresolved"
    for row in children:
        payload = row["payload"]
        if row["kind"] == "execution" and (payload.get("status") in
                {"pending_admission", "unknown", "recovery_hold"} or
                payload.get("status") == "admission_blocked" and not payload.get("job_id")):
            return "admission_unresolved"
        if row["kind"] in {"turn", "import", "result_import"} and payload.get("status") in {"running", "pending", "unknown"}:
            return "authoring_operation_unresolved"
    if (session["payload"].get("title_generation") or {}).get("status") in {"pending", "running"}:
        return "authoring_operation_unresolved"
    for row in rows_if_present(connection, receipts):
        if row["tenant"] == service.tenant and row["owner"] == owner and row["project_id"] == project:
            receipt = json.loads(row["record"])
            if receipt.get("busy") or any(spec.get("phase") != "verified" for spec in receipt.get("objects", {}).values()):
                return "upload_unresolved"
    for row in rows_if_present(connection, write_receipts):
        if row["tenant"] == service.tenant and row["owner"] == owner and row["job_id"] in ids:
            if json.loads(row["record"]).get("phase") != "settled":
                return "collection_unresolved"
    # Legacy rows without a journal must already be durable/ready.
    if any(row["status"] not in {"ready", "duplicate", "purged"} for row in
           connection.execute(select(asset_table).where(asset_table.c.tenant == service.tenant,
               asset_table.c.owner == owner, asset_table.c.project_id == project)).mappings()):
        return "upload_unresolved"
    return None


def _object(spec):
    key = spec.get("object_key", spec.get("key"))
    size = spec.get("size_bytes", spec.get("size"))
    sha = spec.get("sha256")
    if (not isinstance(key, str) or type(size) is not int or size <= 0
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
        return None
    return {"object_key": key, "size_bytes": size, "sha256": sha}


def cleanup_plan(connection, service, principal, sessions, *, media_targets=None, ignored_trash_ids=(), purged_projects=()):
    from .quick_chat import objects
    from .assets import asset_table
    from .storage_asset_journal import receipts
    from .artifact_writer import write_receipts
    owner, tenant = principal.owner, service.tenant
    session_ids = {row["id"] for row in sessions}
    project_ids = {row["payload"]["project_id"] for row in sessions}
    all_jobs = list(connection.execute(select(jobs)).mappings())
    target_jobs = [row for row in all_jobs if row["tenant_id"] == tenant and row["owner_id"] == owner
                   and row["project_id"] in project_ids]
    job_ids = {row["id"] for row in target_jobs}
    all_assets = list(connection.execute(select(asset_table)).mappings())
    candidate_assets = {row["id"]: json.loads(row["record"]) for row in all_assets
        if row["tenant"] == tenant and row["owner"] == owner and row["project_id"] in project_ids and row["status"] != "purged"}
    if media_targets is not None:
        candidate_assets = {row["id"]:json.loads(row["record"]) for row in all_assets if row["tenant"]==tenant
            and row["owner"]==owner and row["id"] in media_targets["asset_ids"] and row["status"]!="purged"}
    all_artifacts = list(connection.execute(select(artifacts)).mappings())
    candidate_artifacts = {row["id"]: dict(row) for row in all_artifacts if row["job_id"] in job_ids
                           and not row["metadata"].get("content_deleted")}
    if media_targets is not None:
        owned_jobs={r["id"] for r in all_jobs if r["tenant_id"]==tenant and r["owner_id"]==owner}
        candidate_artifacts={r["id"]:dict(r) for r in all_artifacts if r["job_id"] in owned_jobs
            and r["id"] in media_targets["artifact_ids"] and not r["metadata"].get("content_deleted")}
    assets_kept, artifacts_kept, warnings = set(), set(), []

    def retain(value, reason, *, project_id=None, label=None):
        asset_ids, artifact_ids = reference_ids(value)
        for kind, identities, candidates, retained in (("asset", asset_ids, candidate_assets, assets_kept),
                                                       ("artifact", artifact_ids, candidate_artifacts, artifacts_kept)):
            for ident in identities & candidates.keys():
                if ident not in retained:
                    retained.add(ident)
                    warning = {"kind": kind, "id": ident, "reason": reason}
                    if project_id and principal.allows(project_id, "projects:read"):
                        warning.update(project_id=project_id, label=label or "其他故事中使用")
                    warnings.append(warning)

    all_documents = list(connection.execute(select(documents)).mappings())
    for row in all_documents:
        if media_targets is not None and row["tenant_id"]==tenant and row["owner_id"]==owner:
            # Only explicit universal purge may ignore this owner's completed
            # import audit capsule. Live documents and accepted inputs remain below.
            if row["project_id"]=="__library" and row["kind"]=="library_import" and row["payload"].get("state")=="completed": continue
            if row["project_id"]=="__trash" and (row["kind"]=="trash_action" or
                    row["kind"]=="trash_item" and (row["document_id"] in ignored_trash_ids or row["payload"].get("state")=="purged")): continue
            if row["project_id"]=="__projects" and row["document_id"] in purged_projects: continue
        if row["tenant_id"] == tenant and row["owner_id"] == owner and (
                row["project_id"] in project_ids or row["project_id"] == "__projects" and row["document_id"] in project_ids):
            continue
        visible = row["tenant_id"] == tenant and row["owner_id"] == owner
        retain(row["payload"], "story_or_profile_reference", project_id=row["document_id"] if visible else None,
               label=row["payload"].get("title") if visible else None)
    authoring = list(connection.execute(select(objects)).mappings())
    for row in authoring:
        if row["kind"] in {"history_preview", "history_action", "content_tombstone", "universal_purge"}:
            continue
        if row["tenant"] == tenant and row["owner"] == owner and row["session_id"] in session_ids:
            continue
        retain(row["payload"], "other_session")
    for row in connection.execute(select(plans)).mappings():
        if row["tenant_id"] == tenant and row["owner_id"] == owner and row["project_id"] in project_ids:
            if not any(job["plan_id"] == row["id"] and job["id"] not in job_ids for job in all_jobs):
                continue
        retain({"request": row["request"], "execution": row["execution_plan"]}, "other_plan_or_job")
    for row in all_jobs:
        if row["id"] not in job_ids:
            # A job's own output association is not a live input reference.
            retained_job={"request":row["request"],"execution":row["execution_plan"]}
            own_output = (media_targets is not None and row["tenant_id"]==tenant and row["owner_id"]==owner
                and any(a["job_id"]==row["id"] for a in candidate_artifacts.values()))
            if not own_output: retained_job["result"]=row["result"]
            retain(retained_job, "other_plan_or_job")
    for row in all_assets:
        if row["id"] not in candidate_assets:
            retain(json.loads(row["record"]), "derived_asset_reference")
    for row in all_artifacts:
        if row["id"] not in candidate_artifacts:
            retain(row["metadata"], "derived_asset_reference")
    from .guided import receipts as edit_receipts
    for row in rows_if_present(connection, edit_receipts):
        if media_targets is not None and row["tenant"]==tenant and row["owner"]==owner: continue
        if not (row["tenant"] == tenant and row["owner"] == owner and row["project_id"] in project_ids):
            retain(row["response"], "other_story_revision")

    # Retained derivatives preserve their source chain. A referenced generated
    # version retains the required video/audio pair from that original attempt.
    changed = True
    while changed:
        before = (len(assets_kept), len(artifacts_kept))
        for ident in list(assets_kept):
            retain(candidate_assets[ident], "derived_asset_reference")
        paired = {(candidate_artifacts[ident]["job_id"], candidate_artifacts[ident]["attempt_id"])
                  for ident in artifacts_kept}
        for ident, row in candidate_artifacts.items():
            if (row["job_id"], row["attempt_id"]) in paired and ident not in artifacts_kept:
                retain({"artifact_id": ident}, "same_generated_version")
        changed = before != (len(assets_kept), len(artifacts_kept))

    uploads = {row["id"]: json.loads(row["record"]) for row in rows_if_present(connection, receipts)
               if row["tenant"] == tenant and row["owner"] == owner}
    writes = {(row["job_id"], row["attempt_id"]): json.loads(row["record"])
              for row in rows_if_present(connection, write_receipts)
              if row["tenant"] == tenant and row["owner"] == owner}
    identity = ({"provider": "local", "root": str(service.storage.root)} if hasattr(service.storage, "root") else
        {key: getattr(service.storage.config, key) for key in ("provider", "endpoint_url", "region", "bucket", "service", "profile")})
    writer_binding = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    blob_owners, specs = {}, {}
    def add(kind, ident, spec, binding):
        spec = _object(spec)
        if spec is None or not key_belongs_to(spec["object_key"], owner):
            return False
        key = spec["object_key"]
        if key in specs and any(specs[key][field] != spec[field] for field in ("size_bytes", "sha256")):
            return False
        specs[key] = {**spec, "storage_binding": binding, "bindings": [service.assets.storage_binding, writer_binding], "state": "pending"}
        blob_owners.setdefault(key, set()).add((kind, ident))
        return True
    for ident, asset in candidate_assets.items():
        receipt = uploads.get(ident)
        bound = receipt and receipt.get("storage_binding") == service.assets.storage_binding
        # A ready original can have an explicitly running display backfill.
        # Preserve the entire asset while any original-receipt write outcome
        # is unknown; do not delete only the visible subset of its keys.
        if receipt and (receipt.get('busy') or receipt.get('rendition_backfill',{}).get('phase') not in (None,'complete')
                or set(receipt.get('objects',{})) != {role for role in ASSET_OBJECT_ROLES if asset.get(role)}
                or any(spec.get('phase')!='verified' or any(spec.get(k)!=asset.get(role,{}).get(k)
                    for k in ('key','size_bytes','sha256','content_type'))
                    for role,spec in receipt.get('objects',{}).items())):
            retain({'asset_id':ident},'original_upload_unconfirmed')
            bound=False
        valid = bool(bound) and all(add("asset", ident, asset[role], service.assets.storage_binding)
            for role in ASSET_OBJECT_ROLES if asset.get(role))
        if not valid or not asset.get("original"):
            retain({"asset_id": ident}, "unknown_storage_binding")
    for ident, row in candidate_artifacts.items():
        receipt = writes.get((row["job_id"], row["attempt_id"]))
        spec = row["metadata"]
        valid = (receipt and receipt.get("phase") == "settled" and receipt.get("binding") == writer_binding
            and any(_object(role) == _object(spec) for role in receipt.get("roles", {}).values()))
        if not valid or not add("artifact", ident, spec, writer_binding):
            retain({"artifact_id": ident}, "unknown_storage_binding")
    # Cross-ID/owner aliases conservatively retain the physical object. No
    # prefix sweep or assumption that a session's project owns every key.
    for row in all_assets:
        if row["id"] in candidate_assets:
            continue
        asset = json.loads(row["record"])
        for role in ASSET_OBJECT_ROLES:
            spec = _object(asset.get(role, {}))
            if spec and spec["object_key"] in blob_owners:
                for kind, ident in blob_owners[spec["object_key"]]:
                    retain({kind+"_id": ident}, "shared_object_key")
    for row in all_artifacts:
        if row["id"] not in candidate_artifacts and row["metadata"].get("object_key") in blob_owners:
            for kind, ident in blob_owners[row["metadata"]["object_key"]]:
                retain({kind+"_id": ident}, "shared_object_key")
    # Retain mixed accounting aliases before closing sources and companion
    # outputs. A kept original/model key retains its whole original asset.
    for aliases in blob_owners.values():
        if len({kind for kind, _ in aliases}) > 1:
            for kind, ident in aliases:
                retain({kind+"_id": ident}, "shared_object_key")
    changed = True
    while changed:
        before = (len(assets_kept), len(artifacts_kept))
        for ident in list(assets_kept):
            record = candidate_assets[ident]
            retain(record, "derived_asset_reference")
            for role in ASSET_OBJECT_ROLES:
                spec = _object(record.get(role, {}))
                if spec:
                    for kind, alias in blob_owners.get(spec["object_key"], ()):
                        retain({kind+"_id": alias}, "shared_object_key")
        paired = {(candidate_artifacts[ident]["job_id"], candidate_artifacts[ident]["attempt_id"])
                  for ident in artifacts_kept}
        for ident, record in candidate_artifacts.items():
            if (record["job_id"], record["attempt_id"]) in paired:
                retain({"artifact_id": ident}, "same_generated_version")
            if ident in artifacts_kept:
                for kind, alias in blob_owners.get(record["metadata"].get("object_key"), ()):
                    retain({kind+"_id": alias}, "shared_object_key")
        changed = before != (len(assets_kept), len(artifacts_kept))
    removed_assets = sorted(set(candidate_assets)-assets_kept)
    removed_artifacts = sorted(set(candidate_artifacts)-artifacts_kept)
    removed_refs = {("asset", ident) for ident in removed_assets} | {("artifact", ident) for ident in removed_artifacts}
    delete_specs = [spec for key, spec in sorted(specs.items()) if blob_owners[key] and blob_owners[key] <= removed_refs]
    audit_jobs=job_ids if media_targets is None else {candidate_artifacts[i]["job_id"] for i in removed_artifacts}
    audit_projects=project_ids if media_targets is None else ({r["project_id"] for r in all_jobs if r["id"] in audit_jobs}
        | {r["project_id"] for r in all_assets if r["id"] in removed_assets})
    return {"session_ids": sorted(session_ids), "project_ids": sorted(audit_projects), "job_ids": sorted(audit_jobs),
        "asset_ids": removed_assets, "artifact_ids": removed_artifacts, "objects": delete_specs,
        "warnings": warnings, "counts": {"sessions": len(sessions),
            "turns": sum(row["kind"] == "turn" and row["session_id"] in session_ids for row in authoring),
            "cards": sum(row["kind"] == "card" and row["session_id"] in session_ids for row in authoring),
            "assets": len(removed_assets), "artifacts": len(removed_artifacts), "objects": len(delete_specs),
            "bytes": sum(spec["size_bytes"] for spec in delete_specs)},
        "kept": {"assets": len(assets_kept), "artifacts": len(artifacts_kept),
            "objects": len(specs)-len(delete_specs), "bytes": sum(v["size_bytes"] for v in specs.values())-
                sum(spec["size_bytes"] for spec in delete_specs)}}

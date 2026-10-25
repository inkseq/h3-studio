"""Explicit editing intent; no extraction, generation, backfill or new authority."""

from sqlalchemy import select

from .repository import artifacts, jobs


def effective_track(track, preferences):
    """Project the preference without overwriting independent mute/review intent."""
    if (
        isinstance(track, dict)
        and track.get("generatedFrom")
        and not track.get("detached")
        and preferences.get(track.get("shotId")) == "off"
    ):
        return {**track, "muted": True}
    return track


def sync_clip_sound(service, conn, principal, project_id, before, project):
    """Run once inside the normal actions CAS, before the atomic document save.

    Only a changed selection or explicit off/absent -> on opts in. Saved tracks
    are never rebound or unmuted implicitly; sound.generated remains the explicit
    confirmation path for stale bindings. Legacy reads/full saves stay untouched.
    """
    previous = {e["id"]: e for e in before["entities"]}
    lookup = {e["id"]: e for e in project["entities"]}
    for shot in list(project["entities"]):
        if shot["type"] != "shot":
            continue
        data = shot["data"]
        old = previous.get(shot["id"], {}).get("data", {})
        selected = data.get("selectedAssetId")
        if not isinstance(selected, str):
            continue
        changed = bool(selected) and selected != old.get("selectedAssetId")
        opted_in = data.get("clipSound") == "on" and old.get("clipSound") != "on"
        if not changed and not opted_in:
            continue
        if changed and "clipSound" not in data:
            data["clipSound"] = old.get("clipSound", "on")
        if data.get("clipSound") != "on":
            continue
        video = lookup.get(selected)
        scene = lookup.get(shot.get("parentId"))
        if not video or video["type"] != "video" or not scene:
            continue
        tracks = (
            project.get("journey", {})
            .get("soundTracks", {})
            .get(scene.get("parentId"), [])
        )
        if not isinstance(tracks, list) or any(not isinstance(t, dict) for t in tracks):
            continue
        if any(isinstance(t, dict) and t.get("shotId") == shot["id"] for t in tracks):
            continue  # Preserve explicit, muted, detached and stale track intent.
        artifact_id = video["data"].get("cloudArtifactId")
        if not isinstance(artifact_id, str) or not principal.allows(
            project_id, "jobs:read"
        ):
            continue
        trusted = (
            conn.execute(
                select(artifacts)
                .join(jobs, artifacts.c.job_id == jobs.c.id)
                .where(
                    artifacts.c.id == artifact_id,
                    jobs.c.tenant_id == service.settings.tenant_id,
                    jobs.c.owner_id == principal.owner,
                    jobs.c.project_id == project_id,
                    jobs.c.status == "succeeded",
                )
            )
            .mappings()
            .first()
        )
        if (
            not trusted
            or trusted["metadata"].get("validated") is not True
            or trusted["metadata"].get("kind") != "video"
            or trusted["job_id"] != video["data"].get("sourceJobId")
        ):
            continue
        candidates = [
            row
            for row in conn.execute(
                select(artifacts).where(artifacts.c.job_id == trusted["job_id"])
            ).mappings()
            if row["metadata"].get("kind") == "audio"
            and row["metadata"].get("validated") is True
            and row["metadata"].get("content_type", row["metadata"].get("mime"))
            == "audio/flac"
        ]
        if len(candidates) != 1:
            continue
        audio_id = candidates[0]["id"]
        audio = next(
            (
                e
                for e in project["entities"]
                if e["data"].get("cloudArtifactId") == audio_id
            ),
            None,
        )
        if audio is not None and (
            audio["type"] != "audio"
            or audio["data"].get("sourceJobId") != trusted["job_id"]
            or audio["data"].get("mime") != "audio/flac"
        ):
            continue
        if audio is not None and any(
            t.get("assetId") == audio["id"]
            and (not t.get("generatedFrom") or t.get("detached"))
            for t in tracks
        ):
            continue  # Do not duplicate detached/unanchored/manual audio.
        # Existing artifact resolver repeats owner/project/status authorization,
        # sanitizes storage metadata, and uses the normal stable media entity ID.
        action = {"op": "artifact.adopt", "artifact_id": audio_id}
        resolved = service.resolve(principal, project_id, action)
        # sound.generated requires one matching library entity too; ambiguity is
        # not an invitation to replace user editing intent.
        if any(
            e["type"] == "audio"
            and e["data"].get("sourceJobId") == trusted["job_id"]
            and e["data"].get("mime") == "audio/flac"
            and e["data"].get("cloudArtifactId") != audio_id
            for e in project["entities"]
        ):
            continue
        service.apply(project, action, resolved)
        service.apply(project, {"op": "sound.generated", "shot_id": shot["id"]})

"""Owner-scoped original render sources shared by HTTP and film execution."""

from sqlalchemy import select

from .assets import AssetNotFound
from .repository import NotFound, artifacts, jobs


class RenderSources:
    def __init__(self, repo, settings, assets, access):
        self.repo, self.settings, self.assets, self.access = (
            repo,
            settings,
            assets,
            access,
        )

    def resolve(self, principal, project_id, entity, kind):
        repo, settings, asset_service = self.repo, self.settings, self.assets
        owned_job = self.access.job
        data = entity.get("data", {})
        artifact_id, asset_id = data.get("cloudArtifactId"), data.get("cloudAssetId")
        if artifact_id:
            if not isinstance(artifact_id, str):
                raise NotFound("source_not_found")
            with repo.engine.connect() as connection:
                row = (
                    connection.execute(
                        select(artifacts)
                        .join(jobs, artifacts.c.job_id == jobs.c.id)
                        .where(
                            artifacts.c.id == artifact_id,
                            jobs.c.tenant_id == settings.tenant_id,
                            jobs.c.owner_id == principal.owner,
                            jobs.c.project_id == project_id,
                            jobs.c.status == "succeeded",
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise NotFound("source_not_found")
            metadata = row["metadata"]
            job = owned_job(principal, row["job_id"])
            if metadata.get("kind") != kind or not metadata.get("validated"):
                raise ValueError("成片来源尚未校验")
            return {
                "kind": kind,
                "duration": metadata.get("duration_s"),
                "source_job_id": row["job_id"],
                "artifact_id": row["id"],
                "object": {
                    "key": metadata["object_key"],
                    "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"],
                    "content_type": metadata.get("content_type", metadata.get("mime")),
                },
                "simulation": job["execution_plan"].get("backend") == "mock"
                or job["request"].get("simulation") is True,
            }
        if not isinstance(asset_id, str):
            raise ValueError("需要先把所选本机素材上传到当前云项目")
        try:
            asset = asset_service.get(principal.owner, asset_id, project_id)
        except AssetNotFound:
            raise NotFound("source_not_found") from None
        if asset.get("status") != "ready" or asset.get("kind") != kind:
            raise ValueError("素材尚未就绪或类型不符")
        return {
            "kind": kind,
            "duration": asset["metadata"].get("source_duration"),
            "source_job_id": None,
            "artifact_id": None,
            "object": asset["original"],
            "simulation": False,
        }

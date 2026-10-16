"""Closed original-ledger host metadata; no transport or provider requests."""
import uuid
from sqlalchemy import select
from .repository import jobs

def _host_telemetry_context(repo, *, node_id, worker_id, spec, job_id):
    """One bounded original-node read per callback; diagnostics never gate work.

    A slot, owner, configuration and physical instance must all match before a
    provider identity is attached. Select only the job's closed binding fields,
    never its prompt/materials. No provider/Cloud request is made here.
    """
    try:
        if (not isinstance(node_id, str) or str(uuid.UUID(node_id)) != node_id
                or worker_id != "dstack-" + node_id.replace("-", "")
                or not isinstance(spec, dict) or spec.get("backend") != "wangp-worker"
                or spec.get("provider") != "vast"):
            return {}
        from .host_policy import bound_observation, finite
        from .operator_capacity import operator_nodes
        with repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes).where(
                operator_nodes.c.intent_id == node_id)).mappings().first()
            job = connection.execute(select(jobs.c.owner_id, jobs.c.tenant_id,
                jobs.c.request["deployment_profile_id"].as_string().label("profile_id"),
                jobs.c.request["request"]["mode"].as_string().label("mode"))
                .where(jobs.c.id == job_id)).mappings().first()
        if node is None or job is None:
            return {}
        payload, binding = node["payload"], node["payload"].get("dstack", {})
        now = repo.clock()
        observed = bound_observation(dict(node), now=now)
        if (observed is None or payload.get("tenant_id") != job["tenant_id"]
                or payload.get("owner_id") != job["owner_id"]
                or binding.get("profile_id") != job["profile_id"]
                or binding.get("mode") != job["mode"]
                or binding.get("configuration_id") != spec.get("configuration_id")
                or binding.get("manifest_digest") != spec.get("engine_manifest_digest")
                or observed["provider_instance_id"] != spec.get("instance_id")):
            return {}
        result = {key: observed[key] for key in ("machine_id", "region", "reliability",
            "quoted_hourly_microusd", "allocated_ram_mib", "allocated_vram_mib", "cpu")}
        result["host_identity_source"] = "vast_machine_id"
        gpu = "".join(c.lower() for c in observed["gpu"] if c.isalnum())
        result["gpu_type"] = ("rtx5090" if gpu in {"rtx5090", "nvidiageforcertx5090"}
            else "rtx-pro6000" if "pro6000" in gpu else "unknown")
        result["dstack_run"] = binding.get("run_name")
        model = binding.get("preparation", {}).get("model", {})
        stamp = binding.get("observed_at")
        result["warmth"] = "unknown"
        if (binding.get("runtime_incarnation") and finite(stamp, minimum=max(0, now-30), maximum=now)
                and isinstance(model, dict)):
            if model.get("state") == "loaded":
                result["warmth"] = "warm"
            elif model.get("state") == "not_loaded":
                result["warmth"] = "cold"
        return result
    except Exception:
        # Optional observation cannot replace original claim/recovery errors.
        return {}

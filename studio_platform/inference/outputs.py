"""Existing output requirements shared by the worker and engine adapters.

Keep the legacy H3 fallback and CPU-render validation during extraction. This
module does not select engines, rewrite accepted requests, or start inference.
"""

import math

from .protocol import BackendError

from .native_outputs import (
    NATIVE_DELIVERY,  # noqa: F401 -- public compatibility re-export
    GPU_R2_DELIVERY,  # noqa: F401 -- public compatibility re-export
    NATIVE_POLICIES,  # noqa: F401 -- public compatibility re-export
    NATIVE_EVIDENCE_FIELDS,  # noqa: F401 -- public compatibility re-export
    validate_delivery_policy,  # noqa: F401 -- public compatibility re-export
    native_delivery_spec,  # noqa: F401 -- public compatibility re-export
    delivery_spec,  # noqa: F401 -- public compatibility re-export
    gpu_delivery,  # noqa: F401 -- public compatibility re-export
    validate_delivery_evidence,  # noqa: F401 -- public compatibility re-export
)


def _request(job):
    return job["request"].get("request", job["request"])


def _shape(job):
    if job["request"].get("recipe_id") == "chapter-roughcut-v1":
        # Pure lazy import avoids the renderer->worker Outcome dependency cycle.
        from ..render_backend import validate_render_request

        shape = validate_render_request(job["request"], owner_id=job.get("owner_id"))
        return shape["width"], shape["height"], shape["duration"], shape["audio"]
    from comfy_workflow import native_output_spec

    request = _request(job)
    spec = job["request"].get("output_spec") or native_output_spec(request)
    width, height, duration = (
        int(spec["width"]),
        int(spec["height"]),
        float(request.get("duration", 5)),
    )
    if not (
        256 <= width <= 1536
        and 256 <= height <= 1536
        and width % 32 == height % 32 == 0
        and math.isfinite(duration)
        and 4 <= duration <= 15
    ):
        raise BackendError("invalid_output_requirements")
    return width, height, duration, bool(request.get("generate_audio", True))

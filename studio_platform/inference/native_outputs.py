"""Pure native output delivery contract shared by CPU and packaged GPU adapters.

This module performs validation only; it never imports renderers or worker code.
"""
import math

from .protocol import BackendError

NATIVE_DELIVERY = "native-frames-v1"
GPU_R2_DELIVERY = "native-frames-r2-v1"
NATIVE_POLICIES = frozenset({NATIVE_DELIVERY, GPU_R2_DELIVERY})
NATIVE_EVIDENCE_FIELDS = {"delivery_spec", "frame_count", "container_duration_s", "audio_duration_s"}


def validate_delivery_policy(backend, policy):
    """An operator/slot capability, never a public generation control."""
    if policy != "" and (backend != "wangp-worker" or policy not in NATIVE_POLICIES):
        raise ValueError("unsupported_output_delivery_policy")


def native_delivery_spec(compiled, policy=NATIVE_DELIVERY):
    """Resolve new-plan delivery once; do not rewrite native or requested inputs."""
    from comfy_workflow import native_output_spec
    validate_delivery_policy("wangp-worker", policy)
    if policy not in NATIVE_POLICIES:
        raise ValueError("invalid_native_delivery_spec")
    request, output = compiled["request"], compiled["output_spec"]
    if (compiled.get("recipe_id") not in {"h3-base-fl2va-v1", "h3-base-ref2va-v1"}
            or type(request.get("duration")) is not int
            or output != native_output_spec(request)
            or type(output.get("frames")) is not int):
        raise ValueError("invalid_native_delivery_spec")
    if compiled.get('deployment_profile_id'):
        from .wangp_profile_dispatch import normalize_request
        normalize_request(request, {k:v['metadata'] for k,v in compiled['assets'].items()}, output,
            compiled['deployment_profile_id'])
    elif compiled["recipe_id"] == "h3-base-ref2va-v1":
        from .wangp_ref_compiler import normalize_request
        normalize_request(request, {k:v["metadata"] for k,v in compiled["assets"].items()}, output)
    return {"policy": policy, "fps": 24, "frame_count": output["frames"],
            "duration_s": output["frames"] / 24, "requested_duration_s": request["duration"]}


def delivery_spec(job):
    """Validate the immutable delivery contract; absent means historical export."""
    execution = job.get("execution_plan", {})
    policy = execution.get("output_delivery", "")
    value = execution.get("delivery_spec")
    if "output_delivery" not in execution and "delivery_spec" not in execution:
        return None
    try:
        validate_delivery_policy(execution.get("backend"), policy)
        expected = native_delivery_spec(job["request"], policy)
        if (policy not in NATIVE_POLICIES or not isinstance(value, dict) or value != expected
                or any(type(value[k]) is not type(v) for k, v in expected.items())):
            raise ValueError
        return dict(value)
    except (KeyError, TypeError, ValueError):
        raise BackendError("invalid_output_delivery_contract") from None


def gpu_delivery(job):
    """Only the accepted immutable policy selects the GPU export route."""
    value = delivery_spec(job)
    return value is not None and value["policy"] == GPU_R2_DELIVERY


def validate_delivery_evidence(job, evidence, kind):
    """Publication metadata may describe only the accepted delivery contract."""
    delivery = delivery_spec(job)
    if delivery is None:
        if NATIVE_EVIDENCE_FIELDS.intersection(evidence):
            raise ValueError("unexpected_native_delivery_evidence")
        return
    if evidence.get("delivery_spec") != delivery:
        raise ValueError("artifact_delivery_evidence_mismatch")
    if kind == "video" and (type(evidence.get("frame_count")) is not int
            or evidence["frame_count"] != delivery["frame_count"]
            or evidence.get("fps") != delivery["fps"]
            or evidence.get("duration_s") != delivery["duration_s"]):
        raise ValueError("artifact_native_frames_mismatch")
    for field in ("duration_s", "container_duration_s", "audio_duration_s"):
        if field in evidence and (type(evidence[field]) not in (int, float)
                or not math.isfinite(evidence[field])
                or abs(evidence[field]-delivery["duration_s"]) > .1):
            raise ValueError("artifact_native_timing_mismatch")



"""Read-only startup telemetry from the original dstack node receipt.

Progress does not grant admission, settle bills or contact a GPU/provider. Missing
old-host telemetry remains unknown. Only static diagnostics and bounded counters
cross this public seam; remote exception text and download URLs never do.
"""

from __future__ import annotations

import copy
import math

from .dstack_capacity import NATIVE_REASON_CODES
from .lium_bootstrap import BOOT_FAILURE_CODES, BOOT_PHASES

PHASES = BOOT_PHASES | {
    "provisioning",
    "image_pull",
    "model_load",
    "stopping",
    "stopped",
    "unknown",
}
MODEL_STATES = {"not_loaded", "loading", "loaded", "not_observed"}
DIAGNOSTICS = (
    NATIVE_REASON_CODES
    | BOOT_FAILURE_CODES
    | {
        "UnclassifiedBootstrapFailure",
        "dstack_native_observation_failed",
        "dstack_response_invalid",
        "dstack_runtime_not_observed",
        "dstack_apply_requires_reconciliation",
        "dstack_exact_run_absent",
        "dstack_stop_pending",
        "dstack_run_terminal_billing_pending",
        "dstack_no_capacity_billing_pending",
        "dstack_never_allocated",
        "dstack_allocation_requires_reconciliation",
        "dstack_business_obligations_pending",
        "dstack_stop_requires_reconciliation",
        "dstack_runtime_busy",
        "dstack_runtime_ready",
        "dstack_api_rejected",
        "dstack_api_unavailable",
        "dstack_response_limit",
        "dstack_backend_binding_mismatch",
        "dstack_run_binding_mismatch",
        "dstack_run_ownership_mismatch",
        "dstack_allocation_binding_mismatch",
        "dstack_native_binding_mismatch",
        "dstack_native_incarnation_changed",
        "dstack_runtime_ready_model_load_unobserved",
        "matching_worker_unavailable",
        "worker_heartbeat_expired",
        "worker_window_expired",
        "dstack_broker_unavailable",
    }
)
REASONS = {
    "dstack_no_capacity_billing_pending": "没有满足当前条件的可用机器；未分配实例，账单待核对",
    "dstack_never_allocated": "启动失败，已核实未分配机器",
    "dstack_allocation_requires_reconciliation": "启动分配结果尚未确认，保留原记录等待核对",
    "model_download_timeout": "权重下载超时",
    "model_download_failed": "权重下载失败",
    "ModelDownloadFailed": "权重下载失败",
    "model_download_size_mismatch": "下载文件大小与清单不符",
    "model_download_disk_headroom": "磁盘剩余空间不足，无法下载权重",
    "model_disk_headroom": "模型所需磁盘空间不足",
    "InsufficientCacheDiskSpace": "模型缓存磁盘空间不足",
    "runtime_process_exited": "推理服务进程已经退出",
    "runtime_readiness_timeout": "推理服务未在规定时间内响应",
    "wangp_model_load_failed": "模型加载失败，推理服务仍需检查",
    "CUDAUnavailable": "GPU 的 CUDA 环境不可用",
    "dstack_runtime_ssh_unavailable_or_host_key_untrusted": "SSH 连接不可用或主机密钥未通过校验",
    "dstack_runtime_ssh_coordinates_unavailable": "供应商尚未返回 SSH 连接信息",
    "dstack_runtime_observation_busy": "另一轮启动观测正在执行，等待其完成",
    "dstack_runtime_source_changed": "运行源码与该机器绑定的版本不符",
    "dstack_runtime_configuration_mismatch": "运行配置与原启动请求不符",
    "dstack_runtime_configuration_missing": "控制服务尚未配置模型运行环境",
    "dstack_runtime_configuration_unavailable": "无法读取模型运行环境配置",
    "dstack_runtime_config_invalid": "模型运行环境配置无效",
    "dstack_runtime_port_range_invalid": "模型连接端口范围配置无效",
    "dstack_runtime_retained_port_invalid": "原机器记录中的连接端口无效，需要管理员核对",
    "dstack_runtime_port_conflict": "模型连接端口与另一台机器冲突，需要管理员核对",
    "dstack_runtime_port_changed": "模型连接端口与原机器记录不符，需要管理员核对",
    "dstack_runtime_stopped_port_unallocated": "已停止的机器没有保留模型连接端口",
    "dstack_runtime_ports_exhausted": "控制服务的模型连接端口已用完，暂时无法准备机器",
    "dstack_runtime_known_hosts_invalid": "SSH 主机密钥记录配置无效",
    "dstack_runtime_source_index_invalid": "模型运行源码索引无效",
    "dstack_runtime_source_entry_invalid": "模型运行源码条目无效",
    "dstack_runtime_source_profile_invalid": "模型运行源码对应的部署配置无效",
    "dstack_runtime_source_binding_invalid": "模型运行源码与部署配置不符",
    "dstack_runtime_source_set_invalid": "模型运行源码文件不完整",
    "dstack_runtime_source_selection_missing": "尚未配置所选模型和运行模式的源码",
    "dstack_native_observation_failed": "未能读取本轮推理服务观测",
    "dstack_response_invalid": "dstack 返回了无法校验的观测",
    "dstack_api_unavailable": "dstack 控制服务暂时无法连接，等待下一轮观测",
    "dstack_api_rejected": "dstack 控制服务拒绝了本轮观测请求",
    "matching_worker_unavailable": "推理接口已启动，等待兼容执行槽注册并接通队列",
    "worker_heartbeat_expired": "执行槽心跳已过期，暂不能接任务",
    "dstack_runtime_bootstrap_failed": "GPU 启动失败，查看阶段诊断代码",
    "dstack_runtime_bootstrap_reconciliation_required": "启动结果尚未确认，请核对原启动记录",
}
NORMAL = {
    "dstack_runtime_preparing",
    "dstack_runtime_downloading_weights",
    "dstack_runtime_starting_runtime",
    "dstack_runtime_loading_model",
    "dstack_runtime_busy",
    "dstack_runtime_ready",
    "dstack_runtime_ready_model_load_unobserved",
    "dstack_stop_pending",
    "dstack_run_terminal_billing_pending",
    "dstack_runtime_not_observed",
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _stamp(value, now):
    return value if _number(value) and value <= now else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 1024**4 else None


def _known(value, choices):
    return isinstance(value, str) and value in choices


def safe_preparation(value, now):
    """Strict additive JSON subset, including old/malformed receipt handling."""
    if not isinstance(value, dict):
        return None
    result = {
        "phase": value.get("phase") if _known(value.get("phase"), PHASES) else "unknown"
    }
    for key in ("started_at", "phase_started_at", "updated_at"):
        result[key] = _stamp(value.get(key), now)
    if _known(value.get("reason_code"), DIAGNOSTICS):
        result["reason_code"] = value["reason_code"]
    if _known(value.get("failure_phase"), PHASES):
        result["failure_phase"] = value["failure_phase"]
    download = value.get("download")
    if isinstance(download, dict):
        result["download"] = {
            key: _count(download.get(key))
            for key in (
                "bytes_complete",
                "bytes_total",
                "files_complete",
                "files_total",
            )
        }
        result["download"]["basis"] = "completed_files"
    image = value.get("image")
    if isinstance(image, dict):
        result["image"] = {
            key: _count(image.get(key))
            for key in ("downloaded_bytes", "extracted_bytes", "total_bytes")
        }
        result["image"]["is_total_bytes_final"] = (
            image.get("is_total_bytes_final") is True
        )
    model = value.get("model")
    if isinstance(model, dict):
        result["model"] = {
            "state": model.get("state")
            if _known(model.get("state"), MODEL_STATES)
            else "not_observed",
            "phase_started_at": _stamp(model.get("phase_started_at"), now),
            "updated_at": _stamp(model.get("updated_at"), now),
        }
    return result


def record_preparation(binding, value, observed_at, *, validation_at=None):
    """Keep the latest safe telemetry in the same row and latch old phase time.

    A controller retry must not restart the stage stopwatch. Completed download
    counts remain useful after the service starts; a missing field cannot erase
    them. These counters never determine readiness or paid-resource authority.
    """
    # Provider proof is timestamped before a potentially long SSH operation;
    # do not refresh its age just to accept the later native receipt timestamp.
    validation_at = observed_at if validation_at is None else validation_at
    incoming = safe_preparation(value, validation_at)
    if incoming is None:
        return
    prior = safe_preparation(binding.get("preparation"), validation_at) or {}
    phase_same = incoming["phase"] == prior.get("phase")
    incoming["phase_started_at"] = (
        incoming.get("phase_started_at")
        or (prior.get("phase_started_at") if phase_same else None)
        or observed_at
    )
    for key in ("download", "image", "model"):
        if key not in incoming and key in prior:
            incoming[key] = copy.deepcopy(prior[key])
    incoming["started_at"] = incoming.get("started_at") or prior.get("started_at")
    binding["preparation"] = incoming


def _transfer(source, *, image=False):
    if not isinstance(source, dict):
        return None
    complete = source.get("bytes_complete")
    if image:
        downloaded, extracted = (
            source.get("downloaded_bytes"),
            source.get("extracted_bytes"),
        )
        complete = (
            downloaded + extracted
            if _number(downloaded) and _number(extracted)
            else None
        )
    total = source.get("total_bytes" if image else "bytes_total")
    # dstack totals are not final until the native client explicitly says so.
    if image and source.get("is_total_bytes_final") is not True:
        total = None
    percent = (
        100 * complete / total
        if (_number(complete) and _number(total) and total > 0 and complete <= total)
        else None
    )
    result = {"bytes_complete": complete, "bytes_total": total, "percent": percent}
    if image:
        result.update(
            downloaded_bytes=source.get("downloaded_bytes"),
            extracted_bytes=source.get("extracted_bytes"),
            basis="download_and_extract",
        )
    else:
        result.update(
            files_complete=source.get("files_complete"),
            files_total=source.get("files_total"),
            basis="completed_files",
        )
    return result


def preparation_view(node, intent, public, now, *, fresh_seconds=30):
    """Owner-scoped public presentation; lifecycle/admission stays authoritative."""
    binding = node["payload"].get("dstack", {})
    raw = safe_preparation(binding.get("preparation"), now) or {}
    started = _stamp(binding.get("apply_started_at"), now)
    if started is None:
        started = _stamp(intent.get("created_at"), now)
    phase = raw.get("phase") or binding.get("bootstrap_phase")
    if phase in {"failed", "setup_failed"} and raw.get("failure_phase"):
        phase = raw["failure_phase"]
    model = raw.get("model") or {
        "state": "not_observed",
        "updated_at": None,
        "phase_started_at": None,
    }
    lifecycle = public.get("lifecycle_phase")
    mapped = {
        "provisioning": "renting",
        "image_pull": "pulling_image",
        "model_download": "downloading_weights",
        "download_file": "downloading_weights",
        "model_download_waiting_for_shared_cache": "downloading_weights",
        "runtime_start": "starting_runtime",
        "runtime_verification": "verifying_runtime",
        "model_load": "loading_model",
        "runtime_ready": "ready",
    }.get(phase, "preparing")
    phase_started = raw.get("phase_started_at")
    if lifecycle in {"stopping", "stopped"}:
        mapped = lifecycle
        phase_started = _stamp(binding.get("stop_started_at"), now)
    elif model["state"] == "loading":
        mapped = "loading_model"
        phase_started = model.get("phase_started_at")
    elif public.get("current_job"):
        mapped = "running_job"
    elif public.get("native_ready") and model["state"] == "not_loaded":
        mapped = "awaiting_first_job"
    elif public.get("ready"):
        mapped = "ready"
    elif phase is None and lifecycle == "renting":
        mapped = "renting"
        phase_started = started
    end = (
        _stamp(binding.get("first_stopped_at"), now) if lifecycle == "stopped" else now
    )
    observed = _stamp(binding.get("observed_at"), now)
    age = now - observed if observed is not None else None
    source_updated = raw.get("updated_at")
    stale = lifecycle != "stopped" and (age is None or age > fresh_seconds)
    # Only describes absence of a telemetry update. A large in-flight file or
    # long model load can produce no new completed-file count without failing.
    stalled = mapped in {
        "downloading_weights",
        "starting_runtime",
        "verifying_runtime",
        "loading_model",
    } and (source_updated is not None and now - source_updated > 120)
    # An old startup error remains historical telemetry after the run ends.
    # The current lifecycle owns its terminal reason; this projection never
    # infers allocation, billing or a refund from a stale preparation receipt.
    reason = (
        public.get("reason_code")
        if lifecycle == "stopped"
        else raw.get("reason_code") or public.get("reason_code")
    )
    if (
        lifecycle != "stopped"
        and public.get("native_ready")
        and not public.get("ready")
        and not public.get("current_job")
    ):
        reason = public.get("dispatch_reason_code") or reason
    reason = reason if _known(reason, DIAGNOSTICS) and reason not in NORMAL else None
    text = REASONS.get(reason)
    if reason and text is None:
        text = "当前阶段未能完成，请根据诊断代码排查"
    if not text and stale:
        text = "启动观测未及时更新，当前状态尚待确认"
    elif not text and stalled:
        text = "一段时间未收到新的进度；可能仍在处理当前文件或加载组件，尚不能判定失败"
    return {
        "phase": mapped,
        "started_at": started,
        "phase_started_at": phase_started,
        "updated_at": source_updated,
        "elapsed_seconds": end - started
        if end is not None and started is not None and end >= started
        else None,
        "phase_elapsed_seconds": end - phase_started
        if end is not None and phase_started is not None and end >= phase_started
        else None,
        "observation_age_seconds": age,
        "stale": stale,
        "stalled": stalled,
        "reason_code": reason,
        "reason_text": text,
        "failure_phase": raw.get("failure_phase"),
        "download": _transfer(raw.get("download")),
        "image": _transfer(raw.get("image"), image=True),
        "model": model,
    }

"""Static, credential-safe validation diagnostics; never expose exception text."""

from __future__ import annotations

import math
import re
import uuid

# Closed source-level vocabulary, not a regex that could disclose a credential
# disguised as a well-formed exception code. Unknown exceptions remain opaque.
WANGP_VALIDATION_CODES = frozenset(
    {
        'wangp_invalid_request',
        'wangp_profile_model_mismatch',
        'wangp_unsupported_controls',
        'wangp_invalid_prompt',
        'wangp_invalid_steps',
        'wangp_invalid_window_seconds',
        'wangp_unsupported_step_skipping',
        'wangp_window_with_injected_frames_unsupported',
        'wangp_unsupported_steps',
        'wangp_invalid_seed',
        'wangp_invalid_duration',
        'wangp_output_spec_mismatch',
        'wangp_invalid_native_frames',
        'wangp_invalid_inputs',
        'wangp_invalid_input_identity',
        'wangp_input_kind_mismatch',
        'wangp_input_snapshot_mismatch',
        'wangp_profile_reference_count_exceeded',
        'wangp_ref_media_not_ready',
        'wangp_ref_invalid_dimensions',
        'wangp_ref_aligned_video_required',
        'wangp_ref_selected_audio_required',
        'wangp_ref_reference_required',
        'wangp_ref_audio_requires_visual_reference',
        'wangp_ref_total_video_duration_exceeded',
        'wangp_ref_total_audio_duration_exceeded',
        'wangp_adapter_fl_reference_inputs_unmapped',
        'wangp_adapter_ref_first_last_unmapped',
        'wangp_adapter_reference_video_soundtrack_unmapped',
        'wangp_unknown_deployment_profile',
        'wangp_profile_mode_unsupported',
        'wangp_first_last_images_only',
        'wangp_ref_first_last_or_guides_unsupported',
        'wangp_ref_soundtrack_unsupported',
    }
    | {
        'wangp_unsupported_' + name
        for name in (
            'sampler_name',
            'scheduler',
            'denoise',
            'shift_video',
            'shift_audio',
            'video_decode',
            'video_tile_size',
            'video_overlap',
            'audio_decode',
            'encoder_device',
            'generate_audio',
            'export_crf',
        )
    }
)

# Native v2 source-level validation vocabulary; unknown/free-form text stays opaque.
WANGP_VALIDATION_CODES |= frozenset(
    {
        'wangp_asset_owner_mismatch',
        'wangp_audio_excerpts_require_video_soundtrack',
        'wangp_audio_from_video_requires_denoise',
        'wangp_audio_guide_requires_soundtrack',
        'wangp_audio_refinement_soundtrack_conflict',
        'wangp_conflicting_guides_alias',
        'wangp_conflicting_image_anchors',
        'wangp_conflicting_video_audio',
        'wangp_control_audio_requires_soundtrack',
        'wangp_control_source_count_exceeded',
        'wangp_control_video_canvas_mismatch',
        'wangp_control_video_required',
        'wangp_control_video_timeline_mismatch',
        'wangp_denoise_requires_control_video',
        'wangp_duplicate_input_handle',
        'wangp_excerpt_outside_selected_source',
        'wangp_excerpts_require_single_reference_video',
        'wangp_fl_audio_requires_soundtrack',
        'wangp_fl_reference_image_unsupported',
        'wangp_fl_reference_video_unsupported',
        'wangp_frame_injection_denoise_conflict',
        'wangp_guide_outside_output',
        'wangp_image_size_requires_reference_images',
        'wangp_input_handles_mismatch',
        'wangp_input_kind_mismatch',
        'wangp_input_snapshot_mismatch',
        'wangp_invalid_audio_conditioning',
        'wangp_invalid_audio_refinement',
        'wangp_invalid_compiled_request',
        'wangp_invalid_custom_settings',
        'wangp_invalid_duration',
        'wangp_invalid_excerpt_duration',
        'wangp_invalid_excerpt_position',
        'wangp_invalid_excerpts',
        'wangp_invalid_denoise',
        'wangp_invalid_shift_video',
        'wangp_native_timed_video_guide_unsupported',
        'wangp_native_timed_audio_guide_unsupported',
        'wangp_invalid_export_crf',
        'wangp_invalid_generate_audio',
        'wangp_invalid_guide_audio',
        'wangp_invalid_guide_time',
        'wangp_invalid_guides',
        'wangp_invalid_image_positions',
        'wangp_invalid_image_refs_relative_size',
        'wangp_invalid_input_identity',
        'wangp_invalid_inputs',
        'wangp_invalid_mask_mode',
        'wangp_invalid_native_flags',
        'wangp_invalid_prompt',
        'wangp_invalid_request',
        'wangp_invalid_seed',
        'wangp_invalid_steps',
        'wangp_invalid_window_seconds',
        'wangp_unsupported_step_skipping',
        'wangp_window_with_injected_frames_unsupported',
        'wangp_invalid_video_audio',
        'wangp_invalid_video_conditioning',
        'wangp_manifest_binding_mismatch',
        'wangp_mask_mode_requires_denoise',
        'wangp_output_spec_mismatch',
        'wangp_profile_model_mismatch',
        'wangp_profile_reference_count_exceeded',
        'wangp_profile_settings_mismatch',
        'wangp_ref_aligned_video_required',
        'wangp_ref_audio_requires_visual_reference',
        'wangp_ref_invalid_dimensions',
        'wangp_ref_media_not_ready',
        'wangp_ref_reference_required',
        'wangp_ref_selected_audio_required',
        'wangp_ref_total_audio_duration_exceeded',
        'wangp_ref_total_excerpt_duration_exceeded',
        'wangp_ref_total_video_duration_exceeded',
        'wangp_selected_video_has_no_audio',
        'wangp_single_soundtrack_required',
        'wangp_staged_input_mismatch',
        'wangp_unbound_input_handle',
        'wangp_unsupported_controls',
        'wangp_unsupported_sampler_name',
        'wangp_unused_input_handle',
        'wangp_video_conditioning_required',
        'wangp_video_excerpts_require_explicit_audio_excerpts',
        'wangp_video_soundtrack_requires_single_video',
    }
)

STATIC_VALIDATION_TEXT = {
    'Reference video exceeds the output length; increase duration or explicitly trim the reference': (
        'reference_video_exceeds_output',
        '参考视频比输出时长长，请增加生成时长或选取更短片段。',
    ),
    '首尾帧配方不能混用全能参考；请明确切换配方或解除关联': (
        'incompatible_fl_ref_inputs',
        '当前适配器分开首尾帧和全能参考输入，请明确选择一种。',
    ),
    '全能参考配方不接受首尾帧约束；原素材仍保留，请明确更改关联': (
        'incompatible_fl_ref_inputs',
        '当前适配器分开首尾帧和全能参考输入，请明确选择一种。',
    ),
    '全能参考至少需要一份参考素材': (
        'reference_required',
        '全能参考需要至少一份参考素材。',
    ),
    'duration must be between 4 and 15 seconds': (
        'invalid_duration',
        '生成时长须在4–15秒范围内。',
    ),
    'Custom width and height must be multiples of 32': (
        'invalid_canvas_grid',
        '自定义宽高须为32的倍数。',
    ),
    'Custom canvas must not exceed the 768*1344 native pixel area': (
        'invalid_canvas_area',
        '自定义画面的像素面积超出原生画面范围。',
    ),
    'Custom canvas aspect ratio must be between 0.4 and 2.5': (
        'invalid_canvas_aspect',
        '自定义画面宽高比须为0.4–2.5。',
    ),
}
COMMON_CODES = {
    'budget_exceeded',
    'job_capacity_exceeded',
    'shot_version_conflict',
    'asset_not_ready',
    'source_hash_conflict',
    'capabilities_version_conflict',
    'insufficient_scope',
    'version_conflict',
}
POLICY_DIAGNOSTICS = {
    'capacity_source_changed_before_activation': (
        'The draft changed before activation. Preflight the current draft; the original job and rental accounting are retained.',
        '启动前草稿已改变，请重新预检当前草稿；原任务和机器费用记录保留。',
    ),
    'content_moderation_refused': (
        'This request did not pass the content policy. No generation credits were charged.',
        '本次请求未通过内容规则，未扣生成积分。',
    ),
    'content_moderation_unavailable': (
        'Content review is temporarily unavailable. The original request is retained.',
        '内容审核暂不可用，原请求已保留。',
    ),
    'content_moderation_changed': (
        'The content policy changed. Preview this request again.',
        '内容规则已更新，请重新预览本次请求。',
    ),
    'content_moderation_receipt_invalid': (
        'The content-review result could not be verified. The original request is retained.',
        '内容审核结果尚未核实，原请求已保留。',
    ),
    'terms_acceptance_required': (
        'Accept the current terms before starting a new paid request.',
        '请先接受当前条款，再开始新的付费请求。',
    ),
    'terms_not_reviewed': (
        'Purchases will open after the terms are approved.',
        '条款批准后才会开放购买。',
    ),
    'terms_version_changed': (
        'The terms changed. Accept the current version and preview again.',
        '条款已更新，请接受当前版本后重新预览。',
    ),
    'terms_changed': (
        'The terms changed. Accept the current version and preview again.',
        '条款已更新，请接受当前版本后重新预览。',
    ),
}


def preflight_diagnostic(error):
    """Return safe fields without logging raw media/prompt/path/upstream text."""
    result = {
        'error_code': 'preflight_rejected',
        'error_stage': 'preflight',
        'error_message': '预检未通过；请检查参数、素材和当前执行条件。',
    }
    if (
        isinstance(error, Exception)
        and len(error.args) == 1
        and type(error.args[0]) is str
    ):
        text = error.args[0]
        if text in POLICY_DIAGNOSTICS:
            en, zh = POLICY_DIAGNOSTICS[text]
            result.update(
                error_code=text,
                error_stage='terms' if text.startswith('terms_') else 'moderation',
                error_message=zh,
                messages={'en': en, 'zh': zh},
            )
        elif text in WANGP_VALIDATION_CODES or text in COMMON_CODES:
            result['error_code'] = text
            result['error_message'] = (
                '当前适配器尚未映射此输入或控制，请保留原设置并联系管理员。'
                if '_unmapped' in text or '_unsupported_' in text
                else '参数或参考素材不符合所选模型的支持范围，请根据错误代码检查设置。'
            )
            if text == 'budget_exceeded':
                result['error_message'] = (
                    '额度不足；可用额度不足以预留本次生成，请联系管理员调整额度。'
                )
        elif text in STATIC_VALIDATION_TEXT:
            result['error_code'], result['error_message'] = STATIC_VALIDATION_TEXT[text]
    return result


_ENUM_CONTROLS = {
    'sampler_name': {'euler', 'er_sde', 'res_multistep', 'ralston_2s'},
    'scheduler': {'auto'},
    'video_decode': {'tiled', 'full'},
    'audio_decode': {'normal', 'chunked'},
    'encoder_device': {'default', 'cpu', 'cuda'},
    'video_conditioning': {'reference', 'denoise', 'in_context'},
    'audio_conditioning': {'reference', 'soundtrack', 'from_video'},
    'audio_refinement': {'none', 'enabled'},
    'mask_mode': {'grouped_rows', 'shared_timestep'},
}
_NUMBER_CONTROLS = {
    'steps': (0, 1000),
    'denoise': (0, 1),
    'shift_video': (0, 100),
    'shift_audio': (0, 100),
    'video_tile_size': (0, 16384),
    'video_overlap': (0, 16384),
    'export_crf': (0, 51),
    'image_refs_relative_size': (0, 1000),
}
_WORKER_CODES = frozenset(
    {
        'submission_rejected',
        'upstream_generation_failed',
        'worker_preparation_not_ready',
        'worker_preparation_failed',
        'collection_failed',
        'upload_failed',
        'artifact_validation_failed',
        'output_video_verification_failed',
        'output_audio_verification_failed',
        'output_dimensions_mismatch',
        'independent_audio_missing',
        'output_too_short',
        'output_duration_unknown',
        'native_output_timing_mismatch',
        'unexpected_output_audio',
        'execution_policy_unavailable_before_submission',
        'render_cache_capacity_exhausted',
        'capacity_wait_deadline_expired',
        'capacity_approval_expired_or_revoked',
        'capacity_no_progress',
        'capacity_runtime_exceeded',
        'capacity_owner_recovery',
        'capacity_provider_removed',
        'capacity_lost_retries_exhausted',
        'machine_failed',
        'runtime_process_exited',
        'worker_process_failed',
        'worker_reconciliation_needed',
        'submission_response_unknown',
        'upstream_status_unknown',
        'generation_failure_unclassified',
        'content_moderation_refused',
        'content_moderation_unavailable',
    }
)


def _number(value, minimum=0, maximum=1_000_000):
    return (
        value
        if type(value) in (int, float)
        and minimum <= value <= maximum
        and math.isfinite(value)
        else None
    )


def _choice(value, allowed, default=None):
    return value if type(value) is str and value in allowed else default


def _identity(value):
    return (
        value
        if type(value) is str and re.fullmatch(r'[1-9][0-9]{0,17}', value)
        else None
    )


def _uuid(value):
    try:
        return value if type(value) is str and str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def failure_reason(code):
    from .inference.protocol import INFERENCE_FAILURE_CODES

    return (
        code
        if type(code) is str
        and code
        in WANGP_VALIDATION_CODES
        | COMMON_CODES
        | POLICY_DIAGNOSTICS.keys()
        | INFERENCE_FAILURE_CODES
        | _WORKER_CODES
        else 'generation_failure_unclassified'
    )


def failure_stage(code):
    if code.startswith('wangp_'):
        phase = code.split('_')[1]
        return {
            'generation': 'generate',
            'runtime': 'runtime',
            'validation': 'validation',
            'unknown': 'unknown',
        }.get(phase, 'validation')
    if code.startswith(('capacity_', 'machine_', 'runtime_', 'worker_process_')):
        return 'capacity_provision'
    if code.startswith('submission_'):
        return 'submission'
    if code.startswith(('worker_preparation_', 'render_cache_')):
        return 'input_transfer'
    if code.startswith(
        (
            'output_',
            'native_output_',
            'artifact_',
            'independent_audio_',
            'unexpected_output_',
        )
    ):
        return 'validate_output'
    return {
        'upstream_generation_failed': 'generate',
        'collection_failed': 'collect',
        'upload_failed': 'upload',
    }.get(code, 'unknown')


def _parts(job):
    compiled = job.get('request') if type(job.get('request')) is dict else {}
    request = compiled.get('request') if type(compiled.get('request')) is dict else {}
    return compiled, request


def control_summary(job):
    """Closed scalar settings and selected-media statistics, never media IDs."""
    compiled, request = _parts(job)
    controls = {
        key: _choice(request.get(key), allowed)
        for key, allowed in _ENUM_CONTROLS.items()
    }
    controls.update(
        {
            key: _number(request.get(key), *bounds)
            for key, bounds in _NUMBER_CONTROLS.items()
        }
    )
    controls['generate_audio'] = (
        request.get('generate_audio')
        if type(request.get('generate_audio')) is bool
        else None
    )
    seed = request.get('seed')
    controls['seed'] = (
        str(seed)
        if type(seed) in (str, int)
        and re.fullmatch(r'[0-9]{1,20}', str(seed))
        and int(seed) <= 2**64 - 1
        else None
    )
    for key in ('video_excerpts', 'audio_excerpts'):
        values = request.get(key)
        controls[key] = (
            [
                {
                    'time_seconds': _number(item.get('time_seconds'), maximum=15),
                    'duration_seconds': _number(
                        item.get('duration_seconds'), maximum=15
                    ),
                }
                for item in values[:3]
                if type(item) is dict
            ]
            if type(values) is list
            else []
        )
    inputs = request.get('inputs') if type(request.get('inputs')) is dict else {}
    stored_assets = (
        compiled.get('assets') if type(compiled.get('assets')) is dict else {}
    )
    assets = {
        identity: item.get('metadata', item)
        for identity, item in stored_assets.items()
        if type(item) is dict
    }
    media = {}
    for role in ('images', 'videos', 'audios'):
        ids = inputs.get(role)
        if type(ids) is not list or len(ids) > 100:
            media[role] = {
                'count': None,
                'durations_seconds': [],
                'total_duration_seconds': None,
            }
            continue
        durations = []
        if role != 'images':
            for identity in ids:
                meta = assets.get(identity, {}) if type(identity) is str else {}
                meta = meta if type(meta) is dict else {}
                durations.append(_number(meta.get('duration'), maximum=86400))
        media[role] = {
            'count': len(ids),
            'durations_seconds': durations,
            'total_duration_seconds': sum(durations)
            if role != 'images' and all(v is not None for v in durations)
            else None,
        }
    media.update(
        {role: inputs.get(role) is not None for role in ('first_frame', 'last_frame')}
    )
    guides = request.get('guides', inputs.get('guides', []))
    media['guides'] = (
        [
            {
                'kind': _choice(
                    assets.get(item.get('media_id'), {}).get('kind'),
                    {'image', 'video', 'audio'},
                ),
                'time_seconds': _number(item.get('time_seconds'), maximum=362 / 24),
                'use_audio': item.get('use_audio')
                if type(item.get('use_audio')) is bool
                else None,
            }
            for item in guides[:8]
            if type(item) is dict
            and type(item.get('media_id')) is str
            and type(assets.get(item['media_id'], {})) is dict
        ]
        if type(guides) is list
        else []
    )
    audio = request.get('video_audio')
    media['video_audio_enabled_count'] = (
        sum(v is True for v in audio.values())
        if type(audio) is dict and len(audio) <= 100
        else None
    )
    output = (
        compiled.get('output_spec') if type(compiled.get('output_spec')) is dict else {}
    )
    shape = {
        key: _number(
            output.get(
                'frame_count' if key == 'frames' else key,
                output.get(key, request.get(key)),
            ),
            maximum=100000,
        )
        for key in ('width', 'height', 'frames', 'fps')
    }
    shape.update(
        duration_s=_number(request.get('duration'), maximum=86400),
        resolution=_choice(
            request.get('resolution'), {'480P', '576P', '768P', 'custom'}
        ),
        aspect_ratio=_choice(
            request.get('aspect_ratio'),
            {'21:9', '16:9', '4:3', '1:1', '3:4', '9:16', 'custom'},
        ),
    )
    from .runtime_catalog import PROFILE_IDS

    execution = (
        job.get('execution_plan') if type(job.get('execution_plan')) is dict else {}
    )
    return {
        'controls': controls,
        'inputs': media,
        'output': shape,
        'mode': _choice(request.get('mode'), {'fl', 'ref'}, 'unknown'),
        'profile_id': _choice(
            execution.get(
                'deployment_profile_id', compiled.get('deployment_profile_id')
            ),
            PROFILE_IDS,
            'unknown',
        ),
    }


def safe_failure_summary(job, context=None):
    summary = control_summary(job)
    source = context if type(context) is dict else {}
    host = {
        key: _identity(source.get(key))
        for key in ('machine_id', 'offer_id', 'provider_instance_id')
    }
    host.update(
        node_id=_uuid(source.get('node_id')),
        provider=_choice(
            source.get('provider'),
            {'vast', 'runpod', 'lium', 'cloudrift', 'modal-dialout', 'targon', 'local'},
            'unknown',
        ),
        gpu_type=_choice(
            source.get('gpu_type'),
            {'rtx5090', 'rtx-pro6000', 'h100', 'h200', 'b200', 'b300'},
            'unknown',
        ),
    )
    code = failure_reason(job.get('error_code'))
    result = {
        'version': 1,
        'stage': failure_stage(code),
        'reason_code': code,
        **summary,
        'host': host,
        'automatic_generation_retry': False,
        'message': '生成未完成。请核对原任务的停止和费用状态，再手动重试；也可减少参考素材、时长或分辨率后重新确认。',
    }
    if code == 'content_moderation_refused':
        result['stage'] = 'moderation'
        result['messages'] = {
            'en': 'The video did not pass the content policy. No generation credits were charged.',
            'zh': '视频未通过内容规则，本次未扣生成积分。',
        }
        result['message'] = result['messages']['zh']
    return result


def failure_notice_id(job, summary):
    """Stable owner-only notice identity; random controls never affect its kind."""
    import json

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            'sixnine:failure-kind:v1:'
            + json.dumps(
                [
                    job['tenant_id'],
                    job['owner_id'],
                    summary['stage'],
                    summary['reason_code'],
                    summary['profile_id'],
                    summary['mode'],
                ],
                ensure_ascii=True,
                separators=(',', ':'),
            ),
        )
    )


def failure_log_context(job):
    """Flat bounded log fields; these must never become metric/index labels."""
    value = control_summary(job)
    context = {
        key: item
        for key, item in value['controls'].items()
        if type(item) in (str, int, float, bool)
    }
    context.update(
        {
            key: item
            for key, item in value['output'].items()
            if type(item) in (str, int, float)
        }
    )
    for plural, name in (
        ('images', 'image_refs'),
        ('videos', 'video_refs'),
        ('audios', 'audio_refs'),
    ):
        entry = value['inputs'][plural]
        if entry['count'] is not None:
            context[name] = entry['count']
        if plural != 'images' and entry['total_duration_seconds'] is not None:
            context[plural[:-1] + '_duration_s'] = entry['total_duration_seconds']
    context.update(
        first_frame=value['inputs']['first_frame'],
        last_frame=value['inputs']['last_frame'],
        guide_count=len(value['inputs']['guides']),
    )
    if value['inputs']['video_audio_enabled_count'] is not None:
        context['video_audio_enabled_count'] = value['inputs'][
            'video_audio_enabled_count'
        ]
    for kind in ('video', 'audio'):
        context[kind + '_ref_durations_s'] = value['inputs'][kind + 's'][
            'durations_seconds'
        ]
        for field in ('time_seconds', 'duration_seconds'):
            context[kind + '_excerpt_' + field] = [
                item[field] for item in value['controls'][kind + '_excerpts']
            ]
    context['guide_times_s'] = [
        item['time_seconds'] for item in value['inputs']['guides']
    ]
    context['guide_kinds'] = [item['kind'] for item in value['inputs']['guides']]
    context['guide_use_audio'] = [
        item['use_audio'] for item in value['inputs']['guides']
    ]
    return context


def safe_failure_log_fields(source):
    """Validate again at the Cloud export boundary, including injected context."""
    result = {
        key: source[key]
        for key, allowed in _ENUM_CONTROLS.items()
        if _choice(source.get(key), allowed) is not None
    }
    for key, bounds in {
        **_NUMBER_CONTROLS,
        'duration_s': (0, 86400),
        'video_duration_s': (0, 8640000),
        'audio_duration_s': (0, 8640000),
        'guide_count': (0, 8),
        'video_audio_enabled_count': (0, 100),
    }.items():
        value = _number(source.get(key), *bounds)
        if value is not None:
            result[key] = value
    for key in ('generate_audio', 'first_frame', 'last_frame'):
        if type(source.get(key)) is bool:
            result[key] = source[key]
    seed = source.get('seed')
    if (
        type(seed) is str
        and re.fullmatch(r'[0-9]{1,20}', seed)
        and int(seed) <= 2**64 - 1
    ):
        result['seed'] = seed
    for key, maximum in {
        'video_ref_durations_s': 100,
        'audio_ref_durations_s': 100,
        'video_excerpt_time_seconds': 3,
        'video_excerpt_duration_seconds': 3,
        'audio_excerpt_time_seconds': 3,
        'audio_excerpt_duration_seconds': 3,
        'guide_times_s': 8,
    }.items():
        values = source.get(key)
        if (
            type(values) is list
            and len(values) <= maximum
            and all(
                item is None or _number(item, maximum=86400) is not None
                for item in values
            )
        ):
            result[key] = list(values)
    for key, allowed in {
        'guide_kinds': {'image', 'video', 'audio'},
        'guide_use_audio': {True, False},
    }.items():
        values = source.get(key)
        if (
            type(values) is list
            and len(values) <= 8
            and all(
                item is None
                or (
                    type(item) is str and item in allowed
                    if key == 'guide_kinds'
                    else type(item) is bool
                )
                for item in values
            )
        ):
            result[key] = list(values)
    return result

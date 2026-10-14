"""Static, credential-safe validation diagnostics; never expose exception text."""
from __future__ import annotations

# Closed source-level vocabulary, not a regex that could disclose a credential
# disguised as a well-formed exception code. Unknown exceptions remain opaque.
WANGP_VALIDATION_CODES = frozenset({
    'wangp_invalid_request','wangp_profile_model_mismatch','wangp_unsupported_controls',
    'wangp_invalid_prompt','wangp_invalid_steps','wangp_unsupported_steps','wangp_invalid_seed',
    'wangp_invalid_duration','wangp_output_spec_mismatch','wangp_invalid_native_frames',
    'wangp_invalid_inputs','wangp_invalid_input_identity','wangp_input_kind_mismatch',
    'wangp_input_snapshot_mismatch','wangp_profile_reference_count_exceeded',
    'wangp_ref_media_not_ready','wangp_ref_invalid_dimensions','wangp_ref_aligned_video_required',
    'wangp_ref_selected_audio_required','wangp_ref_reference_required',
    'wangp_ref_audio_requires_visual_reference','wangp_ref_total_video_duration_exceeded',
    'wangp_ref_total_audio_duration_exceeded','wangp_adapter_fl_reference_inputs_unmapped',
    'wangp_adapter_ref_first_last_unmapped','wangp_adapter_reference_video_soundtrack_unmapped',
    'wangp_unknown_deployment_profile','wangp_profile_mode_unsupported',
    'wangp_first_last_images_only','wangp_ref_first_last_or_guides_unsupported',
    'wangp_ref_soundtrack_unsupported',
} | {'wangp_unsupported_'+name for name in ('sampler_name','scheduler','denoise',
    'shift_video','shift_audio','video_decode','video_tile_size','video_overlap','audio_decode',
    'encoder_device','generate_audio','export_crf')})

STATIC_VALIDATION_TEXT = {
    'Reference video exceeds the output length; increase duration or explicitly trim the reference':
        ('reference_video_exceeds_output','参考视频比输出时长长，请增加生成时长或选取更短片段。'),
    '首尾帧配方不能混用全能参考；请明确切换配方或解除关联':
        ('incompatible_fl_ref_inputs','当前适配器分开首尾帧和全能参考输入，请明确选择一种。'),
    '全能参考配方不接受首尾帧约束；原素材仍保留，请明确更改关联':
        ('incompatible_fl_ref_inputs','当前适配器分开首尾帧和全能参考输入，请明确选择一种。'),
    '全能参考至少需要一份参考素材':
        ('reference_required','全能参考需要至少一份参考素材。'),
    'duration must be between 4 and 15 seconds':
        ('invalid_duration','生成时长须在4–15秒范围内。'),
    'Custom width and height must be multiples of 32':
        ('invalid_canvas_grid','自定义宽高须为32的倍数。'),
    'Custom canvas must not exceed the 768*1344 native pixel area':
        ('invalid_canvas_area','自定义画面的像素面积超出原生画面范围。'),
    'Custom canvas aspect ratio must be between 0.4 and 2.5':
        ('invalid_canvas_aspect','自定义画面宽高比须为0.4–2.5。'),
}
COMMON_CODES = {'budget_exceeded','job_capacity_exceeded','shot_version_conflict','asset_not_ready',
    'source_hash_conflict','capabilities_version_conflict','insufficient_scope','version_conflict'}


def preflight_diagnostic(error):
    """Return safe fields without logging raw media/prompt/path/upstream text."""
    result = {'error_code':'preflight_rejected','error_stage':'preflight',
        'error_message':'预检未通过；请检查参数、素材和当前执行条件。'}
    if isinstance(error, Exception) and len(error.args)==1 and type(error.args[0]) is str:
        text = error.args[0]
        if text in WANGP_VALIDATION_CODES or text in COMMON_CODES:
            result['error_code'] = text
            result['error_message'] = ('当前适配器尚未映射此输入或控制，请保留原设置并联系管理员。'
                if '_unmapped' in text or '_unsupported_' in text else '参数或参考素材不符合所选模型的支持范围，请根据错误代码检查设置。')
            if text == 'budget_exceeded':
                result['error_message'] = '额度不足；可用额度不足以预留本次生成，请联系管理员调整额度。'
        elif text in STATIC_VALIDATION_TEXT:
            result['error_code'],result['error_message'] = STATIC_VALIDATION_TEXT[text]
    return result

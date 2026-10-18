"""Implemented native-profile support, independently of historical measurements.

Source: Wan2GP 0e58385fbde7ff102d276e4a9e490845de76b4ea,
models/minimax_h3/minimax_h3_handler.py (FL2VA/REF2VA infos and model definition).
The shared native canvas/17k+5 frame contract supplies platform output bounds.
Protected execution policies can impose narrower resource/budget limits.
"""

from __future__ import annotations

from .runtime_catalog import model_for, get_profile

MAX_PIXELS = 768 * 1344
MAX_FRAMES = 362
MAX_STEPS = 100  # Existing public API range, not a measured-case ceiling.
MAX_REFERENCE_PIXELS = 5760**2  # Existing inspected-upload bound.
MAX_REFERENCE_SECONDS = 15
SAMPLERS = ['euler', 'res_multistep', 'ralston_2s']


def envelope(profile_id, mode):
    model_for(profile_id, mode)
    if full_controls(profile_id):
        from .inference.wangp_full_compiler import SAMPLERS as native_samplers

        return {
            'max_pixels': MAX_PIXELS,
            'max_duration_seconds': MAX_FRAMES / 24,
            'max_steps': MAX_STEPS,
            'max_reference_files': 12,
            'max_guides': 8,
            'allow_first_last': True,
            'allow_audio': True,
            'controls': {
                'sampler_name': list(native_samplers),
                'scheduler': ['auto'],
                'video_decode': ['tiled'],
                'audio_decode': ['normal'],
                'encoder_device': ['default'],
            },
            'input_limits': {
                'max_images': 17 if mode == 'ref' else 8,
                'max_videos': 3 if mode == 'ref' else 1,
                'max_audios': 3 if mode == 'ref' else 1,
                'max_image_pixels': MAX_REFERENCE_PIXELS,
                'max_video_pixels': MAX_REFERENCE_PIXELS,
                'max_video_duration_seconds': MAX_FRAMES / 24,
                'max_audio_duration_seconds': 15,
                'guide_kinds': ['image', 'video', 'audio'],
                'guide_recipe_ids': [
                    model_for(profile_id, mode)['generation_recipe_id']
                ],
                'max_guide_time_seconds': MAX_FRAMES / 24,
                'allow_video_audio': True,
            },
        }
    reference = mode == 'ref'
    return {
        'max_pixels': MAX_PIXELS,
        'max_duration_seconds': MAX_FRAMES / 24,
        'max_steps': MAX_STEPS,
        'max_reference_files': 12 if reference else 2,
        'max_guides': 0,
        'allow_first_last': not reference,
        'allow_audio': True,
        'controls': {
            'sampler_name': list(SAMPLERS),
            'scheduler': ['auto'],
            'video_decode': ['tiled'],
            'audio_decode': ['normal'],
            'encoder_device': ['default'],
        },
        'input_limits': {
            'max_images': 9 if reference else 0,
            'max_videos': 3 if reference else 0,
            'max_audios': 3 if reference else 0,
            'max_image_pixels': MAX_REFERENCE_PIXELS,
            'max_video_pixels': MAX_REFERENCE_PIXELS,
            'max_video_duration_seconds': MAX_FRAMES / 24,
            'max_audio_duration_seconds': MAX_REFERENCE_SECONDS,
            'guide_kinds': [],
            'guide_recipe_ids': [],
            'max_guide_time_seconds': 15,
            'allow_video_audio': False,
        },
    }


def full_controls(profile_id):
    return get_profile(profile_id).get('compiler_epoch') == 'full-controls-v2'


def input_support(profile_id, mode):
    if not full_controls(profile_id):
        return dict(INPUT_SUPPORT[mode])
    return {
        **INPUT_SUPPORT[mode],
        'first_frame': True,
        'last_frame': True,
        'first_and_last': True,
        'timed_image_guides': True,
        'video_conditioning': True,
        'video_soundtrack_selection': True,
        'kept_soundtrack': True,
        'reference_excerpts': mode == 'ref',
    }


def adapter_gaps(profile_id):
    if not full_controls(profile_id):
        return list(ADAPTER_GAPS)
    return [
        'nonzero_target_time_video_or_audio_guides',
        'audio_chunked_decode',
        'arbitrary_vae_tile_configuration',
    ]


def limits(mode, profile_id=None):
    if profile_id is not None and full_controls(profile_id):
        return {
            'input_count_semantics': 'native_roles_v2',
            'max_images': 9 if mode == 'ref' else 8,
            'max_videos': 3 if mode == 'ref' else 1,
            'max_audios': 3 if mode == 'ref' else 1,
            'max_total_files': 12,
            'max_guides': 8,
            'min_clip_duration': 2,
            'max_clip_duration': 15,
            'max_video_clip_duration': MAX_FRAMES / 24,
            'max_audio_clip_duration': 15,
            'max_total_video_duration': MAX_FRAMES / 24,
            'max_total_audio_duration': 15,
        }
    return {
        'max_images': 9 if mode == 'ref' else 0,
        'max_videos': 3 if mode == 'ref' else 0,
        'max_audios': 3 if mode == 'ref' else 0,
        'max_total_files': 12 if mode == 'ref' else 2,
        'max_guides': 0,
        'min_clip_duration': 2,
        'max_clip_duration': 15,
        'max_video_clip_duration': MAX_FRAMES / 24,
        'max_audio_clip_duration': 15,
        'max_total_video_duration': MAX_FRAMES / 24,
        'max_total_audio_duration': 15,
    }


INPUT_SUPPORT = {
    'fl': {
        'first_frame': True,
        'last_frame': True,
        'first_and_last': True,
        'text_only': True,
        'reference_images': False,
        'reference_video': False,
        'reference_audio': False,
    },
    'ref': {
        'first_frame': False,
        'last_frame': False,
        'first_and_last': False,
        'text_only': False,
        'reference_images': True,
        'reference_video': True,
        'reference_audio': True,
    },
}
ADAPTER_GAPS = [
    'timed_guides',
    'reference_video_soundtrack',
    'fl_ref_mixing',
    'audio_chunked_decode',
    'per_request_runtime_decoder_or_encoder_configuration',
]

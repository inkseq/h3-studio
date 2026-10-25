"""Additive H3 controls contract for Wan2GP 0e58385 (no runtime imports).

Old profile compilers remain immutable. Native flags are constructed here, never
accepted from a caller. Source selections have already been normalized by the
asset service; this compiler never trims or substitutes an accepted input.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from .protocol import BackendError
from .wangp_compiler import MAX_SEED, control_schema as legacy_schema
from .wangp_contract import InputDescriptor, PreparedRequest, canonical_json
from ..runtime_catalog import get_profile, model_for, validate_manifest
from ..storage import key_belongs_to, validate_key

COMPILER_ID = 'sixnine-h3-native-profile-v2'
SAMPLERS = ['euler', 'er_sde', 'res_multistep', 'ralston_2s']
MAX_FRAMES = 362
FIXED = {
    'scheduler': 'auto',
    'shift_audio': 3,
    'video_decode': 'tiled',
    'video_tile_size': 256,
    'video_overlap': 64,
    'audio_decode': 'normal',
    'encoder_device': 'default',
}


STEP_SKIPPING = ('none', 'spectrum', 'first_block')
WHOLE_WINDOW = 362


def acceleration(profile_id):
    """A profile's pinned turbo LoRA binding, or None for the plain sampler."""
    value = get_profile(profile_id).get('acceleration')
    if value is None:
        return None
    component = get_profile(profile_id)['components'][value['component']]
    return {**value, 'path': component['files'][0]['path']}


def window_frames(seconds):
    """Nearest H3 window on the native 17k+5 grid, 124..362 frames."""
    frames = 17 * round((seconds * 24 - 5) / 17) + 5
    return max(124, min(WHOLE_WINDOW, frames))


def control_schema(profile_id, mode):
    model_for(profile_id, mode)
    schema = legacy_schema()
    turbo = acceleration(profile_id)
    schema.update(
        {
            # A turbo profile's LoRA is distilled for one step count; users get it fixed.
            'steps': {'type': 'integer', 'default': 20, 'minimum': 1, 'maximum': 100}
            if turbo is None
            else {
                'type': 'integer',
                'default': turbo['steps'],
                'minimum': turbo['steps'],
                'maximum': turbo['steps'],
            },
            'sampler_name': {'type': 'string', 'default': 'euler', 'enum': SAMPLERS[:]},
            'denoise': {'type': 'number', 'default': 1, 'minimum': 0.01, 'maximum': 1},
            'shift_video': {
                'type': 'number',
                'default': 12,
                'minimum': 0.01,
                'maximum': 100,
            },
            'export_crf': {
                'type': 'integer',
                'default': 18,
                'minimum': 0,
                'maximum': 51,
            },
            'generate_audio': {
                'type': 'boolean',
                'default': True,
                'description': 'False exports a silent MP4 without an audio artifact; joint model generation is unchanged.',
            },
            'video_conditioning': {
                'type': 'string',
                'enum': ['reference', 'denoise', 'in_context'],
            },
            'audio_conditioning': {
                'type': 'string',
                'default': 'reference',
                'enum': ['reference', 'soundtrack', 'from_video'],
            },
            'image_refs_relative_size': {
                'type': 'integer',
                'default': 100,
                'minimum': 50,
                'maximum': 400,
            },
            'audio_refinement': {
                'type': 'string',
                'default': 'none',
                'enum': ['none', 'enabled'],
            },
            # Long-shot speed (h3-studio#555): attention grows with the square of the
            # window, so a 15 s shot in one 362-frame window is ~3x slower than in
            # 5 s windows. Omitted = the whole shot in one window, as before.
            'window_seconds': {
                'type': 'integer',
                'minimum': 5,
                'maximum': 15,
                'description': 'Generate in H3 sliding windows of about this many seconds (0.75 s overlap). Omit for one window.',
            },
            'step_skipping': {
                'type': 'string',
                'default': 'none',
                'enum': list(STEP_SKIPPING),
                'description': 'H3 steps skipping: Spectrum feature forecasting or First Block Cache (threshold 0.08).',
            },
            'mask_mode': {
                'type': 'string',
                'default': 'grouped_rows',
                'enum': ['grouped_rows', 'shared_timestep'],
            },
            'guides': {
                'type': 'array',
                'default': [],
                'items': {
                    'type': 'object',
                    'required': ['media_id', 'time_seconds'],
                    'additionalProperties': False,
                    'properties': {
                        'media_id': {'type': 'string'},
                        'time_seconds': {'type': 'number', 'minimum': 0},
                        'use_audio': {'type': 'boolean', 'default': False},
                    },
                },
            },
            'video_excerpts': {'type': 'array', 'default': [], 'maxItems': 3},
            'audio_excerpts': {'type': 'array', 'default': [], 'maxItems': 3},
            'video_audio': {
                'type': 'object',
                'default': {},
                'additionalProperties': {'type': 'boolean'},
                'description': 'Select each reference video soundtrack explicitly; false excludes that track.',
            },
        }
    )
    # The pinned native pipeline fixes these settings; do not advertise
    # a selectable range that would only be rejected or ignored later.
    for field, value in FIXED.items():
        schema[field] = {
            **schema[field],
            'default': value,
            'enum': [value],
            'description': 'Fixed by the selected pinned native runtime.',
        }
        if type(value) in (int, float):
            schema[field].update(minimum=value, maximum=value)
    schema['duration']['default'] = 5
    schema['resolution']['default'] = '480P'
    for field in ('video_excerpts', 'audio_excerpts'):
        schema[field]['items'] = {
            'type': 'object',
            'additionalProperties': False,
            'required': ['time_seconds', 'duration_seconds'],
            'properties': {
                'time_seconds': {
                    'type': 'number',
                    'minimum': 0,
                    'description': 'Center in the selected source clip, not output time.',
                },
                'duration_seconds': {'type': 'number', 'minimum': 2, 'maximum': 15},
            },
        }
    schema['video_excerpts']['description'] = (
        'Durations are explicitly normalized to the native 17k+5 frame grid before the plan is frozen; edge clamping is rejected.'
    )
    return schema


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _enum(value, choices, code):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(code)
    return value


def _bound(value, low, high, code, *, integer=False):
    if (
        type(value) is not int if integer else not _number(value)
    ) or not low <= value <= high:
        raise ValueError(code)
    return value


def _identity(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', value)
        or value in {'.', '..'}
    ):
        raise ValueError('wangp_invalid_input_identity')
    return value


def _excerpts(value, metadata, video, *, check_media, video_frames=False):
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError('wangp_invalid_excerpts')
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            'time_seconds',
            'duration_seconds',
        }:
            raise ValueError('wangp_invalid_excerpts')
        center = _bound(
            entry['time_seconds'], 0, MAX_FRAMES / 24, 'wangp_invalid_excerpt_position'
        )
        duration = _bound(
            entry['duration_seconds'], 2, 15, 'wangp_invalid_excerpt_duration'
        )
        if video_frames:
            # Exact pinned excerpts.parse_excerpts video_frames=True formula.
            # This is returned to the caller before accepting/fingerprinting a
            # plan; the GPU receives only these canonical values.
            steps = round((duration * 24 - 5) / 17)
            if check_media:
                if duration > metadata[video]['duration'] + 1e-7:
                    raise ValueError('wangp_excerpt_outside_selected_source')
                steps = min(steps, int((metadata[video]['duration'] * 24 - 5) // 17))
            duration = (steps * 17 + 5) / 24
        # Native excerpts are centered, not output-time anchors. Refuse its
        # implicit inward clamp instead of changing an accepted source range.
        if center - duration / 2 < 0 or (
            check_media and center + duration / 2 > metadata[video]['duration'] + 1e-7
        ):
            raise ValueError('wangp_excerpt_outside_selected_source')
        result.append({'time_seconds': center, 'duration_seconds': duration})
    if sum(x['duration_seconds'] for x in result) > 15:
        raise ValueError('wangp_ref_total_excerpt_duration_exceeded')
    return result


def _normalize(
    request,
    metadata,
    output_spec,
    profile_id,
    *,
    check_media=True,
    host_media=False,
    return_usage=False,
):
    profile = get_profile(profile_id)
    if (
        not isinstance(request, dict)
        or not isinstance(metadata, dict)
        or not isinstance(output_spec, dict)
    ):
        raise ValueError('wangp_invalid_request')
    mode = request.get('mode')
    model_for(profile_id, mode)
    if request.get('model') != profile['model_id']:
        raise ValueError('wangp_profile_model_mismatch')
    allowed = set(control_schema(profile_id, mode)) | {
        'backend',
        'model',
        'mode',
        'prompt',
        'inputs',
        'video_audio',
    }
    if set(request) - allowed:
        raise ValueError('wangp_unsupported_controls')
    value = copy.deepcopy(request)
    value['backend'] = 'wangp-local'
    if (
        not isinstance(value.get('prompt'), str)
        or not value['prompt'].strip()
        or len(value['prompt']) > 12000
    ):
        raise ValueError('wangp_invalid_prompt')
    for key, expected in FIXED.items():
        actual = value.get(key, expected)
        if key == 'shift_audio' and actual is None:
            actual = expected
        if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            raise ValueError('wangp_unsupported_' + key)
        value[key] = expected
    turbo = acceleration(profile_id)
    low, high = (1, 100) if turbo is None else (turbo['steps'], turbo['steps'])
    value['steps'] = _bound(
        value.get('steps', 20 if turbo is None else turbo['steps']),
        low,
        high,
        'wangp_invalid_steps',
        integer=True,
    )
    value['step_skipping'] = _enum(
        value.get('step_skipping', 'none'),
        STEP_SKIPPING,
        'wangp_unsupported_step_skipping',
    )
    if value.get('window_seconds') is None:
        value.pop('window_seconds', None)
    else:
        value['window_seconds'] = _bound(
            value['window_seconds'], 5, 15, 'wangp_invalid_window_seconds', integer=True
        )
    value['sampler_name'] = _enum(
        value.get('sampler_name', 'euler'), SAMPLERS, 'wangp_unsupported_sampler_name'
    )
    for key, default, low, high in (
        ('denoise', 1, 0.01, 1),
        ('shift_video', 12, 0.01, 100),
    ):
        raw = value.get(key, default)
        if raw is None and key == 'shift_video':
            raw = default
        value[key] = _bound(raw, low, high, 'wangp_invalid_' + key)
    value['export_crf'] = _bound(
        value.get('export_crf', 18), 0, 51, 'wangp_invalid_export_crf', integer=True
    )
    if type(value.setdefault('generate_audio', True)) is not bool:
        raise ValueError('wangp_invalid_generate_audio')
    seed = value.get('seed', '0')
    if (
        isinstance(seed, bool)
        or not isinstance(seed, (int, str))
        or not re.fullmatch(r'[0-9]{1,20}', str(seed))
        or int(seed) > MAX_SEED
    ):
        raise ValueError('wangp_invalid_seed')
    value['seed'] = str(int(seed))
    _bound(
        value.setdefault('duration', 5), 4, 15, 'wangp_invalid_duration', integer=True
    )
    value.setdefault('resolution', '480P')
    value.setdefault('aspect_ratio', '16:9')
    from comfy_workflow import native_output_spec

    if (
        native_output_spec(value) != output_spec
        or not 107 <= output_spec.get('frames', 0) <= MAX_FRAMES
    ):
        raise ValueError('wangp_output_spec_mismatch')
    for key in ('width', 'height'):
        if key in value and (
            type(value[key]) is not int or value[key] != output_spec[key]
        ):
            raise ValueError('wangp_output_spec_mismatch')
    raw_inputs = value.get('inputs', {})
    if not isinstance(raw_inputs, dict) or set(raw_inputs) - {
        'images',
        'videos',
        'audios',
        'first_frame',
        'last_frame',
        'guides',
    }:
        raise ValueError('wangp_invalid_inputs')
    if 'guides' in raw_inputs and 'guides' in value:
        raise ValueError('wangp_conflicting_guides_alias')
    guides = value.get('guides', raw_inputs.get('guides', []))
    inputs = {
        **{
            'images': [],
            'videos': [],
            'audios': [],
            'first_frame': None,
            'last_frame': None,
        },
        **{k: v for k, v in raw_inputs.items() if k != 'guides'},
    }
    roles = {}

    def take(identity, kind=None):
        _identity(identity)
        meta = metadata.get(identity)
        if (
            not isinstance(meta, dict)
            or meta.get('kind') not in {'image', 'video', 'audio'}
            or (kind and meta['kind'] != kind)
        ):
            raise ValueError('wangp_input_kind_mismatch')
        roles[identity] = meta['kind']
        return meta

    for plural, kind, maximum in (
        ('images', 'image', 9),
        ('videos', 'video', 3),
        ('audios', 'audio', 3),
    ):
        ids = inputs[plural]
        if (
            not isinstance(ids, list)
            or len(ids) > maximum
            or any(not isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError('wangp_profile_reference_count_exceeded')
        for identity in ids:
            take(identity, kind)
    for role in ('first_frame', 'last_frame'):
        if inputs[role] is not None:
            take(inputs[role], 'image')
    if not isinstance(guides, list) or len(guides) > 8:
        raise ValueError('wangp_invalid_guides')
    normalized_guides, frames = [], set()
    video_guides, audio_guides = [], []
    for guide in guides:
        if (
            not isinstance(guide, dict)
            or set(guide) - {'media_id', 'time_seconds', 'use_audio'}
            or 'media_id' not in guide
        ):
            raise ValueError('wangp_invalid_guides')
        identity = guide['media_id']
        kind = take(identity)['kind']
        time = _bound(
            guide.get('time_seconds', 0),
            0,
            output_spec['frames'] / 24,
            'wangp_invalid_guide_time',
        )
        frame = round(time * 24)
        if frame >= output_spec['frames']:
            raise ValueError('wangp_guide_outside_output')
        use_audio = guide.get('use_audio', False)
        if type(use_audio) is not bool or (kind != 'video' and use_audio):
            raise ValueError('wangp_invalid_guide_audio')
        if kind == 'image':
            if (
                frame in frames
                or (frame == 0 and inputs['first_frame'] is not None)
                or (
                    frame == output_spec['frames'] - 1
                    and inputs['last_frame'] is not None
                )
            ):
                raise ValueError('wangp_conflicting_image_anchors')
            frames.add(frame)
            # Publish the effective native frame time before plan acceptance;
            # a 5-second request has 124 native frames, including its tail.
            time = frame / 24
        elif time != 0:
            raise ValueError('wangp_native_timed_' + kind + '_guide_unsupported')
        elif kind == 'video':
            video_guides.append(identity)
        else:
            audio_guides.append(identity)
        normalized_guides.append(
            {'media_id': identity, 'time_seconds': time, 'use_audio': use_audio}
        )
    if len(video_guides) > 1 or len(audio_guides) > 1:
        raise ValueError('wangp_control_source_count_exceeded')
    videos = list(inputs['videos'])
    audios = list(inputs['audios'])
    for target, extra in ((videos, video_guides), (audios, audio_guides)):
        for identity in extra:
            if identity not in target:
                target.append(identity)
    if len(videos) > 3 or len(audios) > 3:
        raise ValueError('wangp_profile_reference_count_exceeded')
    conditioning = value.get(
        'video_conditioning', 'reference' if mode == 'ref' else None
    )
    if videos and conditioning is None:
        raise ValueError('wangp_video_conditioning_required')
    if conditioning is not None:
        _enum(
            conditioning,
            ['reference', 'denoise', 'in_context'],
            'wangp_invalid_video_conditioning',
        )
    if mode == 'fl' and conditioning == 'reference':
        raise ValueError('wangp_fl_reference_video_unsupported')
    if (video_guides and conditioning == 'reference') or (
        conditioning in {'denoise', 'in_context'} and len(videos) != 1
    ):
        raise ValueError('wangp_control_video_required')
    if not videos and conditioning in {'denoise', 'in_context'}:
        raise ValueError('wangp_control_video_required')
    if mode == 'fl' and any(
        i
        not in {
            g['media_id'] for g in normalized_guides if roles[g['media_id']] == 'image'
        }
        for i in inputs['images']
    ):
        raise ValueError('wangp_fl_reference_image_unsupported')
    if conditioning == 'denoise' and frames:
        raise ValueError('wangp_frame_injection_denoise_conflict')
    if value['denoise'] != 1 and conditioning != 'denoise':
        raise ValueError('wangp_denoise_requires_control_video')
    mask_mode = _enum(
        value.get('mask_mode', 'grouped_rows'),
        ['grouped_rows', 'shared_timestep'],
        'wangp_invalid_mask_mode',
    )
    if mask_mode != 'grouped_rows' and conditioning != 'denoise':
        raise ValueError('wangp_mask_mode_requires_denoise')
    audio_mode = _enum(
        value.get('audio_conditioning', 'reference'),
        ['reference', 'soundtrack', 'from_video'],
        'wangp_invalid_audio_conditioning',
    )
    if audio_guides and audio_mode != 'soundtrack':
        raise ValueError('wangp_audio_guide_requires_soundtrack')
    video_audio = value.get('video_audio', {})
    if (
        not isinstance(video_audio, dict)
        or set(video_audio) - set(videos)
        or any(type(v) is not bool for v in video_audio.values())
    ):
        raise ValueError('wangp_invalid_video_audio')
    video_audio = {i: video_audio.get(i, False) for i in videos}
    for guide in normalized_guides:
        if roles[guide['media_id']] == 'video' and guide['use_audio']:
            if (
                guide['media_id'] in value.get('video_audio', {})
                and not video_audio[guide['media_id']]
            ):
                raise ValueError('wangp_conflicting_video_audio')
            video_audio[guide['media_id']] = True
    selected_tracks = [i for i in videos if video_audio[i]]
    if audio_mode == 'from_video':
        if mode != 'fl' or conditioning != 'denoise' or audios or selected_tracks:
            raise ValueError('wangp_audio_from_video_requires_denoise')
    if audio_mode == 'soundtrack' and len(audios) + len(selected_tracks) != 1:
        raise ValueError('wangp_single_soundtrack_required')
    if audio_mode == 'soundtrack' and selected_tracks and len(videos) != 1:
        # Native KS/K always keeps video_guide's track. It cannot select a
        # later reference video's soundtrack or choose one among several.
        raise ValueError('wangp_video_soundtrack_requires_single_video')
    if mode == 'fl' and audio_mode == 'reference' and (audios or selected_tracks):
        raise ValueError('wangp_fl_audio_requires_soundtrack')
    if conditioning == 'denoise' and selected_tracks and audio_mode != 'soundtrack':
        raise ValueError('wangp_control_audio_requires_soundtrack')
    video_excerpts = value.get('video_excerpts', [])
    audio_excerpts = value.get('audio_excerpts', [])
    if (video_excerpts or audio_excerpts) and (
        mode != 'ref' or len(videos) != 1 or conditioning != 'reference'
    ):
        raise ValueError('wangp_excerpts_require_single_reference_video')
    video_excerpts = _excerpts(
        video_excerpts,
        metadata,
        videos[0] if videos else None,
        check_media=check_media,
        video_frames=True,
    )
    audio_excerpts = _excerpts(
        audio_excerpts, metadata, videos[0] if videos else None, check_media=check_media
    )
    if audio_excerpts and (not selected_tracks or audios or audio_mode != 'reference'):
        raise ValueError('wangp_audio_excerpts_require_video_soundtrack')
    if (
        video_excerpts
        and selected_tracks
        and not audio_excerpts
        and audio_mode != 'soundtrack'
    ):
        raise ValueError('wangp_video_excerpts_require_explicit_audio_excerpts')
    ordinary_images = inputs['images'] if mode == 'ref' else []
    effective_videos = (
        len(video_excerpts)
        if video_excerpts
        else len(videos)
        if conditioning != 'denoise'
        else 0
    )
    combined_tracks = bool(
        selected_tracks
        and len(selected_tracks) == len(videos)
        and not audios
        and not audio_excerpts
    )
    audio_count = (
        (len(audio_excerpts) if audio_excerpts else len(selected_tracks) + len(audios))
        if audio_mode == 'reference'
        else 0
    )
    if audio_count > 3 or audio_count > len(ordinary_images) + effective_videos:
        raise ValueError('wangp_ref_audio_requires_visual_reference')
    native_rows = (
        len(ordinary_images)
        + effective_videos
        + (0 if combined_tracks else audio_count)
    )
    if native_rows > 12:
        raise ValueError('wangp_profile_reference_count_exceeded')
    if mode == 'ref' and not (
        ordinary_images
        or videos
        or frames
        or inputs['first_frame']
        or inputs['last_frame']
        or audio_mode == 'soundtrack'
    ):
        raise ValueError('wangp_ref_reference_required')
    refinement = _enum(
        value.get('audio_refinement', 'none'),
        ['none', 'enabled'],
        'wangp_invalid_audio_refinement',
    )
    if refinement != 'none' and audio_mode == 'soundtrack':
        raise ValueError('wangp_audio_refinement_soundtrack_conflict')
    image_size = _bound(
        value.get('image_refs_relative_size', 100),
        50,
        400,
        'wangp_invalid_image_refs_relative_size',
        integer=True,
    )
    if image_size != 100 and not ordinary_images:
        raise ValueError('wangp_image_size_requires_reference_images')
    if set(roles) != set(metadata):
        raise ValueError('wangp_input_snapshot_mismatch')
    if check_media:
        for identity, kind in roles.items():
            meta = metadata[identity]
            if meta.get('model_ready') is not True:
                raise ValueError('wangp_ref_media_not_ready')
            if kind in {'image', 'video'}:
                w, h = meta.get('width'), meta.get('height')
                if (
                    type(w) is not int
                    or type(h) is not int
                    or min(w, h) < 256
                    or max(w, h) > 5760
                    or not 0.4 <= w / h <= 2.5
                ):
                    raise ValueError('wangp_ref_invalid_dimensions')
            if kind == 'video':
                if (
                    type(meta.get('has_audio')) is not bool
                    or meta.get('fps') != 24
                    or type(meta.get('frame_count')) is not int
                    or not 56 <= meta['frame_count'] <= MAX_FRAMES
                    or meta['frame_count'] % 17 != 5
                    or not _number(meta.get('duration'))
                    or abs(meta['duration'] - meta['frame_count'] / 24) > 1e-6
                    or (
                        not host_media
                        and (
                            not _number(meta.get('source_duration'))
                            or not 2 <= meta['source_duration'] <= 15
                        )
                    )
                ):
                    raise ValueError('wangp_ref_aligned_video_required')
                if identity in selected_tracks and not meta['has_audio']:
                    raise ValueError('wangp_selected_video_has_no_audio')
                if conditioning in {'denoise', 'in_context'} and (w, h) != (
                    output_spec['width'],
                    output_spec['height'],
                ):
                    raise ValueError('wangp_control_video_canvas_mismatch')
                if (
                    conditioning in {'denoise', 'in_context'}
                    and meta['frame_count'] != output_spec['frames']
                ):
                    raise ValueError('wangp_control_video_timeline_mismatch')
            if kind == 'audio' and (
                not _number(meta.get('duration'))
                or not 2 <= meta['duration'] <= 15
                or meta.get('sample_rate') != 32000
                or meta.get('channels') != 2
            ):
                raise ValueError('wangp_ref_selected_audio_required')
        if (
            conditioning == 'reference'
            and not video_excerpts
            and sum(metadata[i]['frame_count'] for i in videos) > MAX_FRAMES
        ):
            raise ValueError('wangp_ref_total_video_duration_exceeded')
        if (
            audio_mode == 'reference'
            and not audio_excerpts
            and sum(metadata[i]['duration'] for i in audios + selected_tracks) > 15
        ):
            raise ValueError('wangp_ref_total_audio_duration_exceeded')
    value.update(
        inputs=inputs,
        guides=normalized_guides,
        video_audio=video_audio,
        video_conditioning=conditioning,
        audio_conditioning=audio_mode,
        mask_mode=mask_mode,
        audio_refinement=refinement,
        image_refs_relative_size=image_size,
        video_excerpts=video_excerpts,
        audio_excerpts=audio_excerpts,
    )
    usage = {
        'reference_rows': native_rows,
        'reference_images': len(ordinary_images),
        'reference_videos': effective_videos,
        'reference_audios': audio_count,
    }
    return (value, usage) if return_usage else value


def normalize_request(request, metadata, output_spec, profile_id):
    return _normalize(request, metadata, output_spec, profile_id)


def reference_usage(request, metadata, output_spec, profile_id):
    """The same validated native-row projection used for compiler admission."""
    return _normalize(request, metadata, output_spec, profile_id, return_usage=True)[1]


def _positions(excerpts):
    return ' '.join(
        f"{float(x['time_seconds'])!r}s/{float(x['duration_seconds'])!r}s"
        for x in excerpts
    )


def _settings(value, output_spec, handles, profile_id):
    runtime = get_profile(profile_id)['runtime']
    inputs = value['inputs']
    mode = value['mode']
    videos, audios = list(inputs['videos']), list(inputs['audios'])
    injections = []
    for guide in value['guides']:
        identity = guide['media_id']
        if identity not in videos and value['_kinds'][identity] == 'video':
            videos.append(identity)
        if identity not in audios and value['_kinds'][identity] == 'audio':
            audios.append(identity)
        if value['_kinds'][identity] == 'image':
            injections.append(guide)
    images = inputs['images'] if mode == 'ref' else []
    selected = [i for i in videos if value['video_audio'][i]]
    image_flag = (
        'FI'
        if injections and mode == 'ref'
        else 'KFI'
        if injections
        else 'I'
        if images
        else ''
    )
    vc = value['video_conditioning']
    video_flag = (
        'GV'
        if vc == 'denoise'
        else 'VU'
        if vc == 'in_context'
        else 'V1-U'
        if value['video_excerpts']
        else {0: '', 1: 'V-U', 2: 'V+-U', 3: 'V+*-U'}[len(videos)]
    )
    if value['audio_conditioning'] == 'from_video':
        audio_flag = '2'
        sources = []
    elif value['audio_conditioning'] == 'soundtrack':
        audio_flag = (
            ('KS' if mode == 'ref' else 'K')
            if selected
            else ('AS' if mode == 'ref' else 'A')
        )
        sources = audios
    elif value['audio_excerpts']:
        audio_flag = 'K1'
        sources = []
    elif selected and len(selected) == len(videos) and not audios:
        audio_flag = 'K'
        sources = []
    else:
        sources = audios + selected
        audio_flag = {0: '', 1: 'A', 2: 'AB', 3: 'ABD'}[len(sources)]
    settings = {
        'model_type': model_for(profile_id, mode)['model_type'],
        'config': runtime['task_config'],
        'image_mode': 0,
        'prompt': value['prompt'],
        'negative_prompt': '',
        'alt_prompt': '',
        'resolution': f"{output_spec['width']}x{output_spec['height']}",
        'video_length': output_spec['frames'],
        'force_fps': '24',
        'num_inference_steps': value['steps'],
        'seed': int(value['seed']),
        'guidance_scale': 1.0,
        'guidance_phases': 1,
        'flow_shift': value['shift_video'],
        'sample_solver': value['sampler_name'],
        'denoising_strength': value['denoise'],
        'image_prompt_type': ('S' if inputs['first_frame'] else 'T')
        + ('E' if inputs['last_frame'] else ''),
        'image_start': handles.get(inputs['first_frame']),
        'image_end': handles.get(inputs['last_frame']),
        'video_prompt_type': image_flag + video_flag,
        'image_refs': [handles[g['media_id']] for g in injections]
        + [handles[i] for i in images]
        or None,
        'image_refs_relative_size': value['image_refs_relative_size'],
        'remove_background_images_ref': 0,
        'video_source': None,
        'audio_source': None,
        'audio_prompt_type': audio_flag,
        'repeat_generation': 1,
        'batch_size': 1,
        'multi_prompts_gen_type': 'FG',
        'multi_images_gen_type': 0,
        'prompt_enhancer': '',
        'activated_loras': [],
        'skip_steps_cache_type': ''
        if value['step_skipping'] == 'none'
        else value['step_skipping'],
        'override_attention': 'sdpa',
        'override_profile': runtime['memory_profile'],
        'guidance2_scale': 1.0,
        'guidance3_scale': 1.0,
        'sliding_window_size': window_frames(value['window_seconds'])
        if 'window_seconds' in value
        else WHOLE_WINDOW,
        'sliding_window_overlap': 18,
        'sliding_window_discard_last_frames': 0,
        'sliding_window_trim_first_frames': 0,
        'temporal_upsampling': '',
        'spatial_upsampling': '',
        'postprocess_audio': '',
        'custom_settings': {
            'audio_refinement': value['audio_refinement'],
            'h3_mask_mode': value['mask_mode'],
        },
        # H3 always generates joint latents. The session seals the raw pair;
        # generate_audio is the existing later export/delivery choice.
        '_api': {
            'return_audio': True,
            'return_video_uint8': False,
            'return_side_files': False,
        },
    }
    turbo = acceleration(profile_id)
    if turbo is not None:
        # Model-root relative and manifest-pinned; only the GPU host maps it to
        # its verified local file (upstream accepts an absolute LoRA path).
        settings['activated_loras'] = [turbo['path']]
        settings['loras_multipliers'] = turbo['lora_multiplier']
        settings['guidance_scale'] = turbo['guidance_scale']
    if value['step_skipping'] == 'first_block':
        # The handler accepts only its listed thresholds; 0.08 is the upstream balanced one.
        settings['skip_steps_multiplier'] = 0.08
    if value['step_skipping'] != 'none':
        settings['skip_steps_start_step_perc'] = 25
    if injections and 'window_seconds' in value:
        # Injected frame positions are per window upstream; whole-shot times would shift.
        raise ValueError('wangp_window_with_injected_frames_unsupported')
    # Native documented first frame is position1 (handler REF2VA_DEEPY_INFOS).
    if injections:
        settings['frames_positions'] = ' '.join(
            str(round(g['time_seconds'] * 24) + 1) for g in injections
        )
    for field, ids in (('video', videos), ('audio', sources)):
        for index, suffix in enumerate(('', '2', '3')):
            settings[field + '_guide' + suffix] = (
                handles[ids[index]] if index < len(ids) else None
            )
    for field, entries in (
        ('video', value['video_excerpts']),
        ('audio', value['audio_excerpts']),
    ):
        if entries:
            settings['custom_settings']['h3_' + field + '_excerpt_positions'] = (
                _positions(entries)
            )
    return settings


def compile_settings(request, metadata, output_spec, handles, profile_id):
    value = normalize_request(request, metadata, output_spec, profile_id)
    if set(handles) != set(metadata) or len(set(handles.values())) != len(handles):
        raise ValueError('wangp_input_handles_mismatch')
    for handle in handles.values():
        _identity(handle)
    value['_kinds'] = {k: v['kind'] for k, v in metadata.items()}
    return _settings(value, output_spec, handles, profile_id)


def audio_source_handles(settings):
    """Handles to resolve as PCM audio, possibly extracted from a video input."""
    return {
        settings[key]
        for key in ('audio_guide', 'audio_guide2', 'audio_guide3')
        if settings.get(key) is not None
    }


class H3ProfileCompiler:
    def __init__(self, manifest, stage_input):
        validate_manifest(manifest)
        if manifest.document['compiler_id'] != COMPILER_ID:
            raise ValueError('wangp_manifest_binding_mismatch')
        self.manifest, self.stage_input = manifest, stage_input

    def __call__(self, job, tag, store, heartbeat):
        try:
            doc = self.manifest.document
            identity = doc['deployment_profile_id']
            compiled = job['request']
            plan = job['execution_plan']
            assets = compiled.get('assets', {})
            metadata = {k: v['metadata'] for k, v in assets.items()}
            request, output = compiled['request'], compiled['output_spec']
            normalize_request(request, metadata, output, identity)
            if (
                request['mode'] != doc['mode']
                or compiled.get('deployment_profile_id') != identity
                or plan.get('deployment_profile_id') != identity
                or compiled.get('recipe_id') != doc['generation_recipe_id']
                or plan.get('engine_manifest_digest') != self.manifest.digest
            ):
                raise ValueError('wangp_manifest_binding_mismatch')
            descriptors, handles, keys = [], {}, {}
            for asset_id, snapshot in assets.items():
                model = snapshot['model']
                key = validate_key(model['key'])
                if not key_belongs_to(key, job['owner_id']):
                    raise ValueError('wangp_asset_owner_mismatch')
                sha, size = model['sha256'], model['size_bytes']
                handle = (
                    'input-'
                    + hashlib.sha256(
                        (tag + '\0' + asset_id + '\0' + sha).encode()
                    ).hexdigest()
                )
                descriptors.append(
                    InputDescriptor(
                        asset_id, handle, metadata[asset_id]['kind'], sha, size
                    )
                )
                handles[asset_id], keys[asset_id] = handle, key
            settings = compile_settings(request, metadata, output, handles, identity)
            settings['output_filename'] = 'sixnine-' + tag
            prepared = PreparedRequest(
                job['id'],
                tag,
                job['request_hash'],
                self.manifest.digest,
                canonical_json(settings),
                canonical_json(output),
                request.get('generate_audio', True),
                tuple(descriptors),
            )
            for item in descriptors:
                heartbeat()
                with store.open(keys[item.asset_id]) as stream:
                    if self.stage_input(item, stream, heartbeat=heartbeat) != item:
                        raise ValueError('wangp_staged_input_mismatch')
            return prepared
        except (KeyError, TypeError, ValueError) as error:
            code = str(error)
            if not re.fullmatch(r'wangp_[a-z0-9_]+', code):
                code = 'wangp_invalid_compiled_request'
            raise BackendError(code) from None


def _parse_positions(text):
    if not isinstance(text, str):
        raise ValueError('wangp_invalid_excerpts')
    result = []
    for token in text.split():
        match = re.fullmatch(r'([0-9.e+-]+)s/([0-9.e+-]+)s', token)
        if not match:
            raise ValueError('wangp_invalid_excerpts')
        result.append(
            {'time_seconds': float(match[1]), 'duration_seconds': float(match[2])}
        )
    return result


def prepared_request(prepared, manifest):
    """Recover the typed template; the host still probes actual staged bytes.

    Audio-guide handles of kind video explicitly require local PCM extraction;
    they are not passed to libsndfile as MP4. Reusing that handle in video_guide
    stages one original input, not a second upload or owner asset.
    """
    profile = validate_manifest(manifest)
    settings = prepared.settings
    doc = manifest.document
    if doc['compiler_id'] != COMPILER_ID or prepared.manifest_digest != manifest.digest:
        raise ValueError('wangp_manifest_binding_mismatch')
    descriptors = {d.handle: d for d in prepared.inputs}
    if len(descriptors) != len(prepared.inputs):
        raise ValueError('wangp_duplicate_input_handle')
    metadata, handles = {}, {}

    def take(handle, kinds):
        if handle not in descriptors or descriptors[handle].kind not in kinds:
            raise ValueError('wangp_unbound_input_handle')
        d = descriptors[handle]
        handles[d.asset_id] = handle
        metadata[d.asset_id] = {'kind': d.kind}
        return d.asset_id

    inputs = {
        'images': [],
        'videos': [],
        'audios': [],
        'first_frame': None,
        'last_frame': None,
    }
    guides = []
    for role, field in (('first_frame', 'image_start'), ('last_frame', 'image_end')):
        if settings.get(field) is not None:
            inputs[role] = take(settings[field], {'image'})
    positions = settings.get('frames_positions', '')
    if not isinstance(positions, str) or (
        positions and not re.fullmatch(r'[0-9]+(?: [0-9]+)*', positions)
    ):
        raise ValueError('wangp_invalid_image_positions')
    positions = [int(x) for x in positions.split()]
    refs = settings.get('image_refs') or []
    if not isinstance(refs, list) or len(positions) > len(refs):
        raise ValueError('wangp_invalid_image_positions')
    for index, handle in enumerate(refs):
        identity = take(handle, {'image'})
        if index < len(positions):
            guides.append(
                {
                    'media_id': identity,
                    'time_seconds': (positions[index] - 1) / 24,
                    'use_audio': False,
                }
            )
        else:
            inputs['images'].append(identity)
    for suffix in ('', '2', '3'):
        handle = settings.get('video_guide' + suffix)
        if handle is not None:
            inputs['videos'].append(take(handle, {'video'}))
    selected = []
    for suffix in ('', '2', '3'):
        handle = settings.get('audio_guide' + suffix)
        if handle is not None:
            identity = take(handle, {'audio', 'video'})
            (
                selected if descriptors[handle].kind == 'video' else inputs['audios']
            ).append(identity)
    if set(handles) != set(d.asset_id for d in descriptors.values()):
        raise ValueError('wangp_unused_input_handle')
    vf = settings.get('video_prompt_type', '')
    af = settings.get('audio_prompt_type', '')
    if not isinstance(vf, str) or not isinstance(af, str):
        raise ValueError('wangp_invalid_native_flags')
    conditioning = (
        'denoise'
        if 'G' in vf
        else 'in_context'
        if 'V' in vf and '-' not in vf
        else 'reference'
        if doc['mode'] == 'ref'
        else None
    )
    audio_mode = (
        'from_video'
        if af == '2'
        else 'soundtrack'
        if 'S' in af or (doc['mode'] == 'fl' and af in {'A', 'K'})
        else 'reference'
    )
    if af.startswith('K'):
        selected = list(inputs['videos'])
    output = json.loads(prepared.output_spec_json)
    from comfy_workflow import native_output_spec

    canvas = None
    for duration in range(4, 16):
        candidate = {
            'duration': duration,
            'resolution': 'custom',
            'width': output.get('width'),
            'height': output.get('height'),
            'aspect_ratio': '16:9',
        }
        if native_output_spec(candidate) == output:
            canvas = candidate
            break
    if canvas is None:
        raise ValueError('wangp_output_spec_mismatch')
    custom = settings.get('custom_settings', {})
    if not isinstance(custom, dict):
        raise ValueError('wangp_invalid_custom_settings')
    request = {
        'model': profile['model_id'],
        'mode': doc['mode'],
        'prompt': settings.get('prompt'),
        **canvas,
        'seed': settings.get('seed'),
        'steps': settings.get('num_inference_steps'),
        'sampler_name': settings.get('sample_solver'),
        'denoise': settings.get('denoising_strength'),
        'shift_video': settings.get('flow_shift'),
        'inputs': inputs,
        'guides': guides,
        'video_conditioning': conditioning,
        'audio_conditioning': audio_mode,
        'video_audio': {i: i in selected for i in inputs['videos']},
        'generate_audio': prepared.generate_audio,
        'image_refs_relative_size': settings.get('image_refs_relative_size'),
        'audio_refinement': custom.get('audio_refinement'),
        'step_skipping': settings.get('skip_steps_cache_type') or 'none',
        'mask_mode': custom.get('h3_mask_mode'),
        'video_excerpts': _parse_positions(custom['h3_video_excerpt_positions'])
        if 'h3_video_excerpt_positions' in custom
        else [],
        'audio_excerpts': _parse_positions(custom['h3_audio_excerpt_positions'])
        if 'h3_audio_excerpt_positions' in custom
        else [],
    }
    window = settings.get('sliding_window_size')
    if window != WHOLE_WINDOW:
        seconds = round(window / 24) if type(window) is int else None
        if seconds is None or window_frames(seconds) != window:
            raise ValueError('wangp_invalid_window_seconds')
        request['window_seconds'] = seconds
    return profile, request, metadata, handles, output


def validate_prepared(prepared, manifest):
    profile, request, metadata, handles, output = prepared_request(prepared, manifest)
    value = _normalize(request, metadata, output, profile['id'], check_media=False)
    value['_kinds'] = {k: v['kind'] for k, v in metadata.items()}
    expected = _settings(value, output, handles, profile['id'])
    expected['output_filename'] = 'sixnine-' + prepared.attempt_tag
    if canonical_json(expected) != canonical_json(prepared.settings):
        raise ValueError('wangp_profile_settings_mismatch')
    return profile


def validate_prepared_media(prepared, manifest, metadata):
    """Recheck the same template with actual host-probed hash-bound metadata."""
    profile, request, _, handles, output = prepared_request(prepared, manifest)
    # The normalized file proves its frame grid; source_duration describes the
    # earlier CPU selection before end-frame padding and cannot be recovered
    # from the normalized bytes. That provenance is checked at CPU admission.
    value = _normalize(request, metadata, output, profile['id'], host_media=True)
    value['_kinds'] = {k: v['kind'] for k, v in metadata.items()}
    expected = _settings(value, output, handles, profile['id'])
    expected['output_filename'] = 'sixnine-' + prepared.attempt_tag
    if canonical_json(expected) != canonical_json(prepared.settings):
        raise ValueError('wangp_profile_settings_mismatch')
    return profile

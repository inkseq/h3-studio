"""Closed, additive GPU-side delivery DTOs; no IO or credential loading."""

from __future__ import annotations
import base64
import hashlib
import math
import re
from urllib.parse import urlsplit, parse_qs, unquote
from .wangp_contract import canonical_json, operation_id

POLICY = 'native-frames-r2-v1'
MAX_BYTES = 512 * 1024**2
IDENTITY_FIELDS = {
    'operation_id',
    'job_id',
    'attempt_tag',
    'request_hash',
    'manifest_digest',
    'slot_key',
    'incarnation',
}
SPEC_FIELDS = {'policy', 'fps', 'frame_count', 'duration_s', 'requested_duration_s'}
STATES = {
    'preparing',
    'prepared',
    'uploading',
    'uploaded',
    'rejected',
    'unknown',
    'failed',
}
ROLE_FIELDS = {
    'kind',
    'size_bytes',
    'sha256',
    'content_md5',
    'content_type',
    'evidence',
}
ERROR_CODES = {
    'wangp_delivery_encoding_failed',
    'wangp_delivery_upload_unknown',
    'wangp_delivery_upload_rejected',
}


def _object(value):
    if type(value) is not dict:
        raise ValueError('wangp_delivery_object_invalid')
    return value


def _sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('wangp_delivery_hash_invalid')
    return value


def _id(value):
    if (
        type(value) is not str
        or not re.fullmatch('[A-Za-z0-9_.-]{1,200}', value)
        or value in {'.', '..'}
    ):
        raise ValueError('wangp_delivery_identity_invalid')
    return value


def _number(value):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 4_102_444_800
    ):
        raise ValueError('wangp_delivery_number_invalid')
    return value


def validate_identity(value):
    value = _object(value)
    if set(value) != IDENTITY_FIELDS:
        raise ValueError('wangp_delivery_identity_invalid')
    for name in ('job_id', 'attempt_tag', 'slot_key'):
        _id(value[name])
    if value['operation_id'] != operation_id(value['attempt_tag']):
        raise ValueError('wangp_delivery_operation_invalid')
    for name in ('request_hash', 'manifest_digest'):
        _sha(value[name])
    if type(value['incarnation']) is not str or not re.fullmatch(
        '[0-9a-f]{32}', value['incarnation']
    ):
        raise ValueError('wangp_delivery_incarnation_invalid')
    return dict(value)


def validate_spec(value):
    value = _object(value)
    if (
        set(value) != SPEC_FIELDS
        or value['policy'] != POLICY
        or type(value['fps']) is not int
        or value['fps'] != 24
    ):
        raise ValueError('wangp_delivery_spec_invalid')
    if (
        type(value['frame_count']) is not int
        or not 1 <= value['frame_count'] <= 1_000_000
    ):
        raise ValueError('wangp_delivery_spec_invalid')
    if (
        _number(value['duration_s']) != value['frame_count'] / 24
        or type(value['requested_duration_s']) is not int
        or not 1 <= value['requested_duration_s'] <= 86400
    ):
        raise ValueError('wangp_delivery_spec_invalid')
    return dict(value)


def delivery_fingerprint(identity, delivery_spec, export_crf, generate_audio, owner_id):
    identity = validate_identity(identity)
    delivery_spec = validate_spec(delivery_spec)
    _id(owner_id)
    if (
        type(export_crf) is not int
        or not 0 <= export_crf <= 51
        or type(generate_audio) is not bool
    ):
        raise ValueError('wangp_delivery_export_invalid')
    return hashlib.sha256(
        canonical_json(
            {
                'identity': identity,
                'owner_id': owner_id,
                'delivery_spec': delivery_spec,
                'export_crf': export_crf,
                'generate_audio': generate_audio,
            }
        ).encode()
    ).hexdigest()


def validate_delivery_request(value):
    value = _object(value)
    if (
        set(value)
        != {
            'version',
            'identity',
            'owner_id',
            'delivery_spec',
            'export_crf',
            'generate_audio',
            'fingerprint',
        }
        or type(value['version']) is not int
        or value['version'] != 1
    ):
        raise ValueError('wangp_delivery_request_invalid')
    if value['fingerprint'] != delivery_fingerprint(
        value['identity'],
        value['delivery_spec'],
        value['export_crf'],
        value['generate_audio'],
        value['owner_id'],
    ):
        raise ValueError('wangp_delivery_fingerprint_mismatch')
    return dict(value)


def validate_role_descriptor(kind, value, *, delivery_spec=None, generate_audio=None):
    value = _object(value)
    if (
        kind not in {'video', 'audio'}
        or set(value) != ROLE_FIELDS
        or value['kind'] != kind
    ):
        raise ValueError('wangp_delivery_role_invalid')
    if (
        type(value['size_bytes']) is not int
        or not 1 <= value['size_bytes'] <= MAX_BYTES
    ):
        raise ValueError('wangp_delivery_size_invalid')
    _sha(value['sha256'])
    try:
        raw = base64.b64decode(value['content_md5'], validate=True)
        if len(raw) != 16 or base64.b64encode(raw).decode() != value['content_md5']:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('wangp_delivery_md5_invalid') from None
    if value['content_type'] != ('video/mp4' if kind == 'video' else 'audio/flac'):
        raise ValueError('wangp_delivery_content_type_invalid')
    evidence = _object(value['evidence'])
    allowed = {
        'width',
        'height',
        'duration_s',
        'fps',
        'has_audio',
        'delivery_spec',
        'frame_count',
        'container_duration_s',
        'audio_duration_s',
    }
    if set(evidence) - allowed:
        raise ValueError('wangp_delivery_evidence_invalid')
    if delivery_spec is not None:
        spec = validate_spec(delivery_spec)
        if evidence.get('delivery_spec') != spec:
            raise ValueError('wangp_delivery_evidence_invalid')
        for name in ('duration_s', 'container_duration_s', 'audio_duration_s'):
            if (
                name in evidence
                and abs(_number(evidence[name]) - spec['duration_s']) > 0.1
            ):
                raise ValueError('wangp_delivery_timing_invalid')
        if kind == 'video':
            if (
                evidence.get('frame_count') != spec['frame_count']
                or type(evidence.get('frame_count')) is not int
                or evidence.get('fps') != 24
                or evidence.get('duration_s') != spec['duration_s']
            ):
                raise ValueError('wangp_delivery_timing_invalid')
            if any(
                type(evidence.get(k)) is not int or not 1 <= evidence[k] <= 16384
                for k in ('width', 'height')
            ):
                raise ValueError('wangp_delivery_geometry_invalid')
            if (
                type(evidence.get('has_audio')) is not bool
                or evidence['has_audio'] is not generate_audio
            ):
                raise ValueError('wangp_delivery_audio_invalid')
        elif evidence.get('has_audio') is not True or 'duration_s' not in evidence:
            raise ValueError('wangp_delivery_audio_invalid')
    return dict(value)


def validate_delivery_receipt(value):
    value = _object(value)
    fields = {
        'version',
        'fingerprint',
        'identity',
        'owner_id',
        'delivery_spec',
        'export_crf',
        'generate_audio',
        'state',
        'roles',
        'objects',
        'stage_durations',
    }
    if set(value) - fields - {'error_code'} or not fields <= set(value):
        raise ValueError('wangp_delivery_receipt_invalid')
    request = {
        k: value[k]
        for k in (
            'version',
            'fingerprint',
            'identity',
            'owner_id',
            'delivery_spec',
            'export_crf',
            'generate_audio',
        )
    }
    validate_delivery_request(request)
    if value['state'] not in STATES:
        raise ValueError('wangp_delivery_state_invalid')
    if 'error_code' in value and value['error_code'] not in ERROR_CODES:
        raise ValueError('wangp_delivery_error_invalid')
    roles = _object(value['roles'])
    expected = {'video', 'audio'} if value['generate_audio'] else {'video'}
    if (
        set(roles) - expected
        or value['state'] not in {'preparing', 'failed'}
        and set(roles) != expected
    ):
        raise ValueError('wangp_delivery_roles_invalid')
    for kind, role in roles.items():
        validate_role_descriptor(
            kind,
            role,
            delivery_spec=value['delivery_spec'],
            generate_audio=value['generate_audio'],
        )
    objects = _object(value['objects'])
    if set(objects) - set(roles):
        raise ValueError('wangp_delivery_objects_invalid')
    for kind, obj in objects.items():
        if type(obj) is not dict or set(obj) != {
            'key',
            'storage_binding',
            'state',
            'stop_proven',
        }:
            raise ValueError('wangp_delivery_objects_invalid')
        if obj['key'] != object_key(
            value['owner_id'], value['identity']['attempt_tag'], kind
        ):
            raise ValueError('wangp_delivery_key_invalid')
        _sha(obj['storage_binding'])
        if (
            obj['state']
            not in {'not_started', 'uploading', 'uploaded', 'rejected', 'unknown'}
            or type(obj['stop_proven']) is not bool
        ):
            raise ValueError('wangp_delivery_objects_invalid')
        if obj['stop_proven'] is not (
            obj['state'] in {'not_started', 'uploaded', 'rejected'}
        ):
            raise ValueError('wangp_delivery_stop_proof_invalid')
    if value['state'] == 'uploaded' and (
        set(objects) != expected
        or any(v['state'] != 'uploaded' for v in objects.values())
    ):
        raise ValueError('wangp_delivery_upload_incomplete')
    durations = _object(value['stage_durations'])
    if set(durations) - {'encode_validate_seconds', 'upload_seconds'}:
        raise ValueError('wangp_delivery_duration_invalid')
    for duration in durations.values():
        _number(duration)
    return json_copy(value)


def json_copy(value):
    import json

    return json.loads(canonical_json(value))


def object_key(owner_id, tag, kind):
    _id(owner_id)
    _id(tag)
    if kind not in {'video', 'audio'}:
        raise ValueError('wangp_delivery_role_invalid')
    return (
        'owners/'
        + owner_id
        + '/assets/'
        + tag
        + '/result.'
        + ('mp4' if kind == 'video' else 'flac')
    )


def validate_upload_target(kind, value, receipt, *, now):
    value = _object(value)
    receipt = validate_delivery_receipt(receipt)
    fields = {
        'key',
        'size_bytes',
        'sha256',
        'content_md5',
        'content_type',
        'expires_at',
        'url',
        'headers',
        'storage_binding',
    }
    if set(value) != fields or kind not in receipt['roles']:
        raise ValueError('wangp_delivery_target_invalid')
    role = receipt['roles'][kind]
    if any(
        value[k] != role[k] or type(value[k]) is not type(role[k])
        for k in ('size_bytes', 'sha256', 'content_md5', 'content_type')
    ):
        raise ValueError('wangp_delivery_target_descriptor_mismatch')
    if value['key'] != object_key(
        receipt['owner_id'], receipt['identity']['attempt_tag'], kind
    ):
        raise ValueError('wangp_delivery_key_invalid')
    _sha(value['storage_binding'])
    if not 0 < _number(value['expires_at']) - now <= 305:
        raise ValueError('wangp_delivery_target_expired')
    url = value['url']
    if type(url) is not str or len(url) > 8192:
        raise ValueError('wangp_delivery_target_invalid')
    p = urlsplit(url)
    if (
        p.scheme != 'https'
        or not p.hostname
        or not re.fullmatch(
            r'[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com', p.hostname
        )
        or p.port not in (None, 443)
        or p.username
        or p.password
        or p.fragment
    ):
        raise ValueError('wangp_delivery_origin_invalid')
    segments = unquote(p.path).split('/')
    if len(segments) < 3 or not segments[1] or '/'.join(segments[2:]) != value['key']:
        raise ValueError('wangp_delivery_key_invalid')
    query = parse_qs(p.query, strict_parsing=True)
    if (
        any(len(v) != 1 for v in query.values())
        or not query.get('X-Amz-Signature')
        or not query.get('X-Amz-Expires')
        or not query['X-Amz-Expires'][0].isdecimal()
        or not 1 <= int(query['X-Amz-Expires'][0]) <= 300
    ):
        raise ValueError('wangp_delivery_signature_invalid')
    headers = _object(value['headers'])
    expected = {
        'Content-Type': role['content_type'],
        'Content-Length': str(role['size_bytes']),
        'Content-MD5': role['content_md5'],
        'If-None-Match': '*',
    }
    if headers != expected:
        raise ValueError('wangp_delivery_headers_invalid')
    return value

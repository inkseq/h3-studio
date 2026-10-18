"""Read-only reconciliation of one original backup copy; never replay a PUT.

An original private intent is the identity authority. Remote completion supplies
version pins only after its claim and complete file inventory match that intent.
No SDK client, file IO, restore, cadence or credentials are created on import.
"""

from dataclasses import asdict
import hashlib
import io
import json
import re
import time

from .backup_remote import (
    FORMAT,
    FORMAT_V2,
    FORMAT_V3,
    coverage_fields,
    MAX_JSON,
    MIB,
    RemoteBackupError,
    _check,
    _completion,
    _preflight,
    _read,
    _version,
    canonical,
    need,
    sha,
)

RECONCILE_FORMAT = 'sixnine-private-backup-reconciliation-v1'
PLAN_KEYS = {
    'format',
    'target',
    'files',
    'snapshot_manifest_sha256',
    'objects',
    'bytes',
    'staging_included',
    'authentication_included',
}
INTENT_KEYS = PLAN_KEYS | {'operation_id', 'phase'}
TRANSFER_KEYS = {
    'format',
    'target',
    'phase',
    'completion_version_id',
    'completion_sha256',
    'completion_bytes',
    'snapshot_manifest_sha256',
    'objects',
    'bytes',
    'restore_verified',
}


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'backup_reconcile_duplicate_json_key')
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(
                RemoteBackupError('backup_reconcile_nonfinite_json')
            ),
        )
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError('backup_reconcile_json_invalid') from None


def validate_intent(target, intent):
    """Strict local validation before any cloud read or client initialization."""
    coverage = coverage_fields(intent)
    need(
        isinstance(intent, dict)
        and set(intent) == INTENT_KEYS | set(coverage)
        and intent['format'] in (FORMAT, FORMAT_V2, FORMAT_V3)
        and intent['target'] == asdict(target)
        and intent['phase'] == 'before_remote_write'
        and isinstance(intent['operation_id'], str)
        and re.fullmatch(
            r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
            intent['operation_id'],
        )
        and intent['staging_included'] is False
        and intent['authentication_included'] is False,
        'backup_reconcile_original_intent_invalid',
    )
    files = intent['files']
    need(
        isinstance(files, list)
        and all(
            isinstance(v, dict) and set(v) == {'name', 'size_bytes', 'sha256'}
            for v in files
        ),
        'backup_reconcile_intent_files_invalid',
    )
    _completion(
        {
            **intent,
            'phase': 'copy_complete',
            'files': [{**v, 'version_id': 'validation-only'} for v in files],
        },
        target,
        intent['snapshot_manifest_sha256'],
    )
    need(
        type(intent['objects']) is int
        and intent['objects'] == len(files) - 2
        and type(intent['bytes']) is int
        and intent['bytes'] == sum(v['size_bytes'] for v in files)
        and [v['name'] for v in files[:-1]] == sorted(v['name'] for v in files[:-1]),
        'backup_reconcile_intent_inventory_mismatch',
    )
    # Even the longest permitted escaped version IDs must fit a private partial
    # evidence record. Refuse before network rather than discard known pins.
    size = 32768 + len(canonical(intent))
    for v in files:
        size += len(canonical({**v, 'version_id': '\\' * 1024})) + 1
    need(size <= MAX_JSON, 'backup_reconcile_evidence_limit')
    return {k: intent[k] for k in PLAN_KEYS | set(coverage)}


def validate_receipt(target, intent, receipt):
    if receipt is None:
        return
    need(
        isinstance(receipt, dict)
        and set(receipt) == TRANSFER_KEYS | set(coverage_fields(intent))
        and receipt['format'] == intent['format']
        and coverage_fields(receipt) == coverage_fields(intent)
        and receipt['phase'] == 'copied_not_restored'
        and receipt['target'] == asdict(target)
        and receipt['snapshot_manifest_sha256'] == intent['snapshot_manifest_sha256']
        and type(receipt['objects']) is int
        and receipt['objects'] == intent['objects']
        and type(receipt['bytes']) is int
        and receipt['bytes'] == intent['bytes']
        and receipt['restore_verified'] is False,
        'backup_reconcile_retained_receipt_invalid',
    )
    _version(receipt['completion_version_id'])
    need(
        type(receipt['completion_bytes']) is int
        and 0 < receipt['completion_bytes'] <= MAX_JSON
        and isinstance(receipt['completion_sha256'], str)
        and re.fullmatch('[0-9a-f]{64}', receipt['completion_sha256']),
        'backup_reconcile_retained_receipt_invalid',
    )


def _current(client, target, name, snapshot, *, expected=None, json_body=False):
    """Read one current version once and retain its returned immutable version.

    Only GetObject's modeled NoSuchKey is an observed missing current object.
    Denied reads, absent pinned versions and transport errors remain unknown.
    No listing, fallback version or metadata-only integrity claim is used.
    """
    try:
        response = client.get_object(**target.request(name), ChecksumMode='ENABLED')
    except client.exceptions.NoSuchKey:
        return None
    except Exception:
        raise RemoteBackupError('backup_reconcile_read_unconfirmed') from None
    try:
        with response['Body'] as body:
            if expected is None:
                expected = {
                    'size_bytes': response.get('ContentLength'),
                    'sha256': response.get('Metadata', {}).get('sha256'),
                }
                need(
                    type(expected['size_bytes']) is int
                    and 0 < expected['size_bytes'] <= MAX_JSON
                    and isinstance(expected['sha256'], str)
                    and re.fullmatch('[0-9a-f]{64}', expected['sha256']),
                    'backup_reconcile_completion_metadata_invalid',
                )
            version = _check(response, target, snapshot, expected)
            total = 0
            checksum = hashlib.sha256()
            output = io.BytesIO() if json_body else None
            while True:
                data = body.read(min(MIB, expected['size_bytes'] - total + 1))
                if not data:
                    break
                total += len(data)
                need(total <= expected['size_bytes'], 'backup_object_size_mismatch')
                checksum.update(data)
                if output is not None:
                    output.write(data)
            need(
                total == expected['size_bytes']
                and checksum.hexdigest() == expected['sha256'],
                'backup_object_content_mismatch',
            )
        return {
            'version_id': version,
            **expected,
            'value': _json(output.getvalue()) if output is not None else None,
        }
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError('backup_reconcile_read_unconfirmed') from None


def reconcile_copy(target, intent, *, client, receipt=None):
    """Classify this exact historical copy; remote methods are reads only.

    `complete` means all version-pinned bytes were read and verified. It does
    not mean a restore succeeded. `partial` is a dated observation, never proof
    that a concurrent/in-flight write failed or that replay would be safe.
    """
    plan = validate_intent(target, intent)
    validate_receipt(target, intent, receipt)
    snapshot = intent['snapshot_manifest_sha256']
    result = {
        'format': RECONCILE_FORMAT,
        'classification': 'unknown',
        'code': 'backup_reconcile_pending',
        'original_intent_sha256': sha(canonical(intent)),
        'operation_id': intent['operation_id'],
        'target': asdict(target),
        'snapshot_manifest_sha256': snapshot,
        'observation_started_at': time.time(),
        'automatic_retry': False,
        'cloud_write_operations': 0,
        'restore_verified': False,
        'verified_files': [],
        'missing_current_files': [],
    }

    def finish():
        result['observation_finished_at'] = time.time()
        return result

    try:
        _preflight(client, target)
        claim = canonical(
            {
                'format': intent['format'],
                'operation_id': intent['operation_id'],
                'snapshot_manifest_sha256': snapshot,
                'target': asdict(target),
            }
        )
        observed = _current(
            client,
            target,
            'claim.json',
            snapshot,
            expected={'size_bytes': len(claim), 'sha256': sha(claim)},
        )
        if observed is None:
            result['code'] = 'backup_reconcile_original_claim_unobserved'
            return finish()
        result['claim_version_id'] = observed['version_id']
        if receipt is None:
            completed = _current(
                client, target, 'complete.json', snapshot, json_body=True
            )
        else:
            expected = {
                'size_bytes': receipt['completion_bytes'],
                'sha256': receipt['completion_sha256'],
            }
            output = io.BytesIO()
            version = _read(
                client,
                target,
                'complete.json',
                expected,
                snapshot,
                version=receipt['completion_version_id'],
                output=output,
            )
            completed = {
                'version_id': version,
                **expected,
                'value': _json(output.getvalue()),
            }
        if completed is not None:
            value = completed['value']
            need(
                isinstance(value, dict)
                and set(value) == INTENT_KEYS | set(coverage_fields(intent))
                and value.get('operation_id') == intent['operation_id'],
                'backup_reconcile_completion_operation_mismatch',
            )
            files = _completion(value, target, snapshot)
            proposed = {k: value[k] for k in PLAN_KEYS | set(coverage_fields(intent))}
            proposed['files'] = [
                {k: v for k, v in item.items() if k != 'version_id'} for item in files
            ]
            need(
                canonical(proposed) == canonical(plan),
                'backup_reconcile_completion_inventory_mismatch',
            )
            result['observed_completion'] = {
                k: completed[k] for k in ('version_id', 'size_bytes', 'sha256')
            }
            for item in files:
                _read(
                    client,
                    target,
                    item['name'],
                    item,
                    snapshot,
                    version=item['version_id'],
                )
                result['verified_files'].append(dict(item))
            transfer = dict(
                format=intent["format"],
                target=asdict(target),
                phase='copied_not_restored',
                completion_version_id=completed['version_id'],
                completion_sha256=completed['sha256'],
                completion_bytes=completed['size_bytes'],
                snapshot_manifest_sha256=snapshot,
                objects=intent['objects'],
                bytes=intent['bytes'],
                restore_verified=False,
            )
            transfer.update(coverage_fields(intent))
            result.update(coverage_fields(intent))
            result.update(
                classification='complete',
                code='backup_copy_complete_bytes_verified',
                transfer_receipt=transfer,
            )
            return finish()
        for item in plan['files']:
            observed = _current(client, target, item['name'], snapshot, expected=item)
            if observed is None:
                result['missing_current_files'].append(item['name'])
            else:
                result['verified_files'].append(
                    {**item, 'version_id': observed['version_id']}
                )
        result.update(
            classification='partial', code='backup_copy_completion_not_observed'
        )
        return finish()
    except RemoteBackupError as error:
        result['code'] = str(error)
        return finish()
    except Exception:
        result['code'] = 'backup_reconcile_read_unconfirmed'
        return finish()

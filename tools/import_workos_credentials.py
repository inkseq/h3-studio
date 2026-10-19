"""Import the owner's existing WorkOS credential in memory; never rotate it.

Only the fixed staging/production Windows targets and AWS secret names are
accepted. No credential is accepted through argv, a file, or the environment.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.platform import deployment_target
from studio_platform.workos_config import validate_credentials, unique_pairs

ACCOUNT = '829135631045'
REGION = 'ap-southeast-1'
SOURCES = {'staging': 'inkseq/workos-staging', 'production': 'inkseq/workos-prod'}
VERSION = re.compile(r'[A-Za-z0-9-]{32,64}')


class ImportErrorSafe(Exception):
    """Static diagnostics only; never wrap provider or credential text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2,
            'Invalid arguments; use --help. Credential values must not be passed on the command line.\n',
        )


def selection(target):
    if target not in SOURCES:
        raise ImportErrorSafe('workos_import_target_invalid')
    selected = deployment_target.profile(target)
    return SOURCES[target], selected['secret_root'] + '/workos', selected['origin']


def read_windows_credential(target):
    if target not in SOURCES.values() or os.name != 'nt':
        raise ImportErrorSafe('workos_windows_credential_unavailable')

    class Credential(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD),
            ('Type', wintypes.DWORD),
            ('TargetName', wintypes.LPWSTR),
            ('Comment', wintypes.LPWSTR),
            ('LastWritten', wintypes.FILETIME),
            ('CredentialBlobSize', wintypes.DWORD),
            ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
            ('Persist', wintypes.DWORD),
            ('AttributeCount', wintypes.DWORD),
            ('Attributes', ctypes.c_void_p),
            ('TargetAlias', wintypes.LPWSTR),
            ('UserName', wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL('Advapi32', use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(Credential)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    pointer = ctypes.POINTER(Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise ImportErrorSafe('workos_windows_credential_unavailable')
    try:
        record = pointer.contents
        if (
            record.Type != 1
            or record.TargetName != target
            or not record.UserName
            or not 2 <= record.CredentialBlobSize <= 1024
            or record.CredentialBlobSize % 2
            or not record.CredentialBlob
        ):
            raise ValueError()
        key = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        return validate_credentials(
            {
                'client_id': record.UserName,
                'api_key': key.decode('utf-16-le').removesuffix('\x00'),
            }
        )
    except (ValueError, UnicodeError):
        raise ImportErrorSafe('workos_windows_credential_invalid') from None
    finally:
        record = pointer.contents
        if record.CredentialBlob and record.CredentialBlobSize <= 1024:
            ctypes.memset(record.CredentialBlob, 0, record.CredentialBlobSize)
        api.CredFree(pointer)


def _record(response, name):
    expected = f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-'
    if (
        not isinstance(response, dict)
        or response.get('Name') != name
        or not isinstance(response.get('ARN'), str)
        or not response['ARN'].startswith(expected)
    ):
        raise ImportErrorSafe('workos_secret_destination_unconfirmed')
    return response


def _value(response, name):
    _record(response, name)
    raw, version = response.get('SecretString'), response.get('VersionId')
    if (
        not isinstance(raw, str)
        or len(raw) > 4096
        or not isinstance(version, str)
        or not VERSION.fullmatch(version)
    ):
        raise ImportErrorSafe('workos_managed_secret_invalid')
    try:
        value = validate_credentials(json.loads(raw, object_pairs_hook=unique_pairs))
    except (ValueError, TypeError):
        raise ImportErrorSafe('workos_managed_secret_invalid') from None
    return value, version


def _same(left, right):
    return left['client_id'] == right['client_id'] and hmac.compare_digest(
        left['api_key'], right['api_key']
    )


def import_existing(target, *, session=None, read_credential=read_windows_credential):
    source, name, origin = selection(target)
    session = session or boto3.Session(region_name=REGION)
    config = Config(
        connect_timeout=5,
        read_timeout=20,
        retries={'mode': 'standard', 'total_max_attempts': 2},
    )
    identity = session.client(
        'sts',
        region_name=REGION,
        endpoint_url=f'https://sts.{REGION}.amazonaws.com',
        config=config,
    ).get_caller_identity()
    if not isinstance(identity, dict) or identity.get('Account') != ACCOUNT:
        raise ImportErrorSafe('workos_aws_account_unconfirmed')
    credentials = validate_credentials(read_credential(source))
    api = session.client(
        'secretsmanager',
        region_name=REGION,
        endpoint_url=f'https://secretsmanager.{REGION}.amazonaws.com',
        config=config,
    )
    try:
        existing, version = _value(
            api.get_secret_value(SecretId=name, VersionStage='AWSCURRENT'), name
        )
    except api.exceptions.ResourceNotFoundException:
        existing = None
    if existing is not None:
        if not _same(existing, credentials):
            raise ImportErrorSafe(
                'workos_existing_secret_differs_rotation_not_authorized'
            )
        return {'status': 'verified', 'version_id': version}
    token = str(uuid.uuid4())
    payload = json.dumps(credentials, sort_keys=True, separators=(',', ':'))
    try:
        description = _record(api.describe_secret(SecretId=name), name)
    except api.exceptions.ResourceNotFoundException:
        api.create_secret(
            Name=name,
            ClientRequestToken=token,
            SecretString=payload,
            Description=f'Existing owner WorkOS credential for {origin}',
            Tags=[
                {'Key': 'project', 'Value': 'inkseq'},
                {'Key': 'environment', 'Value': target},
            ],
        )
    else:
        # An existing uninitialized container is supported. Never replace any
        # retained version, a pending deletion, or a concurrent import.
        if description.get('DeletedDate') or description.get('VersionIdsToStages'):
            raise ImportErrorSafe('workos_existing_secret_requires_review')
        api.put_secret_value(
            SecretId=name, ClientRequestToken=token, SecretString=payload
        )
    result, version = _value(api.get_secret_value(SecretId=name, VersionId=token), name)
    if version != token or not _same(result, credentials):
        raise ImportErrorSafe('workos_secret_readback_mismatch')
    current, current_version = _value(
        api.get_secret_value(SecretId=name, VersionStage='AWSCURRENT'), name
    )
    if current_version != token or not _same(current, credentials):
        raise ImportErrorSafe('workos_secret_current_version_changed')
    return {'status': 'imported', 'version_id': version}


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--target', choices=tuple(SOURCES), required=True)
    args = parser.parse_args(argv)
    # A process-wide debug logger must not serialize the Secrets Manager body.
    logging.getLogger('boto3').setLevel(logging.CRITICAL)
    logging.getLogger('botocore').setLevel(logging.CRITICAL)
    try:
        result = import_existing(args.target)
    except ImportErrorSafe as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ClientError, BotoCoreError, ValueError, OSError):
        print('workos_credential_import_failed', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

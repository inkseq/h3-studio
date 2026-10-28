"""Host-only read of the existing staging Modal secret; no creation or rotation.

Call inside the normal zero-work release/CAS adoption. This module is inert;
it does not expose a standalone bypass around those operational gates.
"""

import json
import os
from pathlib import Path
import re
import stat

from studio_platform.modal_credentials import unique_pairs, validate

SECRET = '/sixnine/staging/modal'
INSTANCE = 'i-07805df7bd6984bd9'
DIRECTORY = Path('/srv/sixnine/dstack-runtime')


def require_staging_host():
    if (
        os.geteuid() != 0
        or Path('/var/lib/cloud/data/instance-id').read_text().strip() != INSTANCE
    ):
        raise ValueError('modal_staging_host_required')


def client():
    require_staging_host()
    import boto3
    from botocore.config import Config

    return boto3.client(
        'secretsmanager',
        region_name='ap-southeast-1',
        endpoint_url='https://secretsmanager.ap-southeast-1.amazonaws.com',
        config=Config(
            connect_timeout=5, read_timeout=20, retries={'total_max_attempts': 1}
        ),
    )


def hydrate(api, writer, *, target):
    """Only the existing AWSCURRENT value, with metadata-only return values."""
    if target != 'staging':
        raise ValueError('modal_staging_only')
    response = api.get_secret_value(SecretId=SECRET, VersionStage='AWSCURRENT')
    try:
        if (
            response['Name'] != SECRET
            or not re.fullmatch(
                r'arn:aws:secretsmanager:ap-southeast-1:829135631045:secret:/sixnine/staging/modal-[A-Za-z0-9]{6}',
                response['ARN'],
            )
            or not re.fullmatch(r'[A-Za-z0-9-]{32,64}', response['VersionId'])
            or type(response['VersionStages']) is not list
            or 'AWSCURRENT' not in response['VersionStages']
            or not isinstance(response['SecretString'], str)
            or len(response['SecretString']) > 16384
        ):
            raise ValueError()
        value = validate(
            json.loads(response['SecretString'], object_pairs_hook=unique_pairs)
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError('modal_existing_staging_secret_invalid') from None
    writer(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
    return {
        'state': 'modal_existing_staging_secret_hydrated',
        'name': SECRET,
        'version_id': response['VersionId'],
    }


def write_runtime(raw):
    """Same no-rotation/inode-preserving policy as runtime_secrets_aws."""
    require_staging_host()
    validate(json.loads(raw, object_pairs_hook=unique_pairs))
    for path in (DIRECTORY, *DIRECTORY.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_mode & 0o022:
            raise ValueError('modal_runtime_directory_unprotected')
    path = DIRECTORY / 'modal-credentials.json'
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        created = False
    else:
        created = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise ValueError('modal_runtime_file_unprotected')
        if not created:
            if (
                info.st_gid != 10001
                or stat.S_IMODE(info.st_mode) != 0o440
                or info.st_size > 16384
                or os.read(fd, 16385) != raw
            ):
                raise ValueError('modal_existing_runtime_differs_rotation_required')
            return
        os.fchown(fd, 0, 10001)
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, 'wb', closefd=False) as output:
            output.write(raw)
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)

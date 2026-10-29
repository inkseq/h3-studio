"""Explicit one-container native entrypoint; never a Modal generation function.

Only the private model cache is shared. The original CPU controller installs a
single launch document over authenticated SDK file I/O after journaling create.
No provider credential, database URL or business request belongs in this file.
"""

import argparse
from functools import partial
import json
import os
from pathlib import Path
import re
import stat
import time
import traceback

from .modal_runtime import PROFILE, host_factory, serve
from .runtime_catalog import engine_manifest, runtime_config

STATE = Path('/run/inkseq-modal')
MODELS = Path('/ckpts/ckpts')
RUNTIME = Path('/opt/workspace-internal/Wan2GP')


def launch_document(path):
    """Fail closed on an incomplete/linked/private file; never echo its bytes."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 4096
            or os.name == 'posix'
            and (info.st_uid != os.getuid() or info.st_mode & 0o077)
        ):
            raise ValueError('modal_launch_file_invalid')
        value = json.loads(stream.read(4097))
    return validate_launch(value)


def validate_launch(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'intent_id', 'mode', 'manifest_digest', 'token'}
        or not isinstance(value['intent_id'], str)
        or not re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['intent_id']
        )
        or value['mode'] not in {'fl', 'ref'}
        or not isinstance(value['token'], str)
        or not re.fullmatch(r'[0-9a-f]{64}', value['token'])
        or value['manifest_digest'] != engine_manifest(PROFILE, value['mode']).digest
    ):
        raise ValueError('modal_launch_binding_invalid')
    return value


def _write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install_launch(value, state=STATE):
    """Publish a complete directory once; interrupted writes remain evidence."""
    validate_launch(value)
    state = Path(state)
    info = state.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise ValueError('modal_launch_directory_invalid')
    target = state / 'launch'
    if target.exists() or target.is_symlink():
        if target.is_symlink() or launch_document(target / 'value.json') != value:
            raise ValueError('modal_launch_binding_changed')
        return 'observed'
    pending = state / 'launch.pending'
    pending.mkdir(mode=0o700, exist_ok=False)
    _write(pending / 'value.json', json.dumps(value, separators=(',', ':')).encode())
    # Both directories are on the private ephemeral filesystem. A nonempty
    # existing target cannot be replaced; no partial file becomes visible.
    pending.rename(target)
    return 'installed'


def prepare_files(value, state, models):
    """Local configuration only; never download, rent, submit or rewrite a job."""
    state, models = Path(state), Path(models)
    if state.is_symlink() or models.is_symlink():
        raise ValueError('modal_runtime_path_invalid')
    manifest = engine_manifest(PROFILE, value['mode'])
    if manifest.digest != value['manifest_digest']:
        raise ValueError('modal_launch_binding_invalid')
    _write(state / 'manifest.json', json.dumps(manifest.document).encode())
    _write(state / 'token', value['token'].encode())
    _write(
        state / 'wgp_config.json', json.dumps(runtime_config(PROFILE, models)).encode()
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit private Modal worker')
    parser.parse_args(argv)
    try:
        # This directory is ephemeral and never mounted on the model Volume.
        STATE.mkdir(mode=0o700, exist_ok=False)
        deadline = time.monotonic() + 60
        launch = STATE / 'launch' / 'value.json'
        while not launch.exists():
            if time.monotonic() >= deadline:
                raise ValueError('modal_launch_not_installed')
            time.sleep(0.25)
        value = launch_document(launch)
        if not MODELS.is_dir():
            raise ValueError('modal_verified_cache_missing')
        prepare_files(value, STATE, MODELS)
        from .runtime_hosts.wangp_launcher import main as native_main
        from .runtime_hosts.wangp_session import create_session

        # Cache population is a separate, recorded qualification operation.
        # Never let concurrent GPU hosts write/download into a shared Volume.
        # The launcher repeats full source/dependency/model verification before
        # it loads the model; the explicit hardware hook admits only one L40S.
        return native_main(
            [
                '--runtime-root',
                str(RUNTIME),
                '--config',
                str(STATE / 'wgp_config.json'),
                '--manifest',
                str(STATE / 'manifest.json'),
                '--model-root',
                str(MODELS),
                '--state-dir',
                str(STATE / 'native'),
                '--token-file',
                str(STATE / 'token'),
                '--slot-key',
                'modal-' + value['intent_id'],
                '--create-journal',
            ],
            host_factory=host_factory,
            session_factory=partial(create_session, modal_l40s=True),
            serve=serve,
        )
    except Exception as error:
        # Any restart gets the existing directory and refuses; no queue replay.
        # Only the class and a short reason code are printed: never file bytes.
        code = str(error)
        print(
            json.dumps(
                {
                    'state': 'modal_native_start_refused',
                    'error': type(error).__name__,
                    'reason': code
                    if re.fullmatch(r'[a-z0-9_.:-]{1,100}', code)
                    else None,
                }
            ),
            flush=True,
        )
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

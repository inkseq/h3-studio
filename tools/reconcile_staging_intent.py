"""One original pre-runtime staging refusal; no deployment or service actions.

Run only the reviewed file as root. A partial operation is never replayed.
The existing release lock, protected metadata and explicit retention pin remain
the authority; this is not a new failed/success state in the release protocol.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

FAILED = 'fd0f7aba83b72a38deb1ac2344445cc23bdb1bda'
CURRENT = 'f8d6fb8c4176c6283b2d400b050c68dea8e543e9'
INSTANCE = 'i-07805df7bd6984bd9'
INTENT_SHA = '1e568d64732cebf60a2fadffa485fb237efd660028ec3e5b44b71cfb7095fe55'
CANDIDATE = '4fad58ba95b46c73218040170ac878624874babc874637433c421b33e92b9157'
OP = 'refused-intent-707-fd0f7ab'
PROOF = {
    'CommandId': 'fcbe99e1-fbd4-477a-8b0a-7d86f6b9df29',
    'InstanceId': INSTANCE,
    'Status': 'Failed',
    'ResponseCode': 1,
    'ExecutionEndDateTime': '2026-10-27T13:02:11.486Z',
    'StandardOutputContent': '',
    'StandardErrorContent': 'migration_root_helpers_need_reviewed_update; preserve and inspect the original candidate/command\nfailed to run commands: exit status 1',
}
HELPERS = {
    'migration_maintenance.py': '135cbf73dfd467ddd870197d650ff66514e17b0808f403623e26ac01a7bffbef',
    'migration_check.py': '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a',
}
MARKERS = (
    'deployment-target.json',
    'environment-release-intent.json',
    'release-state.json',
    'migration/active.json',
    'frontend/current.json',
    'migration/paths.json',
    'release-retention-pins.json',
)


def need(value, code):
    if not value:
        raise ValueError(code)


def encode(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def protected_dir(path):
    for parent in (path, *path.parents):
        info = parent.lstat()
        need(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022,
            'unprotected_directory',
        )


def read(path):
    protected_dir(path.parent)
    before = path.lstat()
    need(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and before.st_uid == 0
        and not before.st_mode & 0o022
        and before.st_size <= 1024 * 1024,
        'unprotected_file',
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        after = os.fstat(fd)
        need(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            'file_changed',
        )
        raw = os.read(fd, before.st_size + 1)
        need(len(raw) == before.st_size, 'file_changed')
        return raw
    finally:
        os.close(fd)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync(path.parent)


def containers():
    env = {
        'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
        'LANG': 'C.UTF-8',
        'DOCKER_CONFIG': '/nonexistent',
    }

    def command(args):
        return subprocess.run(
            ['/usr/bin/docker', *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout

    ids = command(
        ['ps', '-aq', '--filter', 'label=com.docker.compose.project=sixnine-platform']
    ).split()
    need(bool(ids), 'containers_missing')
    template = '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},"started":{{json .State.StartedAt}},"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}},"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
    rows = [
        json.loads(line)
        for line in command(['inspect', '--format', template, *ids]).splitlines()
    ]
    live = {row['service'] for row in rows if row['running']}
    need(
        {'app', 'controller', 'db', 'dstack', 'caddy'} <= live,
        'required_service_missing',
    )
    need(
        all(row['health'] in (None, 'healthy') for row in rows if row['running']),
        'container_unhealthy',
    )
    return sorted(rows, key=lambda row: row['id'])


def snapshot(root, helpers, proof, observe):
    proof_raw = read(proof)
    need(json.loads(proof_raw) == PROOF, 'original_failure_proof_required')
    raw = {name: read(root / name) for name in MARKERS}
    values = {name: json.loads(data) for name, data in raw.items()}
    target = values['deployment-target.json']
    need(
        target.get('target') == 'staging' and target.get('instance_id') == INSTANCE,
        'staging_only',
    )
    need(digest(raw['environment-release-intent.json']) == INTENT_SHA, 'intent_changed')
    need(
        values['environment-release-intent.json']
        == {
            'version': 1,
            'commit': FAILED,
            'candidate_sha256': CANDIDATE,
            'state': 'in_progress',
        },
        'intent_changed',
    )
    platform = values['release-state.json']
    active = values['migration/active.json']
    need(
        platform.get('current') == CURRENT
        and platform.get('pending') is None
        and platform.get('status') == 'app_ready',
        'platform_changed',
    )
    need(
        active.get('current') == CURRENT
        and active.get('pending') is None
        and active.get('active') is True,
        'migration_changed',
    )
    need(values['frontend/current.json'].get('commit') == CURRENT, 'frontend_changed')
    pins = values['release-retention-pins.json']
    need(
        type(pins) is dict
        and set(pins) == {'version', 'commits'}
        and type(pins['version']) is int
        and pins['version'] == 1
        and type(pins['commits']) is list
        and len(pins['commits']) < 128
        and all(
            type(x) is str and len(x) == 40 and all(c in '0123456789abcdef' for c in x)
            for x in pins['commits']
        ),
        'pins_invalid',
    )
    hashes = {name: digest(read(helpers / name)) for name in HELPERS}
    need(hashes == HELPERS, 'trusted_helpers_changed')
    plan = {
        'version': 1,
        'failed_commit': FAILED,
        'current_commit': CURRENT,
        'markers': {name: digest(data) for name, data in raw.items()},
        'helpers': hashes,
        'containers': observe(),
        'proof_sha256': digest(proof_raw),
    }
    return plan, raw, pins


def run(root, helpers, proof, expected=None, *, observe=containers):
    import fcntl

    protected_dir(root)
    read(root / 'release.lock')
    with (root / 'release.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, raw, pins = snapshot(root, helpers, proof, observe)
        plan_sha = digest(encode(plan))
        if expected is None:
            return {'state': 'plan_only', 'plan_sha256': plan_sha, 'plan': plan}
        need(expected == plan_sha, 'snapshot_changed')
        need(digest(read(proof)) == plan['proof_sha256'], 'proof_changed')
        operation = root / OP
        need(
            not operation.exists() and not operation.is_symlink(),
            'operation_exists_reconcile_only',
        )
        operation.mkdir(mode=0o700)
        sync(root)
        save(operation / 'plan.json', encode(plan))
        save(operation / 'failure-proof.json', read(proof))
        save(operation / 'original-pins.json', raw['release-retention-pins.json'])
        new_pins = {'version': 1, 'commits': sorted(set(pins['commits']) | {FAILED})}
        fd, temporary = tempfile.mkstemp(prefix='.reconcile-707-', dir=root)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encode(new_pins))
            stream.flush()
            os.fsync(stream.fileno())
        # Pin first. A crash keeps the old intent or the explicit failed pin.
        need(
            read(root / 'release-retention-pins.json')
            == raw['release-retention-pins.json'],
            'pins_changed',
        )
        os.replace(temporary, root / 'release-retention-pins.json')
        sync(root)
        for name in MARKERS:
            if name != 'release-retention-pins.json':
                need(read(root / name) == raw[name], 'marker_changed')
        need(observe() == plan['containers'], 'containers_changed')
        os.rename(
            root / 'environment-release-intent.json', operation / 'original-intent.json'
        )
        sync(operation)
        sync(root)
        need(
            digest(read(operation / 'original-intent.json')) == INTENT_SHA,
            'archive_changed',
        )
        receipt = {
            'state': 'original_refusal_archived_not_deployed',
            'failed_commit': FAILED,
            'plan_sha256': plan_sha,
            'pins_sha256': digest(read(root / 'release-retention-pins.json')),
        }
        save(operation / 'completed.json', encode(receipt))
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--apply-plan-sha256')
    args = parser.parse_args()
    need(os.geteuid() == 0, 'root_required')
    print(
        json.dumps(
            run(
                Path('/srv/sixnine'),
                Path('/opt/inkseq-release-staging-v1'),
                args.proof,
                args.apply_plan_sha256,
            ),
            sort_keys=True,
        )
    )


if __name__ == '__main__':
    main()

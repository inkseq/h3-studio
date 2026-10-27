#!/usr/bin/python3
"""Read-only host tuple qualification; never install or execute an operation.

The reviewed writer must call locked_qualification with the SAME binding and
keep that context open through its own intent/write boundary. CLI success is
only an observation, never a reusable execution token. No application imports.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

ROOT = Path('/srv/sixnine')
HELPERS = Path('/opt/inkseq-release-staging-v1')
INSTANCE = Path('/var/lib/cloud/data/instance-id')
HOSTS = {'production': 'i-03d81d2d153b5e2fd', 'staging': 'i-07805df7bd6984bd9'}
SERVICES = {'app', 'controller', 'dstack', 'db', 'caddy'}
REQUIRED_HELPERS = {'release.py', 'deployment_target.py', 'check_config.py',
                    'migration_check.py', 'migration_maintenance.py'}
MARKERS = {'release-state.json', 'deployment-target.json', 'migration/active.json',
           'migration/paths.json', 'site.env'}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
       'DOCKER_CONFIG': '/nonexistent'}
QUIET = """import json
from sqlalchemy import create_engine,text
from studio_platform.settings import Settings
e=create_engine(Settings.from_environment().database_url)
with e.connect() as c:
 c.execute(text('SET TRANSACTION READ ONLY'))
 n=c.execute(text("SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled')")).scalar_one()
print(json.dumps({'nonterminal_jobs':n}))
e.dispose()
"""


class Refused(Exception):
    pass


def need(ok, code):
    if not ok:
        raise Refused(code)


def matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def relative(value):
    return (type(value) is str and bool(value) and '\\' not in value
            and not PurePosixPath(value).is_absolute()
            and str(PurePosixPath(value)) == value
            and all(x not in {'.', '..'} for x in value.split('/')))


def file_map(value):
    return (type(value) is dict and bool(value)
            and all(relative(k) and matches(v, '[0-9a-f]{64}') for k, v in value.items()))


def validate(binding):
    need(type(binding) is dict and set(binding) == {
        'version', 'environment', 'instance_id', 'current', 'previous',
        'helpers', 'files', 'containers', 'absent'}, 'qualification_binding_invalid')
    need(type(binding['version']) is int and binding['version'] == 1
         and binding['environment'] in HOSTS
         and binding['instance_id'] == HOSTS[binding['environment']],
         'qualification_host_binding_invalid')
    for key in ('current', 'previous'):
        need(matches(binding[key], '[0-9a-f]{40}'), 'qualification_release_pin_required')
    need(binding['current'] != binding['previous'], 'qualification_distinct_rollback_required')
    need(file_map(binding['helpers']) and REQUIRED_HELPERS <= set(binding['helpers'])
         and all('/' not in k for k in binding['helpers']), 'qualification_helper_pins_required')
    required = set(MARKERS)
    for commit in (binding['current'], binding['previous']):
        required.update({f'releases/{commit}/release-manifest.json',
                         f'approved-releases/{commit}.sha256',
                         f'migration/releases/{commit}/artifact/migration-manifest.json',
                         f'approved-migrations/{commit}.sha256'})
    need(file_map(binding['files']) and required <= set(binding['files']),
         'qualification_marker_pins_required')
    need(type(binding['absent']) is list
         and all(relative(x) for x in binding['absent'])
         and len(set(binding['absent'])) == len(binding['absent'])
         and not set(binding['absent']) & set(binding['files']), 'qualification_absence_invalid')
    rows = binding['containers']
    need(type(rows) is dict and set(rows) == SERVICES, 'qualification_runtime_pins_required')
    for row in rows.values():
        need(type(row) is dict and set(row) == {'id', 'image', 'started', 'running'}
             and matches(row['id'], '[0-9a-f]{64}')
             and matches(row['image'], 'sha256:[0-9a-f]{64}')
             and type(row['started']) is str and bool(row['started'])
             and row['running'] is True, 'qualification_runtime_pin_invalid')
    need(len({row['id'] for row in rows.values()}) == len(SERVICES),
         'qualification_runtime_identity_repeated')
    return binding


def directory(path):
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
         'qualification_directory_unprotected')


def protected(path):
    need(path.is_absolute(), 'qualification_path_not_absolute')
    for parent in path.parents:
        directory(parent)
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
         and not info.st_mode & 0o022 and info.st_size <= 1024**2,
         'qualification_file_unprotected')
    return info


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read(path):
    before = protected(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        need(identity(before) == identity(os.fstat(fd)), 'qualification_file_changed')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read(1024**2 + 1)
        need(len(raw) == before.st_size and identity(before) == identity(os.fstat(fd))
             and identity(before) == identity(path.lstat()), 'qualification_file_changed')
        return raw
    finally:
        os.close(fd)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def docker(args, timeout=30):
    process = subprocess.run(['/usr/bin/docker', *args], capture_output=True,
                             env=ENV, stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    need(process.returncode == 0, 'qualification_docker_read_failed')
    return process.stdout


def runtime():
    result = {}
    for service in sorted(SERVICES):
        ids = docker(['ps', '-q', '--no-trunc', '--filter',
                      'label=com.docker.compose.project=sixnine-platform', '--filter',
                      'label=com.docker.compose.service=' + service]).decode().split()
        need(len(ids) == 1 and matches(ids[0], '[0-9a-f]{64}'),
             'qualification_service_not_unique')
        fmt = ('{"id":{{json .Id}},"image":{{json .Image}},'
               '"running":{{json .State.Running}},"started":{{json .State.StartedAt}}}')
        result[service] = json.loads(docker(['inspect', '--format', fmt, ids[0]]))
    return result


def archive_image_ids(commit, manifest):
    # OCI manifest and Docker config IDs may differ in the same approved archive.
    # Reuse the installed validator without loading images or incoming scripts.
    program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import release
assert Path(release.__file__)==Path(sys.argv[1])/'release.py'
value=json.loads(sys.stdin.read())
print(json.dumps(sorted(release.validate_image_archive(Path(sys.argv[2]),value))))
"""
    archive = ROOT / 'releases' / commit / 'image.tar.gz'
    process = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', program,
                              str(HELPERS), str(archive)],
                             input=json.dumps(manifest).encode(), capture_output=True,
                             env=ENV, timeout=120, check=False, cwd='/')
    need(process.returncode == 0, 'qualification_approved_archive_invalid')
    values = json.loads(process.stdout)
    need(type(values) is list and bool(values)
         and all(matches(x, 'sha256:[0-9a-f]{64}') for x in values),
         'qualification_archive_identity_invalid')
    return set(values)


def check(binding):
    need(read(INSTANCE).decode().strip() == binding['instance_id'], 'qualification_wrong_host')
    for name, digest in binding['helpers'].items():
        need(sha(read(HELPERS / name)) == digest, 'qualification_helper_changed')
    files = {name: read(ROOT / name) for name in binding['files']}
    need(all(sha(raw) == binding['files'][name] for name, raw in files.items()),
         'qualification_marker_changed')
    state = json.loads(files['release-state.json'])
    need(state.get('current') == binding['current'] and state.get('previous') == binding['previous']
         and state.get('status') == 'app_ready' and not state.get('pending'),
         'qualification_release_not_ready')
    active = json.loads(files['migration/active.json'])
    need(active.get('current') == binding['current'] and not active.get('pending'),
         'qualification_migration_not_ready')
    target = json.loads(files['deployment-target.json'])
    need(target.get('target') == binding['environment']
         and target.get('instance_id') == binding['instance_id'], 'qualification_target_changed')
    for commit in (binding['current'], binding['previous']):
        for manifest, approval in (
            (f'releases/{commit}/release-manifest.json', f'approved-releases/{commit}.sha256'),
            (f'migration/releases/{commit}/artifact/migration-manifest.json',
             f'approved-migrations/{commit}.sha256')):
            need(files[approval].decode().strip() == sha(files[manifest]),
                 'qualification_independent_approval_mismatch')
        manifest = json.loads(files[f'releases/{commit}/release-manifest.json'])
        need(manifest.get('commit') == commit, 'qualification_manifest_commit_changed')
        if commit == binding['current']:
            need(binding['containers']['app']['image'] in archive_image_ids(commit, manifest),
                 'qualification_manifest_image_changed')
    for name in binding['absent']:
        path = ROOT / name
        # Existing evidence, including dangling links, must never be overwritten.
        need(not path.exists() and not path.is_symlink(), 'qualification_prior_outcome_exists')
        directory(path.parent)
        for parent in path.parents:
            directory(parent)
    need(runtime() == binding['containers'], 'qualification_runtime_changed')


@contextmanager
def locked_qualification(binding):
    """Shared pre-write boundary. Caller still owns operation-specific checks."""
    binding = validate(json.loads(json.dumps(binding)))  # Detach caller mutable pins.
    need(os.geteuid() == 0, 'qualification_root_required')
    lock = ROOT / 'release.lock'
    before = protected(lock)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)  # Never create a missing lock.
    try:
        need(identity(before) == identity(os.fstat(fd)), 'qualification_lock_changed')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check(binding)
        quiet = json.loads(docker(['exec', binding['containers']['app']['id'],
                                  'python', '-B', '-c', QUIET], timeout=60))
        need(type(quiet) is dict and set(quiet) == {'nonterminal_jobs'}
             and type(quiet['nonterminal_jobs']) is int and quiet['nonterminal_jobs'] == 0,
             'qualification_zero_work_required')
        check(binding)
        need(identity(before) == identity(lock.lstat()), 'qualification_lock_changed')
        yield {'state': 'qualified_no_writes', 'environment': binding['environment'],
               'current': binding['current'], 'previous': binding['previous'],
               'app_image': binding['containers']['app']['image'], 'nonterminal_jobs': 0,
               'binding_sha256': sha(json.dumps(binding, sort_keys=True).encode())}
    finally:
        os.close(fd)


def main(args):
    try:
        need(len(args) == 1, 'qualification_binding_path_required')
        binding = json.loads(read(Path(args[0])))
        with locked_qualification(binding) as receipt:
            print(json.dumps(receipt))
        return 0
    except Exception as error:
        # Never emit subprocess stderr, config contents or arbitrary exception text.
        code = str(error) if isinstance(error, Refused) else 'qualification_read_failed'
        print(json.dumps({'state': 'refused', 'code': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

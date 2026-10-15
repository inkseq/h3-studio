"""Root-owned static release installer. Never starts/stops/recreates a service."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import frontend_bundle
import release
import deployment_target


def protected_directory(path, *, create=False):
    created = False
    if create:
        try:
            path.mkdir(mode=0o755)
            created = True
        except FileExistsError:
            pass
    info = path.lstat()
    release.require(stat.S_ISDIR(info.st_mode) and not path.is_symlink()
        and not getattr(info, 'st_file_attributes', 0) & 0x400
        and (os.name == 'nt' or info.st_uid == 0 and not info.st_mode & 0o022),
        'frontend_directory_not_protected')
    if created:
        # mkdtemp/root operators may inherit umask 077; app UID 10001 must
        # traverse these public static directories despite that host umask.
        path.chmod(0o755)


def approved(root, directory, commit):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_frontend_commit')
    protected_directory(root / 'approved-frontends')
    path = root / 'approved-frontends' / (commit + '.sha256')
    release.regular(path, root_owned=True, maximum=128)
    release.regular(directory / 'frontend-manifest.json', maximum=256 * 1024)
    digest = path.read_text(encoding='ascii').strip()
    release.require(release.DIGEST.fullmatch(digest)
        and digest == release.checksum(directory / 'frontend-manifest.json'), 'frontend_independent_approval_mismatch')


def atomic_file(path, content):
    if path.exists() or path.is_symlink():
        release.regular(path, root_owned=True, maximum=128 * 1024**2)
    descriptor, name = tempfile.mkstemp(prefix='.frontend-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
        release.sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def immutable_file(path, content):
    if path.exists() or path.is_symlink():
        release.regular(path, root_owned=True, maximum=32 * 1024**2)
        release.require(path.read_bytes() == content, 'immutable_frontend_file_collision')
    else:
        atomic_file(path, content)


def compatible(candidate, backend):
    contracts = release.validate_contracts(backend.get('contracts'))
    release.require(contracts['api_compatibility'] == candidate['api_compatibility']
        and contracts['frontend_contract'] == candidate['api_contract'],
        'frontend_requires_matching_deployed_api')


def app_snapshot(directory, environment):
    # Fixed, read-only commands. Never accept arbitrary code or URLs from bundle.
    script = (
        "import json,os,urllib.request; from urllib.parse import urlsplit; "
        "origin=os.environ.get('PUBLIC_ORIGIN',os.environ.get('SIXNINE_PUBLIC_ORIGIN','')); "
        "assert origin in " + repr((deployment_target.environment(environment)['origin'],)
            if deployment_target.environment(environment)['name'] == 'staging'
            else ('https://inkseq.com', 'https://www.sixnine.art')) + "; "
        "assert not os.environ.get('SIXNINE_PUBLIC_ORIGIN') or os.environ['SIXNINE_PUBLIC_ORIGIN']==origin; "
        "h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); "
        "r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8845/',"
        "headers={'Host':urlsplit(origin).netloc}),timeout=5); "
        "print(json.dumps({'contract':h.get('frontend_contract'),"
        "'commit':r.headers.get('X-Sixnine-Frontend-Commit'),"
        "'html_contract':r.headers.get('X-Sixnine-Frontend-Contract')}))"
    )
    raw = release.compose(directory, environment, 'exec', '-T', 'app', 'python', '-c', script)
    value = json.loads(raw)
    release.require(isinstance(value, dict) and set(value) == {'contract', 'commit', 'html_contract'},
        'frontend_probe_invalid')
    return value


def install_locked(root, directory, commit, *, current_api=None, probe=None):
    """Caller holds release.lock; production dependencies can be faked offline."""
    root, directory = Path(root), Path(directory)
    protected_directory(root)
    approved(root, directory, commit)
    candidate = frontend_bundle.validate(directory, commit)
    api_commit, api_directory, environment = (current_api or release.current_application)(root)
    backend = release.manifest(api_directory, api_commit)
    compatible(candidate, backend)
    # Bind the approved source to the actual running API, not just a disk pointer.
    expected = {**backend, 'archive_image_ids': release.validate_image_archive(api_directory / 'image.tar.gz', backend)}
    release.verify_running_app(api_directory, environment, expected)
    check = probe or app_snapshot
    before = check(api_directory, environment)
    release.require(before['contract'] == candidate['api_contract'], 'deployed_api_frontend_contract_missing')
    contents = frontend_bundle.archive_files(directory, candidate)
    approved(root, directory, commit)
    base = root / 'frontend'
    for path in (base, base / 'assets', base / 'releases'):
        protected_directory(path, create=True)
    selected = base / 'releases' / commit
    protected_directory(selected, create=True)
    # All bytes are verified before the pointer can change. Shared assets are
    # never overwritten; browser tabs from earlier releases keep working.
    for name, content in contents.items():
        target = selected / 'index.html' if name == 'index.html' else base / name
        immutable_file(target, content)
    immutable_file(selected / 'frontend-manifest.json', (directory / 'frontend-manifest.json').read_bytes())
    pointer = base / 'current.json'
    previous = None
    if pointer.exists() or pointer.is_symlink():
        release.regular(pointer, root_owned=True, maximum=1024)
        previous = pointer.read_bytes()
    value = {'version': 1, 'commit': commit, 'api_contract': candidate['api_contract']}
    atomic_file(pointer, (json.dumps(value, sort_keys=True) + '\n').encode('utf-8'))
    try:
        after = check(api_directory, environment)
        release.require(after == {'contract': candidate['api_contract'], 'commit': commit,
            'html_contract': candidate['api_contract']}, 'frontend_activation_probe_failed')
    except Exception:
        if previous is None:
            pointer.unlink()
            release.sync_directory(base)
        else:
            atomic_file(pointer, previous)
        raise
    if previous is not None:
        atomic_file(base / 'previous.json', previous)
    return {'state': 'approved_frontend_release_healthy', 'commit': commit}

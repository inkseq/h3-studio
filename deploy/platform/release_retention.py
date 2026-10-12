#!/usr/bin/python3
"""Bounded root-host release/cache retention; plan by default, never data GC.

Install this reviewed file independently beside release.py. All callers hold the
existing release.lock. S3 is read-only and only immutable publisher objects are
considered. Unknown/partial artifacts stay on disk for an explicit review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import release

IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
       "DOCKER_CONFIG": "/opt/sixnine-release/docker-config"}
MAX_ENTRIES = 8192
MAX_OBJECTS = 4096


def safe_directory(path, *, incoming=False):
    info = Path(path).lstat()
    release.require(stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
        and (os.name == 'nt' or info.st_uid == 0 and not info.st_mode & (0o002 if incoming else 0o022)),
        'retention_directory_not_protected')
    return info


def safe_ancestors(root, path):
    root, path = Path(root), Path(path)
    release.require(path.is_relative_to(root) and path != root, 'retention_path_outside_release_root')
    safe_directory(root)
    for parent in reversed(path.parents):
        if parent != root and parent.is_relative_to(root):
            safe_directory(parent, incoming=parent == root / 'incoming')


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)


def read_json(root, relative, observed, *, optional=False):
    path = root / relative
    safe_ancestors(root, path)
    if optional and not path.exists() and not path.is_symlink():
        observed[relative] = None
        return None
    release.regular(path, root_owned=True, maximum=262144)
    before = identity(path.lstat())
    raw = path.read_bytes()
    release.require(identity(path.lstat()) == before, 'retention_pointer_changed')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            release.require(key not in result, 'retention_duplicate_pointer_field')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    release.require(type(value) is dict, 'retention_pointer_invalid')
    observed[relative] = {'identity': before, 'sha256': hashlib.sha256(raw).hexdigest()}
    return value


def commit_value(value):
    release.require(type(value) is str and release.SHA.fullmatch(value), 'retention_commit_invalid')
    return value


def closure(root, containers):
    """App/frontend/migration/execution pins and real container bind references."""
    observed, kept = {}, set()
    state = read_json(root, 'release-state.json', observed)
    release.require(state.get('status') in {'app_ready', 'rolled_back_app_only'} and state.get('pending') is None,
                    'retention_release_requires_reconciliation')
    kept.add(commit_value(state.get('current')))
    if state.get('previous') is not None:
        kept.add(commit_value(state['previous']))
    for relative in ('frontend/current.json', 'frontend/previous.json', 'frontend/previous-platform-pointer.json'):
        if not (root / relative).parent.exists():
            continue
        value = read_json(root, relative, observed, optional=True)
        if value is not None:
            kept.add(commit_value(value.get('commit')))
    if (root / 'migration').exists():
        value = read_json(root, 'migration/active.json', observed, optional=True)
        if value is not None:
            release.require(value.get('pending') is None, 'retention_migration_requires_reconciliation')
            if value.get('current') is not None:
                kept.add(commit_value(value['current']))
    for folder in ('operator-capacity', 'gpu-acceptance', 'gpu-scaler'):
        if not (root / folder).exists():
            continue
        value = read_json(root, folder + '/active.json', observed, optional=True)
        if value is not None:
            release.require(type(value.get('active')) is bool, 'retention_execution_marker_unknown')
            if value['active']:
                kept.add(commit_value(value.get('commit')))
                prepared = read_json(root, folder + '/prepared.json', observed, optional=True)
                if prepared is not None:
                    kept.add(commit_value(prepared.get('commit')))
    for item in containers:
        labels = item.get('Config', {}).get('Labels', {}) or {}
        references = [labels.get('com.docker.compose.project.working_dir', '')] if item.get('State', {}).get('Running') is True else []
        references += [mount.get('Source', '') for mount in item.get('Mounts', []) if mount.get('Type') == 'bind']
        for ref in references:
            # No substring guessing: only an exact managed release path prefix.
            for prefix in (root / 'releases', root / 'migration/releases', root / 'frontend/releases'):
                pieces = Path(ref).parts
                if ref and Path(ref).is_relative_to(prefix) and len(pieces) > len(prefix.parts):
                    kept.add(commit_value(pieces[len(prefix.parts)]))
    return kept, observed


def docker_snapshot(command=release.command):
    ids = command(['ps', '--all', '--quiet', '--no-trunc'], environment=ENV, timeout=30).decode().split()
    release.require(len(ids) <= 128 and all(re.fullmatch(r'[0-9a-f]{64}', value) for value in ids),
                    'retention_container_inventory_invalid')
    containers = json.loads(command(['inspect', *ids], environment=ENV, timeout=30)) if ids else []
    images = command(['image', 'ls', '--all', '--quiet', '--no-trunc'], environment=ENV, timeout=30).decode().split()
    release.require(len(images) <= 512 and all(IMAGE.fullmatch(value) for value in images),
                    'retention_image_inventory_invalid')
    images = sorted(set(images))
    details = json.loads(command(['image', 'inspect', *images], environment=ENV, timeout=30)) if images else []
    release.require(type(containers) is list and len(containers) == len(ids)
        and type(details) is list and len(details) == len(images), 'retention_docker_inventory_incomplete')
    return containers, details


def tree_snapshot(root, path):
    """Bound and hash every regular file; links/devices/unexpected races reject."""
    safe_ancestors(root, path)
    entries, total = {}, 0
    def walk(current, relative):
        nonlocal total
        info = current.lstat()
        release.require(not getattr(info, 'st_file_attributes', 0) & 0x400,
                        'retention_tree_linked')
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = {'identity': identity(info), 'kind': 'directory'}
            for child in sorted(current.iterdir()):
                release.require(len(entries) < MAX_ENTRIES, 'retention_tree_limit')
                walk(child, (relative + '/' if relative else '') + child.name)
        else:
            release.regular(current, maximum=2 * 1024**3)
            digest = release.checksum(current)
            release.require(identity(current.lstat()) == identity(info), 'retention_file_changed')
            entries[relative] = {'identity': identity(info), 'kind': 'file', 'sha256': digest}
            total += info.st_size
    walk(path, '')
    return {'relative': path.relative_to(root).as_posix(), 'entries': entries, 'bytes': total}


def approval(root, kind, commit, manifest_path):
    directory = {'platform': 'approved-releases', 'migration': 'approved-migrations',
                 'frontend': 'approved-frontends'}[kind]
    path = root / directory / (commit + '.sha256')
    safe_ancestors(root, path)
    release.regular(path, root_owned=True, maximum=128)
    release.regular(manifest_path, maximum=262144)
    digest = path.read_text(encoding='ascii').strip()
    release.require(release.DIGEST.fullmatch(digest) and digest == release.checksum(manifest_path),
                    'retention_independent_approval_missing')


def remote_proof(api, commit, prefix, payloads):
    import fetch_release_s3
    release.require(len(payloads) <= MAX_OBJECTS, 'retention_remote_proof_limit')
    for name, (digest, size) in payloads.items():
        response = api.head_object(Bucket=fetch_release_s3.BUCKET, Key=f'releases/{commit}/{prefix}{name}')
        release.require(response.get('ContentLength') == size
            and response.get('Metadata', {}).get('sha256') == digest
            and response.get('ServerSideEncryption') == 'AES256', 'retention_s3_restore_proof_missing')


def bundle(root, directory, kind, api, *, installed=False, image_cache=None):
    """Return validated local tree + exact immutable publisher restore proof."""
    manifest_name = {'platform': 'release-manifest.json', 'migration': 'migration-manifest.json',
                     'frontend': 'frontend-manifest.json'}[kind]
    artifact = directory / 'artifact' if kind == 'migration' and installed else directory
    safe_ancestors(root, artifact / manifest_name)
    release.regular(artifact / manifest_name, maximum=262144)
    raw = json.loads((artifact / manifest_name).read_text())
    commit = commit_value(raw.get('commit'))
    release.require(directory.name == commit or directory.name.startswith(('.download-', '.release-',
        '.migration-download-', '.migration-release-', '.frontend-download-')), 'retention_directory_identity_invalid')
    approval(root, kind, commit, artifact / manifest_name)
    if kind == 'platform':
        value = release.manifest(artifact, commit)
        files = value['files']
        release.require({p.name for p in artifact.iterdir()} == release.FILES | {manifest_name},
                        'retention_platform_extra_files')
    elif kind == 'migration':
        helper = release.migration_helper()
        value = helper.manifest(artifact, commit)
        files = value['files']
        if installed:
            release.require({p.name for p in directory.iterdir()} == {'artifact', 'sources', 'overlay.yaml'},
                            'retention_migration_extra_files')
            helper.verify_sources(directory / 'sources', value['source_index_sha256'])
            release.regular(directory / 'overlay.yaml', root_owned=True, maximum=1024**2)
            expected = (artifact / 'platform-overlay.yaml').read_text().replace(
                'source: ../hatchet/entrypoint.sh', 'source: ' + json.dumps(str(directory / 'artifact/hatchet-entrypoint.sh')))
            release.require((directory / 'overlay.yaml').read_text() == expected, 'retention_derived_overlay_changed')
    else:
        import frontend_bundle
        value = frontend_bundle.parse_manifest((artifact / manifest_name).read_bytes(), commit)
        if installed:
            release.require({p.name for p in directory.iterdir()} == {'index.html', manifest_name},
                            'retention_frontend_extra_files')
            release.require(release.checksum(directory / 'index.html') == value['files']['index.html']['sha256'],
                            'retention_frontend_index_changed')
            files = {}
        else:
            frontend_bundle.validate(directory, commit)
            files = {'frontend.tar.gz': value['archive_sha256']}
    snapshot = tree_snapshot(root, directory)
    payloads = {manifest_name: (release.checksum(artifact / manifest_name), (artifact / manifest_name).stat().st_size)}
    for name, digest in files.items():
        payloads[name] = (digest, (artifact / name).stat().st_size)
    if kind == 'frontend' and installed:
        payloads['frontend.tar.gz'] = (value['archive_sha256'], value['archive_bytes'])
    remote_proof(api, commit, {'platform': '', 'migration': 'migration/', 'frontend': 'frontend/'}[kind], payloads)
    image_id = value.get('image_id') or value.get('dispatch', {}).get('image_id')
    image_ids = []
    if image_id:
        cache = image_cache if image_cache is not None else {}
        key = (kind, commit, payloads[manifest_name][0])
        if key not in cache:
            image_value = value if kind == 'platform' else {'commit': commit, **value['dispatch']}
            cache[key] = release.validate_image_archive(artifact / ('image.tar.gz' if kind == 'platform' else
                'dispatch-image.tar.gz'), image_value)
        image_ids = sorted(set(cache[key]) | {image_id})
    return {**snapshot, 'commit': commit, 'kind': kind, 'image_ids': image_ids}


def plan_locked(root=release.ROOT, *, api, command=release.command):
    """No mutation. Caller keeps release.lock until apply completes."""
    root = Path(root)
    containers, images = docker_snapshot(command)
    kept, observed = closure(root, containers)
    candidates, retained, removable_image_ids, image_cache = [], [], set(), {}
    locations = (('releases', 'platform', True), ('incoming', 'platform', False),
        ('migration/releases', 'migration', True), ('incoming-migrations', 'migration', False),
        ('frontend/releases', 'frontend', True))
    for relative, kind, installed in locations:
        parent = root / relative
        if not parent.exists() and not parent.is_symlink():
            continue
        safe_ancestors(root, parent)
        safe_directory(parent, incoming=relative == 'incoming')
        for directory in sorted(parent.iterdir()):
            release.require(len(candidates) + len(retained) < 512, 'retention_release_count_limit')
            if installed and release.SHA.fullmatch(directory.name) and directory.name in kept:
                retained.append({'relative': directory.relative_to(root).as_posix(), 'reason': 'rollback_or_in_use'})
                continue
            try:
                item = bundle(root, directory, kind, api, installed=installed, image_cache=image_cache)
                if item['commit'] in kept and installed:
                    retained.append({'relative': item['relative'], 'reason': 'rollback_or_in_use'})
                    continue
                if item['commit'] in kept:
                    installed_artifact = root / ('releases' if kind == 'platform' else 'migration/releases') / item['commit']
                    if kind == 'migration':
                        installed_artifact /= 'artifact'
                    safe_ancestors(root, installed_artifact / ('release-manifest.json' if kind == 'platform' else 'migration-manifest.json'))
                    release.require(installed_artifact.is_dir() and not installed_artifact.is_symlink(), 'retention_kept_bundle_not_installed')
                    manifest_name = 'release-manifest.json' if kind == 'platform' else 'migration-manifest.json'
                    release.require(release.checksum(installed_artifact / manifest_name) ==
                        release.checksum(directory / manifest_name), 'retention_kept_staging_differs')
                candidates.append(item)
                removable_image_ids.update(item['image_ids'])
            except Exception:
                # No arbitrary provider/error text (or credential) in receipts.
                retained.append({'relative': directory.relative_to(root).as_posix(), 'reason': 'unproven_or_partial_artifact'})
    # Fresh private downloads not renamed into their managed parents after a
    # crash can be complete/restorable too; incomplete ones remain for review.
    for directory in sorted(root.iterdir()):
        kind = next((kind for prefix, kind in (('.download-', 'platform'), ('.migration-download-', 'migration'),
            ('.frontend-download-', 'frontend')) if directory.name.startswith(prefix)), None)
        if kind is not None:
            try:
                item = bundle(root, directory, kind, api, image_cache=image_cache)
                if item['commit'] not in kept:
                    candidates.append(item)
                else:
                    retained.append({'relative': item['relative'], 'reason': 'rollback_or_in_use'})
            except Exception:
                retained.append({'relative': directory.name, 'reason': 'unproven_or_partial_artifact'})
    protected_images = {item.get('Image') for item in containers}
    for commit in sorted(kept):
        directory = root / 'releases' / commit
        if directory.exists():
            safe_ancestors(root, directory); safe_directory(directory)
            value = release.manifest(directory, commit)
            release.approved_manifest(root, directory, commit)
            protected_images.update(release.validate_image_archive(directory / 'image.tar.gz', value))
            protected_images.add(value['image_id'])
        directory = root / 'migration/releases' / commit / 'artifact'
        if directory.exists():
            helper = release.migration_helper()
            value = helper.manifest(directory, commit); helper.approved(root, directory, commit)
            protected_images.update(release.validate_image_archive(directory / 'dispatch-image.tar.gz',
                {'commit': commit, **value['dispatch']}))
            protected_images.add(value['dispatch']['image_id'])
    image_candidates = []
    for image in images:
        image_id = image.get('Id')
        release.require(type(image_id) is str and IMAGE.fullmatch(image_id), 'retention_image_identity_invalid')
        tags, digests = image.get('RepoTags') or [], image.get('RepoDigests') or []
        owned = image_id in removable_image_ids and not digests and all(
            re.fullmatch(r'sixnine-(platform|dispatch):[0-9a-f]{40}', tag) for tag in tags)
        dangling = not tags and not digests
        if image_id not in protected_images and (owned or dangling):
            image_candidates.append(image_id)
    return {'version': 1, 'state': 'retention_plan', 'kept_commits': sorted(kept), 'pointers': observed,
        'candidates': candidates, 'retained': retained, 'image_candidates': sorted(image_candidates),
        'protected_images': sorted(value for value in protected_images if value),
        'reclaimable_file_bytes': sum(item['bytes'] for item in candidates)}


def delete_tree(root, snapshot):
    """Linux fd-relative removal; parent/path swaps never redirect deletion."""
    release.require(os.name == 'posix' and os.O_NOFOLLOW and os.O_DIRECTORY, 'retention_posix_required')
    relative = snapshot['relative']
    release.require(relative and all(part not in {'', '.', '..'} for part in relative.split('/')),
                    'retention_delete_path_invalid')
    safe_ancestors(root, root / relative)
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(fd)
        for part in relative.split('/')[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); descriptors.append(fd)
        parent_fd, leaf = fd, relative.split('/')[-1]
        target = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        descriptors.append(target)
        entries = snapshot['entries']
        release.require(identity(os.fstat(target)) == tuple(entries['']['identity']), 'retention_delete_tree_changed')
        def remove(directory_fd, name_prefix):
            expected_names = {name[len(name_prefix):].split('/')[0] for name in entries
                if name.startswith(name_prefix) and name != name_prefix.rstrip('/')}
            release.require(set(os.listdir(directory_fd)) == expected_names, 'retention_delete_entries_changed')
            for name in sorted(expected_names):
                key = name_prefix + name
                recorded = entries[key]
                actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                release.require(identity(actual) == tuple(recorded['identity']), 'retention_delete_entry_changed')
                if recorded['kind'] == 'directory':
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        release.require(identity(os.fstat(child)) == tuple(recorded['identity']), 'retention_delete_entry_changed')
                        remove(child, key + '/')
                        release.require((os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_dev,
                            os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_ino) ==
                            (actual.st_dev, actual.st_ino), 'retention_delete_parent_changed')
                        os.rmdir(name, dir_fd=directory_fd)
                    finally:
                        os.close(child)
                else:
                    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        release.require(identity(os.fstat(handle)) == tuple(recorded['identity']), 'retention_delete_entry_changed')
                        digest = hashlib.sha256()
                        while block := os.read(handle, 1024 * 1024):
                            digest.update(block)
                        release.require(digest.hexdigest() == recorded['sha256'] and
                            identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) == tuple(recorded['identity']),
                            'retention_delete_file_changed')
                        os.unlink(name, dir_fd=directory_fd)
                    finally:
                        os.close(handle)
        remove(target, '')
        actual = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        release.require((actual.st_dev, actual.st_ino) == tuple(entries['']['identity'])[:2], 'retention_delete_parent_changed')
        os.rmdir(leaf, dir_fd=parent_fd); os.fsync(parent_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def apply_locked(root, plan, *, command=release.command, prune_cache=True):
    containers, _ = docker_snapshot(command)
    kept, observed = closure(root, containers)
    release.require(sorted(kept) == plan['kept_commits'] and observed == plan['pointers'], 'retention_plan_stale')
    # Validate ALL trees before the first delete; an altered later artifact may
    # not leave a partially applied earlier plan that appeared fully accepted.
    for item in plan['candidates']:
        actual = tree_snapshot(root, root / item['relative'])
        release.require(all(actual[field] == item[field] for field in ('entries', 'bytes', 'relative')),
                        'retention_plan_tree_changed')
    removed = []
    for item in plan['candidates']:
        delete_tree(root, item)
        removed.append(item['relative'])
    for image_id in plan['image_candidates']:
        # Docker itself refuses images referenced by a new container; never
        # force removal. Re-read all containers before each precise deletion.
        current, _ = docker_snapshot(command)
        release.require(image_id not in {item.get('Image') for item in current}
            and image_id not in plan['protected_images'], 'retention_image_became_in_use')
        command(['image', 'rm', image_id], environment=ENV, timeout=120)
    if prune_cache:
        command(['builder', 'prune', '--force', '--filter', 'until=72h'], environment=ENV, timeout=120)
    return {'version': 1, 'state': 'retention_applied', 'removed': removed,
        'removed_file_bytes': plan['reclaimable_file_bytes'], 'removed_images': plan['image_candidates'],
        'retained': plan['retained'], 'kept_commits': plan['kept_commits'],
        'persistent_data_touched': False, 'cache_unused_older_than_hours': 72 if prune_cache else None}


def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', region_name='ap-southeast-1', endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        config=Config(connect_timeout=5, read_timeout=15, retries={'mode': 'standard', 'total_max_attempts': 2}))


def maintain_locked(root=release.ROOT, *, apply=False, api=None, command=release.command):
    root = Path(root)
    result = plan_locked(root, api=api or s3_client(), command=command)
    return apply_locked(root, result, command=command) if apply else result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='apply verified plan under existing release.lock')
    args = parser.parse_args(argv)
    try:
        release.require(os.name == 'posix' and os.geteuid() == 0, 'retention_root_host_required')
        safe_directory(release.ROOT)
        import fcntl
        if (release.ROOT / 'release.lock').exists() or (release.ROOT / 'release.lock').is_symlink():
            release.regular(release.ROOT / 'release.lock', root_owned=True, maximum=16384)
        descriptor = os.open(release.ROOT / 'release.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(maintain_locked(apply=args.apply), sort_keys=True))
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, release.ReleaseError) else 'retention_failed_details_suppressed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

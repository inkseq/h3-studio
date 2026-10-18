#!/usr/bin/python3
"""Bounded root-host release/cache retention; plan by default, never data GC.

Install this reviewed file independently beside release.py. All callers hold the
existing release.lock. S3 is read-only and only immutable publisher objects are
considered. Unknown/partial artifacts stay on disk for an explicit review.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import time
import zlib

import release

IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
       "DOCKER_CONFIG": "/opt/sixnine-release/docker-config"}
MAX_ENTRIES = 8192
MAX_OBJECTS = 4096
EXTRA_RELEASES = 3
NONCE = re.compile(r'[0-9a-f]{32}\Z')


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
    if state.get('failed_release') is not None:
        kept.add(commit_value(state['failed_release']))
    pins = read_json(root, 'release-retention-pins.json', observed, optional=True)
    if pins is not None:
        release.require(set(pins) == {'version', 'commits'} and type(pins['version']) is int
            and pins['version'] == 1 and type(pins['commits']) is list and len(pins['commits']) <= 128,
            'retention_explicit_pins_unknown')
        kept.update(commit_value(value) for value in pins['commits'])
    intent = read_json(root, 'environment-release-intent.json', observed, optional=True)
    if intent is not None:
        release.require(set(intent) == {'version', 'commit', 'candidate_sha256', 'state'}
            and type(intent['version']) is int and intent['version'] == 1
            and intent['state'] in {'in_progress', 'completed'}
            and type(intent['candidate_sha256']) is str and release.DIGEST.fullmatch(intent['candidate_sha256']),
            'retention_promotion_intent_unknown')
        commit = commit_value(intent['commit'])
        if intent['state'] == 'in_progress':
            kept.add(commit)
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
            prefix = root / 'publication'
            if ref and Path(ref).is_relative_to(prefix) and len(Path(ref).parts) > len(prefix.parts):
                commit = Path(ref).parts[len(prefix.parts)]
                if release.SHA.fullmatch(commit):
                    kept.add(commit)
    return kept, observed


def keep_policy(root, containers):
    """Pins plus three newest distinct installed versions; incoming is not a version."""
    kept, observed = closure(root, containers)
    installed, newest = {}, {}
    for relative in ('releases', 'migration/releases', 'frontend/releases'):
        parent = root / relative
        if not parent.exists() and not parent.is_symlink():
            continue
        safe_ancestors(root, parent)
        safe_directory(parent)
        for directory in sorted(parent.iterdir()):
            release.require(len(installed) < 1536, 'retention_release_count_limit')
            if not release.SHA.fullmatch(directory.name):
                continue  # Unknown entries are never selected for deletion.
            info = safe_directory(directory)
            installed[directory.relative_to(root).as_posix()] = identity(info)
            kind = {'releases': 'platform', 'migration/releases': 'migration', 'frontend/releases': 'frontend'}[relative]
            try:
                artifact = directory / 'artifact' if kind == 'migration' else directory
                manifest_name = {'platform': 'release-manifest.json', 'migration': 'migration-manifest.json',
                    'frontend': 'frontend-manifest.json'}[kind]
                manifest_path = artifact / manifest_name
                safe_ancestors(root, manifest_path)
                release.regular(manifest_path, maximum=262144)
                metadata = json.loads(manifest_path.read_text())
                release.require(metadata.get('commit') == directory.name, 'retention_directory_identity_invalid')
                approval(root, kind, directory.name, manifest_path)
                if kind == 'platform':
                    metadata = release.manifest(artifact, directory.name)
                expected = set(metadata['files']) | {manifest_name} if kind == 'platform' else {
                    'migration': {'artifact', 'sources', 'overlay.yaml'},
                    'frontend': {'index.html', manifest_name}}[kind]
                release.require({entry.name for entry in directory.iterdir()} == expected, 'retention_bundle_shape_unknown')
            except Exception:
                continue  # Unknown/partial objects remain, but never consume a newest-version slot.
            newest[directory.name] = max(newest.get(directory.name, 0), info.st_mtime_ns)
    extras = sorted((commit for commit in newest if commit not in kept),
        key=lambda commit: (newest[commit], commit), reverse=True)[:EXTRA_RELEASES]
    observed['installed_versions'] = installed
    return kept | set(extras), observed, extras


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


def bundle(root, directory, kind, api, *, installed=False, image_cache=None, publication_commit=None):
    """Return validated local tree + exact immutable publisher restore proof."""
    manifest_name = {'platform': 'release-manifest.json', 'migration': 'migration-manifest.json',
                     'frontend': 'frontend-manifest.json'}[kind]
    artifact = directory / 'artifact' if kind == 'migration' and installed else directory
    safe_ancestors(root, artifact / manifest_name)
    release.regular(artifact / manifest_name, maximum=262144)
    raw = json.loads((artifact / manifest_name).read_text())
    commit = commit_value(raw.get('commit'))
    if publication_commit is not None:
        pieces = directory.relative_to(root).parts
        release.require(not installed and kind in {'platform','migration'} and len(pieces) == 4
            and pieces[0] == 'publication' and pieces[1] == commit == publication_commit
            and NONCE.fullmatch(pieces[2]) and pieces[3] == kind+'-release',
            'retention_publication_namespace_invalid')
    else:
        release.require(directory.name == commit or directory.name.startswith(('.download-', '.release-',
            '.migration-download-', '.migration-release-', '.frontend-download-')), 'retention_directory_identity_invalid')
    approval(root, kind, commit, artifact / manifest_name)
    if kind == 'platform':
        value = release.manifest(artifact, commit)
        files = value['files']
        release.require({p.name for p in artifact.iterdir()} == set(value['files']) | {manifest_name},
                        'retention_platform_extra_files')
    elif kind == 'migration':
        helper = release.migration_helper()
        value = helper.manifest(artifact, commit)
        files = value['files']
        if installed:
            release.require({p.name for p in directory.iterdir()} == {'artifact', 'sources', 'overlay.yaml'},
                            'retention_migration_extra_files')
            helper.verify_sources(directory / 'sources', value['source_index_sha256'],
                source_commits=value.get('native_source_commits'))
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


@contextmanager
def publication_lock(root, nonce):
    """Original publisher lock; never create it or take over a live publisher."""
    import fcntl
    release.require(type(nonce) is str and NONCE.fullmatch(nonce), 'retention_publication_nonce_invalid')
    path = root / 'publication' / (nonce+'.lock')
    safe_ancestors(root,path)
    release.regular(path,root_owned=True,maximum=16384)
    before = identity(path.lstat())
    release.require(not before[2] & 0o077,'retention_publication_lock_unprotected')
    descriptor = os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    try:
        release.require(identity(os.fstat(descriptor)) == before, 'retention_publication_lock_changed')
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield before
        release.require(identity(os.fstat(descriptor)) == before and identity(path.lstat()) == before,
            'retention_publication_lock_changed')
    finally:
        os.close(descriptor)


def publication_context(root, commit, nonce, observed):
    """Validate only the original completed publisher's protected metadata.

    These records remain on disk. They grant no new publish/deploy authority;
    independent manifest approval and immutable S3 proof are still required.
    """
    value = read_json(root,'publication/'+nonce+'.json',observed)
    fields = {'binding','created_at','downloads','publication_started_at','publisher_exit_code',
        'status','verified_at','verified_object_count'}
    binding = value.get('binding')
    release.require(set(value) == fields and type(binding) is dict and set(binding) == {
        'version','authority_sha256','commit','policy_name','policy_sha256','host_id','inventory_sha256'}
        and type(binding['version']) is int and binding['version'] == 1 and binding['commit'] == commit
        and all(type(binding[name]) is str and release.DIGEST.fullmatch(binding[name])
            for name in ('authority_sha256','policy_sha256','inventory_sha256'))
        and type(binding['policy_name']) is str and binding['policy_name'].endswith('-'+nonce)
        and type(binding['host_id']) is str and re.fullmatch(r'i-[0-9a-f]{8,17}',binding['host_id'])
        and value['status'] == 'immutable_objects_verified_pending_independent_host_approval'
        and type(value['publisher_exit_code']) is int and value['publisher_exit_code'] == 0
        and type(value['verified_object_count']) is int and value['verified_object_count'] == 16
        and all(type(value[name]) in (int,float) and math.isfinite(value[name]) and value[name] > 0
            for name in ('created_at','publication_started_at','verified_at'))
        and value['created_at'] <= value['publication_started_at'] <= value['verified_at']
        and type(value['downloads']) is dict, 'retention_publication_not_terminal')
    relative = 'publication/publication-authority-'+commit[:12]+'.json'
    authority = read_json(root,relative,observed)
    release.require(observed[relative]['sha256'] == binding['authority_sha256']
        and type(authority.get('version')) is int and authority['version'] == 1
        and authority.get('purpose') == 'temporary-exact-ci-publication-only'
        and authority.get('commit') == commit and authority.get('host_id') == binding['host_id']
        and authority.get('policy_name') == binding['policy_name']
        and authority.get('policy_sha256') == binding['policy_sha256']
        and authority.get('inventory_sha256') == binding['inventory_sha256']
        and authority.get('bucket') == 'sixnine-platform-releases-829135631045-ap-southeast-1'
        and authority.get('region') == 'ap-southeast-1'
        and type(authority.get('staging')) is list and len(authority['staging']) == 3,
        'retention_publication_authority_unknown')
    names = {'platform-transport.tar.gz','migration-transport.tar.gz','publisher-source-transport.tar.gz'}
    release.require({row.get('filename') for row in authority['staging']} == names
        and set(value['downloads']) == names,'retention_publication_downloads_unknown')
    for row in authority['staging']:
        name = row['filename']; download = value['downloads'][name]
        expected = f'publication-staging/{commit}/{nonce}/{name}'
        release.require(row.get('key') == expected and type(row.get('sha256')) is str
            and release.DIGEST.fullmatch(row['sha256']) and type(download) is dict
            and set(download) == {'key','requested_at','verified_sha256'}
            and download['key'] == expected and download['verified_sha256'] == row['sha256']
            and type(download['requested_at']) in (int,float) and math.isfinite(download['requested_at'])
            and value['created_at'] <= download['requested_at'] <= value['publication_started_at'],
            'retention_publication_downloads_unknown')
    return authority


def publication_transport(root, leaf, payload, authority):
    """Prove a transport is only a wrapper for this complete restorable bundle.

    Every member already has independent approval and immutable S3 restore
    proof from bundle(). This verifies the original wrapper SHA and exact
    member bytes, not a speculative staging-prefix restore capability.
    """
    filename = payload['kind']+'-transport.tar.gz'
    path = leaf/'archives'/filename
    safe_ancestors(root,path); release.regular(path,root_owned=True,maximum=1024**3)
    row = next(value for value in authority['staging'] if value['filename'] == filename)
    expected = {name:value for name,value in payload['entries'].items() if name}
    release.require(all('/' not in name and item['kind']=='file' for name,item in expected.items())
        and type(row.get('size_bytes')) is int and 0 < row['size_bytes'] <= 1024**3
        and type(row.get('members')) is list and len(row['members']) == len(expected)
        and {value.get('path') for value in row['members']} == set(expected),
        'retention_publication_transport_unknown')
    for value in row['members']:
        member = expected[value['path']]
        release.require(type(value.get('size_bytes')) is int
            and value['size_bytes'] == member['identity'][4] and value.get('sha256') == member['sha256'],
            'retention_publication_transport_unknown')
    before = identity(path.lstat())
    release.require(before[4] == row['size_bytes'],'retention_publication_transport_changed')
    class HashedReader:
        def __init__(self, source):
            self.source,self.size,self.sha = source,0,hashlib.sha256()
        def read(self,size=1024*1024):
            block = self.source.read(min(size,1024*1024) if size>=0 else 1024*1024)
            self.size += len(block)
            release.require(self.size <= row['size_bytes'],'retention_publication_transport_changed')
            self.sha.update(block)
            return block
    descriptor = os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as source:
        release.require(identity(os.fstat(source.fileno())) == before,'retention_publication_transport_changed')
        hashed = HashedReader(source); seen = set()
        maximum = sum(512 + ((item['identity'][4]+511)//512)*512 for item in expected.values())+tarfile.RECORDSIZE
        class GzipReader:
            def __init__(self):
                self.decoder,self.pending,self.total,self.ended = zlib.decompressobj(31),b'',0,False
            def read(self,size):
                release.require(0 < size <= 1024*1024,'retention_publication_transport_unsafe')
                output = []
                while size and not self.ended:
                    block = self.pending or hashed.read(65536)
                    release.require(block,'retention_publication_transport_incomplete')
                    value = self.decoder.decompress(block,size)
                    self.pending = self.decoder.unconsumed_tail
                    self.total += len(value)
                    release.require(self.total <= maximum,'retention_publication_transport_unsafe')
                    output.append(value);size -= len(value)
                    if self.decoder.eof:
                        # Exactly one complete gzip stream, with valid CRC,
                        # no extra member, compressed trailer or raw suffix.
                        release.require(not self.decoder.unused_data and not hashed.read(1),
                            'retention_publication_transport_trailing')
                        self.ended = True
                return b''.join(output)
        decoded = GzipReader()
        while True:
            header = decoded.read(512)
            release.require(len(header) == 512,'retention_publication_transport_incomplete')
            if not any(header):
                second = decoded.read(512)
                release.require(len(second) == 512 and not any(second),'retention_publication_transport_trailing')
                while block := decoded.read(1024*1024):
                    release.require(not any(block),'retention_publication_transport_trailing')
                break
            entry = tarfile.TarInfo.frombuf(header,'utf-8','strict')
            # Inspect each raw header BEFORE tarfile can process PAX/GNU
            # extensions or allocate their untrusted declared payload.
            release.require(entry.type in {tarfile.REGTYPE,tarfile.AREGTYPE} and not entry.linkname
                and entry.name in expected and entry.name not in seen
                and entry.size == expected[entry.name]['identity'][4], 'retention_publication_transport_unsafe')
            seen.add(entry.name);digest = hashlib.sha256();remaining = entry.size
            while remaining:
                block = decoded.read(min(remaining,1024*1024))
                release.require(block,'retention_publication_transport_incomplete')
                remaining -= len(block);digest.update(block)
            padding = (-entry.size)%512
            if padding:
                block = decoded.read(padding)
                release.require(len(block) == padding and not any(block),'retention_publication_transport_trailing')
            release.require(digest.hexdigest() == expected[entry.name]['sha256'],
                'retention_publication_transport_changed')
        release.require(seen == set(expected) and hashed.size == row['size_bytes']
            and hashed.sha.hexdigest() == row['sha256'] and identity(os.fstat(source.fileno())) == before
            and identity(path.lstat()) == before, 'retention_publication_transport_changed')
    return {'relative':path.relative_to(root).as_posix(),'commit':payload['commit'],
        'kind':'publication_transport','image_ids':[],'bytes':row['size_bytes'],
        'entries':{'':{'identity':before,'kind':'file','sha256':row['sha256']}}}


def publication_payloads(root, kept, api, observed, image_cache):
    """Only complete, independently approved payload dirs; never parent sweeps."""
    parent = root/'publication'
    if not parent.exists() and not parent.is_symlink():
        return [],[],[]
    safe_ancestors(root,parent); safe_directory(parent)
    candidates, retained, contexts = [],[],[]
    for namespace in sorted(parent.iterdir()):
        if not release.SHA.fullmatch(namespace.name):
            continue  # SDK, authorities, recovery scripts and nonce records stay.
        release.require(len(contexts)+len(retained)<512,'retention_publication_count_limit')
        if namespace.name in kept:
            retained.append({'relative':namespace.relative_to(root).as_posix(),'reason':'rollback_or_in_use'})
            continue
        try:
            safe_directory(namespace)
        except Exception:
            retained.append({'relative':namespace.relative_to(root).as_posix(),'reason':'unproven_publication'})
            continue
        for index,leaf in enumerate(sorted(namespace.iterdir())):
            release.require(index < 128 and len(contexts)+len(retained)<512,'retention_publication_count_limit')
            if not NONCE.fullmatch(leaf.name):
                continue
            context = {'commit':namespace.name,'nonce':leaf.name}
            try:
                safe_directory(leaf)
                with publication_lock(root,leaf.name) as lock_identity:
                    facts = {}
                    authority = publication_context(root,namespace.name,leaf.name,facts)
                    facts['publication/'+leaf.name+'.lock'] = {'identity':lock_identity}
                    selected = []
                    for kind in ('platform','migration'):
                        directory = leaf/(kind+'-release')
                        try:
                            selected.append(bundle(root,directory,kind,api,image_cache=image_cache,
                                publication_commit=namespace.name))
                        except Exception:
                            retained.append({'relative':directory.relative_to(root).as_posix(),
                                'reason':'unproven_or_partial_artifact'})
                    if selected:
                        for payload in list(selected):
                            try:
                                selected.append(publication_transport(root,leaf,payload,authority))
                            except Exception:
                                retained.append({'relative':(leaf/'archives'/(payload['kind']+'-transport.tar.gz')).relative_to(root).as_posix(),
                                    'reason':'unproven_publication_transport'})
                        for item in selected:
                            item['publication_nonce'] = leaf.name
                # A failed lock/path recheck must not leak any candidate from
                # this context into an otherwise accepted plan.
                if selected:
                    candidates.extend(selected); contexts.append(context)
                    observed.update(facts)
            except Exception:
                retained.append({'relative':leaf.relative_to(root).as_posix(),'reason':'unproven_publication'})
    return candidates,retained,contexts


def plan_locked(root=release.ROOT, *, api, command=release.command):
    """No mutation. Caller keeps release.lock until apply completes."""
    root = Path(root)
    containers, images = docker_snapshot(command)
    kept, observed, extras = keep_policy(root, containers)
    candidates, retained, image_cache = [], [], {}
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
    publication, publication_retained, contexts = publication_payloads(root,kept,api,observed,image_cache)
    candidates.extend(publication);retained.extend(publication_retained)
    protected_images = {item.get('Image') for item in containers}
    for commit in sorted(kept):
        directory = root / 'releases' / commit
        if directory.exists():
            safe_ancestors(root, directory); safe_directory(directory)
            value = release.manifest(directory, commit)
            release.approved_manifest(root, directory, commit)
            key = ('platform', commit, release.checksum(directory / 'release-manifest.json'))
            if key not in image_cache:
                image_cache[key] = release.validate_image_archive(directory / 'image.tar.gz', value)
            protected_images.update(image_cache[key])
            protected_images.add(value['image_id'])
        directory = root / 'migration/releases' / commit / 'artifact'
        if directory.exists():
            helper = release.migration_helper()
            value = helper.manifest(directory, commit); helper.approved(root, directory, commit)
            key = ('migration', commit, release.checksum(directory / 'migration-manifest.json'))
            if key not in image_cache:
                image_cache[key] = release.validate_image_archive(directory / 'dispatch-image.tar.gz',
                    {'commit': commit, **value['dispatch']})
            protected_images.update(image_cache[key])
            protected_images.add(value['dispatch']['image_id'])
    image_candidates = eligible_image_tags(images, candidates, protected_images)
    return {'version': 1, 'state': 'retention_plan', 'kept_commits': sorted(kept), 'pointers': observed,
        'newest_extra_commits': extras,
        'publication_contexts': contexts,
        'candidates': candidates, 'retained': retained, 'image_candidates': image_candidates,
        'protected_images': sorted(value for value in protected_images if value),
        'reclaimable_file_bytes': sum(item['bytes'] for item in candidates)}


def eligible_image_tags(images, candidates, protected_images):
    """Only exact managed tags bound to a proven old archive, never image IDs."""
    aliases = {}
    for candidate in candidates:
        repository = {'platform': 'sixnine-platform', 'migration': 'sixnine-dispatch'}.get(candidate['kind'])
        if repository is None:
            continue
        commit = commit_value(candidate['commit'])
        identities = set(candidate['image_ids'])
        release.require(all(type(value) is str and IMAGE.fullmatch(value) for value in identities),
                        'retention_image_identity_invalid')
        for image_id in identities:
            allowed = aliases.setdefault(image_id, {'tags': set(), 'digests': set()})
            allowed['tags'].add(repository + ':' + commit)
            allowed['digests'].add(repository + '@' + image_id)
    result = []
    for image in images:
        image_id = image.get('Id')
        release.require(type(image_id) is str and IMAGE.fullmatch(image_id), 'retention_image_identity_invalid')
        tags = image.get('RepoTags') or []
        digests = image.get('RepoDigests') or []
        release.require(type(tags) is list and all(type(tag) is str for tag in tags), 'retention_image_tags_invalid')
        allowed = aliases.get(image_id)
        owned = allowed and set(tags).issubset(allowed['tags']) and set(digests).issubset(allowed['digests'])
        if image_id not in protected_images and owned:
            result.extend({'tag': tag, 'image_id': image_id} for tag in sorted(set(tags)))
    return sorted(result, key=lambda value: value['tag'])


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
        if snapshot['entries']['']['kind'] == 'file':
            pieces = relative.split('/')
            release.require(len(pieces) == 5 and pieces[0]=='publication' and release.SHA.fullmatch(pieces[1])
                and NONCE.fullmatch(pieces[2]) and pieces[3]=='archives'
                and pieces[4] in {'platform-transport.tar.gz','migration-transport.tar.gz'},
                'retention_delete_file_scope_invalid')
            handle = os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
            try:
                entry = snapshot['entries']['']
                release.require(identity(os.fstat(handle)) == tuple(entry['identity']), 'retention_delete_entry_changed')
                digest = hashlib.sha256()
                while block := os.read(handle,1024*1024):
                    digest.update(block)
                release.require(digest.hexdigest() == entry['sha256'] and identity(os.stat(leaf,dir_fd=parent_fd,
                    follow_symlinks=False)) == tuple(entry['identity']), 'retention_delete_file_changed')
                os.unlink(leaf,dir_fd=parent_fd);os.fsync(parent_fd)
                return
            finally:
                os.close(handle)
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
    with ExitStack() as locks:
        publication_observed = {}
        for context in plan.get('publication_contexts',[]):
            commit,nonce = commit_value(context['commit']),context['nonce']
            lock_identity = locks.enter_context(publication_lock(root,nonce))
            publication_context(root,commit,nonce,publication_observed)
            publication_observed['publication/'+nonce+'.lock'] = {'identity':lock_identity}
        return _apply_locked(root,plan,command=command,prune_cache=prune_cache,
            publication_observed=publication_observed)


def _apply_locked(root, plan, *, command, prune_cache, publication_observed):
    containers, _ = docker_snapshot(command)
    kept, observed, _ = keep_policy(root, containers)
    observed.update(publication_observed)
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
    removed_tags = []
    for candidate in plan['image_candidates']:
        # Docker itself refuses images referenced by a new container; never
        # force removal. Re-read all containers before each precise deletion.
        image_id, tag = candidate['image_id'], candidate['tag']
        current, images = docker_snapshot(command)
        release.require(image_id not in {item.get('Image') for item in current}
            and image_id not in plan['protected_images'], 'retention_image_became_in_use')
        release.require(any(image.get('Id') == image_id and tag in (image.get('RepoTags') or [])
            for image in images), 'retention_image_tag_changed')
        command(['image', 'rm', tag], environment=ENV, timeout=120)
        removed_tags.append(candidate)
    dangling_pruned = False
    if prune_cache:
        # A previous rollback image may be dangling but not container-referenced.
        # Preserve it rather than allow a global dangling prune to remove it.
        _, images = docker_snapshot(command)
        protected_dangling = any(image.get('Id') in plan['protected_images']
            and not image.get('RepoTags') for image in images)
        if not protected_dangling:
            command(['image', 'prune', '--force'], environment=ENV, timeout=120)
            dangling_pruned = True
    return {'version': 1, 'state': 'retention_applied', 'removed': removed,
        'removed_artifacts': [{'relative': item['relative'], 'commit': item['commit'], 'bytes': item['bytes']}
            for item in plan['candidates']],
        'removed_file_bytes': plan['reclaimable_file_bytes'], 'removed_image_tags': removed_tags,
        'retained': plan['retained'], 'kept_commits': plan['kept_commits'],
        'persistent_data_touched': False, 'dangling_images_pruned': dangling_pruned,
        'build_cache_touched': False}


def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', region_name='ap-southeast-1', endpoint_url='https://s3.ap-southeast-1.amazonaws.com',
        config=Config(connect_timeout=5, read_timeout=15, retries={'mode': 'standard', 'total_max_attempts': 2}))


def maintain_locked(root=release.ROOT, *, apply=False, api=None, command=release.command):
    root = Path(root)
    result = plan_locked(root, api=api or s3_client(), command=command)
    if not apply:
        return result
    import frontend_release
    folder = root / 'retention-receipts'
    safe_directory(root)
    frontend_release.protected_directory(folder, create=True)
    receipt = folder / (str(time.time_ns()) + '.json')
    # Persist the exact approved plan before any mutation, including partial-failure evidence.
    payload = {'version': 1, 'state': 'retention_started', 'plan': compact_plan(result)}
    frontend_release.immutable_file(receipt, (json.dumps(payload, sort_keys=True) + '\n').encode())
    try:
        outcome = apply_locked(root, result, command=command)
    except Exception:
        # The pre-delete receipt remains. Never claim all planned bytes were freed.
        raise
    payload = {'version': 1, **outcome}
    frontend_release.atomic_file(receipt, (json.dumps(payload, sort_keys=True) + '\n').encode())
    return {**outcome, 'receipt': receipt.relative_to(root).as_posix()}


def compact_plan(value):
    """Receipts describe exact versions/bytes, not thousands of file inode entries."""
    return {**{key: item for key, item in value.items() if key not in {'candidates', 'pointers'}},
        'candidates': [{key: item[key] for key in ('relative', 'commit', 'kind', 'bytes')}
            for item in value['candidates']],
        'pointer_sha256': hashlib.sha256(json.dumps(value['pointers'], sort_keys=True).encode()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='apply verified plan under existing release.lock')
    parser.add_argument('--scheduled', action='store_true', help='require the exact independently enabled retention policy')
    args = parser.parse_args(argv)
    try:
        release.require(os.name == 'posix' and os.geteuid() == 0, 'retention_root_host_required')
        safe_directory(release.ROOT)
        if args.scheduled:
            policy = read_json(release.ROOT, 'retention-policy.json', {})
            release.require(args.apply and policy == {'version': 1, 'enabled': True}
                and type(policy.get('version')) is int and policy.get('enabled') is True,
                'retention_policy_requires_review')
        import fcntl
        if (release.ROOT / 'release.lock').exists() or (release.ROOT / 'release.lock').is_symlink():
            release.regular(release.ROOT / 'release.lock', root_owned=True, maximum=16384)
        descriptor = os.open(release.ROOT / 'release.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = maintain_locked(apply=args.apply)
            print(json.dumps(result if args.apply else compact_plan(result), sort_keys=True))
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, release.ReleaseError) else 'retention_failed_details_suppressed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

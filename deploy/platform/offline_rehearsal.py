"""Rehearse schema initialization on an offline disposable backup copy only.

The copy is NEVER a public staging database: no app server, controller, dispatch,
provider credentials, published port or network is attached. The original dump
is read-only and retained. Real staging starts from its own fresh database.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import stat
import re
import uuid

import deployment_target
import release


def commands(commit, postgres_image, nonce):
    release.require(release.SHA.fullmatch(commit) and len(nonce) == 32 and all(c in '0123456789abcdef' for c in nonce),
        'rehearsal_identity_invalid')
    release.require(isinstance(postgres_image, str) and re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}', postgres_image),
        'rehearsal_postgres_unpinned')
    name = 'inkseq-rehearsal-'+nonce
    data, socket = name+'-data', name+'-socket'
    return {
        'name': name, 'volumes': [data, socket],
        'database': ['run', '--pull', 'never', '--detach', '--name', name, '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER',
            '--cap-add', 'SETGID', '--cap-add', 'SETUID', '--security-opt', 'no-new-privileges:true',
            '--memory', '512m', '--pids-limit', '128', '--tmpfs', '/tmp:size=67108864',
            '--mount', 'type=volume,src='+data+',dst=/var/lib/postgresql/data',
            '--mount', 'type=volume,src='+socket+',dst=/var/run/postgresql',
            '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'POSTGRES_DB=rehearsal',
            postgres_image],
        'ready': ['exec', name, 'pg_isready', '-h', '127.0.0.1', '-p', '5432',
            '-U', 'postgres', '-d', 'rehearsal'],
        'restore': ['exec', '-i', name, 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl',
            '--host', '127.0.0.1', '--port', '5432', '--username', 'postgres', '--dbname', 'rehearsal'],
        'migrate': ['run', '--pull', 'never', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--memory', '1g', '--pids-limit', '256',
            '--tmpfs', '/data:size=67108864,uid=10001,gid=10001,mode=0700',
            '--tmpfs', '/tmp:size=67108864,mode=1777',
            '--mount', 'type=volume,src='+socket+',dst=/var/run/postgresql,readonly',
            '--env', 'SIXNINE_DATABASE_URL=postgresql+psycopg://postgres@/rehearsal?host=/var/run/postgresql',
            '--env', 'SIXNINE_DATA=/data', '--env', 'SIXNINE_GENERATION_ENABLED=0',
            '--env', 'SIXNINE_EXECUTION_BACKEND=disabled', '--env', 'SIXNINE_STORAGE_PROVIDER=local',
            'sixnine-platform:'+commit, 'python', '-m', 'studio_platform.manage', 'init-db'],
    }


def workflow_restore_command(plan, directory, commit):
    command = list(plan['migrate'])
    image_index = command.index('sixnine-platform:'+commit)
    command[image_index:image_index] = ['--user','0:0','--mount',
        'type=bind,src='+str(directory)+',dst=/backup,readonly']
    command[image_index+5:] = ['python','-m','studio_platform.workflow_backup',
        'restore-rehearsal','--directory','/backup']
    return command


def restore_copy(plan, source, environment):
    # The image entrypoint's temporary bootstrap server accepts Unix sockets
    # before database creation finishes. Only the final server listens on TCP.
    # Loopback remains inside the network-none disposable container.
    import time
    for _ in range(40):
        try:
            release.command(plan['ready'], environment=environment, timeout=10)
            break
        except release.ReleaseError:
            time.sleep(1)
    else:
        raise release.ReleaseError('rehearsal_database_not_ready')
    try:
        with source.open('rb') as dump:
            subprocess.run([release.DOCKER, '--host', 'unix:///var/run/docker.sock', *plan['restore']],
                stdin=dump, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, timeout=600, check=True)
    except subprocess.TimeoutExpired:
        raise release.ReleaseError('rehearsal_restore_timeout_retained') from None
    except (OSError, subprocess.SubprocessError):
        # Do not expose SQL, rows or client stderr. The original intent and
        # disposable resources remain for an explicit reviewed reconciliation.
        raise release.ReleaseError('rehearsal_restore_failed_retained') from None


def run(commit, *, root=release.ROOT):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_rehearsal_commit')
    release.require(deployment_target.host(root)['name'] == 'staging', 'rehearsal_requires_staging')
    directory = root/'releases'/commit
    release.approved_manifest(root, directory, commit)
    manifest = release.manifest(directory, commit)
    environment = release.deployment_environment(root/'site.env', commit)
    inspected = json.loads(release.command(['image', 'inspect', manifest['image']], environment=environment))[0]
    release.require(inspected['Id'] in release.validate_image_archive(directory/'image.tar.gz', manifest),
        'rehearsal_image_differs')
    source = root/'rehearsal'/'incoming'/'production.dump'
    for parent in (root/'rehearsal', source.parent):
        release.protected_directory(parent)
        release.require(stat.S_IMODE(parent.lstat().st_mode) == 0o700, 'rehearsal_private_directory_required')
    info = release.regular(source, root_owned=True, maximum=8*1024**3)
    release.require(stat.S_IMODE(info.st_mode) == 0o600, 'rehearsal_private_dump_required')
    original = release.checksum(source)
    plan = commands(commit, environment['SIXNINE_POSTGRES_IMAGE'], uuid.uuid4().hex)
    companions = [source.parent/'workflow-state.json', source.parent/'workflow-manifest.json']
    workflow = None
    if any(p.exists() or p.is_symlink() for p in companions):
        for path in companions:
            item = release.regular(path, root_owned=True, maximum=256*1024**2 if path.name == 'workflow-state.json' else 16*1024**2)
            release.require(stat.S_IMODE(item.st_mode) == 0o600, 'rehearsal_private_workflow_required')
        workflow = json.loads(companions[1].read_bytes())
        release.require(workflow.get('kind') == 'postgres-database-only'
            and workflow.get('version') == 1 and workflow.get('database_sha256') == original
            and workflow.get('consistent_business_workflow_snapshot') is True
            and workflow.get('workflow_state',{}).get('file') == 'workflow-state.json'
            and workflow['workflow_state'].get('sha256') == release.checksum(companions[0])
            and workflow['workflow_state'].get('size_bytes') == companions[0].stat().st_size,
            'rehearsal_workflow_companion_mismatch')
        workflow = {'state_sha256':release.checksum(companions[0]),'manifest_sha256':release.checksum(companions[1])}
        # Same isolated socket/database and exact image, with only protected
        # incoming evidence mounted read-only. No network/provider is attached.
        plan['restore_workflows'] = workflow_restore_command(plan, source.parent, commit)
    intent = root/'rehearsal'/'intent.json'
    release.require(not intent.exists() and not intent.is_symlink(), 'rehearsal_existing_intent_requires_review')
    with intent.open('x', encoding='utf-8') as output:
        json.dump({'version': 1, 'commit': commit, 'dump_sha256': original, 'name': plan['name'], 'volumes': plan['volumes'],
            **({'workflow_state':workflow} if workflow else {})}, output)
        output.flush()
        import os
        os.fsync(output.fileno())
    intent.chmod(0o600); release.sync_directory(intent.parent)
    # No retries after an uncertain Docker mutation; the intent retains exact
    # disposable names for operator recovery. Never inspect/delete other names.
    release.command(plan['database'], environment=environment)
    restore_copy(plan, source, environment)
    release.command(plan['migrate'], environment=environment, timeout=600)
    if workflow:
        release.command(plan['restore_workflows'], environment=environment, timeout=600)
        release.require(release.checksum(companions[0]) == workflow['state_sha256']
            and release.checksum(companions[1]) == workflow['manifest_sha256'], 'rehearsal_workflow_original_changed')
    release.require(release.checksum(source) == original, 'rehearsal_original_dump_changed')
    release.command(['rm', '--force', plan['name']], environment=environment)
    for volume in plan['volumes']:
        release.command(['volume', 'rm', volume], environment=environment)
    receipt = {'state': 'offline_migration_rehearsal_passed', 'commit': commit,
        'dump_sha256': original, 'copy_published': False, 'original_retained': True}
    if workflow:
        receipt.update(workflow_state=workflow,workflow_execution='held',workflow_original_ids=True)
    # Keep the intent even on success. Repeating a rehearsal is a deliberate new
    # operation with a reviewed preserved intent, never a silent cleanup/replay.
    return receipt


if __name__ == '__main__':
    import sys
    try:
        release.require(len(sys.argv) == 2, 'one_commit_required')
        print(json.dumps(run(sys.argv[1])))
    except Exception:
        raise SystemExit('Offline rehearsal incomplete; inspect the retained exact intent; no live database was used') from None

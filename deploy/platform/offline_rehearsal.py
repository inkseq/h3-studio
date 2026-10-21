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


PAYMENT_ROWS = """import hashlib,json
from sqlalchemy import create_engine,inspect
from studio_platform.settings import Settings
from studio_platform.credits import credit_policy
s=Settings.from_environment(); e=create_engine(s.database_url,hide_parameters=True)
out={}
with e.connect() as c, c.begin():
 c.exec_driver_sql('SET TRANSACTION READ ONLY')
 c.exec_driver_sql(\"SET LOCAL statement_timeout = '30s'\")
 charging=credit_policy(c,s.tenant_id)['enabled']
 for name in sorted(set(inspect(c).get_table_names(schema='public'))-{'alembic_version'}):
  quoted=c.dialect.identifier_preparer.quote(name)
  digest=hashlib.sha256(); count=0
  rows=c.execution_options(stream_results=True).exec_driver_sql(
   'SELECT to_jsonb(t)::text FROM public.'+quoted+' AS t ORDER BY 1')
  for row in rows:
   raw=row[0].encode(); digest.update(len(raw).to_bytes(8,'big')); digest.update(raw); count+=1
  rows.close(); out[name]={'rows':count,'sha256':digest.hexdigest()}
print(json.dumps({'tables':out,'charging_enabled':charging,'tenant_id':s.tenant_id},sort_keys=True))
e.dispose()
"""


def payment_command(plan, commit, image_id, *arguments, snapshot=False):
    """The same network-none copy and socket, with immutable image identity."""
    release.require(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id or ''), 'rehearsal_payment_image_unpinned')
    prefix = plan['migrate'][:plan['migrate'].index('sixnine-platform:'+commit)]
    tail = ['python', '-c', PAYMENT_ROWS] if snapshot else [
        'python', '-m', 'studio_platform.schema_baseline', *arguments]
    return [*prefix, '--env', 'INKSEQ_STRIPE_MODE=off', image_id, *tail]


def payment_copy(plan, commit, image_id, readers, environment):
    """Only exact 005→006 adoption on the already restored disposable copy."""
    def check(image):
        value = json.loads(release.command(payment_command(plan, commit, image, 'check'), environment=environment, timeout=120))
        release.require(value.get('matches') is True and value.get('recorded_revision') in
            {'20261020_0005', '20261021_0006'} and release.DIGEST.fullmatch(value.get('schema_fingerprint', '')),
            'rehearsal_payment_schema_unrecognized')
        return value
    before = check(image_id)
    release.require(before['recorded_revision'] == '20261020_0005', 'rehearsal_payment_requires_original_005_copy')
    rows = json.loads(release.command(payment_command(plan, commit, image_id, snapshot=True), environment=environment, timeout=120))
    release.require(rows.get('charging_enabled') is False, 'rehearsal_payment_charging_not_off')
    release.command(payment_command(plan, commit, image_id, 'adopt-payment-live',
        '--expected-fingerprint', before['schema_fingerprint']), environment=environment, timeout=120)
    after = check(image_id)
    release.require(after['recorded_revision'] == '20261021_0006', 'rehearsal_payment_adoption_failed')
    repeated = json.loads(release.command(payment_command(plan, commit, image_id, 'adopt-payment-live',
        '--expected-fingerprint', after['schema_fingerprint']), environment=environment, timeout=120))
    release.require(repeated.get('state') == 'already_adopted', 'rehearsal_payment_repeat_not_noop')
    for reader in readers.values():
        observed = check(reader)
        release.require(observed['recorded_revision'] == after['recorded_revision']
            and observed['schema_fingerprint'] == after['schema_fingerprint'], 'rehearsal_payment_reader_mismatch')
    final = json.loads(release.command(payment_command(plan, commit, image_id, snapshot=True), environment=environment, timeout=120))
    release.require(final == rows, 'rehearsal_payment_original_rows_changed')
    return {'before_revision': before['recorded_revision'], 'after_revision': after['recorded_revision'],
        'schema_fingerprint': after['schema_fingerprint'], 'reader_images': readers, 'reader_check': 'schema_baseline',
        'business_rows': rows, 'repeat': 'already_adopted'}


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


def _run(commit, *, root, payment_readers=None):
    release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'invalid_rehearsal_commit')
    release.require(deployment_target.host(root)['name'] == 'staging', 'rehearsal_requires_staging')
    directory = root/'releases'/commit
    release.approved_manifest(root, directory, commit)
    manifest = release.manifest(directory, commit)
    environment = release.deployment_environment(root/'site.env', commit)
    inspected = json.loads(release.command(['image', 'inspect', manifest['image']], environment=environment))[0]
    release.require(inspected['Id'] in release.validate_image_archive(directory/'image.tar.gz', manifest),
        'rehearsal_image_differs')
    readers = {}
    for reader in payment_readers or ():
        base = root/'releases'/reader
        release.approved_manifest(root, base, reader)
        expected = release.manifest(base, reader)
        observed = json.loads(release.command(['image', 'inspect', expected['image']], environment=environment))[0]
        release.require(observed['Id'] in release.validate_image_archive(base/'image.tar.gz', expected), 'rehearsal_reader_image_differs')
        readers[reader] = observed['Id']
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
    if not payment_readers and any(p.exists() or p.is_symlink() for p in companions):
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
    intent = root/'rehearsal'/('payment-live-'+commit+'.json' if payment_readers else 'intent.json')
    release.require(not intent.exists() and not intent.is_symlink(), 'rehearsal_existing_intent_requires_review')
    with intent.open('x', encoding='utf-8') as output:
        json.dump({'version': 1, 'commit': commit, 'dump_sha256': original, 'name': plan['name'], 'volumes': plan['volumes'],
            **({'payment_reader_images':readers} if payment_readers else {}),
            **({'workflow_state':workflow} if workflow else {})}, output)
        output.flush()
        import os
        os.fsync(output.fileno())
    intent.chmod(0o600); release.sync_directory(intent.parent)
    # No retries after an uncertain Docker mutation; the intent retains exact
    # disposable names for operator recovery. Never inspect/delete other names.
    release.command(plan['database'], environment=environment)
    restore_copy(plan, source, environment)
    payment = payment_copy(plan, commit, inspected['Id'], readers, environment) if payment_readers else None
    if not payment_readers:
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
    if payment is not None:
        receipt['payment_live'] = payment
    # Keep the intent even on success. Repeating a rehearsal is a deliberate new
    # operation with a reviewed preserved intent, never a silent cleanup/replay.
    return receipt


def run(commit, *, root=release.ROOT, payment_readers=None):
    if payment_readers is None:
        return _run(commit, root=root)
    release.require(type(payment_readers) in (tuple, list) and len(payment_readers) == 2
        and all(type(c) is str and release.SHA.fullmatch(c) for c in payment_readers) and len(set(payment_readers)) == 2,
        'rehearsal_two_exact_payment_readers_required')
    import fcntl
    release.regular(root/'release.lock', root_owned=True, maximum=16384)
    with (root/'release.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run(commit, root=root, payment_readers=payment_readers)


if __name__ == '__main__':
    import sys
    try:
        release.require(len(sys.argv) == 2 or len(sys.argv) == 5 and sys.argv[2] == '--payment-live-readers', 'rehearsal_arguments_invalid')
        print(json.dumps(run(sys.argv[1], payment_readers=sys.argv[3:] if len(sys.argv) == 5 else None)))
    except Exception:
        raise SystemExit('Offline rehearsal incomplete; inspect the retained exact intent; no live database was used') from None

#!/usr/bin/python3
"""First unpublished release accounts from host-only SM, never stdout or argv."""
import json
import sys
import time

import bootstrap
import release
import runtime_secrets_aws
import deployment_target

# Fixed code, not credentials. Input is a private process pipe; output is static.
ACCOUNT_CODE = '''
import json,sys
try:
 from sqlalchemy import select
 from studio_platform.settings import Settings
 from studio_platform.repository import Repository
 from studio_platform.auth import Auth,accounts,USERS
 value=json.loads(sys.stdin.buffer.read(4097))
 if not isinstance(value,dict) or not set(value).issubset(USERS): raise ValueError()
 settings=Settings.from_environment()
 if settings.auth_mode!='password' or settings.generation_enabled or settings.render_enabled: raise ValueError()
 repo=Repository(settings.database_url)
 try:
  auth=Auth(repo.engine,tenant=settings.tenant_id,mode=settings.auth_mode)
  with repo.engine.connect() as conn:
   present=set(conn.execute(select(accounts.c.username).where(accounts.c.tenant==settings.tenant_id)).scalars())
  if present.intersection(value): raise ValueError()
  for name,password in value.items(): auth.set_password(name,password)
 finally: repo.close()
 print('Missing formal accounts initialized')
except BaseException:
 print('Account initialization failed; details suppressed',file=sys.stderr)
 sys.exit(1)
'''


def bootstrap_locked(root, commit):
    state_file = root/'release-state.json'
    state = {}
    if state_file.exists():
        release.regular(state_file, root_owned=True, maximum=16384)
        state = json.loads(state_file.read_text())
    release.require(state.get('current') is None and state.get('pending') in (None, commit),
                    'bootstrap_only_for_first_unpublished_release')
    release.approved_manifest(root, root/'incoming'/commit, commit)
    directory = release.prepare_bundle(root, commit)
    release.approved_manifest(root, directory, commit)
    environment = release.deployment_environment(root/'site.env', commit)
    dependencies = release.pinned_dependencies(environment)
    release.require(state.get('dependencies') in (None, dependencies), 'dependency_change_requires_separate_maintenance')
    release.approved_configuration(directory, environment)
    state = {**state, 'current': None, 'pending': commit, 'dependencies': dependencies,
             'status': 'prepared_needs_accounts', 'updated_at': time.time()}
    release.write_state(root, state)
    release.load_approved_image(root, directory, commit, environment)
    release.compose(directory, environment, 'up', '-d', 'db')
    release.compose(directory, environment, 'run', '--rm', 'db-init')
    release.compose(directory, environment, 'run', '--rm', '--no-deps', '-T', 'app',
                    'python', '-m', 'studio_platform.manage', 'init-db')
    status = bootstrap.management_status(directory, environment)
    missing = {'superdan', 'supervan'} - {row['username'] for row in status['accounts']}
    if missing:
        target = deployment_target.host(root)['name']
        credentials = runtime_secrets_aws.value(runtime_secrets_aws.client(),
            deployment_target.profile(target)['secret_root']+'/bootstrap-accounts', target=target)
        payload = json.dumps({name: credentials[name] for name in sorted(missing)}).encode()
        try:
            release.command(['compose', '--project-directory', str(directory), '-f', str(directory/'compose.yaml'),
                'run', '--rm', '--no-deps', '-T', 'app', 'python', '-c', ACCOUNT_CODE],
                environment=environment, input_data=payload, timeout=180)
        finally:
            del credentials, payload
    release.require(bootstrap.management_status(directory, environment).get('auth_ready') is True,
                    'accounts_not_ready_application_stays_unpublished')
    release.write_state(root, {**state, 'status': 'accounts_ready_waiting_release', 'updated_at': time.time()})


def main():
    try:
        import fcntl
        release.require(len(sys.argv) == 2 and bool(release.SHA.fullmatch(sys.argv[1])), 'one_commit_argument_required')
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            bootstrap_locked(release.ROOT, sys.argv[1])
        print('Accounts ready; application and proxy remain unpublished')
        return 0
    except Exception:
        print('Account bootstrap incomplete; details suppressed; retry the same approved commit', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

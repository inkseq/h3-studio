#!/usr/bin/python3
"""Time-limited public API acceptance using an already rented, qualified GPU.

Operator-only overlay. Does not rent, stop or renew any cloud instance, handle
supplier API credentials, approve images, or change the CPU release controller.
The separately authorized lifecycle controller remains responsible for billing.

Routine CD is blocked by the root-owned active.json marker until restore-cpu
has independently confirmed ledger drain, upstream idle, and CPU-worker exit.
Do not remove this marker or use Compose stop/down/up to bypass reconciliation.
The GPU can remain billable after CPU-only restoration; only its original
lifecycle controller may verify and perform cloud destruction.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import math
import re
import stat
import sys
import time

import release
from check_config import validate as validate_base

ROOT = Path('/srv/sixnine/gpu-acceptance')
SERVICE = 'gpu-worker'
POLICY_SOURCE = ROOT/'operator'/'execution-policy.json'
POLICY_TARGET = '/run/acceptance/execution-policy.json'
GPU_HEALTH = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='comfy-worker'"


def bind(source, target, readonly=False):
    value = {'type': 'bind', 'source': source.as_posix() if isinstance(source, Path) else source, 'target': target,
             'bind': {'create_host_path': False}}
    if readonly:
        value['read_only'] = True
    return value


def worker(image):
    return {'image': image, 'pull_policy': 'never', 'user': '10001:10001',
        'init': True, 'restart': 'no', 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'], 'pids_limit': 128,
        'mem_limit': 512*1024**2, 'cpus': .5, 'stop_grace_period': '4m',
        'environment': {'SIXNINE_DATA': '/data',
            'SIXNINE_DATABASE_URL_FILE': '/run/secrets/app_database_url',
            'PUBLIC_ORIGIN': 'https://inkseq.com',
            'SIXNINE_AUTH_MODE': 'password', 'SIXNINE_GENERATION_ENABLED': '1',
            'SIXNINE_RENDER_ENABLED': '0', 'SIXNINE_EXECUTION_BACKEND': 'comfy-worker',
            'SIXNINE_CLOUD_CREATION_ENABLED': '0',
            'SIXNINE_STORAGE_PROVIDER': 'local', 'SIXNINE_EXECUTION_POLICY_FILE': POLICY_TARGET},
        'command': ['python', '-m', 'studio_platform.production_worker',
                    '--config', '/worker-config/worker.json', '--enabled'],
        'secrets': [{'source': 'app_database_url', 'target': '/run/secrets/app_database_url'}],
        'volumes': [bind('/srv/sixnine/platform-data', '/data'),
            bind(ROOT/'worker', '/worker'), bind(ROOT/'tmp', '/tmp'),
            bind(ROOT/'operator'/'worker.json', '/worker-config/worker.json', True),
            bind(ROOT/'identity', '/worker-identity', True),
            bind(POLICY_SOURCE, POLICY_TARGET, True)],
        'networks': {'database': {}, 'edge': {}},
        'depends_on': {'db': {'condition': 'service_healthy', 'required': True}},
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}


def overlay(image):
    return {'services': {'app': {'environment': {
            'SIXNINE_GENERATION_ENABLED': '1', 'SIXNINE_EXECUTION_BACKEND': 'comfy-worker',
            'SIXNINE_EXECUTION_POLICY_FILE': POLICY_TARGET},
        'volumes': [bind(POLICY_SOURCE, POLICY_TARGET, True)],
        'healthcheck': {'test': ['CMD', 'python', '-c', GPU_HEALTH]}},
        SERVICE: worker(image)}}


def validate(config, *, deployment_directory, compose_version):
    """Remove only the exact reviewed delta, then apply unchanged CPU policy."""
    config = copy.deepcopy(config)
    # CI's trusted Compose 2.38.2 serializes explicit false as bind:{}.
    # Match the unchanged base validator's exact-version exception. Newer
    # Compose can interpret omission as TRUE, so never generalize this rule.
    if compose_version in ('2.38.2', 'v2.38.2'):
        for name in (SERVICE, 'app'):
            service = config.get('services', {}).get(name, {})
            for mount in service.get('volumes', []):
                if isinstance(mount, dict) and mount.get('type') == 'bind' and mount.get('bind') == {}:
                    mount['bind'] = {'create_host_path': False}
    service = config.get('services', {}).pop(SERVICE, None)
    # Compose emits byte limits as strings and Go-normalized duration text.
    # Accept only exact equivalent values, never omit resource checks.
    if isinstance(service, dict):
        if service.get('mem_limit') == str(512*1024**2):
            service['mem_limit'] = 512*1024**2
        if service.get('stop_grace_period') in ('4m0s', '240s'):
            service['stop_grace_period'] = '4m'
        if service.get('entrypoint', False) is None:
            del service['entrypoint']
    release.require(service == worker(config['services']['app']['image']), 'unexpected_gpu_worker_configuration')
    app = config['services']['app']
    env = app.get('environment', {})
    release.require(env.get('SIXNINE_GENERATION_ENABLED') == '1'
        and env.get('SIXNINE_EXECUTION_BACKEND') == 'comfy-worker'
        and env.pop('SIXNINE_EXECUTION_POLICY_FILE', None) == POLICY_TARGET, 'gpu_acceptance_settings_invalid')
    env['SIXNINE_GENERATION_ENABLED'], env['SIXNINE_EXECUTION_BACKEND'] = '0', 'disabled'
    mount = bind(POLICY_SOURCE, POLICY_TARGET, True)
    release.require(app.get('volumes', []).count(mount) == 1, 'gpu_policy_mount_invalid')
    app['volumes'].remove(mount)
    release.require(app['healthcheck']['test'] == ['CMD', 'python', '-c', GPU_HEALTH], 'gpu_acceptance_health_invalid')
    app['healthcheck']['test'][3] = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and not h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='disabled'"
    return validate_base(config, deployment_directory=deployment_directory, compose_version=compose_version)


def compose(directory, environment, *arguments, timeout=180):
    return release.command(['compose', '--project-directory', str(directory),
        '-f', str(directory/'compose.yaml'), '-f', str(ROOT/'overlay.json'), *arguments],
        environment=environment, timeout=timeout)


def require_new_acceptance(environment):
    # Compose up may recreate a running service and eventually force-kill it.
    # Even a repeated start after a lost response must reconcile explicitly;
    # this operator must never replace a previous acceptance worker implicitly.
    existing = release.command(['ps', '--all', '--quiet',
        '--filter', 'label=com.docker.compose.project=sixnine-platform',
        '--filter', 'label=com.docker.compose.service='+SERVICE], environment=environment, timeout=20)
    release.require(not existing.strip(), 'existing_acceptance_worker_requires_reconciliation')


def require_no_parallel_cycle():
    # Share the exact CD barrier before any new acceptance service mutation.
    # A finite controller and a single-handoff worker must never own the same
    # production queue concurrently, even when started in the reverse order.
    from deploy_approved import require_no_gpu_acceptance
    require_no_gpu_acceptance(release.ROOT)


def acceptance_marker(commit, active):
    """Root-owned cross-controller barrier; never infer completion from a TTL."""
    path = ROOT/'active.json'
    temporary = ROOT/'active.next'
    with temporary.open('w', encoding='utf-8') as output:
        json.dump({'version': 1, 'active': active, 'commit': commit, 'updated_at': time.time()}, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o644)
    temporary.replace(path)


def protected_inputs():
    for path in (ROOT, ROOT/'operator'):
        info = path.lstat()
        release.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                        'operator_config_not_protected')
    release.regular(POLICY_SOURCE, root_owned=True, maximum=65536)
    release.regular(ROOT/'operator'/'worker.json', root_owned=True, maximum=65536)


def worker_control(directory, environment, action):
    release.require(action in ('--request-drain', '--status'), 'invalid_worker_control_action')
    # A bounded control process can inspect even after the worker naturally
    # exits. These two CLI actions never register/claim/start a new worker; the
    # exact existing ledger and independently checked upstream remain required.
    raw = compose(directory, environment, 'run', '--rm', '--no-deps', '-T',
        '--entrypoint', 'python', SERVICE, '-m',
        'studio_platform.production_worker', '--config', '/worker-config/worker.json', action, timeout=45)
    release.require(len(raw) <= 16384, 'worker_status_exceeds_limit')
    value = json.loads(raw)
    release.require(isinstance(value, dict), 'worker_status_invalid')
    return value


def drained_status(value, expected, *, now=None):
    """Fresh explicit ledger + upstream confirmation, never absence of work."""
    now = time.time() if now is None else now
    observed = value.get('observed_at')
    return (value.get('handoff_id') == expected['handoff_id']
        and value.get('worker_ids') == [expected['worker_id']]
        and value.get('hard_deadline') == expected['hard_deadline']
        and type(observed) in (int, float) and math.isfinite(observed) and 0 <= now-observed <= 30
        and value.get('drained') is True and value.get('drain_requested') is True
        and value.get('upstream_idle_confirmed') is True and value.get('ledger_safe') is True
        and value.get('active_job_ids') == [] and isinstance(value.get('worker_ids'), list)
        and bool(value['worker_ids']) and all(isinstance(x, str) and x for x in value['worker_ids']))


def restore_cpu(directory, environment, expected, *, drain_seconds=240, exit_seconds=30,
                clock=time.monotonic, sleep=time.sleep):
    # Stop new API admission first, while the worker can finish existing work.
    # Failure later leaves the API CPU-only and the uncertain worker untouched.
    release.compose(directory, environment, 'up', '-d', '--no-deps', 'app')
    release.wait_ready(directory, environment)
    worker_control(directory, environment, '--request-drain')
    deadline = clock()+drain_seconds
    while True:
        status = worker_control(directory, environment, '--status')
        if drained_status(status, expected):
            break
        release.require(clock() < deadline, 'worker_not_safely_drained_no_stop_sent')
        sleep(3)
    raw = compose(directory, environment, 'ps', '--all', '--quiet', SERVICE, timeout=20).decode().strip()
    release.require(bool(re.fullmatch(r'[0-9a-f]{12,64}', raw)), 'worker_container_not_unique')
    values = json.loads(release.command(['inspect', raw], environment=environment, timeout=20))
    release.require(isinstance(values, list) and len(values) == 1, 'worker_container_not_unique')
    labels = values[0].get('Config', {}).get('Labels', {})
    release.require(labels.get('com.docker.compose.project') == 'sixnine-platform'
        and labels.get('com.docker.compose.service') == SERVICE, 'worker_container_identity_mismatch')
    # Docker stop eventually escalates to SIGKILL. Send only TERM after the
    # ledger drain barrier; if natural exit stalls, leave it for reconciliation.
    if values[0].get('State', {}).get('Running'):
        release.command(['kill', '--signal', 'TERM', raw], environment=environment, timeout=20)
    deadline = clock()+exit_seconds
    while True:
        values = json.loads(release.command(['inspect', raw], environment=environment, timeout=20))
        release.require(isinstance(values, list) and len(values) == 1, 'worker_container_not_unique')
        state = values[0].get('State', {})
        if state.get('Running') is False and state.get('Restarting') is False:
            release.require(state.get('Status') == 'exited' and state.get('ExitCode') in (0, 143)
                            and not state.get('OOMKilled'), 'worker_exit_requires_reconciliation')
            return
        release.require(clock() < deadline, 'worker_exit_unconfirmed_no_forced_kill')
        sleep(2)


def main(argv=None):
    try:
        import fcntl
        args = sys.argv[1:] if argv is None else argv
        release.require(args in (['start'], ['restore-cpu']), 'explicit_acceptance_action_required')
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state_file = release.ROOT/'release-state.json'
            release.regular(state_file, root_owned=True, maximum=16384)
            state = json.loads(state_file.read_text())
            commit = state.get('current')
            release.require(isinstance(commit, str) and release.SHA.fullmatch(commit)
                and state.get('status') == 'app_ready', 'published_healthy_release_required')
            directory = release.ROOT/'releases'/commit
            release.approved_manifest(release.ROOT, directory, commit)
            environment = release.deployment_environment(release.ROOT/'site.env', commit)
            release.approved_configuration(directory, environment)
            if args == ['restore-cpu']:
                protected_inputs()
                release.regular(ROOT/'overlay.json', root_owned=True, maximum=65536)
                version = release.command(['compose', 'version', '--short'], environment=environment).decode().strip()
                config = json.loads(compose(directory, environment, 'config', '--format', 'json'))
                validate(config, deployment_directory=directory, compose_version=version)
                expected = json.loads((ROOT/'operator'/'worker.json').read_text())
                restore_cpu(directory, environment, expected)
                acceptance_marker(commit, False)
                print('CPU-only application restored; GPU billing requires separate provider verification')
                return 0
            require_no_parallel_cycle()
            protected_inputs()
            require_new_acceptance(environment)
            policy = json.loads(POLICY_SOURCE.read_text())
            deadline = policy.get('qualification', {}).get('expires_at', 0)
            release.require(policy.get('enabled') is True and policy.get('qualification', {}).get('status') == 'accepted'
                            and type(deadline) in (int, float) and math.isfinite(deadline)
                            and time.time()+300 < deadline <= time.time()+4*3600,
                            'bounded_live_qualification_required')
            desired = json.dumps(overlay(environment['SIXNINE_IMAGE']), indent=2)
            temporary = ROOT/'overlay.next'
            temporary.write_text(desired)
            temporary.chmod(0o644)
            temporary.replace(ROOT/'overlay.json')
            version = release.command(['compose', 'version', '--short'], environment=environment).decode().strip()
            config = json.loads(compose(directory, environment, 'config', '--format', 'json'))
            validate(config, deployment_directory=directory, compose_version=version)
            # Persist before the first service mutation. Partial/lost results
            # block routine deployments until restore proves a safe drain.
            acceptance_marker(commit, True)
            compose(directory, environment, 'up', '-d', '--no-deps', SERVICE)
            compose(directory, environment, 'up', '-d', '--no-deps', 'app')
            release.wait_ready(directory, environment)
            print('Limited GPU acceptance enabled; no cloud instance was created')
        return 0
    except Exception:
        print('GPU acceptance operation failed; credentials and runtime output suppressed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

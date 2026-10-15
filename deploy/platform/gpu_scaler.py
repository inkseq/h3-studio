#!/usr/bin/python3
"""Root-only, bounded production GPU control. No action is the default.

The host reads one pinned encrypted runtime credential into a private stdin pipe.
Only the controller owns ordinary provider reconciliation. Restore closes API
admission and proves a natural exit. An exceptional frozen backlog transfer is
available only through the separately authenticated gpu_handoff receipt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

import release
from check_config import validate as validate_base
from gpu_acceptance import bind, GPU_HEALTH

ROOT = Path('/srv/sixnine/gpu-scaler')
SERVICE = 'gpu-controller'
ENTRY_MODULE = 'studio_platform.scaler_entry'
CONFIG_SOURCE = ROOT/'operator'/'scaler.json'
CONFIG_TARGET = '/control-config/scaler.json'
POLICY_SOURCE = ROOT/'operator'/'execution-policy.json'
POLICY_TARGET = '/control-config/execution-policy.json'
SOURCE = ROOT/'public-source'
RUNTIME_METADATA = release.ROOT/'lium-runtime-import.json'
CPU_HEALTH = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and not h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='disabled'"


def fingerprint(value):
    # Same canonical JSON contract as repository.request_hash; no database
    # dependencies or credentials are imported by the host config checker.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def engine_config(config):
    backend = config.get('execution_backend', 'comfy-worker')
    recipe = config.get('execution_recipe_id', '')
    release.require(isinstance(recipe, str) and (not recipe or backend == 'wangp-worker'
        and recipe == 'h3-base-ref2va-v1' and config.get('output_delivery') == 'native-frames-v1'),
        'scaler_recipe_identity_invalid')
    release.require(backend in ('comfy-worker', 'wangp-worker'), 'scaler_backend_invalid')
    if backend == 'wangp-worker':
        release.require(isinstance(config.get('engine_manifest_digest'), str)
            and re.fullmatch(r'[0-9a-f]{64}', config['engine_manifest_digest'])
            and config.get('qualification_profile') == 'queued-task-first-v1', 'scaler_engine_identity_invalid')
    else:
        release.require(not config.get('engine_manifest_digest'), 'scaler_engine_identity_invalid')
    return backend


def controller(image, backend='comfy-worker'):
    release.require(backend in ('comfy-worker', 'wangp-worker'), 'scaler_backend_invalid')
    value = {'image': image, 'pull_policy': 'never', 'user': '10001:10001',
        'init': True, 'restart': 'no', 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'], 'pids_limit': 192,
        'mem_limit': 1024**3, 'cpus': .75, 'stop_grace_period': '4m',
        'environment': {'SIXNINE_DATA': '/data',
            'SIXNINE_DATABASE_URL_FILE': '/run/secrets/app_database_url',
            'PUBLIC_ORIGIN': 'https://inkseq.com',
            'SIXNINE_AUTH_MODE': 'password', 'SIXNINE_GENERATION_ENABLED': '1',
            'SIXNINE_RENDER_ENABLED': '0', 'SIXNINE_EXECUTION_BACKEND': backend,
            'SIXNINE_CLOUD_CREATION_ENABLED': '0', 'SIXNINE_STORAGE_PROVIDER': 'local',
            'SIXNINE_EXECUTION_POLICY_FILE': POLICY_TARGET,
            'AWS_EC2_METADATA_DISABLED': 'true'},
        # Accidental compose up validates then exits. Only host start appends
        # both enable flags and supplies the bounded, single-use stdin envelope.
        'command': ['python', '-m', ENTRY_MODULE, '--config', CONFIG_TARGET],
        'secrets': [{'source': 'app_database_url', 'target': '/run/secrets/app_database_url'}],
        'volumes': [bind('/srv/sixnine/platform-data', '/data'),
            bind(ROOT/'control', '/control'), bind(ROOT/'tmp', '/tmp'),
            bind(CONFIG_SOURCE, CONFIG_TARGET, True), bind(POLICY_SOURCE, POLICY_TARGET, True),
            bind(ROOT/'identity'/'key', '/worker-identity/key', True),
            bind(SOURCE/'bootstrap_cloud.py', '/bootstrap-source/bootstrap_cloud.py', True),
            bind(SOURCE/'model_manifest.json', '/bootstrap-source/model_manifest.json', True)],
        'networks': {'database': {}, 'edge': {}},
        'depends_on': {'db': {'condition': 'service_healthy', 'required': True}},
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}
    if backend == 'wangp-worker':
        value['volumes'] = [m for m in value['volumes'] if not m['target'].startswith('/bootstrap-source/')]
        value['volumes'] += [bind(SOURCE/name, '/bootstrap-source/'+name, True) for name in
            ('wangp-bootstrap.py', 'wangp-manifest.json', 'wangp-runtime.json', 'wangp-package.tar.gz',
             'wangp-dependencies.tar.gz')]
    return value


def overlay(image, backend='comfy-worker'):
    health = GPU_HEALTH.replace("=='comfy-worker'", "=="+repr(backend))
    return {'services': {'app': {'environment': {
        'SIXNINE_GENERATION_ENABLED': '1', 'SIXNINE_EXECUTION_BACKEND': backend,
        'SIXNINE_EXECUTION_POLICY_FILE': POLICY_TARGET},
        'volumes': [bind(POLICY_SOURCE, POLICY_TARGET, True)],
        'healthcheck': {'test': ['CMD', 'python', '-c', health]}}, SERVICE: controller(image, backend)}}


def validate(config, *, deployment_directory, compose_version):
    config = copy.deepcopy(config)
    if compose_version in ('2.38.2', 'v2.38.2'):
        for name in (SERVICE, 'app'):
            for mount in config.get('services', {}).get(name, {}).get('volumes', []):
                if mount.get('type') == 'bind' and mount.get('bind') == {}:
                    mount['bind'] = {'create_host_path': False}
    actual = config.get('services', {}).pop(SERVICE, None)
    if isinstance(actual, dict):
        if actual.get('mem_limit') == str(1024**3):
            actual['mem_limit'] = 1024**3
        if actual.get('stop_grace_period') in ('4m0s', '240s'):
            actual['stop_grace_period'] = '4m'
        if actual.get('entrypoint', False) is None:
            del actual['entrypoint']
    app = config['services']['app']
    env = app.get('environment', {})
    backend = env.get('SIXNINE_EXECUTION_BACKEND')
    release.require(actual == controller(app['image'], backend), 'unexpected_gpu_controller_configuration')
    release.require(env.get('SIXNINE_GENERATION_ENABLED') == '1'
        and backend in ('comfy-worker', 'wangp-worker')
        and env.pop('SIXNINE_EXECUTION_POLICY_FILE', None) == POLICY_TARGET, 'scaler_admission_configuration_invalid')
    env['SIXNINE_GENERATION_ENABLED'], env['SIXNINE_EXECUTION_BACKEND'] = '0', 'disabled'
    mount = bind(POLICY_SOURCE, POLICY_TARGET, True)
    release.require(app.get('volumes', []).count(mount) == 1, 'scaler_policy_mount_invalid')
    app['volumes'].remove(mount)
    release.require(app['healthcheck']['test'] == ['CMD', 'python', '-c', GPU_HEALTH.replace("=='comfy-worker'", "=="+repr(backend))], 'scaler_health_invalid')
    app['healthcheck']['test'][3] = CPU_HEALTH
    return validate_base(config, deployment_directory=deployment_directory, compose_version=compose_version)


def compose_args(directory, *args):
    return ['compose', '--project-directory', str(directory), '-f', str(directory/'compose.yaml'),
        '-f', str(ROOT/'overlay.json'), *args]


def compose(directory, environment, *args, timeout=180):
    return release.command(compose_args(directory, *args), environment=environment, timeout=timeout)


def atomic(path, value):
    temporary = path.with_suffix('.next')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o644)
    temporary.replace(path)
    release.sync_directory(path.parent)


def read_json(path, maximum=65536):
    release.regular(path, root_owned=True, maximum=maximum)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            release.require(key not in value, 'duplicate_operator_field')
            value[key] = item
        return value
    value = json.loads(path.read_text(), object_pairs_hook=unique)
    release.require(isinstance(value, dict), 'operator_object_required')
    return value


def on_demand_config(config):
    """The host accepts one narrow on-demand mode; unknown modes fail closed."""
    mode = config.get('service_mode')
    release.require(mode in (None, 'on-demand'), 'scaler_service_mode_invalid')
    timeout = config.get('provider_preparation_timeout_s')
    release.require(timeout is None or type(timeout) is int and 120 <= timeout <= 7200,
        'invalid_provider_preparation_timeout')
    failures = config.get('provider_preparation_failure_limit', 2)
    release.require(type(failures) is int and 1 <= failures <= 5 and (timeout is not None or failures == 2),
        'invalid_provider_preparation_failure_limit')
    if config.get('service_policy') is not None:
        # Exact schema is checked again by the pinned application before launch.
        # The pure source module is installed with reviewed host helpers.
        from service_policy import validate_service_config
        from types import SimpleNamespace
        release.require(mode == 'on-demand', 'scaler_service_policy_mode_invalid')
        validate_service_config(SimpleNamespace(**{'authorization_extension_s': 0, **config}))
        return True
    extension = config.get('authorization_extension_s', 0)
    release.require(type(extension) is int and extension in (0, 18000)
        and (extension == 0 or mode == 'on-demand'
            and config.get('allowed_owners') == ['superdan', 'supervan']
            and config.get('qualification_profile') == 'queued-task-first-v1'),
        'scaler_authorization_extension_invalid')
    if mode is None:
        return False
    policy = config.get('scale_policy')
    release.require(isinstance(policy, dict)
        and type(config.get('max_cycles')) is int and 1 <= config['max_cycles'] <= 8
        and config.get('allowed_owners') == ['superdan', 'supervan']
        and type(policy.get('idle_before_drain_s')) is int and policy['idle_before_drain_s'] == 600
        and type(policy.get('max_instances')) is int and policy['max_instances'] == 1
        and type(policy.get('max_physical_gpus')) is int and policy['max_physical_gpus'] == 1
        and type(policy.get('new_instance_slots', 1)) is int and policy.get('new_instance_slots', 1) == 1
        and type(policy.get('new_instance_physical_gpus', 1)) is int and policy.get('new_instance_physical_gpus', 1) == 1,
        'scaler_on_demand_limits_invalid')
    created, deadline = config.get('created_at'), config.get('hard_deadline')
    release.require(type(created) in (int, float) and type(deadline) in (int, float)
        and math.isfinite(created) and math.isfinite(deadline) and 0 < deadline-created <= 24*3600+extension,
        'scaler_on_demand_authorization_window_invalid')
    return True


def protected_inputs(*, starting=False, now=None):
    for path in (ROOT, ROOT/'operator', ROOT/'identity', SOURCE):
        info = path.lstat()
        release.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                        'scaler_operator_directory_not_protected')
    for path in (ROOT/'control', ROOT/'tmp'):
        info = path.lstat()
        release.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 10001 and not info.st_mode & 0o077,
                        'scaler_runtime_directory_not_private')
    key = release.regular(ROOT/'identity'/'key')
    release.require(key.st_uid == 10001 and stat.S_IMODE(key.st_mode) == 0o400,
                    'scaler_ssh_identity_permissions_invalid')
    config, policy, metadata = read_json(CONFIG_SOURCE), read_json(POLICY_SOURCE), read_json(RUNTIME_METADATA)
    backend = engine_config(config)
    on_demand = on_demand_config(config)
    expected_paths = {'work_dir': '/control', 'data_dir': '/data', 'source_dir': '/bootstrap-source',
                     'ssh_key_file': '/worker-identity/key', 'known_hosts_file': '/control/known_hosts'}
    release.require(all(config.get(k) == v for k, v in expected_paths.items())
        and config.get('tenant') == 'sixnine' and config.get('owner') == 'superdan'
        and type(config.get('cycle_id')) is str and bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', config['cycle_id']))
        and config.get('secret_arn') == metadata.get('secret_arn')
        and config.get('secret_version_id') == metadata.get('version_id')
        and metadata.get('service') == 'lium' and metadata.get('profile') == 'lium--rig-root',
        'scaler_identity_or_runtime_metadata_mismatch')
    release.require(config.get('execution_policy_sha256') == fingerprint(policy), 'scaler_policy_hash_mismatch')
    release.require(policy.get('backend') == backend
        and policy.get('engine_manifest_digest', '') == config.get('engine_manifest_digest', ''), 'scaler_policy_engine_mismatch')
    sources = config.get('source_sha256', {})
    expected_sources = ({'bootstrap_cloud.py', 'model_manifest.json'} if backend == 'comfy-worker' else
        {'wangp-bootstrap.py', 'wangp-manifest.json', 'wangp-runtime.json', 'wangp-package.tar.gz'})
    release.require(set(sources) == expected_sources, 'scaler_source_set_invalid')
    for name, digest in sources.items():
        source = SOURCE/name
        release.regular(source, root_owned=True, maximum=16*1024**2 if name.endswith('.gz') else 524288)
        release.require(release.checksum(source) == digest, 'scaler_source_hash_mismatch')
    if backend == 'wangp-worker':
        runtime = read_json(SOURCE/'wangp-runtime.json', 524288)
        release.require(runtime.get('dependency_artifact_path') == '/root/sixnine-cache/wangp-dependencies.tar.gz'
            and not runtime.get('dependency_artifact_url') and not runtime.get('prepared_root'),
            'scaler_wangp_archive_source_required')
        artifact = SOURCE/'wangp-dependencies.tar.gz'
        release.regular(artifact, root_owned=True, maximum=32*1024**3)
        release.require(release.checksum(artifact) == runtime.get('dependency_artifact_sha256'),
            'scaler_dependency_hash_mismatch')
    if starting:
        now = time.time() if now is None else now
        deadline = config.get('hard_deadline')
        release.require(config.get('enabled') is True and type(deadline) in (int, float)
            and math.isfinite(deadline) and now+300 < deadline
            and (config.get('service_policy') is not None or deadline <= now+(24 if on_demand else 4)*3600
                + config.get('authorization_extension_s', 0)),
            'scaler_explicit_finite_authorization_required')
        # Never resume an uncertain previous lifecycle via a fresh start.
        entries = {path.name for path in (ROOT/'control').iterdir()}
        release.require(entries <= {'known_hosts'}, 'scaler_previous_cycle_requires_reconciliation')
        if entries:
            hosts = release.regular(ROOT/'control'/'known_hosts', maximum=262144)
            release.require(hosts.st_uid == 10001 and stat.S_IMODE(hosts.st_mode) == 0o600,
                            'scaler_known_hosts_permissions_invalid')
    return config


def container_name(config):
    return 'sixnine-finite-'+hashlib.sha256(config['cycle_id'].encode()).hexdigest()[:20]


def marker(commit, config, active):
    value = {'version': 1, 'active': active, 'commit': commit,
        'cycle_id': config['cycle_id'], 'config_hash': fingerprint(config),
        'container_name': container_name(config), 'updated_at': time.time()}
    if not active:
        previous = verify_marker(commit, config, allow_inactive=True)
        if previous['version'] == 2:
            value = {**previous, 'active': False, 'admission': 'closed', 'updated_at': time.time()}
    else:
        directory = release.ROOT/'releases'/commit
        release.approved_manifest(release.ROOT, directory, commit)
        expected = release.manifest(directory, commit)
        if 'contracts' in expected:
            contracts = release.validate_contracts(expected['contracts'])
            environment = release.deployment_environment(release.ROOT/'site.env', commit)
            identities = release.validate_image_archive(directory/'image.tar.gz', expected)
            images = json.loads(release.command(['image', 'inspect', environment['SIXNINE_IMAGE']], environment=environment))
            release.require(isinstance(images, list) and len(images) == 1 and images[0].get('Id') in identities,
                'scaler_execution_image_unapproved')
            value.update(version=2, image_id=images[0]['Id'], contracts=contracts, admission='closed')
    atomic(ROOT/'active.json', value)


def verify_marker(commit, config, *, allow_inactive=False):
    value = read_json(ROOT/'active.json', 16384)
    release.require(type(value.get('version')) is int and value['version'] in (1, 2) and type(value.get('active')) is bool
        and (value['active'] or allow_inactive)
        and value.get('commit') == commit and value.get('cycle_id') == config['cycle_id']
        and value.get('config_hash') == fingerprint(config)
        and value.get('container_name') == container_name(config), 'scaler_active_identity_mismatch')
    if value['version'] == 2:
        release.validate_contracts(value.get('contracts'))
        release.require(set(value) == {'version', 'active', 'commit', 'cycle_id', 'config_hash',
            'container_name', 'updated_at', 'image_id', 'contracts', 'admission'}
            and isinstance(value.get('image_id'), str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id'])
            and value.get('admission') in ('open', 'closed'), 'scaler_execution_pin_invalid')
    return value


def admission_marker(commit, config, admission):
    release.require(admission in ('open', 'closed'), 'scaler_admission_state_invalid')
    value = verify_marker(commit, config, allow_inactive=admission == 'closed')
    if value['version'] == 2:
        atomic(ROOT/'active.json', {**value, 'admission': admission, 'updated_at': time.time()})


def require_new_controller(environment):
    from deploy_approved import require_no_gpu_acceptance
    require_no_gpu_acceptance(release.ROOT)
    previous = release.command(['ps', '--all', '--quiet', '--filter',
        'label=com.docker.compose.project=sixnine-platform', '--filter',
        'label=com.docker.compose.service='+SERVICE], environment=environment, timeout=20)
    release.require(not previous.strip(), 'previous_controller_requires_explicit_reconciliation')


def controller_control(directory, environment, action):
    release.require(action in ('--status', '--request-drain', '--validate'), 'controller_control_action_invalid')
    extra = [] if action == '--validate' else [action]
    raw = compose(directory, environment, 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'python',
        SERVICE, '-m', ENTRY_MODULE, '--config', CONFIG_TARGET, *extra, timeout=45)
    release.require(len(raw) <= 32768, 'controller_status_too_large')
    value = json.loads(raw)
    release.require(isinstance(value, dict), 'controller_status_invalid')
    return value


def fresh_drained(value, config, *, now=None):
    now = time.time() if now is None else now
    observed, instances = value.get('observed_at'), value.get('instances')
    return (value.get('cycle_id') == config['cycle_id'] and value.get('config_hash') == fingerprint(config)
        and value.get('hard_deadline') == config['hard_deadline']
        and type(observed) in (int, float) and math.isfinite(observed) and 0 <= now-observed <= 30
        and value.get('snapshot_only') is False and value.get('controller_exit_required') is True
        and value.get('ledger_safe') is True and value.get('all_destroyed') is True
        and value.get('drained') is True and value.get('active_job_ids') == []
        and value.get('active_jobs_truncated') is False
        and isinstance(instances, list) and all(isinstance(row, dict) and row.get('state') == 'destroyed'
            and isinstance(row.get('id'), str) and row['id'] for row in instances)
        and type(value.get('billing_pending')) is int and value['billing_pending'] >= 0)


def inspect_controller(environment, config):
    values = json.loads(release.command(['inspect', container_name(config)], environment=environment, timeout=20))
    release.require(isinstance(values, list) and len(values) == 1, 'controller_container_unknown')
    value = values[0]
    labels = value.get('Config', {}).get('Labels', {})
    release.require(value.get('Name') == '/'+container_name(config)
        and labels.get('com.docker.compose.project') == 'sixnine-platform'
        and labels.get('com.docker.compose.service') == SERVICE
        and labels.get('com.sixnine.finite.config-hash') == fingerprint(config), 'controller_container_identity_mismatch')
    if (ROOT/'active.json').exists():
        pin = read_json(ROOT/'active.json', 16384)
        if pin.get('version') == 2:
            verify_marker(pin.get('commit'), config, allow_inactive=True)
            release.require(value.get('Image') == pin['image_id'], 'controller_execution_image_changed')
    return value.get('State', {})


def fresh_ready(value, config, *, now=None):
    """A durable startup admission proof, distinct from an offline validation."""
    now = time.time() if now is None else now
    observed = value.get('observed_at')
    return (value.get('cycle_id') == config['cycle_id'] and value.get('config_hash') == fingerprint(config)
        and value.get('hard_deadline') == config['hard_deadline']
        and type(observed) in (int, float) and math.isfinite(observed) and 0 <= now-observed <= 30
        and value.get('snapshot_only') is False and value.get('admission_ready') is True)


def wait_until_ready(process, directory, environment, config, *, timeout=90,
                     clock=time.monotonic, sleep=time.sleep):
    """Do not expose generation until this exact controller's admission is ready."""
    deadline = clock()+timeout
    while True:
        release.require(process.poll() is None, 'controller_exited_before_admission_ready')
        try:
            state = inspect_controller(environment, config)
            if (state.get('Running') is True and state.get('Restarting') is False
                    and state.get('OOMKilled') is False and state.get('Status') == 'running'):
                proof = controller_control(directory, environment, '--status')
                if fresh_ready(proof, config):
                    return
        except (release.ReleaseError, ValueError, KeyError):
            # The initial container/receipt may not yet exist. Keep admission
            # closed and retry reads only, never launch another process.
            pass
        release.require(clock() < deadline, 'controller_admission_not_ready_barrier_retained')
        sleep(3)


def close_admission(directory, environment):
    # Caller holds release.lock. directory/environment belong to execution,
    # not necessarily the latest approved website after compatible CD.
    pin = read_json(ROOT/'active.json', 16384)
    release.require(pin.get('commit') == directory.name, 'scaler_execution_release_mismatch')
    if pin.get('version') == 2:
        atomic(ROOT/'active.json', {**pin, 'admission': 'closed', 'updated_at': time.time()})
    return release.restore_current_cpu_locked(release.ROOT)


def enable_admission(directory, environment, config):
    pin = verify_marker(directory.name, config)
    if pin['version'] == 1:
        # Legacy releases retain their strict no-CD barrier for the whole cycle.
        compose(directory, environment, 'up', '-d', '--no-deps', 'app')
        release.wait_ready(directory, environment)
        return
    admission_marker(directory.name, config, 'open')
    current, app_directory, app_environment = release.current_application(release.ROOT)
    context = release.gpu_deployment_context(release.ROOT, release.manifest(app_directory, current))
    release.approved_application_configuration(app_directory, app_environment, gpu_context=context)
    release.application_compose(app_directory, app_environment, 'up', '-d', '--no-deps', 'app', gpu_context=context)
    release.wait_ready(app_directory, app_environment)


def restore_cpu(directory, environment, config, *, timeout=240, clock=time.monotonic, sleep=time.sleep):
    close_admission(directory, environment)
    controller_control(directory, environment, '--request-drain')
    deadline = clock()+timeout
    while True:
        state = inspect_controller(environment, config)
        if state.get('Running') is False and state.get('Restarting') is False:
            release.require(state.get('Status') == 'exited' and state.get('ExitCode') == 0
                and state.get('OOMKilled') is False, 'controller_exit_requires_reconciliation')
            status = controller_control(directory, environment, '--status')
            release.require(fresh_drained(status, config), 'controller_ledger_requires_reconciliation')
            return {'billing_pending': status['billing_pending'], 'instance_count': len(status['instances'])}
        release.require(clock() < deadline, 'controller_still_reconciling_no_stop_sent')
        sleep(3)


def launch(directory, environment, config, *, loader_factory=None, popen=subprocess.Popen):
    # Import only the trusted host-installed, dependency-light runtime loader.
    # Container never receives an AWS token/role, SDK, metadata mount or socket.
    from studio_platform.lium_runtime_aws import AwsLiumLoader
    loader = (loader_factory or AwsLiumLoader)(config['secret_arn'], config['secret_version_id'])
    envelope = payload = runtime = None
    try:
        runtime = loader('lium', profile='lium--rig-root')
        envelope = {'secret_arn': config['secret_arn'], 'version_id': config['secret_version_id'],
            'payload': {'schema_version': 1, 'service': runtime.service, 'profile': runtime.profile,
                'base_url': runtime.base_url, 'primary_key_variable': runtime.primary_key_variable,
                'api_key': runtime.api_key}}
        payload = json.dumps(envelope, separators=(',', ':')).encode()
        release.require(len(payload) <= 24576, 'credential_envelope_exceeds_limit')
        args = [release.DOCKER, '--host', 'unix:///var/run/docker.sock', *compose_args(directory,
            'run', '-T', '--name', container_name(config), '--no-deps', '--label',
            'com.sixnine.finite.config-hash='+fingerprint(config), '--entrypoint', 'python', SERVICE,
            '-m', ENTRY_MODULE, '--config', CONFIG_TARGET, '--enabled', '--credential-stdin')]
        process = popen(args, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Bounded one-time write, explicit EOF; child subprocesses get DEVNULL.
        # Never communicate(timeout=...) / terminate: Docker client loss is not
        # evidence that provider resources or submitted work are safe to stop.
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except Exception:
            raise release.ReleaseError('controller_stdin_delivery_unconfirmed') from None
        return process
    finally:
        payload = runtime = None
        if envelope is not None:
            envelope.clear()
        loader.close()


def checked_release(commit=None):
    if commit is None:
        state = read_json(release.ROOT/'release-state.json', 16384)
        commit = state.get('current')
        release.require(isinstance(commit, str) and release.SHA.fullmatch(commit)
            and state.get('pending') is None and state.get('status') in ('app_ready', 'rolled_back_app_only'),
            'healthy_approved_release_required')
    else:
        release.require(isinstance(commit, str) and release.SHA.fullmatch(commit), 'execution_release_invalid')
    directory = release.ROOT/'releases'/commit
    release.approved_manifest(release.ROOT, directory, commit)
    manifest = release.manifest(directory, commit)
    # approved_manifest authenticates files; validate all supported Docker/OCI
    # archive identities rather than assuming image inspect uses config digest.
    identities = release.validate_image_archive(directory/'image.tar.gz', manifest)
    environment = release.deployment_environment(release.ROOT/'site.env', commit)
    image = json.loads(release.command(['image', 'inspect', environment['SIXNINE_IMAGE']], environment=environment))
    release.require(isinstance(image, list) and len(image) == 1 and image[0].get('Id') in identities,
                    'scaler_image_identity_unapproved')
    release.approved_configuration(directory, environment)
    return commit, directory, environment


def wait_for_controller(process, directory, environment, config=None):
    """A host TERM requests drain; it never proxies a kill to the container."""
    requested = [False]
    handlers = {}
    def request(*_):
        requested[0] = True
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, request)
        while True:
            if requested[0]:
                try:
                    import fcntl
                    with (release.ROOT/'release.lock').open('a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if config is not None:
                            verify_marker(directory.name, config, allow_inactive=True)
                        close_admission(directory, environment)
                        controller_control(directory, environment, '--request-drain')
                    requested[0] = False
                except Exception:
                    # Boot may not yet have its cycle receipt. Retain the
                    # request and retry; never infer that no resource exists.
                    pass
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('Finite GPU deployment disabled; explicit root action required')
        return 0
    process = directory = environment = None
    owns_barrier = False
    try:
        import fcntl
        release.require(args in (['start'], ['restore-cpu'], ['resume-handoff'], ['resume-preparation']), 'explicit_scaler_action_required')
        release.check_host(release.ROOT)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = protected_inputs(starting=args == ['start'])
            if args in (['start'], ['resume-handoff'], ['resume-preparation']):
                commit, directory, environment = checked_release()
                require_new_controller(environment)
                if args == ['resume-handoff']:
                    import gpu_handoff
                    gpu_handoff.verify_resume(config, commit, environment)
                if args == ['resume-preparation']:
                    import gpu_preparation_recovery
                    gpu_preparation_recovery.verify_resume(config, commit, environment)
                atomic(ROOT/'overlay.json', overlay(environment['SIXNINE_IMAGE'], engine_config(config)))
                atomic(ROOT/'app-admission.json', release.app_admission_overlay(release.ROOT))
            else:
                pin = read_json(ROOT/'active.json', 16384)
                commit, directory, environment = checked_release(pin.get('commit'))
                verify_marker(commit, config, allow_inactive=True)
                release.regular(ROOT/'overlay.json', root_owned=True, maximum=65536)
                owns_barrier = True
            version = release.command(['compose', 'version', '--short'], environment=environment).decode().strip()
            validate(json.loads(compose(directory, environment, 'config', '--format', 'json')),
                deployment_directory=directory, compose_version=version)
            if args == ['restore-cpu']:
                result = restore_cpu(directory, environment, config)
                marker(commit, config, False)
                print(json.dumps({'state': 'cpu_restored_resources_destroyed', **result}))
                return 0
            validation = controller_control(directory, environment, '--validate')
            release.require(validation.get('config_valid') is True
                and validation.get('provider_calls_enabled') is False
                and validation.get('config_hash') == fingerprint(config), 'finite_configuration_validation_failed')
            if args == ['resume-handoff']:
                gpu_handoff.activate_resume(config, commit, environment)
            if args == ['resume-preparation']:
                gpu_preparation_recovery.activate_resume(config, commit, environment)
            marker(commit, config, True)
            owns_barrier = True
            process = launch(directory, environment, config)
            if on_demand_config(config):
                wait_until_ready(process, directory, environment, config)
            enable_admission(directory, environment, config)
        # Caller runs this root helper under a persistent systemd unit. Release
        # lock is free during reconciliation so restore-cpu can close admission.
        wait_for_controller(process, directory, environment, config)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A concurrent operator restore may already have set this exact
            # cycle inactive. Recheck the fresh proof; do not report failure or
            # accept another cycle merely because its marker is inactive.
            verify_marker(commit, config, allow_inactive=True)
            close_admission(directory, environment)
            release.require(process.returncode == 0, 'controller_uncertain_exit_barrier_retained')
            result = restore_cpu(directory, environment, config)
            marker(commit, config, False)
            print(json.dumps({'state': 'finite_cycle_complete_cpu_restored', **result}))
        return 0
    except Exception:
        # Fail closed for new work, but never stop/kill/remove an uncertain
        # controller. Its independent provider TTL and durable ledger remain.
        if owns_barrier and directory is not None and environment is not None:
            try:
                with (release.ROOT/'release.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    verify_marker(commit, config, allow_inactive=True)
                    close_admission(directory, environment)
            except Exception:
                pass
        print('Finite GPU operation incomplete; admission closure attempted, reconciliation barrier retained', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

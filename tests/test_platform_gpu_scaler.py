"""Offline finite scaler deployment policy; no AWS/provider/service operations."""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_platform_gpu_acceptance import load, DEPLOY, validator, release, acceptance

with patch.dict(sys.modules, {'release': release, 'check_config': validator, 'gpu_acceptance': acceptance}):
    scaler = load('test_finite_deploy', 'gpu_scaler.py')

with patch.dict(sys.modules, {'release':release, 'gpu_scaler':scaler}):
    handoff = load('test_backlog_host','gpu_handoff.py')


def configuration():
    return {'cycle_id': 'synthetic-cycle', 'hard_deadline': 2000000000,
        'secret_arn': 'arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/lium-ABCDEF',
        'secret_version_id': 'synthetic-version-'+'a'*32}


def on_demand_configuration():
    return {**configuration(), 'service_mode': 'on-demand', 'max_cycles': 8,
        'allowed_owners': ['superdan', 'supervan'], 'created_at': 1999913600,
        'scale_policy': {'idle_before_drain_s': 600, 'max_instances': 1, 'max_physical_gpus': 1,
                         'new_instance_slots': 1, 'new_instance_physical_gpus': 1}}


def good_status(config):
    return {'cycle_id': config['cycle_id'], 'config_hash': scaler.fingerprint(config),
        'hard_deadline': config['hard_deadline'], 'observed_at': time.time(),
        'snapshot_only': False, 'controller_exit_required': True, 'ledger_safe': True,
        'all_destroyed': True, 'drained': True, 'active_job_ids': [], 'active_jobs_truncated': False, 'billing_pending': 1,
        'instances': [{'id': 'synthetic-intent', 'state': 'destroyed'}]}


def container(config, *, running=False, **state):
    return {'Name': '/'+scaler.container_name(config), 'Config': {'Labels': {
        'com.docker.compose.project': 'sixnine-platform', 'com.docker.compose.service': scaler.SERVICE,
        'com.sixnine.finite.config-hash': scaler.fingerprint(config)}},
        'State': {'Running': running, 'Restarting': False, 'ExitCode': 0,
            'Status': 'running' if running else 'exited', 'OOMKilled': False, **state}}


class FiniteComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose CLI unavailable; no daemon required')
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary)/'overlay.json'
            overlay.write_text(json.dumps(scaler.overlay('sixnine-platform:'+'a'*40)))
            empty = Path(temporary)/'empty.env'
            empty.write_text('')
            env = dict(os.environ, SIXNINE_IMAGE='sixnine-platform:'+'a'*40,
                SIXNINE_POSTGRES_IMAGE='postgres@sha256:'+'b'*64, SIXNINE_CADDY_IMAGE='caddy@sha256:'+'c'*64,
                SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
                SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
            raw = subprocess.run(['docker', 'compose', '--env-file', str(empty), '-f', str(DEPLOY/'compose.yaml'),
                '-f', str(overlay), 'config', '--format', 'json'], env=env, capture_output=True, timeout=30)
            if raw.returncode:
                raise AssertionError('Synthetic Compose rendering failed; output withheld')
            cls.config = json.loads(raw.stdout)
            cls.version = subprocess.run(['docker', 'compose', 'version', '--short'],
                check=True, capture_output=True, timeout=10).stdout.decode().strip()

    def check(self, config, version=None):
        return scaler.validate(config, deployment_directory=DEPLOY, compose_version=version or self.version)

    def test_exact_rendered_overlay_preserves_base_and_disabled_controller(self):
        before = copy.deepcopy(self.config)
        self.assertTrue(self.check(self.config))
        self.assertEqual(self.config, before)
        control = self.config['services'][scaler.SERVICE]
        self.assertNotIn('--enabled', control['command'])
        self.assertNotIn('--credential-stdin', control['command'])
        self.assertEqual(control['command'][2], 'studio_platform.scaler_entry')
        self.assertEqual(set(control['networks']), {'database', 'edge'})
        self.assertEqual(control['environment']['AWS_EC2_METADATA_DISABLED'], 'true')

    def test_unreviewed_controller_or_admission_deltas_fail(self):
        mutations = [lambda w: w.update(user='0:0'), lambda w: w.update(privileged=True),
            lambda w: w.update(network_mode='host'), lambda w: w.update(ports=['8188:8188']),
            lambda w: w.update(mem_limit=2*1024**3), lambda w: w.update(cpus=2),
            lambda w: w['command'].append('--enabled'),
            lambda w: w['environment'].update(AWS_ACCESS_KEY_ID='synthetic'),
            lambda w: w['volumes'].append(acceptance.bind('/var/run/docker.sock', '/var/run/docker.sock')),
            lambda w: w['volumes'][-1].update(read_only=False),
            lambda w: w['networks'].update(web={})]
        for mutation in mutations:
            value = copy.deepcopy(self.config)
            mutation(value['services'][scaler.SERVICE])
            with self.subTest(mutation=mutation), self.assertRaises(release.ReleaseError):
                self.check(value)
        for key, value in [('SIXNINE_AUTH_MODE', 'local-test'), ('SIXNINE_CLOUD_CREATION_ENABLED', '1')]:
            changed = copy.deepcopy(self.config)
            changed['services']['app']['environment'][key] = value
            with self.assertRaises(validator.ConfigurationError):
                self.check(changed)

    def test_only_observed_2382_bind_false_serialization_is_normalized(self):
        changed = copy.deepcopy(self.config)
        for service in changed['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('type') == 'bind':
                    mount['bind'] = {}
        self.assertTrue(self.check(changed, '2.38.2'))
        for version in ('2.38.1', '2.39.0', '5.6.0'):
            with self.assertRaises((release.ReleaseError, validator.ConfigurationError)):
                self.check(changed, version)
        changed['services'][scaler.SERVICE]['volumes'][0]['bind'] = {'create_host_path': True}
        with self.assertRaises(release.ReleaseError):
            self.check(changed, '2.38.2')


class FiniteControlTests(unittest.TestCase):
    def test_provider_preparation_timeout_is_explicit_bounded_host_configuration(self):
        original = on_demand_configuration()
        self.assertTrue(scaler.on_demand_config({**original, 'provider_preparation_timeout_s': 1800}))
        for invalid in (True, 0, -1, 119, 7201, 1800.0, float('nan'), '1800'):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                scaler.on_demand_config({**original, 'provider_preparation_timeout_s': invalid})
        for invalid in (True, 0, -1, 6, 2.0, None, '2'):
            with self.subTest(limit=invalid), self.assertRaises(release.ReleaseError):
                scaler.on_demand_config({**original, 'provider_preparation_timeout_s': 1800,
                    'provider_preparation_failure_limit': invalid})

    def test_on_demand_requires_exact_owner_pair_and_single_gpu_idle_policy(self):
        config = on_demand_configuration()
        self.assertTrue(scaler.on_demand_config(config))
        self.assertFalse(scaler.on_demand_config(configuration()))
        for field, value in [('service_mode', 'warm'), ('service_mode', ''), ('max_cycles', 0),
                             ('max_cycles', 9), ('max_cycles', True), ('allowed_owners', ['superdan']),
                             ('allowed_owners', ['superdan', 'other']), ('created_at', False),
                             ('created_at', 1999913599), ('hard_deadline', float('inf'))]:
            with self.subTest(field=field, value=value), self.assertRaises(release.ReleaseError):
                scaler.on_demand_config({**config, field: value})
        for field, value in [('idle_before_drain_s', 599), ('idle_before_drain_s', 601),
                             ('max_instances', 2), ('max_physical_gpus', 2), ('max_instances', True),
                             ('new_instance_slots', 2), ('new_instance_physical_gpus', 2)]:
            changed = {**config, 'scale_policy': {**config['scale_policy'], field: value}}
            with self.subTest(field=field, value=value), self.assertRaises(release.ReleaseError):
                scaler.on_demand_config(changed)

    def test_operator_policy_sources_and_runtime_identity_are_bound_before_start(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for folder in ('operator', 'identity', 'public-source', 'control', 'tmp'):
                (root/folder).mkdir()
            source = root/'public-source'
            (source/'bootstrap_cloud.py').write_text('# synthetic public source')
            (source/'model_manifest.json').write_text('{}')
            config = {**configuration(), 'enabled': True, 'tenant': 'sixnine', 'owner': 'superdan',
                'work_dir': '/control', 'data_dir': '/data', 'source_dir': '/bootstrap-source',
                'ssh_key_file': '/worker-identity/key', 'known_hosts_file': '/control/known_hosts',
                'hard_deadline': 5000, 'source_sha256': {name: hashlib.sha256((source/name).read_bytes()).hexdigest()
                    for name in ('bootstrap_cloud.py', 'model_manifest.json')},
                'execution_policy_sha256': scaler.fingerprint({'approved': 'synthetic', 'backend': 'comfy-worker'})}
            config_path = root/'operator'/'scaler.json'
            config_path.write_text(json.dumps(config))
            (root/'operator'/'execution-policy.json').write_text(json.dumps({'approved': 'synthetic', 'backend': 'comfy-worker'}))
            metadata = {'secret_arn': config['secret_arn'], 'version_id': config['secret_version_id'],
                'service': 'lium', 'profile': 'lium--rig-root'}
            metadata_path = root/'runtime.json'
            metadata_path.write_text(json.dumps(metadata))
            for field, path in [('ROOT', root), ('SOURCE', source), ('CONFIG_SOURCE', config_path),
                                ('POLICY_SOURCE', root/'operator'/'execution-policy.json'), ('RUNTIME_METADATA', metadata_path)]:
                stack.enter_context(patch.object(scaler, field, path))
            regular = stack.enter_context(patch.object(release, 'regular', side_effect=lambda path, **kw:
                SimpleNamespace(st_uid=10001, st_mode=stat.S_IFREG|(0o600 if path.name == 'known_hosts' else 0o400))))
            stack.enter_context(patch.object(Path, 'lstat', autospec=True, side_effect=lambda path:
                SimpleNamespace(st_uid=10001 if path in (root/'control', root/'tmp') else 0, st_mode=stat.S_IFDIR|0o700)))
            self.assertEqual(scaler.protected_inputs(starting=True, now=4000), config)
            ondemand = {**config, **{key: value for key, value in on_demand_configuration().items()
                if key in ('service_mode', 'max_cycles', 'allowed_owners', 'scale_policy')},
                'created_at': 4000, 'hard_deadline': 4000+24*3600}
            config_path.write_text(json.dumps(ondemand))
            self.assertEqual(scaler.protected_inputs(starting=True, now=4000), ondemand)
            legacy_long = {**config, 'created_at': 4000, 'hard_deadline': 4000+24*3600}
            config_path.write_text(json.dumps(legacy_long))
            with self.assertRaisesRegex(release.ReleaseError, 'finite_authorization'):
                scaler.protected_inputs(starting=True, now=4000)
            config_path.write_text(json.dumps(config))
            for field, value in [('owner', 'supervan'), ('work_dir', '/other'), ('secret_version_id', 'wrong')]:
                changed = {**config, field: value}
                config_path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(release.ReleaseError):
                    scaler.protected_inputs(starting=True, now=4000)
            config_path.write_text(json.dumps(config))
            (source/'model_manifest.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(release.ReleaseError, 'source_hash_mismatch'):
                scaler.protected_inputs(starting=True, now=4000)
            (source/'model_manifest.json').write_text('{}')
            (root/'control'/'known_hosts').write_text('synthetic-public-host-key')
            self.assertEqual(scaler.protected_inputs(starting=True, now=4000), config)
            self.assertTrue(any(call.args[0].name == 'known_hosts' for call in regular.call_args_list))
            (root/'control'/'cycle-state.json').write_text('{}')
            with self.assertRaisesRegex(release.ReleaseError, 'previous_cycle'):
                scaler.protected_inputs(starting=True, now=4000)

    def test_default_never_loads_or_calls_anything(self):
        with patch.object(release, 'check_host') as host, patch.object(scaler, 'launch') as launch, \
                patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(scaler.main([]), 0)
        host.assert_not_called()
        launch.assert_not_called()

    def test_credential_only_once_in_pipe_no_file_env_argv_or_output(self):
        config = configuration()
        runtime = SimpleNamespace(service='lium', profile='lium--rig-root', base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY', api_key='synthetic-secret-value')
        loader = Mock(return_value=runtime)
        process = Mock()
        captured = []
        process.stdin.write.side_effect = lambda data: captured.append(json.loads(data))
        factory, popen = Mock(return_value=loader), Mock(return_value=process)
        env = {'PATH': '/usr/bin', 'LANG': 'C.UTF-8'}
        self.assertIs(scaler.launch(Path('/release'), env, config, loader_factory=factory, popen=popen), process)
        loader.assert_called_once_with('lium', profile='lium--rig-root')
        loader.close.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['payload']['api_key'], runtime.api_key)
        args, kwargs = popen.call_args
        self.assertNotIn(runtime.api_key, json.dumps(args))
        self.assertEqual(kwargs['env'], env)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIn('--credential-stdin', args[0])
        self.assertIn('studio_platform.scaler_entry', args[0])
        self.assertNotIn('--rm', args[0])
        process.wait.assert_not_called()
        process.terminate.assert_not_called()

    def test_delivery_unknown_does_not_kill_or_retry(self):
        runtime = SimpleNamespace(service='lium', profile='lium--rig-root', base_url='https://lium.io/api',
            primary_key_variable='LIUM_API_KEY', api_key='synthetic')
        loader, process, popen = Mock(return_value=runtime), Mock(), Mock()
        popen.return_value = process
        process.stdin.write.side_effect = BrokenPipeError('synthetic')
        with self.assertRaisesRegex(release.ReleaseError, 'delivery_unconfirmed'):
            scaler.launch(Path('/release'), {}, configuration(), loader_factory=Mock(return_value=loader), popen=popen)
        self.assertEqual(popen.call_count, 1)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        loader.close.assert_called_once()

    def test_status_is_fresh_bound_and_not_cached_snapshot(self):
        config = configuration()
        self.assertTrue(scaler.fresh_drained(good_status(config), config))
        for key, value in [('snapshot_only', True), ('ledger_safe', False), ('drained', False),
                           ('all_destroyed', False), ('active_job_ids', ['job']), ('observed_at', 0),
                           ('config_hash', 'wrong'), ('cycle_id', 'wrong'), ('controller_exit_required', False),
                           ('active_jobs_truncated', True),
                           ('instances', [{'id': 'x', 'state': 'unknown'}]), ('billing_pending', -1)]:
            data = good_status(config)
            data[key] = value
            with self.subTest(key=key):
                self.assertFalse(scaler.fresh_drained(data, config))

    def test_already_restored_marker_is_idempotent_only_for_exact_cycle(self):
        config, commit = configuration(), 'a'*40
        value = {'version': 1, 'active': False, 'commit': commit, 'cycle_id': config['cycle_id'],
                 'config_hash': scaler.fingerprint(config), 'container_name': scaler.container_name(config)}
        with patch.object(scaler, 'read_json', return_value=value):
            with self.assertRaises(release.ReleaseError):
                scaler.verify_marker(commit, config)
            scaler.verify_marker(commit, config, allow_inactive=True)
            value['config_hash'] = 'wrong'
            with self.assertRaises(release.ReleaseError):
                scaler.verify_marker(commit, config, allow_inactive=True)

    def test_restore_closes_admission_waits_natural_exit_and_never_sends_signal(self):
        config, events = configuration(), []
        states = iter([container(configuration(), running=True), container(configuration())])
        def docker(args, **kwargs):
            events.append(args)
            self.assertEqual(args[0], 'inspect')
            return json.dumps([next(states)]).encode()
        def control(*args):
            events.append(args[-1])
            return good_status(config)
        with patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')), \
                patch.object(scaler, 'controller_control', side_effect=control), \
                patch.object(release, 'command', side_effect=docker):
            result = scaler.restore_cpu(Path('/release'), {}, config, sleep=lambda _: None)
        self.assertEqual(events[0:2], ['cpu', '--request-drain'])
        self.assertEqual(events[-1], '--status')
        self.assertEqual(result, {'billing_pending': 1, 'instance_count': 1})

    def test_unknown_running_nonzero_oom_or_wrong_container_never_complete(self):
        config = configuration()
        for data in [container(config, running=True), container(config, ExitCode=1),
                     container(config, OOMKilled=True), {**container(config), 'Name': '/other'}]:
            with patch.object(scaler, 'close_admission'), patch.object(scaler, 'controller_control', return_value=good_status(config)), \
                    patch.object(release, 'command', return_value=json.dumps([data]).encode()) as docker:
                with self.assertRaises(release.ReleaseError):
                    scaler.restore_cpu(Path('/release'), {}, config, timeout=0)
                self.assertTrue(all(c.args[0][0] == 'inspect' for c in docker.call_args_list))

    def test_control_process_is_independent_and_never_enabled(self):
        for action in ('--status', '--request-drain', '--validate'):
            with patch.object(scaler, 'compose', return_value=b'{}') as compose:
                scaler.controller_control(Path('/release'), {}, action)
            self.assertIn('--rm', compose.call_args.args)
            self.assertNotIn('--enabled', compose.call_args.args)
            self.assertNotIn('--credential-stdin', compose.call_args.args)
            self.assertIn('studio_platform.scaler_entry', compose.call_args.args)

    def test_readiness_wait_requires_live_exact_container_and_fresh_admission_proof(self):
        config = on_demand_configuration()
        ready = {**good_status(config), 'admission_ready': True}
        not_ready = {**ready, 'admission_ready': False}
        process = Mock()
        process.poll.return_value = None
        states = [container(config, running=True)['State']]*2
        with patch.object(scaler, 'inspect_controller', side_effect=states), \
                patch.object(scaler, 'controller_control', side_effect=[not_ready, ready]) as control:
            scaler.wait_until_ready(process, Path('/release'), {}, config, sleep=lambda _: None)
        self.assertEqual(control.call_count, 2)
        self.assertTrue(all(call.args[-1] == '--status' for call in control.call_args_list))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_invalid_or_stale_readiness_proof_never_enables_api(self):
        config = on_demand_configuration()
        good = {**good_status(config), 'admission_ready': True}
        self.assertTrue(scaler.fresh_ready(good, config))
        for key, value in [('snapshot_only', True), ('admission_ready', False), ('observed_at', 0),
                           ('observed_at', float('nan')), ('cycle_id', 'another'), ('config_hash', 'another'),
                           ('hard_deadline', config['hard_deadline']+1)]:
            with self.subTest(key=key):
                self.assertFalse(scaler.fresh_ready({**good, key: value}, config))
        process = Mock()
        process.poll.return_value = None
        with patch.object(scaler, 'inspect_controller', return_value=container(config, running=True)['State']), \
                patch.object(scaler, 'controller_control', return_value={}), \
                self.assertRaisesRegex(release.ReleaseError, 'admission_not_ready'):
            scaler.wait_until_ready(process, Path('/release'), {}, config, timeout=0)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.poll.return_value = 1
        with patch.object(scaler, 'controller_control') as control, \
                self.assertRaisesRegex(release.ReleaseError, 'exited_before_admission'):
            scaler.wait_until_ready(process, Path('/release'), {}, config, timeout=0)
        control.assert_not_called()

    def test_term_only_requests_drain_without_signalling_container(self):
        handlers, events = {}, []
        process = Mock()
        def wait(**kwargs):
            if not events:
                handlers[scaler.signal.SIGTERM]()
                events.append('term')
                raise subprocess.TimeoutExpired('synthetic', 5)
            return 0
        process.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as tmp, patch.object(release, 'ROOT', Path(tmp)), \
                patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, flock=lambda *_: None)}), \
                patch.object(scaler.signal, 'getsignal', return_value='original'), \
                patch.object(scaler.signal, 'signal', side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')), \
                patch.object(scaler, 'controller_control', side_effect=lambda *a: events.append(a[-1])):
            self.assertEqual(scaler.wait_for_controller(process, Path('/release'), {}), 0)
        self.assertEqual(events, ['term', 'cpu', '--request-drain'])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(all(handler == 'original' for handler in handlers.values()))

    def test_auto_exit_restores_cpu_before_releasing_barrier_even_on_error(self):
        for code in (0, 1):
            config, events = configuration(), []
            process = Mock(returncode=code)
            process.wait.side_effect = lambda **_: events.append('wait')
            with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)}))
                stack.enter_context(patch.object(release, 'ROOT', Path(tmp)))
                for name in ('check_host', 'wait_ready'):
                    stack.enter_context(patch.object(release, name))
                stack.enter_context(patch.object(release, 'command', return_value=b'2.38.2'))
                stack.enter_context(patch.object(scaler, 'checked_release', return_value=(
                    'a'*40, Path(tmp), {'SIXNINE_IMAGE': 'sixnine-platform:'+'a'*40})))
                stack.enter_context(patch.object(scaler, 'protected_inputs', return_value=config))
                for name in ('require_new_controller', 'atomic', 'validate', 'verify_marker'):
                    stack.enter_context(patch.object(scaler, name))
                stack.enter_context(patch.object(scaler, 'compose', return_value=b'{}'))
                stack.enter_context(patch.object(scaler, 'controller_control', return_value={
                    'config_valid': True, 'provider_calls_enabled': False, 'config_hash': scaler.fingerprint(config)}))
                stack.enter_context(patch.object(scaler, 'launch', return_value=process))
                stack.enter_context(patch.object(scaler, 'marker', side_effect=lambda c, x, a: events.append(('marker', a))))
                stack.enter_context(patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')))
                stack.enter_context(patch.object(scaler, 'enable_admission'))
                stack.enter_context(patch.object(scaler, 'restore_cpu', side_effect=lambda *_: events.append('restored') or {'billing_pending': 0}))
                stack.enter_context(patch('sys.stdout', new=io.StringIO()))
                stack.enter_context(patch('sys.stderr', new=io.StringIO()))
                self.assertEqual(scaler.main(['start']), 0 if code == 0 else 1)
            self.assertLess(events.index('wait'), events.index('cpu'))
            if code == 0:
                self.assertLess(events.index('cpu'), events.index(('marker', False)))
            else:
                self.assertNotIn(('marker', False), events)

    def test_on_demand_start_cannot_enable_app_before_startup_admission_ready(self):
        config, events = on_demand_configuration(), []
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)}))
            stack.enter_context(patch.object(release, 'ROOT', Path(tmp)))
            stack.enter_context(patch.object(release, 'check_host'))
            stack.enter_context(patch.object(release, 'command', return_value=b'2.38.2'))
            stack.enter_context(patch.object(scaler, 'checked_release', return_value=(
                'a'*40, Path(tmp), {'SIXNINE_IMAGE': 'sixnine-platform:'+'a'*40})))
            stack.enter_context(patch.object(scaler, 'protected_inputs', return_value=config))
            for name in ('require_new_controller', 'atomic', 'validate', 'verify_marker'):
                stack.enter_context(patch.object(scaler, name))
            compose = stack.enter_context(patch.object(scaler, 'compose', return_value=b'{}'))
            stack.enter_context(patch.object(scaler, 'controller_control', return_value={
                'config_valid': True, 'provider_calls_enabled': False, 'config_hash': scaler.fingerprint(config)}))
            process = Mock()
            launch = stack.enter_context(patch.object(scaler, 'launch', return_value=process))
            stack.enter_context(patch.object(scaler, 'marker', side_effect=lambda c, x, a: events.append(('marker', a))))
            wait = stack.enter_context(patch.object(scaler, 'wait_until_ready',
                side_effect=release.ReleaseError('synthetic_not_ready')))
            stack.enter_context(patch.object(scaler, 'close_admission', side_effect=lambda *_: events.append('cpu')))
            stack.enter_context(patch('sys.stderr', new=io.StringIO()))
            self.assertEqual(scaler.main(['start']), 1)
        launch.assert_called_once()
        wait.assert_called_once()
        self.assertEqual(events, [('marker', True), 'cpu'])
        self.assertTrue(all('up' not in call.args for call in compose.call_args_list))
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_closing_old_controller_uses_current_app_and_marks_admission_closed(self):
        config, commit = configuration(), 'a'*40
        pin = {'version': 2, 'active': True, 'commit': commit, 'admission': 'open'}
        with patch.object(scaler, 'read_json', return_value=pin), patch.object(scaler, 'atomic') as atomic, \
                patch.object(release, 'restore_current_cpu_locked', return_value='b'*40) as restore, \
                patch.object(release, 'compose') as compose:
            scaler.close_admission(Path('/old-release')/commit, {'SIXNINE_IMAGE': commit})
        restore.assert_called_once_with(release.ROOT)
        compose.assert_not_called()
        self.assertEqual(atomic.call_args.args[1]['admission'], 'closed')
        self.assertEqual(atomic.call_args.args[1]['commit'], commit)

    def test_wrong_execution_release_cannot_close_current_admission(self):
        with patch.object(scaler, 'read_json', return_value={'commit': 'a'*40}), \
                patch.object(release, 'restore_current_cpu_locked') as restore, \
                self.assertRaisesRegex(release.ReleaseError, 'execution_release_mismatch'):
            scaler.close_admission(Path('/release')/('b'*40), {})
        restore.assert_not_called()

    def test_new_marker_pins_approved_execution_image_and_contracts(self):
        contracts = {'version': 1, 'api_compatibility': '1'*64,
            'worker_compatibility': '2'*64, 'frontend_contract': 'sixnine-web-v1'}
        image_id = 'sha256:'+'c'*64
        with patch.object(release, 'approved_manifest'), \
                patch.object(release, 'manifest', return_value={'contracts': contracts}), \
                patch.object(release, 'deployment_environment', return_value={'SIXNINE_IMAGE': 'sixnine-platform:'+'a'*40}), \
                patch.object(release, 'validate_image_archive', return_value={image_id}), \
                patch.object(release, 'command', return_value=json.dumps([{'Id': image_id}]).encode()), \
                patch.object(scaler, 'atomic') as atomic:
            scaler.marker('a'*40, configuration(), True)
        value = atomic.call_args.args[1]
        self.assertEqual(value['version'], 2)
        self.assertEqual(value['image_id'], image_id)
        self.assertEqual(value['contracts'], contracts)
        self.assertEqual(value['admission'], 'closed')
        with patch.object(scaler, 'read_json', return_value=value), patch.object(scaler, 'atomic') as atomic:
            scaler.marker('a'*40, configuration(), False)
        closed = atomic.call_args.args[1]
        self.assertFalse(closed['active'])
        self.assertEqual(closed['image_id'], image_id)
        self.assertEqual(closed['contracts'], contracts)


class BacklogHostTests(unittest.TestCase):
    def configs(self):
        old=on_demand_configuration()
        old['scale_policy'].update(instance_reservation_microusd=5600000,approved_remaining_microusd=46509655)
        old.update(enabled=True,budget_account_ids=['original-budget'],execution_policy_sha256='e'*64,
            launches=[{'provider':'lium','configuration_id':'original','model_id':'h3','offer_id':'old',
                'image_id':'original-template'}], manifests=[{'configuration_id':'original','model_id':'h3',
                'executor_id':'old','template_id':'original-template','gpu_count':1,'execution_slots':1,
                'max_price_per_gpu_hour_microusd':1400000,'termination_hours':4,'approved_until':old['hard_deadline']}])
        new=copy.deepcopy(old)
        new['scale_policy']['instance_reservation_microusd']=4500000
        new['launches'][0]['offer_id']=''
        new['manifests'][0].update(executor_id='',compatible_gpu_names=['NVIDIA H100 80GB HBM3'],
            minimum_vram_mib=70000,server_side_selection=True,minimum_ram_gib=64,minimum_disk_gib=100,
            require_docker_in_docker=True,max_price_per_gpu_hour_microusd=1500000,termination_hours=3)
        return old,new

    def test_only_selector_price_ttl_and_reservation_can_change(self):
        old,new=self.configs()
        self.assertTrue(handoff.validate_delta(old,new))
        changes=[lambda c:c.update(hard_deadline=c['hard_deadline']+1),
            lambda c:c.update(created_at=c['created_at']+1),
            lambda c:c.update(budget_account_ids=['replacement']),
            lambda c:c.update(execution_policy_sha256='d'*64),
            lambda c:c.update(max_cycles=7),
            lambda c:c['scale_policy'].update(approved_remaining_microusd=50000000),
            lambda c:c['scale_policy'].update(instance_reservation_microusd=4499999),
            lambda c:c['manifests'][0].update(gpu_count=8),
            lambda c:c['manifests'][0].update(template_id='replacement'),
            lambda c:c['manifests'][0].update(termination_hours=4),
            lambda c:c['manifests'][0].update(max_price_per_gpu_hour_microusd=1500001)]
        for change in changes:
            value=copy.deepcopy(new);change(value)
            with self.subTest(change=change),self.assertRaises(release.ReleaseError):
                handoff.validate_delta(old,value)

    def test_freeze_precedes_admission_close_and_never_sends_term_or_drain(self):
        old,_=self.configs();events=[]
        pin={'commit':'a'*40}
        frozen={'config_hash':scaler.fingerprint(old),'pid':100,'paused':True}
        with patch.object(handoff,'record_path',return_value=Mock(exists=lambda:False)), \
                patch.object(scaler,'protected_inputs',return_value=old), \
                patch.object(scaler,'read_json',return_value=pin),patch.object(scaler,'verify_marker'), \
                patch.object(scaler,'checked_release',return_value=('a'*40,Path('/release')/('a'*40),{})), \
                patch.object(handoff,'supervisor',return_value={'pid':90,'active_state':'active'}), \
                patch.object(handoff,'inspect',return_value={'State':{'Running':True,'Paused':False}}), \
                patch.object(handoff,'frozen_identity',side_effect=lambda *a,**kw:{**frozen,'frozen_at':kw['frozen_at']}), \
                patch.object(handoff.time,'time',side_effect=[1000,1120]), \
                patch.object(release,'command',side_effect=lambda a,**kw:events.append(a)), \
                patch.object(scaler,'close_admission',side_effect=lambda *a:events.append('close')), \
                patch.object(scaler,'atomic'),patch.object(handoff,'command') as systemd:
            result=handoff.freeze('b'*40,'sixnine-old.service')
        self.assertEqual(events,[['pause',scaler.container_name(old)],'close'])
        self.assertEqual(result['phase'],'frozen')
        self.assertEqual(result['frozen']['frozen_at'],1120)
        systemd.assert_not_called()

    def test_retirement_kills_confirmed_main_then_already_paused_container_without_unpause(self):
        old,_=self.configs();events=[]
        host={'unit':'sixnine-old.service','pid':90,'active_state':'active'}
        receipt={'phase':'ledger_fenced','old_commit':'a'*40,'target_commit':'b'*40,
            'frozen':{'frozen_at':1000},'supervisor':host,'ledger':{'job_hashes':{'original-job':'a'*64}}}
        def read(path,*args):
            return receipt if Path(path).name == handoff.RECEIPT else old
        def kill(*args):
            events.append(('systemd',args));host['pid']=0;host['active_state']='failed';return b''
        with patch.object(scaler,'read_json',side_effect=read), \
                patch.object(scaler,'protected_inputs',return_value=old),patch.object(handoff,'core',return_value={'verified':True}), \
                patch.object(scaler,'checked_release',return_value=('a'*40,Path('/release')/('a'*40),{})), \
                patch.object(handoff,'frozen_identity',return_value=receipt['frozen']), \
                patch.object(handoff,'supervisor',side_effect=lambda unit:dict(host)), \
                patch.object(handoff,'command',side_effect=kill), \
                patch.object(release,'command',side_effect=lambda a,**kw:events.append(('docker',a))), \
                patch.object(handoff,'inspect',return_value={'State':{'Running':False,'Paused':False,
                    'Restarting':False,'Status':'exited','ExitCode':137}}),patch.object(scaler,'marker'), \
                patch.object(scaler,'atomic'):
            result=handoff.retire(sleep=lambda s:None)
        self.assertEqual(events[0],('systemd',('kill','--kill-whom=main','--signal=SIGKILL','sixnine-old.service')))
        self.assertEqual(events[1],('docker',['kill','--signal=SIGKILL',scaler.container_name(old)]))
        self.assertEqual(events[2],('docker',['rm',scaler.container_name(old)]))
        self.assertEqual(result['preserved_job_ids'],['original-job'])
        self.assertTrue(receipt['ledger']['host_retirement_confirmed'])

    def test_ledger_failure_sends_no_host_signal_or_container_kill(self):
        old,_=self.configs()
        receipt={'phase':'ledger_fenced','target_commit':'b'*40,'ledger':{}}
        with patch.object(scaler,'read_json',side_effect=[receipt,old]), \
                patch.object(scaler,'protected_inputs',return_value=old), \
                patch.object(handoff,'core',side_effect=release.ReleaseError('changed')), \
                patch.object(handoff,'command') as systemd,patch.object(release,'command') as docker:
            with self.assertRaises(release.ReleaseError):
                handoff.retire()
        systemd.assert_not_called();docker.assert_not_called()

    def resume_fixture(self):
        old,new=self.configs()
        next_state={'version':1,'config_hash':scaler.fingerprint(new),'sequence':2,
            'created_at':old['created_at'],'transfer_from':'original-grant-001'}
        receipt={'version':1,'phase':'staged','target_commit':'b'*40,'old_commit':'a'*40,
            'old_config_hash':scaler.fingerprint(old),'new_config_hash':scaler.fingerprint(new),
            'supervisor':{'unit':'sixnine-old.service'},'ledger':{'host_retirement_confirmed':True},
            'next_service_state':next_state}
        pin={'active':False,'admission':'closed','commit':'a'*40,'config_hash':scaler.fingerprint(old)}
        def read(path,*args):
            return {handoff.RECEIPT:receipt,handoff.OLD:old,'active.json':pin}[Path(path).name]
        return old,new,receipt,pin,read

    def test_resume_gate_checks_staged_identity_host_job_proof_without_releasing_leader(self):
        old,new,receipt,pin,read=self.resume_fixture()
        with patch.object(scaler,'read_json',side_effect=read), \
                patch.object(handoff,'supervisor',return_value={'pid':0}), \
                patch.object(scaler,'require_new_controller') as empty, \
                patch.object(handoff,'runtime_json',return_value=receipt['next_service_state']), \
                patch.object(Path,'exists',return_value=False),patch.object(handoff,'core',return_value={'verified':True}) as core:
            self.assertEqual(handoff.verify_resume(new,'b'*40,{}),receipt)
        empty.assert_called_once()
        self.assertEqual(core.call_args.args[2],'verify')
        self.assertNotIn('apply',core.call_args.kwargs)

    def test_unapproved_or_reused_handoff_can_never_release_leader(self):
        old,new,receipt,pin,read=self.resume_fixture()
        for phase in ['frozen','ledger_fenced','retired','activated']:
            receipt['phase']=phase
            with patch.object(scaler,'read_json',side_effect=read),patch.object(handoff,'core') as core:
                with self.subTest(phase=phase),self.assertRaises(release.ReleaseError):
                    handoff.verify_resume(new,'b'*40,{})
            core.assert_not_called()

    def test_activate_consumes_only_after_fresh_ledger_release(self):
        old,new,receipt,pin,read=self.resume_fixture()
        events=[]
        with patch.object(handoff,'verify_resume',return_value=receipt), \
                patch.object(handoff,'core',side_effect=lambda *a,**kw:events.append(('ledger',a[2],kw))), \
                patch.object(scaler,'atomic',side_effect=lambda p,v:events.append(('receipt',v['phase']))):
            result=handoff.activate_resume(new,'b'*40,{})
        self.assertEqual(events[0],('ledger','release-leader',{'apply':True}))
        self.assertEqual(events[1],('receipt','activated'))
        self.assertEqual(result['phase'],'activated')

    def test_resume_action_cannot_launch_before_offline_configuration_and_handoff_gate(self):
        for gate_ok in (False,True):
            _,config=self.configs();events=[]
            process=Mock(returncode=0)
            with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules,{'gpu_handoff':handoff,
                    'fcntl':SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *_:None)}))
                stack.enter_context(patch.object(release,'ROOT',Path(tmp)))
                stack.enter_context(patch.object(release,'check_host'))
                stack.enter_context(patch.object(release,'command',return_value=b'2.38.2'))
                stack.enter_context(patch.object(scaler,'checked_release',return_value=(
                    'b'*40,Path(tmp),{'SIXNINE_IMAGE':'sixnine-platform:'+'b'*40})))
                inputs=stack.enter_context(patch.object(scaler,'protected_inputs',return_value=config))
                for name in ('require_new_controller','atomic','validate','verify_marker'):
                    stack.enter_context(patch.object(scaler,name))
                stack.enter_context(patch.object(scaler,'compose',return_value=b'{}'))
                def gate(*a):
                    events.append('gate')
                    if not gate_ok:raise release.ReleaseError('invalid_handoff')
                stack.enter_context(patch.object(handoff,'verify_resume',side_effect=gate))
                stack.enter_context(patch.object(handoff,'activate_resume',side_effect=lambda *a:events.append('activate')))
                stack.enter_context(patch.object(scaler,'controller_control',side_effect=lambda *a:
                    events.append('validate') or {'config_valid':True,'provider_calls_enabled':False,
                        'config_hash':scaler.fingerprint(config)}))
                launch=stack.enter_context(patch.object(scaler,'launch',side_effect=lambda *a:events.append('launch') or process))
                stack.enter_context(patch.object(scaler,'marker'))
                stack.enter_context(patch.object(scaler,'wait_until_ready',side_effect=lambda *a:events.append('ready')))
                stack.enter_context(patch.object(scaler,'enable_admission',side_effect=lambda *a:events.append('open')))
                stack.enter_context(patch.object(scaler,'wait_for_controller'))
                stack.enter_context(patch.object(scaler,'close_admission'))
                stack.enter_context(patch.object(scaler,'restore_cpu',return_value={}))
                stack.enter_context(patch('sys.stdout',new=io.StringIO()))
                stack.enter_context(patch('sys.stderr',new=io.StringIO()))
                self.assertEqual(scaler.main(['resume-handoff']),0 if gate_ok else 1)
                inputs.assert_called_once_with(starting=False)
            if gate_ok:
                self.assertEqual(events,['gate','validate','activate','launch','ready','open'])
            else:
                launch.assert_not_called();self.assertEqual(events,['gate'])


if __name__ == '__main__':
    unittest.main()

"""Offline acceptance-overlay/drain regressions. Never start cloud or services."""
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1]/'deploy'/'platform'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DEPLOY/filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


validator = load('acceptance_test_validator', 'check_config.py')
with patch.dict(sys.modules, {'check_config': validator}):
    release = load('acceptance_test_release', 'release.py')
with patch.dict(sys.modules, {'check_config': validator, 'release': release}):
    acceptance = load('acceptance_test_overlay', 'gpu_acceptance.py')

EXPECTED = {'handoff_id': 'synthetic-handoff', 'worker_id': 'synthetic-worker', 'hard_deadline': 2000000000}
CONTAINER = 'a'*64


def fixture_diagnostics(config, version):
    """Synthetic fixture only; never call this with a real runtime config."""
    actual = config['services']['gpu-worker']
    expected = acceptance.worker('sixnine-platform:'+'a'*40)
    missing = '<missing>'
    return {'compose_version': version, 'worker_fields': {key:
        {'actual': actual.get(key, missing), 'expected': expected.get(key, missing)}
        for key in sorted(set(actual)|set(expected)) if actual.get(key, missing) != expected.get(key, missing)},
        'app_policy_mounts': [item for item in config['services']['app']['volumes']
            if item.get('target') == acceptance.POLICY_TARGET]}


def safe_status():
    return {'handoff_id': EXPECTED['handoff_id'], 'worker_ids': [EXPECTED['worker_id']],
        'hard_deadline': EXPECTED['hard_deadline'], 'observed_at': time.time(),
        'drained': True, 'drain_requested': True, 'upstream_idle_confirmed': True,
        'ledger_safe': True, 'active_job_ids': []}


def container(*, running=True, **state):
    return {'Config': {'Labels': {'com.docker.compose.project': 'sixnine-platform',
        'com.docker.compose.service': 'gpu-worker'}},
        'State': {'Running': running, 'Restarting': False, 'Status': 'running' if running else 'exited',
                  'ExitCode': 0, 'OOMKilled': False, **state}}


class ComposeOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose CLI unavailable; no daemon required')
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary)/'overlay.json'
            overlay.write_text(json.dumps(acceptance.overlay('sixnine-platform:'+'a'*40)), encoding='utf-8')
            empty = Path(temporary)/'empty.env'
            empty.write_text('', encoding='ascii')
            env = dict(os.environ, SIXNINE_IMAGE='sixnine-platform:'+'a'*40,
                SIXNINE_POSTGRES_IMAGE='postgres@sha256:'+'b'*64,
                SIXNINE_CADDY_IMAGE='caddy@sha256:'+'c'*64,
                SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
                SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
            result = subprocess.run(['docker', 'compose', '--env-file', str(empty),
                '-f', str(DEPLOY/'compose.yaml'), '-f', str(overlay), 'config', '--format', 'json'],
                env=env, capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError('Offline Compose rendering failed; output withheld')
            cls.config = json.loads(result.stdout)
            cls.version = subprocess.run(['docker', 'compose', 'version', '--short'],
                check=True, capture_output=True, timeout=10).stdout.decode().strip()

    def test_actual_compose_render_preserves_cpu_policy_with_exact_gpu_delta(self):
        before = copy.deepcopy(self.config)
        try:
            self.assertTrue(acceptance.validate(self.config, deployment_directory=DEPLOY, compose_version=self.version))
        except (release.ReleaseError, validator.ConfigurationError):
            # No secret file is read: this fixture uses fake image hashes and
            # fixed secret-reference paths, so precise CI diagnostics are safe.
            self.fail(json.dumps(fixture_diagnostics(self.config, self.version), sort_keys=True))
        self.assertEqual(self.config, before)
        for mount in self.config['services']['gpu-worker']['volumes']:
            self.assertTrue(mount['source'].startswith('/srv/sixnine/'))
        env = self.config['services']['gpu-worker']['environment']
        self.assertEqual(env['PUBLIC_ORIGIN'], 'https://inkseq.com')
        self.assertEqual(env['SIXNINE_CLOUD_CREATION_ENABLED'], '0')

    def test_worker_boundary_changes_fail_closed(self):
        mutations = [lambda w: w.update(user='0:0'), lambda w: w.update(privileged=True),
            lambda w: w.update(entrypoint=['sh']), lambda w: w.update(mem_limit='1073741824'),
            lambda w: w.update(stop_grace_period='3m'), lambda w: w.update(network_mode='host'),
            lambda w: w['command'].append('--unreviewed'),
            lambda w: w['volumes'][0].update(source='/'),
            lambda w: w['volumes'][-1].update(read_only=False)]
        for change in mutations:
            with self.subTest(change=change):
                value = copy.deepcopy(self.config)
                change(value['services']['gpu-worker'])
                with self.assertRaises(release.ReleaseError):
                    acceptance.validate(value, deployment_directory=DEPLOY, compose_version=self.version)

    def test_observed_compose_2382_omitted_false_requires_exact_trusted_version(self):
        # Observed in GitHub run 37178897092: the worker and app's added
        # policy mount serialize explicit false as {}, just like base binds.
        legacy = copy.deepcopy(self.config)
        for service in legacy['services'].values():
            for mount in service.get('volumes', []):
                if mount.get('type') == 'bind':
                    mount['bind'] = {}
        before = copy.deepcopy(legacy)
        for version in ('2.38.2', 'v2.38.2'):
            self.assertTrue(acceptance.validate(legacy, deployment_directory=DEPLOY, compose_version=version))
        self.assertEqual(legacy, before)
        for version in (None, '2.38.1', '2.39.0', '5.6.0'):
            with self.subTest(version=version), self.assertRaises((release.ReleaseError, validator.ConfigurationError)):
                acceptance.validate(legacy, deployment_directory=DEPLOY, compose_version=version)

    def test_legacy_normalization_never_accepts_enabled_or_missing_bind_options(self):
        for name in ('gpu-worker', 'app'):
            for options in ({'create_host_path': True}, None, {'create_host_path': False, 'propagation': 'rshared'}):
                changed = copy.deepcopy(self.config)
                mount = next(item for item in changed['services'][name]['volumes']
                    if item['target'] == acceptance.POLICY_TARGET)
                if options is None:
                    mount.pop('bind', None)
                else:
                    mount['bind'] = options
                with self.subTest(service=name, options=options), self.assertRaises(release.ReleaseError):
                    acceptance.validate(changed, deployment_directory=DEPLOY, compose_version='2.38.2')

    def test_app_base_safety_is_not_bypassed_by_overlay(self):
        for field, value in [('SIXNINE_AUTH_MODE', 'local-test'), ('SIXNINE_RENDER_ENABLED', '1'),
                             ('SIXNINE_CLOUD_CREATION_ENABLED', '1')]:
            changed = copy.deepcopy(self.config)
            changed['services']['app']['environment'][field] = value
            with self.assertRaises(validator.ConfigurationError):
                acceptance.validate(changed, deployment_directory=DEPLOY, compose_version=self.version)


class DrainTests(unittest.TestCase):
    def test_start_shares_scaler_and_acceptance_barrier(self):
        # The shared guard's real two-marker/two-service behavior is exercised
        # in test_platform_aws_deploy. This binds the legacy start to it too.
        from unittest.mock import Mock
        guard = Mock(side_effect=release.ReleaseError('gpu_acceptance_requires_explicit_safe_restore'))
        with patch.dict(sys.modules, {'deploy_approved': SimpleNamespace(require_no_gpu_acceptance=guard)}), \
                patch.object(release, 'command') as command:
            with self.assertRaisesRegex(release.ReleaseError, 'explicit_safe_restore'):
                acceptance.require_no_parallel_cycle()
        guard.assert_called_once_with(release.ROOT)
        command.assert_not_called()

    def test_operator_marker_persists_explicit_state_without_worker_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(acceptance, 'ROOT', Path(directory)):
            for state in (True, False):
                acceptance.acceptance_marker('a'*40, state)
                value = json.loads((Path(directory)/'active.json').read_text())
                self.assertEqual(set(value), {'version', 'active', 'commit', 'updated_at'})
                self.assertIs(value['active'], state)
                self.assertFalse((Path(directory)/'active.next').exists())

    def test_repeated_start_never_recreates_even_an_exited_acceptance_container(self):
        with patch.object(release, 'command', return_value=CONTAINER.encode()) as command:
            with self.assertRaisesRegex(release.ReleaseError, 'existing_acceptance_worker_requires_reconciliation'):
                acceptance.require_new_acceptance({})
            self.assertEqual(command.call_args.args[0], ['ps', '--all', '--quiet',
                '--filter', 'label=com.docker.compose.project=sixnine-platform',
                '--filter', 'label=com.docker.compose.service=gpu-worker'])
        with patch.object(release, 'command', return_value=b''):
            acceptance.require_new_acceptance({})

    def harness(self, statuses, states):
        events = []
        clock = [0.0]
        states = iter(states)
        statuses = iter(statuses)

        def command(args, **kwargs):
            events.append(('docker', args))
            if args[0] == 'inspect':
                return json.dumps([next(states)]).encode()
            self.assertEqual(args, ['kill', '--signal', 'TERM', CONTAINER])
            return b''

        def compose(directory, env, *args, **kwargs):
            events.append(('overlay', args))
            self.assertEqual(args, ('ps', '--all', '--quiet', 'gpu-worker'))
            return CONTAINER.encode()

        def control(directory, env, action):
            events.append(('control', action))
            return {'drained': False} if action == '--request-drain' else next(statuses)

        def sleep(seconds):
            clock[0] += seconds

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(release, 'compose', side_effect=lambda *a, **k: events.append(('cpu', a[2:]))))
        stack.enter_context(patch.object(release, 'wait_ready', side_effect=lambda *a, **k: events.append(('ready',))))
        stack.enter_context(patch.object(release, 'command', side_effect=command))
        stack.enter_context(patch.object(acceptance, 'compose', side_effect=compose))
        stack.enter_context(patch.object(acceptance, 'worker_control', side_effect=control))
        return events, {'drain_seconds': 2, 'exit_seconds': 1, 'clock': lambda: clock[0], 'sleep': sleep}

    def test_fresh_exact_ledger_and_upstream_confirmation_required(self):
        original = safe_status()
        self.assertTrue(acceptance.drained_status(original, EXPECTED))
        for key, value in [('observed_at', time.time()-31), ('observed_at', time.time()+30),
                           ('observed_at', float('nan')), ('drained', 1), ('ledger_safe', False),
                           ('drain_requested', False), ('upstream_idle_confirmed', False),
                           ('active_job_ids', ['unresolved']), ('worker_ids', ['another-worker']),
                           ('handoff_id', 'previous-handoff'), ('hard_deadline', 1)]:
            with self.subTest(key=key, value=value):
                self.assertFalse(acceptance.drained_status({**original, key: value}, EXPECTED))

    def test_cpu_admission_closes_before_drain_and_only_term_after_full_proof(self):
        events, kwargs = self.harness([safe_status()], [container(), container(running=False)])
        acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)
        self.assertEqual(events[:4], [('cpu', ('up', '-d', '--no-deps', 'app')), ('ready',),
            ('control', '--request-drain'), ('control', '--status')])
        commands = [event[1] for event in events if event[0] == 'docker']
        self.assertEqual(commands, [['inspect', CONTAINER], ['kill', '--signal', 'TERM', CONTAINER], ['inspect', CONTAINER]])

    def test_unknown_or_expired_proof_never_sends_signal_or_stops_worker(self):
        events, kwargs = self.harness([{'drained': False}, {'drained': False}], [])
        with self.assertRaisesRegex(release.ReleaseError, 'worker_not_safely_drained_no_stop_sent'):
            acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)
        self.assertFalse(any(event[0] in ('docker', 'overlay') for event in events))

    def test_wrong_container_identity_never_signals(self):
        wrong = container()
        wrong['Config']['Labels']['com.docker.compose.service'] = 'another-service'
        events, kwargs = self.harness([safe_status()], [wrong])
        with self.assertRaisesRegex(release.ReleaseError, 'worker_container_identity_mismatch'):
            acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)
        self.assertNotIn(('docker', ['kill', '--signal', 'TERM', CONTAINER]), events)

    def test_exit_timeout_does_not_escalate_to_kill(self):
        events, kwargs = self.harness([safe_status()], [container(), container(), container()])
        with self.assertRaisesRegex(release.ReleaseError, 'worker_exit_unconfirmed_no_forced_kill'):
            acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)
        self.assertEqual([e[1] for e in events if e[0] == 'docker' and e[1][0] == 'kill'],
                         [['kill', '--signal', 'TERM', CONTAINER]])

    def test_naturally_exited_worker_needs_proof_but_no_signal(self):
        events, kwargs = self.harness([safe_status()], [container(running=False), container(running=False)])
        acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)
        self.assertFalse(any(e[0] == 'docker' and e[1][0] == 'kill' for e in events))

    def test_oom_or_nonzero_exit_requires_reconciliation(self):
        _, kwargs = self.harness([safe_status()], [container(running=False, OOMKilled=True),
            container(running=False, OOMKilled=True)])
        with self.assertRaisesRegex(release.ReleaseError, 'worker_exit_requires_reconciliation'):
            acceptance.restore_cpu(DEPLOY, {}, EXPECTED, **kwargs)

    def test_control_actions_are_separate_processes_without_enabled_or_provider_actions(self):
        with patch.object(acceptance, 'compose', return_value=b'{"drained": false}') as compose:
            for action in ('--request-drain', '--status'):
                acceptance.worker_control(DEPLOY, {}, action)
                args = compose.call_args.args[2:]
                self.assertEqual(args[:6], ('run', '--rm', '--no-deps', '-T', '--entrypoint', 'python'))
                self.assertEqual(args[-1], action)
                self.assertNotIn('--enabled', args)
            with self.assertRaises(release.ReleaseError):
                acceptance.worker_control(DEPLOY, {}, '--enabled')


if __name__ == '__main__':
    unittest.main()

"""Cold WanGP lifecycle checks with fake SSH/runtime and temporary ledgers only."""
from dataclasses import replace
import contextlib
import hashlib
import io
import json
import socket
import socketserver
import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from studio_platform.inference.wangp_contract import EngineManifest, HostReadiness
from studio_platform.fleet import read_config as read_fleet
from studio_platform.lium_bootstrap import BootConfig, BootController, BootError
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.wangp_bootstrap import SOURCE_NAMES, WanGPSSHHost, connect_backend, read_sources, validate_report
from test_platform_repository import LedgerCase
from test_platform_lium_bootstrap import FakeFleet, POD, GPU


class WanGPBootTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.sources = self.root/'sources'; self.sources.mkdir()
        raw = (Path(__file__).resolve().parents[1]/'deploy/wangp/manifest.json').read_bytes()
        self.manifest = EngineManifest.from_dict(json.loads(raw))
        for name, content in {'wangp-manifest.json': raw, 'wangp-bootstrap.py': b'# offline fake',
                'wangp-runtime.json': b'{}', 'wangp-package.tar.gz': b'offline fixture'}.items():
            (self.sources/name).write_bytes(content)
        self.config = BootConfig(self.root/'boot', self.sources, self.root/'key', self.root/'known',
            18900, 'test-wangp', enabled=True, qualification_profile=QUEUED_TASK_PROFILE,
            execution_backend='wangp-worker', engine_manifest_digest=self.manifest.digest)
        self.repo.configure_pool('wangp-cold', max_instances=1, max_physical_gpus=1)
        self.intent = self.repo.reserve_instance_intent(self.scope, 'wangp-cold', 'cold', physical_gpus=1,
            slots=1, reserved_cost_microusd=100_000, hard_deadline=self.now+10000,
            budget_account_ids=('owner-budget',), dry_run=False, provider='lium')
        self.repo.update_instance(self.intent['id'], 'creating')
        self.repo.update_instance(self.intent['id'], 'starting', provider_instance_id=POD)
        self.host = Host(self.manifest)
        self.backend = SimpleNamespace(is_idle=lambda: True)
        self.provider = SimpleNamespace(ssh_connection=lambda *a: {})

    def boot(self, **options):
        return BootController(self.repo, self.provider, replace(self.config, **options),
            ssh_factory=lambda *a: self.host, fleet_factory=FakeFleet)

    def connect(self, boot, intent, directory, state):
        state['runtime_incarnation'] = 'a'*32
        boot._save(directory/'bootstrap-state.json', state)
        return self.backend

    def test_runtime_ready_never_claims_inference_and_fleet_is_engine_bound(self):
        boot = self.boot(fleet_enabled=True)
        with patch('studio_platform.wangp_bootstrap.connect_backend', self.connect):
            result = boot.tick(self.intent['id'])
        self.assertEqual(result['state'], 'fleet_running')
        self.assertFalse(result['generation_verified'])
        slot = boot.fleet.config.slots[0]
        self.assertEqual(slot.spec.backend, 'wangp-worker')
        self.assertEqual(slot.spec.engine_manifest_digest, self.manifest.digest)
        self.assertEqual(slot.comfy_revision, '')
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(set(self.host.identity['sources']), SOURCE_NAMES)
        # Exercise the same persisted file/parser boundary used by run_child;
        # checking only FakeFleet's in-memory config misses schema mismatches.
        fleet_path = self.config.work_dir/self.intent['id']/'fleet.json'
        persisted = read_fleet(fleet_path)
        self.assertEqual(json.loads(fleet_path.read_text())['version'], 2)
        self.assertEqual(persisted, boot.fleet.config)
        self.assertEqual(persisted.fingerprint(), boot.fleet.config.fingerprint())

    def test_lost_start_response_reconnects_original_without_install_or_start(self):
        self.host.lose_start = True
        with patch('studio_platform.wangp_bootstrap.connect_backend', self.connect):
            self.assertEqual(self.boot().tick(self.intent['id'])['state'], 'bootstrap_start_unknown')
            self.host.lose_start = False
            self.assertEqual(self.boot().tick(self.intent['id'])['state'], 'runtime_ready')
        self.assertEqual((self.host.starts, self.host.uploads), (1, 1))

    def test_native_delivery_capability_binds_boot_marker_slot_and_reconnect(self):
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        boot = self.boot(fleet_enabled=True, output_delivery=NATIVE_DELIVERY)
        with patch('studio_platform.wangp_bootstrap.connect_backend', self.connect):
            result = boot.tick(self.intent['id'])
        self.assertEqual(result['state'], 'fleet_running')
        self.assertEqual(boot.fleet.config.slots[0].spec.output_delivery, NATIVE_DELIVERY)
        self.assertEqual(self.host.identity['output_delivery'], NATIVE_DELIVERY)
        self.assertEqual(self.host.starts, 1)

    def test_wrong_manifest_and_missing_marker_never_register_or_relaunch(self):
        self.host.lose_start = True
        self.boot().tick(self.intent['id'])
        self.host.identity = None
        self.assertEqual(self.boot().tick(self.intent['id'])['state'], 'bootstrap_start_unknown')
        self.assertEqual(self.host.starts, 1)
        files, manifest = read_sources(self.config)
        report = self.host.report(); report['engine_manifest_digest'] = 'b'*64
        with self.assertRaisesRegex(BootError, 'identity_unconfirmed'):
            validate_report(self.config, report, manifest)
        with self.assertRaisesRegex(BootError, 'manifest_mismatch'):
            read_sources(replace(self.config, engine_manifest_digest='b'*64))

    def test_old_smoke_profile_cannot_authorize_wangp(self):
        for changes in ({'smoke_enabled': True}, {'qualification_profile': ''},
                        {'recipe_ids': ('h3-base-fl2va-v1', 'h3-base-ref2va-v1')}):
            with self.assertRaises(ValueError):
                replace(self.config, **changes)

    def test_changed_incarnation_retains_evidence_and_closes_transport(self):
        boot = self.boot()
        directory = self.config.work_dir/self.intent['id']; directory.mkdir(parents=True)
        token = directory/'wangp-token'; token.write_text('SYNTHETIC-ONLY-'+'x'*40); token.chmod(0o600)
        state = {'hardware': {'gpu': {'uuid': GPU}}, 'runtime_incarnation': 'b'*32}
        closed = []
        transport = SimpleNamespace(close=lambda: closed.append(True), readiness=lambda:
            HostReadiness(self.manifest.digest, self.intent['id'], 'a'*32, True))
        with patch('studio_platform.wangp_bootstrap.HTTPWanGPTransport', return_value=transport):
            with self.assertRaisesRegex(BootError, 'incarnation_changed'):
                connect_backend(boot, {**self.intent, 'provider_instance_id': POD}, directory, state)
        self.assertEqual(state['runtime_incarnation'], 'b'*32)
        self.assertEqual(closed, [True])

    def test_idle_probe_uses_identity_bound_runtime_readiness(self):
        boot = self.boot()
        with patch('studio_platform.wangp_bootstrap.connect_backend', self.connect):
            boot.tick(self.intent['id'])
        self.assertTrue(boot.idle_probe(self.intent['id'], POD).idle)
        self.backend.is_idle = lambda: False
        self.assertFalse(boot.idle_probe(self.intent['id'], POD).idle)

    def test_existing_backend_rejects_changed_incarnation_for_new_work(self):
        from studio_platform.inference.wangp import WanGPBackend
        info = HostReadiness(self.manifest.digest, self.intent['id'], 'a'*32, True)
        transport = SimpleNamespace(readiness=lambda: info)
        backend = WanGPBackend(enabled=True, slot_key=self.intent['id'], manifest=self.manifest,
            compiler=lambda *a: None, transport=transport, expected_incarnation='a'*32)
        self.assertTrue(backend.is_idle())
        info = HostReadiness(self.manifest.digest, self.intent['id'], 'b'*32, True)
        self.assertFalse(backend.is_idle())


class Host:
    def __init__(self, manifest):
        self.manifest = manifest
        self.starts = self.uploads = 0
        self.identity = None
        self.lose_start = False

    def upload(self, files):
        assert set(files) == SOURCE_NAMES
        self.uploads += 1

    def start(self, identity):
        self.starts += 1; self.identity = identity
        if self.lose_start:
            raise OSError('synthetic lost response')

    def report(self):
        return {'identity': self.identity, 'state': 'ready', 'engine_manifest_digest': self.manifest.digest,
            'source_revision': self.manifest.document['source_revision'], 'runtime_verified': True,
            'gpus': [{'uuid': GPU}], 'runtime': {'gpu_total_bytes': 180*1024**3}}

    def open_tunnel(self, port):
        pass


class SystemDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = WanGPSSHHost.__new__(WanGPSSHHost)
        self.host.config = SimpleNamespace(work_dir=self.root/'cpu')
        self.host.coordinates = {'instance_id': POD}

    def test_unknown_runtime_report_has_static_diagnosis_without_remote_details(self):
        value={'state':'unknown','phase':'runtime_start_unknown','failure_phase':'runtime_manifest',
            'code':'wangp_configuration_permissions','error_type':'ValueError','runtime_verified':False,
            'system_package_diagnostics':{'secret':'SECRET'},'download_failure':{'url':'SECRET'},
            'runtime':{'debug':'SECRET'},'log':'SECRET'}
        (self.root/'setup-status.json').write_text(json.dumps(value))
        (self.root/'sixnine-bootstrap-identity.json').write_text('{}')
        def execute(script, **kwargs):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(script.replace('/workspace/h3-studio',self.root.as_posix()),'<offline-report>','exec'),{})
            return json.loads(output.getvalue())
        self.host.run=execute
        result=self.host.report()
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['error_code'],'wangp_configuration_permissions')
        self.assertEqual(result['failure_phase'],'runtime_manifest')
        self.assertNotIn('SECRET',json.dumps(result))
        value.update(code='https://invalid/?token=SECRET',error_type={'message':'SECRET'},failure_phase='SECRET')
        (self.root/'setup-status.json').write_text(json.dumps(value))
        result=self.host.report()
        self.assertEqual(result['error_code'],'UnclassifiedBootstrapFailure')
        self.assertEqual(result['failure_phase'],'unknown')
        self.assertNotIn('SECRET',json.dumps(result))

    def test_report_preserves_old_failed_phase_and_sanitizes_package_details(self):
        value = {'state': 'failed', 'phase': 'setup_failed', 'failed_phase': 'system_package_verification',
            'code': 'system_package_mismatch', 'error_type': 'ValueError', 'log': 'SECRET',
            'system_package_diagnostics': {'total': 2, 'truncated': False, 'mismatches': [
                {'package': 'openssl', 'expected': '3.0.2', 'observed': '3.0.3', 'url': 'SECRET'},
                {'package': 'libc6:amd64', 'expected': '2.35', 'observed': 'https://private.invalid/?secret=SECRET'}]}}
        (self.root/'setup-status.json').write_text(json.dumps(value))
        (self.root/'sixnine-bootstrap-identity.json').write_text('{}')
        def execute(script, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(script.replace('/workspace/h3-studio', self.root.as_posix()), '<offline-report>', 'exec'), {})
            return json.loads(output.getvalue())
        self.host.run = execute
        result = self.host.report()
        self.assertEqual(result['failure_phase'], 'system_package_verification')
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertEqual(result['system_package_diagnostics']['mismatches'], [
            {'package': 'openssl', 'expected': '3.0.2', 'observed': '3.0.3'}])
        self.assertTrue(result['system_package_diagnostics']['truncated'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_report_projects_only_static_download_cause_and_stop_certainty(self):
        value = {'state': 'failed', 'phase': 'setup_failed', 'failure_phase': 'model_download',
            'code': 'model_download_stop_unconfirmed', 'error_type': 'ValueError',
            'download_failure': {'error_code': 'model_download_size_mismatch', 'stop_status': 'unconfirmed',
                                 'url': 'https://private.invalid/?token=SECRET'}}
        (self.root/'setup-status.json').write_text(json.dumps(value))
        (self.root/'sixnine-bootstrap-identity.json').write_text('{}')
        def execute(script, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(script.replace('/workspace/h3-studio', self.root.as_posix()), '<offline-report>', 'exec'), {})
            return json.loads(output.getvalue())
        self.host.run = execute
        result = self.host.report()
        self.assertEqual(result['error_code'], 'model_download_stop_unconfirmed')
        self.assertEqual(result['download_failure'],
            {'error_code': 'model_download_size_mismatch', 'stop_status': 'unconfirmed'})
        self.assertNotIn('SECRET', json.dumps(result))
        value['download_failure']['error_code'] = {'untrusted': 'SECRET'}
        (self.root/'setup-status.json').write_text(json.dumps(value))
        self.assertNotIn('download_failure', self.host.report())

    def test_preupload_inventory_is_saved_before_archive_transfer_and_not_overwritten(self):
        files = {name: b'{}' for name in SOURCE_NAMES}
        inventory = {'packages': {'libc6:amd64': '2.35-0ubuntu3.8', 'openssl': '3.0.2',
                                 'bad/package': 'SECRET'}, 'total': 3, 'truncated': False}
        def query(script, **kwargs):
            return inventory if 'dpkg-query' in script else {'ok': True}
        self.host.run = Mock(side_effect=query)
        class SourceHandle(io.BytesIO):
            def chmod(self, mode):
                self.permissions = mode
        remote = SimpleNamespace(open=lambda name, mode: SourceHandle(files[Path(name).name]))
        self.host.client = SimpleNamespace(open_sftp=lambda: contextlib.nullcontext(remote))
        record_path = self.host.config.work_dir/'os-observations'/(POD+'.json')
        saved = []
        def transfer(*args, **options):
            self.assertEqual(options, {'progress': None, 'should_stop': None})
            saved.append(json.loads(record_path.read_text()))
        self.host._upload_dependency = transfer
        with patch('studio_platform.wangp_bootstrap.dependency_source', return_value=('unused', 'a'*64, 1)):
            self.host.upload(files)
        self.assertEqual(saved[0]['packages'], {'libc6:amd64': '2.35-0ubuntu3.8', 'openssl': '3.0.2'})
        self.assertTrue(saved[0]['truncated'])
        self.assertNotIn('SECRET', record_path.read_text())
        original = record_path.read_bytes()
        with patch.object(self.host, 'inspect_system_packages', side_effect=AssertionError('preserve first observation')):
            self.host._capture_system_observation()
        self.assertEqual(record_path.read_bytes(), original)

    def test_inventory_failure_retains_static_code_only(self):
        self.host.run = Mock(side_effect=RuntimeError('SECRET_TOKEN https://private.invalid'))
        self.host._capture_system_observation()
        raw = (self.host.config.work_dir/'os-observations'/(POD+'.json')).read_text()
        self.assertEqual(json.loads(raw)['state'], 'unavailable')
        self.assertNotIn('SECRET', raw)
        self.assertNotIn('private.invalid', raw)


class SSHRecoveryTests(unittest.TestCase):
    def command_host(self, *, never_ack=False):
        from studio_platform.lium_bootstrap import SSHHost

        class Channel:
            def __init__(self):
                self.closed = threading.Event()
                self.commands = []
                self.output = b'{"ok":true}'
                self.stderr = b'synthetic diagnostic; must not become a response'
                self.io_timeout = None

            def settimeout(self, value):
                self.io_timeout = value

            def exec_command(self, command):
                self.commands.append(command)
                if never_ack:
                    # A broken guard fails this test after two seconds instead
                    # of hanging the test runner indefinitely.
                    if not self.closed.wait(2):
                        raise AssertionError('Command ACK wait was never interrupted')
                    raise EOFError('Synthetic channel closed before server ACK')

            def close(self):
                self.closed.set()

            def exit_status_ready(self):
                return True

            def recv_ready(self):
                return bool(self.output)

            def recv_stderr_ready(self):
                return bool(self.stderr)

            def recv(self, maximum):
                value, self.output = self.output[:maximum], self.output[maximum:]
                return value

            def recv_stderr(self, maximum):
                value, self.stderr = self.stderr[:maximum], self.stderr[maximum:]
                return value

            def recv_exit_status(self):
                return 0

        channel = Channel()
        transport = SimpleNamespace(open_session=Mock(return_value=channel))
        host = SSHHost.__new__(SSHHost)
        host.client = SimpleNamespace(get_transport=lambda: transport)
        host.ensure_connected = Mock()
        host.start = Mock(side_effect=AssertionError('Command transport must never launch a runtime'))
        return host, channel, transport

    def test_command_without_server_ack_times_out_without_replay_or_launch(self):
        host, channel, transport = self.command_host(never_ack=True)
        started = time.monotonic()
        with self.assertRaisesRegex(BootError, 'bootstrap_ssh_command_timeout'):
            host.run('print("offline")', timeout=.1)
        self.assertLess(time.monotonic()-started, 1.5)
        self.assertTrue(channel.closed.is_set())
        self.assertEqual(len(channel.commands), 1)
        transport.open_session.assert_called_once()
        self.assertTrue(0 < transport.open_session.call_args.kwargs['timeout'] <= .1)
        host.start.assert_not_called()

    def test_command_reads_json_and_discards_stderr_with_bounded_channel(self):
        host, channel, transport = self.command_host()
        self.assertEqual(host.run('print("offline")', timeout=1), {'ok': True})
        self.assertTrue(channel.closed.is_set())
        self.assertTrue(0 < channel.io_timeout <= 1)
        self.assertEqual(len(channel.commands), 1)
        transport.open_session.assert_called_once()
        self.assertTrue(0 < transport.open_session.call_args.kwargs['timeout'] <= 1)
        host.start.assert_not_called()

    def test_targon_command_uses_noninteractive_privilege_and_preserves_shell_quoting(self):
        host, channel, _ = self.command_host()
        host.config = SimpleNamespace(provider='targon')
        self.assertEqual(host.run('print("$HOME; synthetic")',timeout=1),{'ok':True})
        import shlex
        self.assertEqual(shlex.split(channel.commands[0]),['sudo','-n','python3','-c','print("$HOME; synthetic")'])
        host.start.assert_not_called()

    def test_existing_tunnel_reuses_pinned_connection_without_replaying_start(self):
        from studio_platform.lium_bootstrap import SSHHost
        import tempfile
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(self.request.recv(64))
        class Transport:
            active = True
            def is_active(self): return self.active
            def is_authenticated(self): return self.active
            def open_channel(self, *args, **kw):
                return socket.create_connection(server.server_address, timeout=2)
        class Client:
            def __init__(self):
                self.transport = Transport(); self.policy = None
                clients.append(self)
            def load_host_keys(self, path): pass
            def set_missing_host_key_policy(self, policy): self.policy = policy
            def connect(self, host, **kw): connections.append((host, kw['port']))
            def get_transport(self): return self.transport
            def close(self): self.transport.active = False
        clients, connections = [], []
        with tempfile.TemporaryDirectory() as temporary, socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            root = Path(temporary)
            config = SimpleNamespace(known_hosts_file=root/'known', ssh_key_file=root/'key', trust_first_host_key=True)
            with patch('paramiko.SSHClient', Client):
                host = SSHHost(config, {'host': 'same-provider-host', 'port': 2345})
                try:
                    # Port zero is used only by this local socket test.
                    host.open_tunnel(0)
                    tunnel = host.tunnel
                    with socket.create_connection(tunnel.server_address, timeout=2) as peer:
                        peer.sendall(b'before'); self.assertEqual(peer.recv(64), b'before')
                    clients[0].transport.active = False
                    host.open_tunnel(tunnel.server_address[1])
                    self.assertIs(host.tunnel, tunnel)
                    with socket.create_connection(tunnel.server_address, timeout=2) as peer:
                        peer.sendall(b'after'); self.assertEqual(peer.recv(64), b'after')
                    self.assertEqual(connections, [('same-provider-host', 2345)]*2)
                    self.assertEqual(type(clients[0].policy).__name__, 'AutoAddPolicy')
                    self.assertEqual(type(clients[1].policy).__name__, 'RejectPolicy')
                finally:
                    host.close(); server.shutdown()


if __name__ == '__main__':
    unittest.main()

"""Fixed-document deployment contract; fake AWS and host only, no network."""

import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tools import deploy_aws_release as runner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy' / 'platform'))
host = importlib.import_module('deploy_approved')
COMMIT = 'a' * 40
COMMAND = 'a1234567-1234-1234-1234-123456789abc'


class RunnerTests(unittest.TestCase):
    def api(self, statuses):
        api = mock.Mock()
        api.send_command.return_value = {'Command': {'CommandId': COMMAND}}
        api.get_command_invocation.side_effect = statuses
        return api

    def success(self, commit=COMMIT):
        return {
            'Status': 'Success',
            'DocumentName': runner.DOCUMENT,
            'DocumentVersion': '2',
            'StandardOutputContent': json.dumps(
                {'state': 'approved_application_release_healthy', 'commit': commit}
            ),
        }

    def test_exact_instance_document_version_and_commit_no_arbitrary_commands(self):
        api = self.api([{'Status': 'Pending'}, self.success()])
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.deploy(COMMIT, api=api, sleep=lambda _: None)
        params = api.send_command.call_args.kwargs
        self.assertEqual(params['InstanceIds'], [runner.INSTANCE])
        self.assertEqual(params['DocumentName'], 'Sixnine-DeployApprovedRelease')
        self.assertEqual(params['DocumentVersion'], '2')
        self.assertEqual(params['Parameters'], {'Commit': [COMMIT]})
        self.assertNotIn('ServiceRoleArn', params)
        self.assertEqual(result['state'], 'deployment_completed')

    def test_unsafe_commit_rejected_before_api(self):
        api = mock.Mock()
        for value in ('main', COMMIT + ';touch /tmp/x', '$HOME', '../' + COMMIT):
            with self.assertRaises(ValueError):
                runner.deploy(value, api=api)
        api.send_command.assert_not_called()

    def test_old_document_receipt_cannot_confirm_new_helper_deployment(self):
        receipt = {**self.success(), 'DocumentVersion': '1'}
        api = self.api([receipt])
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, 'document version'),
        ):
            runner.deploy(COMMIT, resume_command=COMMAND, api=api)
        api.send_command.assert_not_called()

    def test_resume_does_not_submit_again_or_accept_another_receipt(self):
        api = self.api([self.success('b' * 40)])
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, 'does not match'),
        ):
            runner.deploy(COMMIT, resume_command=COMMAND, api=api)
        api.send_command.assert_not_called()

    def test_failed_remote_output_is_never_printed_or_resubmitted(self):
        api = self.api(
            [{'Status': 'Failed', 'StandardErrorContent': 'synthetic-private-output'}]
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(RuntimeError):
            runner.deploy(COMMIT, api=api)
        self.assertNotIn('synthetic-private-output', out.getvalue())
        self.assertEqual(api.send_command.call_count, 1)

    def test_lost_send_response_not_retried_automatically(self):
        api = mock.Mock()
        api.send_command.side_effect = TimeoutError('synthetic-freeform-error')
        with self.assertRaisesRegex(RuntimeError, 'outcome unknown') as error:
            runner.deploy(COMMIT, api=api)
        self.assertNotIn('synthetic-freeform-error', str(error.exception))
        self.assertEqual(api.send_command.call_count, 1)
        api.get_command_invocation.assert_not_called()

    def test_timeout_does_not_cancel_a_potentially_running_release(self):
        api = self.api([{'Status': 'InProgress'}])
        times = iter((0, 0, 2001))
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, 'not cancelled'),
        ):
            runner.deploy(
                COMMIT, api=api, clock=lambda: next(times), sleep=lambda _: None
            )
        api.cancel_command.assert_not_called()


class HostAndDocumentTests(unittest.TestCase):
    def test_active_unknown_scaler_or_running_controller_blocks_cd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'gpu-scaler'
            folder.mkdir()
            marker = folder / 'active.json'
            with (
                mock.patch.object(host.release, 'regular'),
                mock.patch.object(host.release, 'command', return_value=b'') as command,
            ):
                for value in ({'version': 1, 'active': True}, {'version': 1}, {}):
                    marker.write_text(json.dumps(value))
                    with self.assertRaisesRegex(
                        host.release.ReleaseError, 'explicit_safe_restore'
                    ):
                        host.require_no_gpu_acceptance(root)
                command.assert_not_called()
                marker.write_text(json.dumps({'version': 1, 'active': False}))
                command.side_effect = [b'', b'controller-id']
                with self.assertRaisesRegex(
                    host.release.ReleaseError, 'worker_still_running'
                ):
                    host.require_no_gpu_acceptance(root)
                self.assertIn(
                    'label=com.docker.compose.service=gpu-controller',
                    command.call_args.args[0],
                )

    def test_active_unknown_and_running_acceptance_block_routine_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'gpu-acceptance'
            folder.mkdir()
            marker = folder / 'active.json'
            with (
                mock.patch.object(host.release, 'regular'),
                mock.patch.object(host.release, 'command', return_value=b'') as command,
            ):
                for value in ({'version': 1, 'active': True}, {'version': 1}, {}):
                    marker.write_text(json.dumps(value))
                    with self.assertRaisesRegex(
                        host.release.ReleaseError, 'explicit_safe_restore'
                    ):
                        host.require_no_gpu_acceptance(root)
                command.assert_not_called()
                marker.write_text(json.dumps({'version': 1, 'active': False}))
                host.require_no_gpu_acceptance(root)
                command.return_value = b'container-id'
                with self.assertRaisesRegex(
                    host.release.ReleaseError, 'worker_still_running'
                ):
                    host.require_no_gpu_acceptance(root)

    def test_active_acceptance_stops_before_fetch_or_apply_under_host_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release-state.json').write_text(json.dumps({'current': 'b' * 40}))
            (root / 'gpu-acceptance').mkdir()
            (root / 'gpu-acceptance' / 'active.json').write_text(
                json.dumps({'version': 1, 'active': True})
            )
            fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=mock.Mock())
            with (
                mock.patch.dict(sys.modules, {'fcntl': fake_fcntl}),
                mock.patch.object(host.release, 'check_host'),
                mock.patch.object(host.release, 'regular'),
                mock.patch.object(host.fetch_release_s3, 'fetch') as fetch,
                mock.patch.object(host.release, 'protected_directory'),
                mock.patch.object(host.release, 'command', return_value=b''),
                mock.patch.object(host.release, 'apply_locked') as apply,
            ):
                with self.assertRaisesRegex(
                    host.release.ReleaseError, 'explicit_safe_restore'
                ):
                    host.deploy(COMMIT, root=root)
                fetch.assert_not_called()
                apply.assert_not_called()

    def test_document_has_one_env_commit_and_fixed_root_entry(self):
        for name, helper in (
            ("ssm-deploy-document.json", "deploy_approved.py"),
            ("ssm-frontend-document.json", "deploy_frontend.py"),
        ):
            with self.subTest(document=name):
                doc = json.loads((ROOT / 'deploy/platform/ec2' / name).read_text())
                self.assertEqual(set(doc['parameters']), {'Commit'})
                self.assertEqual(
                    doc['parameters']['Commit']['allowedPattern'], '^[0-9a-f]{40}$'
                )
                self.assertEqual(
                    doc['parameters']['Commit']['interpolationType'], 'ENV_VAR'
                )
                self.assertEqual(len(doc['mainSteps']), 1)
                commands = doc['mainSteps'][0]['inputs']['runCommand']
                self.assertNotIn('{{', '\n'.join(commands))
                self.assertEqual(
                    commands[-1],
                    'exec /usr/bin/python3 /opt/sixnine-release-origin-v1/'
                    + helper
                    + ' "$SSM_Commit"',
                )

    def test_host_rejects_first_publication_and_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / 'release-state.json'
            state_file.write_text(json.dumps({'current': None, 'pending': COMMIT}))
            fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=mock.Mock())
            with (
                mock.patch.dict(sys.modules, {'fcntl': fake_fcntl}),
                mock.patch.object(host.release, 'check_host'),
                mock.patch.object(host.release, 'regular'),
                mock.patch.object(host.fetch_release_s3, 'fetch') as fetch,
                mock.patch.object(host.release, 'apply_locked') as apply,
            ):
                with self.assertRaisesRegex(host.release.ReleaseError, 'first_release'):
                    host.deploy(COMMIT, root=root)
                fetch.assert_not_called()
                apply.assert_not_called()
            self.assertIsNone(json.loads(state_file.read_text())['current'])

    def test_host_fetch_failure_never_applies_or_self_approves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release-state.json').write_text(json.dumps({'current': 'b' * 40}))
            fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=mock.Mock())
            with (
                mock.patch.dict(sys.modules, {'fcntl': fake_fcntl}),
                mock.patch.object(host.release, 'check_host'),
                mock.patch.object(host.release, 'regular'),
                mock.patch.object(
                    host.fetch_release_s3,
                    'fetch',
                    side_effect=host.release.ReleaseError(
                        'independent_release_approval_missing'
                    ),
                ),
                mock.patch.object(host.release, 'command', return_value=b''),
                mock.patch.object(host.release, 'apply_locked') as apply,
            ):
                with self.assertRaises(host.release.ReleaseError):
                    host.deploy(COMMIT, root=root)
                apply.assert_not_called()
            self.assertFalse((root / 'approved-releases').exists())


if __name__ == '__main__':
    unittest.main()

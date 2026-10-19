"""Native rehearsal keeps its original disconnected disposable DB boundary."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class WorkflowRehearsalTests(unittest.TestCase):
    def test_sdk_restore_uses_same_offline_copy_and_readonly_evidence_without_provider(
        self,
    ):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            'workflow_rehearsal_fixture', root / 'deploy/platform/offline_rehearsal.py'
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                'release': SimpleNamespace(ROOT=Path('/private/root')),
                'deployment_target': SimpleNamespace(),
            },
        ):
            spec.loader.exec_module(module)
        commit = 'a' * 40
        migrate = [
            'run',
            '--pull',
            'never',
            '--rm',
            '--network',
            'none',
            '--read-only',
            '--cap-drop',
            'ALL',
            '--memory',
            '1g',
            '--mount',
            'type=volume,src=original-socket,dst=/var/run/postgresql,readonly',
            '--env',
            'SIXNINE_DATABASE_URL=postgresql+psycopg://postgres@/rehearsal?host=/var/run/postgresql',
            '--env',
            'SIXNINE_GENERATION_ENABLED=0',
            '--env',
            'SIXNINE_EXECUTION_BACKEND=disabled',
            'sixnine-platform:' + commit,
            'python',
            '-m',
            'studio_platform.manage',
            'init-db',
        ]
        command = module.workflow_restore_command(
            {'migrate': migrate}, Path('/private/incoming'), commit
        )
        self.assertEqual(command[: len(migrate) - 5], migrate[: len(migrate) - 5])
        self.assertIn(
            'type=bind,src=' + str(Path('/private/incoming')) + ',dst=/backup,readonly',
            command,
        )
        self.assertIn(
            'type=volume,src=original-socket,dst=/var/run/postgresql,readonly', command
        )
        self.assertEqual(
            command[-6:],
            [
                'python',
                '-m',
                'studio_platform.workflow_backup',
                'restore-rehearsal',
                '--directory',
                '/backup',
            ],
        )
        self.assertIn('--network', command)
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertFalse(any('AWS_' in arg or 'docker.sock' in arg for arg in command))

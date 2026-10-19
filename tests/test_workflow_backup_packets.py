"""Original portable-copy pipeline: SDK sidecar never bypasses file/hash proofs."""

import json
from unittest import TestCase
from unittest.mock import patch

from studio_platform.backup import BackupError, _write_json
from studio_platform import backup_remote as remote
from studio_platform.workflow_backup import (
    CATALOG_SHA256,
    FORMAT,
    inspect_file,
    tables,
)
import test_platform_backup as fixtures
import test_platform_backup_remote as remote_fixtures


class WorkflowBackupPacketTests(TestCase):
    setUp = fixtures.BackupTests.setUp
    backup = fixtures.BackupTests.backup
    target = remote_fixtures.RemoteBackupTests.target

    def sidecar(self, source):
        value = dict(
            format=FORMAT,
            catalog_sha256=CATALOG_SHA256,
            tables={name: [] for name in tables()},
        )
        value['tables']['dbos_migrations'] = [{'version': '123'}]
        _write_json(source / 'workflow-state.json', value)
        manifest = json.loads((source / 'manifest.json').read_bytes())
        manifest['workflow_state'] = inspect_file(source)
        (source / 'manifest.json').write_text(json.dumps(manifest))
        return value

    def test_original_copy_recovers_unknown_sidecar_put_once_then_restores_held_bytes(
        self,
    ):
        source = self.backup()
        self.sidecar(source)
        target = self.target()
        client = remote_fixtures.FakeS3(target)
        client.fail_after = 'workflow-state.json'
        with patch.dict('sys.modules', {'dbos': None, 'alembic': None}):
            receipt = remote.copy_backup(
                source, target, self.root / 'receipt', client=client
            )
            result = remote.restore_copy(
                target,
                receipt,
                self.root / 'download',
                self.root / 'recovery',
                client=client,
            )
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(client.writes.count(target.prefix + '/workflow-state.json'), 1)
        self.assertEqual(
            (source / 'workflow-state.json').read_bytes(),
            (self.root / 'recovery/workflow-state.json').read_bytes(),
        )
        hold = json.loads((self.root / 'recovery/RECOVERY-HOLD.json').read_bytes())
        self.assertEqual(hold['workflow_state']['execution_restore'], 'hold')
        self.assertFalse(hold['execution_enabled'])
        self.assertNotIn(
            self.canary.encode(), (source / 'workflow-state.json').read_bytes()
        )

    def test_unreferenced_or_tampered_sidecar_is_rejected_before_remote_write(self):
        source = self.backup()
        value = self.sidecar(source)
        value['tables']['workflow_status'] = [{'extra': 'not-a-valid-row'}]
        (source / 'workflow-state.json').write_text(json.dumps(value))
        client = remote_fixtures.FakeS3(self.target())
        with self.assertRaises(BackupError):
            remote.copy_backup(
                source, self.target(), self.root / 'refused', client=client
            )
        self.assertFalse(client.writes)
        manifest = json.loads((source / 'manifest.json').read_bytes())
        manifest.pop('workflow_state')
        (source / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BackupError, 'backup_workflow_state_unreferenced'):
            remote.plan_copy(source, self.target())

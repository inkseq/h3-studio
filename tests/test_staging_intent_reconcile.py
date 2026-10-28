"""Original refusal recovery boundaries, synthetic files and no service calls."""

import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import reconcile_staging_intent as recovery


class ContainerObservationTests(unittest.TestCase):
    def rows(self):
        return [
            {
                'id': service,
                'service': service,
                'running': True,
                'health': None if service == 'caddy' else 'healthy',
            }
            for service in ('app', 'controller', 'db', 'dstack', 'caddy')
        ]

    def observe(self, rows):
        with patch.object(
            recovery.subprocess,
            'run',
            side_effect=[
                SimpleNamespace(stdout=' '.join(row['id'] for row in rows)),
                SimpleNamespace(stdout='\n'.join(json.dumps(row) for row in rows)),
            ],
        ):
            return recovery.containers()

    def test_missing_health_is_supported_but_unhealthy_still_refuses(self):
        rows = self.rows()
        self.assertEqual(len(self.observe(rows)), 5)
        rows[0]['health'] = 'unhealthy'
        with self.assertRaisesRegex(ValueError, 'container_unhealthy'):
            self.observe(rows)

    def test_required_service_must_still_be_running(self):
        rows = self.rows()
        rows[0]['running'] = False
        with self.assertRaisesRegex(ValueError, 'required_service_missing'):
            self.observe(rows)


class RootOwnedStat:
    """Synthetic root ownership only; retain real inode/mode/link/race evidence."""

    st_uid = 0

    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)


@unittest.skipIf(
    os.name == 'nt', 'Host recovery requires Linux flock and root metadata'
)
class StagingIntentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        # Temp fixtures can be beneath /tmp; production checks all parent owners.
        self.parents = patch.object(recovery, 'protected_dir')
        self.parents.start()
        self.addCleanup(self.parents.stop)
        original_lstat = Path.lstat
        self.owners = patch.object(
            Path, 'lstat', lambda path: RootOwnedStat(original_lstat(path))
        )
        self.owners.start()
        self.addCleanup(self.owners.stop)
        self.observe = lambda: [{'id': 'original', 'image': 'image', 'running': True}]
        self.proof = self.root / 'proof.json'
        self.put('proof.json', recovery.PROOF)
        self.put(
            'deployment-target.json',
            {'version': 1, 'target': 'staging', 'instance_id': recovery.INSTANCE},
        )
        self.put(
            'environment-release-intent.json',
            {
                'version': 1,
                'commit': recovery.FAILED,
                'candidate_sha256': recovery.CANDIDATE,
                'state': 'in_progress',
            },
        )
        self.put(
            'release-state.json', {'current': recovery.CURRENT, 'status': 'app_ready'}
        )
        self.put('migration/active.json', {'current': recovery.CURRENT, 'active': True})
        self.put('frontend/current.json', {'commit': recovery.CURRENT})
        self.put('migration/paths.json', {'version': 1})
        self.put('release-retention-pins.json', {'version': 1, 'commits': ['a' * 40]})
        (self.root / 'release.lock').touch(mode=0o600)
        hashes = {}
        for name in recovery.HELPERS:
            (self.helpers / name).write_bytes(name.encode())
            hashes[name] = recovery.digest(name.encode())
        self.hashes = patch.object(recovery, 'HELPERS', hashes)
        self.hashes.start()
        self.addCleanup(self.hashes.stop)

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(recovery.encode(value))
        path.chmod(0o600)

    def run_recovery(self, expected=None, observe=None):
        return recovery.run(
            self.root,
            self.helpers,
            self.proof,
            expected,
            observe=observe or self.observe,
        )

    def test_plan_only_and_apply_preserve_original_failure_and_every_other_marker(self):
        before = {name: (self.root / name).read_bytes() for name in recovery.MARKERS}
        plan = self.run_recovery()
        self.assertFalse((self.root / recovery.OP).exists())
        self.assertEqual(
            before, {name: (self.root / name).read_bytes() for name in recovery.MARKERS}
        )
        receipt = self.run_recovery(plan['plan_sha256'])
        self.assertEqual(receipt['state'], 'original_refusal_archived_not_deployed')
        self.assertEqual(
            (self.root / recovery.OP / 'original-intent.json').read_bytes(),
            before['environment-release-intent.json'],
        )
        self.assertFalse((self.root / 'environment-release-intent.json').exists())
        self.assertEqual(
            json.loads((self.root / 'release-retention-pins.json').read_text())[
                'commits'
            ],
            sorted(['a' * 40, recovery.FAILED]),
        )
        for name in recovery.MARKERS:
            if name not in (
                'environment-release-intent.json',
                'release-retention-pins.json',
            ):
                self.assertEqual((self.root / name).read_bytes(), before[name])
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.run_recovery(plan['plan_sha256'])

    def test_changed_snapshot_writes_nothing(self):
        plan = self.run_recovery()
        self.put('migration/paths.json', {'version': 2})
        with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
            self.run_recovery(plan['plan_sha256'])
        self.assertFalse((self.root / recovery.OP).exists())

    def test_wrong_or_unknown_original_failure_refuses(self):
        for field, value in [
            ('Status', 'InProgress'),
            ('CommandId', 'other'),
            ('ResponseCode', 0),
            ('StandardErrorContent', 'other failure'),
        ]:
            proof = copy.deepcopy(recovery.PROOF)
            proof[field] = value
            self.put('proof.json', proof)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, 'original_failure_proof_required'),
            ):
                self.run_recovery()

    def test_production_and_other_instance_refuse(self):
        for target in (
            {'target': 'production', 'instance_id': recovery.INSTANCE},
            {'target': 'staging', 'instance_id': 'other'},
        ):
            self.put('deployment-target.json', target)
            with self.assertRaisesRegex(ValueError, 'staging_only'):
                self.run_recovery()

    def test_partial_operation_is_never_replayed(self):
        plan = self.run_recovery()
        (self.root / recovery.OP).mkdir()
        with self.assertRaisesRegex(ValueError, 'operation_exists_reconcile_only'):
            self.run_recovery(plan['plan_sha256'])

    def test_busy_release_lock_refuses(self):
        import fcntl

        with (self.root / 'release.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_recovery()

    def test_pending_or_changed_runtime_refuses(self):
        for marker, field, value, error in (
            ('release-state.json', 'pending', recovery.FAILED, 'platform_changed'),
            ('migration/active.json', 'pending', recovery.FAILED, 'migration_changed'),
            ('frontend/current.json', 'commit', recovery.FAILED, 'frontend_changed'),
            ('environment-release-intent.json', 'commit', 'b' * 40, 'intent_changed'),
        ):
            path = self.root / marker
            original = path.read_bytes()
            item = json.loads(original)
            item[field] = value
            self.put(marker, item)
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, error):
                self.run_recovery()
            path.write_bytes(original)

    def test_symlink_or_hardlink_refuses(self):
        path = self.root / 'migration/paths.json'
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.proof)
        with self.assertRaisesRegex(ValueError, 'unprotected_file'):
            self.run_recovery()
        path.unlink()
        path.write_bytes(original)
        os.link(path, self.root / 'other-link')
        with self.assertRaisesRegex(ValueError, 'unprotected_file'):
            self.run_recovery()

    def test_changed_container_after_pin_preserves_intent_and_refuses_retry(self):
        plan = self.run_recovery()
        rows = iter([self.observe(), [{'id': 'replacement'}]])
        with self.assertRaisesRegex(ValueError, 'containers_changed'):
            self.run_recovery(plan['plan_sha256'], lambda: next(rows))
        self.assertTrue((self.root / 'environment-release-intent.json').exists())
        self.assertIn(
            recovery.FAILED,
            json.loads((self.root / 'release-retention-pins.json').read_text())[
                'commits'
            ],
        )
        self.assertFalse((self.root / recovery.OP / 'completed.json').exists())

    def test_changed_helper_refuses(self):
        (self.helpers / 'migration_check.py').write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, 'trusted_helpers_changed'):
            self.run_recovery()

    @unittest.skipIf(
        hasattr(os, 'geteuid') and os.geteuid() == 0, 'Needs a non-root fixture owner'
    )
    def test_real_non_root_file_owner_is_rejected(self):
        self.owners.stop()
        with self.assertRaisesRegex(ValueError, 'unprotected_file'):
            self.run_recovery()

    def test_failure_after_archive_keeps_pin_and_never_fabricates_completion(self):
        plan = self.run_recovery()
        original_save = recovery.save

        def interrupted(path, raw):
            if path.name == 'completed.json':
                raise OSError('synthetic interruption')
            original_save(path, raw)

        with patch.object(recovery, 'save', interrupted), self.assertRaises(OSError):
            self.run_recovery(plan['plan_sha256'])
        operation = self.root / recovery.OP
        self.assertEqual(
            recovery.digest((operation / 'original-intent.json').read_bytes()),
            recovery.INTENT_SHA,
        )
        self.assertIn(
            recovery.FAILED,
            json.loads((self.root / 'release-retention-pins.json').read_text())[
                'commits'
            ],
        )
        self.assertFalse((operation / 'completed.json').exists())
        with self.assertRaises(FileNotFoundError):
            self.run_recovery(plan['plan_sha256'])

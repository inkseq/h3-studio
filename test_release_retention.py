"""Isolated artifact/identity retention proof, no AWS/GPU/service calls."""
from contextlib import ExitStack
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
import shutil
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'deploy/platform'))
retention = importlib.import_module('release_retention')
release = retention.release


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        # CI owns the fixture rather than root. Retain all actual type, link,
        # inode, permission and content checks while emulating ownership only.
        if os.name == 'posix':
            original = Path.lstat
            def root_owned(path, *args, **kwargs):
                real = original(path, *args, **kwargs)
                fields = {name: getattr(real, name) for name in dir(real) if name.startswith('st_')}
                fields['st_uid'] = 0
                return types.SimpleNamespace(**fields)
            self.stack.enter_context(patch.object(Path, 'lstat', root_owned))
        self.stack.enter_context(patch.dict(sys.modules, {'fetch_release_s3': types.SimpleNamespace(BUCKET='fixture')}))
        for directory in ('releases', 'incoming', 'approved-releases', 'frontend/releases', 'approved-frontends'):
            (self.root / directory).mkdir(parents=True)
        self.current, self.previous, self.old, self.front = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        self.state({'current': self.current, 'previous': self.previous, 'pending': None, 'status': 'app_ready'})
        self.s3, self.image_details, self.containers, self.calls = {}, [], [], []
        for commit in (self.current, self.previous, self.old):
            self.platform(commit)
        self.frontend(self.front)
        (self.root / 'frontend/current.json').write_text(json.dumps({'commit': self.current}))
        (self.root / 'frontend/previous.json').write_text(json.dumps({'commit': self.front}))
        (self.root / 'platform-data').mkdir()
        (self.root / 'platform-data/user-media').write_bytes(b'UNIQUE_MEDIA')
        (self.root / 'postgres').mkdir()
        (self.root / 'postgres/db').write_bytes(b'UNIQUE_DATABASE')
        self.api = Mock()
        self.api.head_object.side_effect = lambda *, Bucket, Key: self.s3[Key]

    def state(self, value):
        (self.root / 'release-state.json').write_text(json.dumps(value))

    def register(self, commit, prefix, directory):
        for path in directory.iterdir():
            if path.is_file():
                self.s3[f'releases/{commit}/{prefix}{path.name}'] = {
                    'ContentLength': path.stat().st_size,
                    'Metadata': {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                    'ServerSideEncryption': 'AES256'}

    def platform(self, commit):
        folder = self.root / 'releases' / commit; folder.mkdir()
        config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'type': 'layers', 'diff_ids': []},
            'config': {'Labels': {'org.opencontainers.image.revision': commit}}}).encode()
        image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, data in ((image_id[7:] + '.json', config), ('manifest.json', json.dumps([{
                    'Config': image_id[7:] + '.json', 'RepoTags': ['sixnine-platform:' + commit], 'Layers': []}]).encode())):
                member = tarfile.TarInfo(name); member.size = len(data); tar.addfile(member, io.BytesIO(data))
        for name in release.FILES:
            (folder / name).write_bytes(gzip.compress(archive.getvalue()) if name == 'image.tar.gz' else b'INERT')
        manifest = {'commit': commit, 'image': 'sixnine-platform:' + commit, 'image_id': image_id,
            'files': {name: release.checksum(folder / name) for name in release.FILES}}
        (folder / 'release-manifest.json').write_text(json.dumps(manifest))
        (self.root / 'approved-releases' / (commit + '.sha256')).write_text(release.checksum(folder / 'release-manifest.json'))
        self.register(commit, '', folder)
        self.image_details.append({'Id': image_id, 'RepoTags': ['sixnine-platform:' + commit], 'RepoDigests': []})
        return folder, image_id

    def frontend(self, commit):
        folder = self.root / 'frontend/releases' / commit; folder.mkdir()
        (folder / 'index.html').write_bytes(b'FRONTEND')
        value = {'version': 1, 'commit': commit, 'api_contract': 'sixnine-web-v1', 'api_compatibility': 'e' * 64,
            'archive_sha256': 'f' * 64, 'archive_bytes': 128,
            'files': {'index.html': {'sha256': release.checksum(folder / 'index.html'), 'size': 8}}}
        (folder / 'frontend-manifest.json').write_text(json.dumps(value))
        (self.root / 'approved-frontends' / (commit + '.sha256')).write_text(release.checksum(folder / 'frontend-manifest.json'))
        self.register(commit, 'frontend/', folder)
        self.s3[f'releases/{commit}/frontend/frontend.tar.gz'] = {'ContentLength': 128,
            'Metadata': {'sha256': 'f' * 64}, 'ServerSideEncryption': 'AES256'}

    def command(self, args, *, environment, timeout=180):
        self.calls.append(args)
        if args[:2] == ['ps', '--all']:
            return ('\n'.join(item['Id'] for item in self.containers)).encode()
        if args[0] == 'inspect':
            return json.dumps(self.containers).encode()
        if args[:2] == ['image', 'ls']:
            return ('\n'.join(item['Id'] for item in self.image_details)).encode()
        if args[:2] == ['image', 'inspect']:
            return json.dumps(self.image_details).encode()
        return b''

    def plan(self):
        return retention.plan_locked(self.root, api=self.api, command=self.command)

    def test_default_plan_is_read_only_and_keeps_independent_rollback(self):
        value = self.plan()
        self.assertEqual(value['kept_commits'], sorted((self.current, self.previous, self.front)))
        self.assertEqual([item['relative'] for item in value['candidates']], ['releases/' + self.old])
        self.assertEqual(value['image_candidates'], [self.image_details[2]['Id']])
        self.assertTrue((self.root / 'releases' / self.old).exists())
        self.assertFalse(any(args[:2] in (['image', 'rm'], ['builder', 'prune']) for args in self.calls))

    def test_missing_or_wrong_remote_restore_proof_retains_old_files_and_image(self):
        self.s3[f'releases/{self.old}/image.tar.gz']['Metadata']['sha256'] = '0' * 64
        value = self.plan()
        self.assertEqual(value['candidates'], [])
        self.assertEqual(value['image_candidates'], [])
        self.assertIn({'relative': 'releases/' + self.old, 'reason': 'unproven_or_partial_artifact'}, value['retained'])

    def test_pending_unknown_state_blocks_before_any_delete(self):
        self.state({'current': self.current, 'previous': self.previous, 'pending': self.old, 'status': 'deploying'})
        with self.assertRaisesRegex(release.ReleaseError, 'requires_reconciliation'):
            self.plan()
        self.api.head_object.assert_not_called()
        self.assertTrue((self.root / 'releases' / self.old).exists())

    def test_real_container_bind_and_stopped_image_are_kept(self):
        self.containers = [{'Id': '1' * 64, 'Image': self.image_details[2]['Id'], 'State': {'Running': False},
            'Config': {'Labels': {}}, 'Mounts': [{'Type': 'bind', 'Source': str(self.root / 'releases' / self.old / 'compose.yaml')}]}]
        value = self.plan()
        self.assertIn(self.old, value['kept_commits'])
        self.assertEqual(value['image_candidates'], [])

    def test_extra_files_links_and_partial_staging_are_not_removed(self):
        (self.root / 'releases' / self.old / 'FOREIGN_ASSET').write_bytes(b'UNIQUE')
        (self.root / '.download-partial').mkdir()
        (self.root / '.download-partial/partial-image').write_bytes(b'PARTIAL')
        value = self.plan()
        self.assertEqual(value['candidates'], [])
        self.assertEqual(len([item for item in value['retained'] if item['reason'] == 'unproven_or_partial_artifact']), 2)

    def test_complete_kept_incoming_duplicate_is_removed_but_installed_copy_stays(self):
        source = self.root / 'releases' / self.current
        target = self.root / 'incoming' / self.current
        shutil.copytree(source, target)
        value = self.plan()
        self.assertIn('incoming/' + self.current, [item['relative'] for item in value['candidates']])
        self.assertNotIn(self.image_details[0]['Id'], value['image_candidates'])
        self.assertTrue((source / 'image.tar.gz').is_file())

    def test_complete_private_download_is_removed_but_kept_download_and_unknown_remain(self):
        shutil.copytree(self.root / 'releases' / self.old, self.root / '.download-old')
        shutil.copytree(self.root / 'releases' / self.current, self.root / '.download-current')
        (self.root / '.download-unknown').mkdir()
        value = self.plan()
        paths = {item['relative'] for item in value['candidates']}
        self.assertIn('.download-old', paths)
        self.assertNotIn('.download-current', paths)
        self.assertNotIn('.download-unknown', paths)

    def test_image_with_foreign_alias_and_stopped_container_only_reference_is_kept(self):
        image = self.image_details[2]
        image['RepoTags'].append('foreign:keep')
        self.assertEqual(self.plan()['image_candidates'], [])
        image['RepoTags'] = ['sixnine-platform:' + self.old]
        self.containers = [{'Id': '1' * 64, 'Image': image['Id'], 'State': {'Running': False},
            'Config': {'Labels': {'com.docker.compose.project.working_dir': str(self.root / 'releases' / self.old)}},
            'Mounts': []}]
        value = self.plan()
        self.assertNotIn(self.old, value['kept_commits'])
        self.assertEqual(value['image_candidates'], [])

    def test_migration_current_is_protected_independently_and_pending_stops_cleanup(self):
        (self.root / 'migration').mkdir()
        path = self.root / 'migration/active.json'
        path.write_text(json.dumps({'version': 1, 'active': True, 'current': 'e' * 40, 'pending': None}))
        self.assertIn('e' * 40, self.plan()['kept_commits'])
        path.write_text(json.dumps({'version': 1, 'active': True, 'current': 'e' * 40, 'pending': self.old}))
        with self.assertRaisesRegex(release.ReleaseError, 'migration_requires_reconciliation'):
            self.plan()

    def test_previous_frontend_asset_files_are_preserved(self):
        (self.root / 'frontend/assets').mkdir()
        asset = self.root / 'frontend/assets/older-tab-12345678.js'
        asset.write_bytes(b'OLD_BROWSER_TAB')
        value = self.plan()
        self.assertFalse(any(item['relative'].startswith('frontend/assets') for item in value['candidates']))
        self.assertEqual(asset.read_bytes(), b'OLD_BROWSER_TAB')

    @unittest.skipUnless(os.name == 'posix', 'Linux fd-relative removal contract')
    def test_apply_deletes_old_reproducible_tree_only_and_bounded_cache(self):
        value = self.plan()
        result = retention.apply_locked(self.root, value, command=self.command)
        self.assertFalse((self.root / 'releases' / self.old).exists())
        self.assertTrue((self.root / 'releases' / self.current).exists())
        self.assertTrue((self.root / 'frontend/releases' / self.front).exists())
        self.assertEqual((self.root / 'platform-data/user-media').read_bytes(), b'UNIQUE_MEDIA')
        self.assertEqual((self.root / 'postgres/db').read_bytes(), b'UNIQUE_DATABASE')
        self.assertFalse(result['persistent_data_touched'])
        self.assertIn(['builder', 'prune', '--force', '--filter', 'until=72h'], self.calls)
        self.assertFalse(any('volume' in call or 'system' in call or '-a' in call for call in self.calls))

    def test_pointer_change_and_file_swap_reject_the_whole_saved_plan(self):
        value = self.plan()
        (self.root / 'releases' / self.old / 'compose.yaml').write_bytes(b'CHANGED')
        with self.assertRaisesRegex(release.ReleaseError, 'plan_tree_changed'):
            retention.apply_locked(self.root, value, command=self.command)
        self.assertTrue((self.root / 'releases' / self.old / 'image.tar.gz').exists())
        self.state({'current': self.current, 'previous': self.old, 'pending': None, 'status': 'app_ready'})
        with self.assertRaisesRegex(release.ReleaseError, 'plan_stale'):
            retention.apply_locked(self.root, value, command=self.command)

    @unittest.skipUnless(os.name == 'posix', 'Linux descriptor/path replacement contract')
    def test_symlink_replacement_before_apply_preserves_external_target(self):
        value = self.plan()
        outside = self.root.parent / (self.root.name + '-external')
        outside.mkdir(); self.addCleanup(lambda: __import__('shutil').rmtree(outside))
        (outside / 'unique').write_bytes(b'UNIQUE_EXTERNAL')
        path = self.root / 'releases' / self.old
        preserved = self.root / 'releases' / ('e' * 40)
        path.rename(preserved); path.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(release.ReleaseError):
            retention.apply_locked(self.root, value, command=self.command)
        self.assertEqual((outside / 'unique').read_bytes(), b'UNIQUE_EXTERNAL')
        self.assertTrue((preserved / 'image.tar.gz').exists())

    def test_post_success_cleanup_failure_does_not_change_healthy_release(self):
        (self.root / 'retention-policy.json').write_text(json.dumps({'version': 1, 'enabled': True}))
        prior = (self.root / 'release-state.json').read_bytes()
        output = io.StringIO()
        with patch('importlib.util.spec_from_file_location', side_effect=OSError('private-error')), \
                __import__('contextlib').redirect_stderr(output):
            value = release.post_release_retention(self.root)
        self.assertEqual(value['state'], 'retention_skipped_preserving_healthy_release')
        self.assertEqual((self.root / 'release-state.json').read_bytes(), prior)
        self.assertNotIn('private-error', output.getvalue())


if __name__ == '__main__':
    unittest.main()

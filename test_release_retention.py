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
        self.extras = ('e' * 40, 'f' * 40, '1' * 40)
        self.state({'current': self.current, 'previous': self.previous, 'pending': None, 'status': 'app_ready'})
        self.s3, self.image_details, self.containers, self.calls = {}, [], [], []
        for commit in (self.current, self.previous, self.old, *self.extras):
            self.platform(commit)
        os.utime(self.root / 'releases' / self.old, ns=(1, 1))
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

    def platform(self, commit, *, targeted=False):
        folder = self.root / 'releases' / commit; folder.mkdir()
        config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'type': 'layers', 'diff_ids': []},
            'config': {'Labels': {'org.opencontainers.image.revision': commit}}}).encode()
        image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, data in ((image_id[7:] + '.json', config), ('manifest.json', json.dumps([{
                    'Config': image_id[7:] + '.json', 'RepoTags': ['sixnine-platform:' + commit], 'Layers': []}]).encode())):
                member = tarfile.TarInfo(name); member.size = len(data); tar.addfile(member, io.BytesIO(data))
        files = release.TARGET_FILES if targeted else release.FILES
        for name in files:
            (folder / name).write_bytes(gzip.compress(archive.getvalue()) if name == 'image.tar.gz' else b'INERT')
        manifest = {'commit': commit, 'image': 'sixnine-platform:' + commit, 'image_id': image_id,
            'files': {name: release.checksum(folder / name) for name in files}}
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
        self.assertEqual(value['kept_commits'], sorted((self.current, self.previous, self.front, *self.extras)))
        self.assertEqual([item['relative'] for item in value['candidates']], ['releases/' + self.old])
        self.assertEqual(value['image_candidates'], [{'tag': 'sixnine-platform:' + self.old, 'image_id': self.image_details[2]['Id']}])
        self.assertTrue((self.root / 'releases' / self.old).exists())
        self.assertFalse(any(args[:2] in (['image', 'rm'], ['builder', 'prune']) for args in self.calls))

    def test_missing_or_wrong_remote_restore_proof_retains_old_files_and_image(self):
        self.s3[f'releases/{self.old}/image.tar.gz']['Metadata']['sha256'] = '0' * 64
        value = self.plan()
        self.assertEqual(value['candidates'], [])
        self.assertEqual(value['image_candidates'], [])
        self.assertIn({'relative': 'releases/' + self.old, 'reason': 'unproven_or_partial_artifact'}, value['retained'])

    def test_real_platform_bundle_accepts_legacy_and_target_aware_manifest_file_sets(self):
        for targeted, commit in ((False, '2' * 40), (True, '3' * 40)):
            source, image_id = self.platform(commit, targeted=targeted)
            incoming = self.root / 'incoming' / commit
            shutil.copytree(source, incoming)
            expected = (release.TARGET_FILES if targeted else release.FILES) | {'release-manifest.json'}
            for directory, installed in ((source, True), (incoming, False)):
                with self.subTest(targeted=targeted, installed=installed):
                    self.api.head_object.reset_mock()
                    bundle = retention.bundle(self.root, directory, 'platform', self.api, installed=installed)
                    self.assertEqual(set(bundle['entries']), expected | {''})
                    self.assertEqual((bundle['commit'], bundle['kind'], bundle['image_ids']),
                        (commit, 'platform', [image_id]))
                    self.assertEqual({call.kwargs['Key'] for call in self.api.head_object.call_args_list},
                        {f'releases/{commit}/{name}' for name in expected})

    def test_target_aware_platform_bundle_still_rejects_an_unlisted_extra_file(self):
        source, _ = self.platform('2' * 40, targeted=True)
        (source / 'FOREIGN_ASSET').write_bytes(b'UNIQUE')
        self.api.head_object.reset_mock()
        with self.assertRaisesRegex(release.ReleaseError, 'retention_platform_extra_files'):
            retention.bundle(self.root, source, 'platform', self.api, installed=True)
        self.api.head_object.assert_not_called()

    def test_target_aware_platform_versions_fill_newest_slots_but_unlisted_files_do_not(self):
        newest = ('2' * 40, '3' * 40, '4' * 40)
        for index, commit in enumerate(newest):
            folder, _ = self.platform(commit, targeted=True)
            timestamp = 2_000_000_000_000_000_000 + index
            os.utime(folder, ns=(timestamp, timestamp))
        foreign, _ = self.platform('5' * 40, targeted=True)
        (foreign / 'FOREIGN_ASSET').write_bytes(b'UNIQUE')
        os.utime(foreign, ns=(2_000_000_000_000_000_010,) * 2)
        kept, _, extras = retention.keep_policy(self.root, [])
        self.assertEqual(extras, list(reversed(newest)))
        self.assertEqual(kept, {self.current, self.previous, self.front, *newest})
        self.api.head_object.assert_not_called()

    def test_explicit_rollback_and_inflight_tuple_are_pinned_beyond_newest_three(self):
        for relative, document in (
                ('release-retention-pins.json', {'version': 1, 'commits': [self.old]}),
                ('environment-release-intent.json', {'version': 1, 'commit': self.old,
                    'candidate_sha256': '0' * 64, 'state': 'in_progress'})):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.write_text(json.dumps(document))
                value = self.plan()
                self.assertIn(self.old, value['kept_commits'])
                self.assertNotIn('releases/' + self.old, [item['relative'] for item in value['candidates']])
                self.assertEqual(len(value['newest_extra_commits']), 3)
                path.unlink()

    def test_unknown_pin_schema_or_duplicate_fields_fail_closed(self):
        for relative, contents in (
                ('release-retention-pins.json', '{"version":1,"commits":[],"future_pin":"' + self.old + '"}'),
                ('release-retention-pins.json', '{"version":1,"commits":["../outside"]}'),
                ('release-retention-pins.json', '{"version":1,"commits":[],"commits":[]}'),
                ('environment-release-intent.json', json.dumps({'version': 1, 'commit': self.old,
                    'candidate_sha256': '0' * 64, 'state': 'unknown'}))):
            with self.subTest(relative=relative, contents=contents):
                path = self.root / relative
                path.write_text(contents)
                with self.assertRaises((release.ReleaseError, ValueError)):
                    self.plan()
                self.assertTrue((self.root / 'releases' / self.old / 'image.tar.gz').is_file())
                path.unlink()

    def test_incoming_duplicates_do_not_consume_extra_slots(self):
        for commit in (self.current, self.previous, *self.extras):
            shutil.copytree(self.root / 'releases' / commit, self.root / 'incoming' / commit)
        value = self.plan()
        self.assertEqual(set(value['newest_extra_commits']), set(self.extras))
        self.assertEqual(set(item['relative'] for item in value['candidates']),
            {'releases/' + self.old} | {'incoming/' + commit for commit in (self.current, self.previous, *self.extras)})

    def test_missing_approval_and_unknown_directory_shape_are_never_removed(self):
        (self.root / 'approved-releases' / (self.old + '.sha256')).unlink()
        value = self.plan()
        self.assertNotIn('releases/' + self.old, [item['relative'] for item in value['candidates']])
        self.assertEqual(value['image_candidates'], [])
        self.assertTrue((self.root / 'releases' / self.old).is_dir())

    def test_compact_plan_preserves_sha_bytes_without_per_file_inventory(self):
        value = retention.compact_plan(self.plan())
        item = next(item for item in value['candidates'] if item['commit'] == self.old)
        self.assertEqual(set(item), {'relative', 'commit', 'kind', 'bytes'})
        self.assertGreater(item['bytes'], 0)
        self.assertNotIn('pointers', value)
        self.assertEqual(len(value['pointer_sha256']), 64)

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
        self.assertFalse(any(item['image_id'] == self.image_details[0]['Id'] for item in value['image_candidates']))
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

    def test_oci_local_sixnine_digest_is_eligible_only_with_approved_archive_closure(self):
        image = self.image_details[2]
        image['RepoDigests'] = ['sixnine-platform@' + image['Id']]
        value = self.plan()
        self.assertEqual(value['image_candidates'], [{'tag': 'sixnine-platform:' + self.old, 'image_id': image['Id']}])
        self.s3[f'releases/{self.old}/image.tar.gz']['Metadata']['sha256'] = '0' * 64
        self.assertEqual(self.plan()['image_candidates'], [])

    def test_foreign_repo_digest_unproven_digest_or_mismatched_tag_is_retained(self):
        image = self.image_details[2]
        for digests, tags in ((['foreign@' + image['Id']], ['sixnine-platform:' + self.old]),
                (['sixnine-platform@sha256:' + '0' * 64], ['sixnine-platform:' + self.old]),
                (['sixnine-dispatch@' + image['Id']], ['sixnine-platform:' + self.old]),
                (['sixnine-platform@' + image['Id']], ['sixnine-platform:' + 'e' * 40]),
                (['sixnine-platform@' + image['Id']], ['sixnine-dispatch:' + self.old])):
            with self.subTest(digests=digests, tags=tags):
                image['RepoDigests'], image['RepoTags'] = digests, tags
                self.assertEqual(self.plan()['image_candidates'], [])
        alternate = 'sha256:' + '1' * 64
        image['RepoTags'], image['RepoDigests'] = ['sixnine-platform:' + self.old], ['sixnine-platform@' + alternate]
        # Even another approved archive identity is not this inspected image's
        # local digest alias. Preserve it rather than broaden the live policy.
        candidates = [{'kind': 'platform', 'commit': self.old, 'image_ids': [image['Id'], alternate]}]
        self.assertEqual(retention.eligible_image_tags([image], candidates, set()), [])

    def test_kept_current_oci_digest_remains_protected_even_with_removable_staging(self):
        image = self.image_details[0]
        image['RepoDigests'] = ['sixnine-platform@' + image['Id']]
        shutil.copytree(self.root / 'releases' / self.current, self.root / 'incoming' / self.current)
        value = self.plan()
        self.assertIn('incoming/' + self.current, [item['relative'] for item in value['candidates']])
        self.assertFalse(any(item['image_id'] == image['Id'] for item in value['image_candidates']))

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
        self.assertIn(['image', 'rm', 'sixnine-platform:' + self.old], self.calls)
        self.assertIn(['image', 'prune', '--force'], self.calls)
        self.assertFalse(any(call[:2] == ['builder', 'prune'] for call in self.calls))
        self.assertFalse(any('volume' in call or 'system' in call or '-a' in call for call in self.calls))

    @unittest.skipUnless(os.name == 'posix', 'Linux ten-cycle real fixture deletion contract')
    def test_ten_deploy_cycles_keep_current_previous_three_extra_and_flat_size(self):
        sizes = []
        previous = self.current
        for index in range(10):
            commit = format(index + 100, '040x')
            source, _ = self.platform(commit)
            os.utime(source, ns=(2_000_000_000_000_000_000 + index, 2_000_000_000_000_000_000 + index))
            shutil.copytree(source, self.root / 'incoming' / commit)
            self.state({'current': commit, 'previous': previous, 'pending': None, 'status': 'app_ready'})
            (self.root / 'frontend/current.json').write_text(json.dumps({'commit': commit}))
            result = retention.maintain_locked(self.root, apply=True, api=self.api, command=self.command)
            installed = [item for item in (self.root / 'releases').iterdir() if item.is_dir()]
            self.assertEqual(len(installed), 5)
            self.assertTrue((self.root / 'releases' / commit).is_dir())
            self.assertTrue((self.root / 'releases' / previous).is_dir())
            self.assertFalse((self.root / 'incoming' / commit).exists())
            self.assertEqual(len(result['kept_commits']), 6)  # Five platform versions plus independent frontend.
            receipt = json.loads((self.root / result['receipt']).read_text())
            self.assertEqual(receipt['state'], 'retention_applied')
            self.assertEqual(receipt['removed_file_bytes'], sum(item['bytes'] for item in receipt['removed_artifacts']))
            sizes.append(sum(path.stat().st_size for folder in installed for path in folder.iterdir()))
            previous = commit
        # Fixed-size synthetic release payloads; only archive metadata byte-count drift is tolerated.
        self.assertLess(max(sizes) - min(sizes), 1024)
        self.assertEqual(len(list((self.root / 'retention-receipts').glob('*.json'))), 10)
        self.assertEqual((self.root / 'postgres/db').read_bytes(), b'UNIQUE_DATABASE')
        self.assertEqual((self.root / 'platform-data/user-media').read_bytes(), b'UNIQUE_MEDIA')

    @unittest.skipUnless(os.name == 'posix', 'Linux link-type checks')
    def test_malicious_symlink_and_hardlink_are_not_followed(self):
        outside = self.root.parent / (self.root.name + '-outside')
        outside.mkdir()
        self.addCleanup(lambda: shutil.rmtree(outside))
        unique = outside / 'unique'
        unique.write_bytes(b'UNIQUE_OUTSIDE')
        copied = self.root / 'incoming' / self.old
        shutil.copytree(self.root / 'releases' / self.old, copied)
        (copied / 'image.tar.gz').unlink()
        (copied / 'image.tar.gz').symlink_to(unique)
        value = self.plan()
        self.assertNotIn('incoming/' + self.old, [item['relative'] for item in value['candidates']])
        (copied / 'image.tar.gz').unlink()
        os.link(unique, copied / 'image.tar.gz')
        value = self.plan()
        self.assertNotIn('incoming/' + self.old, [item['relative'] for item in value['candidates']])
        self.assertEqual(unique.read_bytes(), b'UNIQUE_OUTSIDE')

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
        preserved = self.root / 'releases' / ('9' * 40)
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

    @unittest.skipUnless(os.name == 'posix', 'Linux fixed host CLI policy/lock boundary')
    def test_scheduled_requires_enabled_protected_policy_manual_apply_still_explicit(self):
        policy = self.root / 'retention-policy.json'
        with patch.object(release, 'ROOT', self.root), patch.object(os, 'geteuid', return_value=0), \
                patch.object(retention, 'maintain_locked', return_value={'state': 'synthetic'}) as maintain, \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            for contents in (None, 'not-json', '{"version":1,"enabled":false}',
                    '{"version":true,"enabled":true}', '{"version":1,"enabled":1}',
                    '{"version":1,"enabled":true,"unknown":1}'):
                policy.unlink(missing_ok=True)
                if contents is not None:
                    policy.write_text(contents)
                self.assertEqual(retention.main(['--apply', '--scheduled']), 1)
                maintain.assert_not_called()
            policy.write_text('{"version":1,"enabled":true}')
            self.assertEqual(retention.main(['--apply', '--scheduled']), 0)
            maintain.assert_called_once_with(apply=True)
            maintain.reset_mock()
            policy.unlink()
            self.assertEqual(retention.main(['--apply']), 0)
            maintain.assert_called_once_with(apply=True)

    def test_post_hook_requires_exact_boolean_enabled_and_integer_version(self):
        policy = self.root / 'retention-policy.json'
        before = (self.root / 'release-state.json').read_bytes()
        with patch('importlib.util.spec_from_file_location') as load, patch('sys.stderr', io.StringIO()):
            for contents in ('{"version":true,"enabled":true}', '{"version":1,"enabled":1}'):
                policy.write_text(contents)
                value = release.post_release_retention(self.root)
                self.assertEqual(value['state'], 'retention_skipped_preserving_healthy_release')
                load.assert_not_called()
                self.assertEqual((self.root / 'release-state.json').read_bytes(), before)

    def test_full_tuple_post_hook_defers_without_changing_healthy_state(self):
        (self.root / 'retention-policy.json').write_text('{"version":1,"enabled":true}')
        (self.root / 'environment-release-intent.json').write_text(json.dumps({'version': 1,
            'commit': self.current, 'candidate_sha256': '0' * 64, 'state': 'in_progress'}))
        prior = (self.root / 'release-state.json').read_bytes()
        self.assertEqual(release.post_release_retention(self.root),
            {'state': 'retention_deferred_until_full_tuple_installed'})
        self.assertEqual((self.root / 'release-state.json').read_bytes(), prior)

    @unittest.skipUnless(os.name == 'posix', 'Linux descriptor removal plus exact Docker commands')
    def test_protected_untagged_digest_image_blocks_global_dangling_prune(self):
        image = self.image_details[1]  # Previous rollback version is unused by containers.
        image['RepoTags'] = []
        image['RepoDigests'] = ['sixnine-platform@' + image['Id']]
        result = retention.apply_locked(self.root, self.plan(), command=self.command)
        self.assertFalse(result['dangling_images_pruned'])
        self.assertNotIn(['image', 'prune', '--force'], self.calls)
        self.assertIn(['image', 'rm', 'sixnine-platform:' + self.old], self.calls)
        self.assertFalse(any(call[:2] == ['image', 'rm'] and call[2].startswith('sha256:') for call in self.calls))


if __name__ == '__main__':
    unittest.main()

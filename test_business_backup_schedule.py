"""Offline safety checks; no AWS calls or Docker/service operations."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).parent
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'deploy/platform'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'fcntl': mock.Mock()} if sys.platform == 'win32' else {}):
        spec.loader.exec_module(module)
    return module

host = load('business_backup')

class ScheduleTests(unittest.TestCase):
    def config(self):
        return {'version': 1, 'environment': 'staging', 'instance_id': host.HOSTS['staging'],
            'target': {'account_id': '829135631045', 'region': 'ap-southeast-1',
                'bucket': 'inkseq-business-backup-staging-test',
                'kms_key_arn': 'arn:aws:kms:ap-southeast-1:829135631045:key/'+'a'*36, 'role_arn': 'arn:aws:iam::829135631045:role/test-backup'}}

    def test_environment_account_region_and_fields_are_closed(self):
        good = self.config()
        self.assertEqual(host.validate_config(good, good['instance_id']), good)
        for key, value in [('account_id','000000000000'), ('region','us-east-1'),
                           ('bucket','sixnine-platform-releases-test'), ('role_arn','arn:aws:iam::000000000000:role/test')]:
            bad = copy.deepcopy(good); bad['target'][key] = value
            with self.assertRaises(ValueError): host.validate_config(bad, good['instance_id'])
        with self.assertRaises(ValueError): host.validate_config(good, host.HOSTS['production'])
        with self.assertRaises(ValueError): host.validate_config({**good, 'credentials': 'not-allowed'}, good['instance_id'])

    def test_docker_failure_does_not_surface_sensitive_subprocess_details(self):
        with mock.patch.object(host.subprocess, 'run', return_value=mock.Mock(returncode=1,stdout=b'private data',stderr=b'secret')):
            with self.assertRaisesRegex(ValueError, '^backup_docker_failed_or_unknown$'):
                host.docker(['ps'])

    def test_transfer_has_no_application_secrets_socket_or_source_write_mount(self):
        cmd = host.transport('sha256:'+'a'*64, 'inkseq-backup-test',
            [('/private/snapshot','/snapshot',True), ('/private/output','/output',False)], ['copy','--execute'])
        mounts = [cmd[i+1] for i, value in enumerate(cmd) if value == '--mount']
        self.assertEqual(mounts, ['type=bind,src=/private/snapshot,dst=/snapshot,readonly',
            'type=bind,src=/private/output,dst=/output'])
        self.assertIn('--instance-role-only', cmd)
        self.assertIn('--read-only', cmd)
        self.assertNotIn('/run/secrets', ' '.join(cmd))
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertNotIn('AWS_ACCESS_KEY_ID', ' '.join(cmd))

    @unittest.skipIf(sys.platform == 'win32', 'POSIX mode semantics required')
    def test_snapshot_private_modes_preserve_host_traversal_barrier(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as temporary:
            operation = Path(temporary); operation.chmod(0o700)
            snapshot = operation/'snapshot'; snapshot.mkdir(mode=0o700)
            media = snapshot/'media'; media.mkdir()
            artifact = media/'file'; artifact.write_bytes(b'fixture')
            try:
                with mock.patch.object(host,'protected',side_effect=lambda p,*a:p), \
                        mock.patch.object(host.os,'chown') as ownership, \
                        mock.patch.object(host.os,'getuid',return_value=0):
                    # Local CI can be unprivileged; only the root-owned copy check is
                    # substituted, while real filesystem modes and traversal remain.
                    original = Path.lstat
                    def root_stat(p):
                        v = original(p)
                        return type('Metadata',(),dict(st_uid=0,st_mode=v.st_mode,st_nlink=v.st_nlink))()
                    with mock.patch.object(Path,'lstat',root_stat):
                        host.freeze_transport_snapshot(snapshot)
                self.assertEqual(stat.S_IMODE(operation.stat().st_mode),0o700)
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode),0o500)
                self.assertEqual(stat.S_IMODE(media.stat().st_mode),0o500)
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode),0o400)
                self.assertEqual({call.args for call in ownership.call_args_list},
                    {(snapshot,10001,10001),(media,10001,10001),(artifact,10001,10001)})
                self.assertFalse(artifact.stat().st_mode & 0o077)
            finally:
                snapshot.chmod(0o700)
                media.chmod(0o700)

    def test_oci_config_identity_requires_approved_archive_and_exact_revision(self):
        manifest_id, config_id = 'sha256:'+'a'*64, 'sha256:'+'b'*64
        commit = 'c'*40; manifest = {'image_id':manifest_id,'image':'sixnine-platform:'+commit}
        release = mock.Mock(); release.validate_image_archive.return_value = (manifest_id,config_id)
        directory = Path('/approved/release')
        with mock.patch.object(host,'docker',return_value=json.dumps({'id':config_id,'revision':commit})):
            self.assertEqual(host.approved_image(release,directory,manifest,commit),config_id)
            self.assertEqual(host.approved_image(release,directory,manifest,commit,pinned_image=config_id),config_id)
        release.validate_image_archive.assert_called_with(directory/'image.tar.gz',manifest)
        for value in ({'id':'sha256:'+'d'*64,'revision':commit}, {'id':config_id,'revision':'e'*40}, {'id':config_id}):
            with mock.patch.object(host,'docker',return_value=json.dumps(value)), self.assertRaises(ValueError):
                host.approved_image(release,directory,manifest,commit)
        with mock.patch.object(host,'docker',return_value=json.dumps({'id':manifest_id,'revision':commit})), self.assertRaises(ValueError):
            host.approved_image(release,directory,manifest,commit,pinned_image=config_id)

    def test_unknown_previous_day_blocks_new_copy_and_completed_same_day_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prior = root/'2026-10-15'; prior.mkdir()
            with mock.patch.object(host,'protected',side_effect=lambda p,*a: p), mock.patch.object(host,'read',side_effect=lambda p:json.loads(p.read_text())):
                with self.assertRaisesRegex(ValueError,'backup_prior_outcome_requires_reconciliation'):
                    host.check_history(root,'2026-10-16')
                receipt = {'version':1,'day':'2026-10-15','completion_sha256':'a'*64,'receipt_sha256':'b'*64}
                (prior/'completed.json').write_text(json.dumps(receipt))
                self.assertFalse(host.check_history(root,'2026-10-16'))
                self.assertTrue(host.check_history(root,'2026-10-15'))
                receipt['day']='2026-10-16';(prior/'completed.json').write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError,'backup_prior_receipt_invalid'):
                    host.check_history(root,'2026-10-16')

if __name__ == '__main__': unittest.main()

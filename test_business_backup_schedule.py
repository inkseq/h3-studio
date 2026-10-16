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

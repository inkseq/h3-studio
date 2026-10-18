"""Real synthetic portable backup + fake S3; never AWS or real customer data."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import backup_remote as remote
from studio_platform.backup_remote_cli import main
from studio_platform.backup_remote_cli import aws_client
from studio_platform.assets import AssetNotFound, AssetService
from studio_platform.auth import Auth
from studio_platform.queue import TaskQueue
from studio_platform.repository import Repository
from studio_platform.storage import LocalObjectStore
from test_platform_assets import png
import test_platform_backup as fixtures


class ProviderFailure(Exception):
    def __init__(self, status=500):
        super().__init__("https://private.invalid/?token=must-not-log")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


class Body(io.BytesIO):
    def __init__(self, data, owner):
        super().__init__(data)
        self.owner = owner

    def read(self, size=-1):
        if not 0 < size <= remote.MIB:
            raise AssertionError("Unbounded readback")
        return super().read(size)

    def close(self):
        if not self.closed:
            self.owner.closed_bodies += 1
        return super().close()


class FakeS3:
    def __init__(self, target):
        self.target = target
        self.objects = {}
        self.writes = []
        self.reads = []
        self.closed_bodies = 0
        self.fail_after = None
        self.fail_before = None
        self.bad_key = False
        self.corrupt = None
        self.versioned = True
        self.account = target.account_id
        self.latest_shift = None

    def owner(self, args):
        if args.get('ExpectedBucketOwner') != self.account:
            raise ProviderFailure(403)
        if args.get('Bucket') != self.target.bucket:
            raise ProviderFailure(403)

    def get_bucket_versioning(self, **args):
        self.owner(args)
        return {'Status': 'Enabled' if self.versioned else 'Suspended'}

    def get_public_access_block(self, **args):
        self.owner(args)
        return {
            'PublicAccessBlockConfiguration': dict.fromkeys(
                (
                    'BlockPublicAcls',
                    'IgnorePublicAcls',
                    'BlockPublicPolicy',
                    'RestrictPublicBuckets',
                ),
                True,
            )
        }

    def get_bucket_ownership_controls(self, **args):
        self.owner(args)
        return {
            'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]}
        }

    def get_bucket_encryption(self, **args):
        self.owner(args)
        return {
            'ServerSideEncryptionConfiguration': {
                'Rules': [
                    {
                        'ApplyServerSideEncryptionByDefault': {
                            'SSEAlgorithm': 'aws:kms',
                            'KMSMasterKeyID': self.target.kms_key_arn,
                        }
                    }
                ]
            }
        }

    def list_object_versions(self, **args):
        self.owner(args)
        return {
            'Versions': [
                {'Key': key} for key in self.objects if key.startswith(args['Prefix'])
            ][:1]
        }

    def put_object(self, **args):
        self.owner(args)
        self.writes.append(args['Key'])
        if args['Key'].endswith(self.fail_before or 'never-fail'):
            raise ProviderFailure()
        if args['Key'] in self.objects:
            raise ProviderFailure(412)
        if args['IfNoneMatch'] != '*':
            raise AssertionError('Missing conditional creation')
        if (
            args['ServerSideEncryption'] != 'aws:kms'
            or args['SSEKMSKeyId'] != self.target.kms_key_arn
        ):
            raise AssertionError('Encryption not explicitly bound')
        data = args['Body'].read()
        version = 'version-' + str(len(self.objects) + 1)
        if len(data) != args['ContentLength']:
            raise AssertionError('Length mismatch')
        if (
            remote.base64.b64encode(hashlib.sha256(data).digest()).decode()
            != args['ChecksumSHA256']
        ):
            raise ProviderFailure(400)
        self.objects[args['Key']] = dict(
            data=data,
            VersionId=version,
            ContentLength=len(data),
            Metadata=args['Metadata'],
            ChecksumSHA256=args['ChecksumSHA256'],
            ServerSideEncryption=args['ServerSideEncryption'],
            SSEKMSKeyId=args['SSEKMSKeyId'],
        )
        if args['Key'].endswith(self.fail_after or 'never-fail'):
            raise ProviderFailure()
        return {'VersionId': version}

    def _get(self, args):
        self.owner(args)
        if args['Key'] not in self.objects:
            raise ProviderFailure(404)
        value = copy.deepcopy(self.objects[args['Key']])
        if args.get('VersionId') not in (None, value['VersionId']):
            raise ProviderFailure(404)
        if self.bad_key:
            value['SSEKMSKeyId'] = 'arn:aws:kms:ap-southeast-1:111111111111:key/wrong'
        return value

    def head_object(self, **args):
        value = self._get(args)
        value.pop('data')
        return value

    def get_object(self, **args):
        self.reads.append(args)
        value = self._get(args)
        data = value.pop('data')
        if args['Key'].endswith(self.corrupt or 'never-corrupt'):
            data = data[:-1] + bytes([data[-1] ^ 1])
        value['Body'] = Body(data, self)
        return value


class RemoteBackupTests(unittest.TestCase):
    setUp = fixtures.BackupTests.setUp
    backup = fixtures.BackupTests.backup

    def target(self):
        return remote.BackupTarget(
            '123456789012',
            'ap-southeast-1',
            'sixnine-private-backups-test',
            'business-backups/51a950cd-8258-4308-9c53-8b85a2f8ea57',
            'arn:aws:kms:ap-southeast-1:123456789012:key/087ab194-7db0-44cb-83ea-2e43d2a24809',
            'arn:aws:iam::123456789012:role/sixnine-business-backup',
        )

    def copied(self, client=None):
        source = self.backup()
        target = self.target()
        client = client or FakeS3(target)
        receipt = remote.copy_backup(
            source, target, self.root / 'copy-receipts', client=client
        )
        return source, target, client, receipt

    def test_synthetic_image_and_story_roundtrip_restores_held_jobs_and_owner_boundaries(
        self,
    ):
        source, target, client, receipt = self.copied()
        self.assertEqual(
            client.writes[-2:],
            [target.prefix + '/manifest.json', target.prefix + '/complete.json'],
        )
        self.assertNotIn(
            self.canary.encode(), b''.join(x['data'] for x in client.objects.values())
        )
        out = self.root / 'restored'
        result = remote.restore_copy(
            target, receipt, self.root / 'readback', out, client=client
        )
        self.assertEqual(result['state'], 'off_device_copy_restored_isolated')
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(result['held_jobs'], 1)
        restored = Repository('sqlite:///' + (out / 'platform.sqlite3').as_posix())
        self.addCleanup(restored.close)
        store = LocalObjectStore(out / 'objects')
        assets = AssetService(restored.engine, store, out)
        asset = assets.get('superdan', self.asset['id'])
        with store.open(asset['original']['key']) as stream:
            self.assertEqual(stream.read(), png())
        with self.assertRaises(AssetNotFound):
            assets.get('supervan', self.asset['id'])
        self.assertFalse(Auth(restored.engine).ready())
        self.assertIsNone(TaskQueue(restored).claim('no-worker', 'test-pool'))
        self.assertEqual(
            self.repo.get_job(self.scope, self.job['id'])['status'], 'queued'
        )
        self.assertTrue(all('VersionId' in args for args in client.reads))
        self.assertEqual(len(client.reads), client.closed_bodies)
        self.assertEqual(
            remote.verify_local(source)['database_sha256'],
            remote._digest(source / 'database.sqlite3'),
        )

    def test_interrupted_put_gets_exact_readback_without_second_put(self):
        client = FakeS3(self.target())
        client.fail_after = 'database.sqlite3'
        _, target, client, receipt = self.copied(client)
        self.assertEqual(client.writes.count(target.prefix + '/database.sqlite3'), 1)
        self.assertEqual(receipt['phase'], 'copied_not_restored')
        self.assertEqual(len(client.reads), 1)
        self.assertEqual(client.closed_bodies, 1)

    def test_unknown_missing_write_retains_intent_and_never_publishes_completion(self):
        client = FakeS3(self.target())
        client.fail_before = 'database.sqlite3'
        source = self.backup()
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'backup_readback_unconfirmed'
        ):
            remote.copy_backup(
                source, self.target(), self.root / 'receipts', client=client
            )
        self.assertTrue((self.root / 'receipts/intent.json').exists())
        self.assertTrue((self.root / 'receipts/uncertain.json').exists())
        self.assertFalse(any(key.endswith('complete.json') for key in client.objects))
        self.assertEqual(
            client.writes.count(self.target().prefix + '/database.sqlite3'), 1
        )
        self.assertEqual(
            remote.verify_local(source)['kind'], 'sixnine-local-business-backup'
        )

    def test_wrong_kms_on_unknown_write_is_not_adopted(self):
        client = FakeS3(self.target())
        client.fail_after = 'claim.json'
        client.bad_key = True
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'backup_object_key_mismatch'
        ):
            self.copied(client)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.closed_bodies, 1)

    def test_unknown_content_or_metadata_mismatch_is_not_adopted(self):
        for kind in ('content', 'metadata'):
            client = FakeS3(self.target())
            client.fail_after = 'claim.json'
            if kind == 'content':
                client.corrupt = 'claim.json'
            else:
                original = client.get_object

                def wrong_metadata(**args):
                    response = original(**args)
                    response['Metadata'] = {}
                    return response

                client.get_object = wrong_metadata
            source = self.backup() if kind == 'content' else self.root / 'backup'
            with self.assertRaises(remote.RemoteBackupError):
                remote.copy_backup(
                    source, self.target(), self.root / kind, client=client
                )
            self.assertEqual(len(client.writes), 1)
            self.assertFalse(any(k.endswith('complete.json') for k in client.objects))

    def test_existing_remote_prefix_refuses_without_overwrite(self):
        source, target, client, _ = self.copied()
        before = copy.deepcopy(client.objects)
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'backup_prefix_already_used'
        ):
            remote.copy_backup(
                source, target, self.root / 'different-receipts', client=client
            )
        self.assertEqual(client.objects, before)

    def test_existing_local_receipts_refuse_before_remote_replay(self):
        source, target, client, _ = self.copied()
        count = len(client.writes)
        with self.assertRaises(remote.BackupError):
            remote.copy_backup(
                source, target, self.root / 'copy-receipts', client=client
            )
        self.assertEqual(len(client.writes), count)

    def test_final_local_receipt_failure_preserves_remote_complete_and_refuses_replay(
        self,
    ):
        source = self.backup()
        target = self.target()
        client = FakeS3(target)
        original = remote._record

        def fail_completion(path, value):
            if path.name == 'completion.json':
                raise OSError('synthetic private provider URL')
            return original(path, value)

        with patch.object(remote, '_record', side_effect=fail_completion):
            with self.assertRaisesRegex(
                remote.RemoteBackupError, 'backup_copy_requires_reconciliation'
            ):
                remote.copy_backup(
                    source, target, self.root / 'receipts', client=client
                )
        self.assertIn(target.prefix + '/complete.json', client.objects)
        self.assertTrue((self.root / 'receipts/intent.json').exists())
        self.assertFalse((self.root / 'receipts/completion.json').exists())
        count = len(client.writes)
        with self.assertRaises(remote.BackupError):
            remote.copy_backup(source, target, self.root / 'receipts', client=client)
        self.assertEqual(len(client.writes), count)

    def test_wrong_account_or_versioning_refuses_before_any_write(self):
        for label in ('foreign', 'unversioned'):
            client = FakeS3(self.target())
            if label == 'foreign':
                client.account = '999999999999'
            else:
                client.versioned = False
            source = self.backup() if label == 'foreign' else self.root / 'backup'
            with self.assertRaises(remote.RemoteBackupError):
                remote.copy_backup(
                    source, self.target(), self.root / label, client=client
                )
            self.assertEqual(client.writes, [])

    def test_corrupt_remote_media_keeps_new_partial_download_without_restore(self):
        _, target, client, receipt = self.copied()
        client.corrupt = next(
            k.split('/')[-1] for k in client.objects if '/media/' in k
        )
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'backup_object_content_mismatch'
        ):
            remote.restore_copy(
                target,
                receipt,
                self.root / 'download',
                self.root / 'restore',
                client=client,
            )
        self.assertFalse((self.root / 'restore').exists())
        self.assertFalse((self.root / 'download/verified-restore.json').exists())
        self.assertEqual(len(client.reads), client.closed_bodies)

    def test_wrong_or_absent_completion_version_never_falls_back_to_latest(self):
        _, target, client, receipt = self.copied()
        for version in (None, 'null', 'different-version'):
            trial = {**receipt, 'completion_version_id': version}
            with self.assertRaises(remote.RemoteBackupError):
                remote.restore_copy(
                    target,
                    trial,
                    self.root / ('dl-' + str(version)),
                    self.root / 'restore',
                    client=client,
                )
        self.assertFalse((self.root / 'restore').exists())
        self.assertTrue(
            all(x.get('VersionId') == 'different-version' for x in client.reads)
        )

    def test_existing_restore_target_is_preserved_without_readback(self):
        _, target, client, receipt = self.copied()
        destination = self.root / 'existing'
        destination.mkdir()
        (destination / 'keep').write_text('unchanged')
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'restore_target_must_be_new'
        ):
            remote.restore_copy(
                target, receipt, self.root / 'download', destination, client=client
            )
        self.assertEqual((destination / 'keep').read_text(), 'unchanged')
        self.assertEqual(client.reads, [])

    def test_source_corruption_or_extra_secret_is_rejected_before_cloud(self):
        source = self.backup()
        client = FakeS3(self.target())
        (source / 'secret.env').write_text('fake-do-not-copy')
        with self.assertRaisesRegex(remote.RemoteBackupError, 'unexpected_entries'):
            remote.copy_backup(
                source, self.target(), self.root / 'receipts', client=client
            )
        self.assertEqual(client.writes, [])

    def test_source_replacement_between_stat_and_open_refuses(self):
        source = self.backup()
        item = next(
            x
            for x in remote.plan_copy(source, self.target())['files']
            if x['name'] == 'database.sqlite3'
        )
        path = source / 'database.sqlite3'
        replacement = self.root / 'replacement.sqlite3'
        replacement.write_bytes(path.read_bytes())
        original = remote.os.open

        def replace(candidate, *args, **kwargs):
            if Path(candidate) == path:
                replacement.replace(path)
            return original(candidate, *args, **kwargs)

        with patch.object(remote.os, 'open', side_effect=replace):
            with self.assertRaisesRegex(
                remote.RemoteBackupError, 'backup_changed_before_open'
            ):
                with remote._source_file(path, item):
                    self.fail('Changed source must not be sent')

    def test_source_receipt_overlap_refused(self):
        source = self.backup()
        client = FakeS3(self.target())
        with self.assertRaisesRegex(
            remote.RemoteBackupError, 'backup_receipts_overlap_source'
        ):
            remote.copy_backup(source, self.target(), source / 'receipt', client=client)
        self.assertEqual(client.writes, [])

    def test_oversized_version_manifest_refuses_before_remote_write_or_local_intent(
        self,
    ):
        source = self.backup()
        client = FakeS3(self.target())
        files = remote.plan_copy(source, self.target())['files']
        large = [
            dict(
                name='media/' + format(i, '064x') + '.bin',
                size_bytes=1,
                sha256='0' * 64,
            )
            for i in range(10000)
        ] + [files[-1]]
        with patch.object(remote, '_files', return_value=large):
            with self.assertRaisesRegex(
                remote.RemoteBackupError, 'backup_completion_limit'
            ):
                remote.copy_backup(
                    source, self.target(), self.root / 'receipts', client=client
                )
        self.assertEqual(client.writes, [])
        self.assertFalse((self.root / 'receipts').exists())

    def test_cli_plan_is_offline_and_online_actions_require_execute(self):
        source = self.backup()
        targetfile = self.root / 'target.json'
        targetfile.write_text(json.dumps(remote.asdict(self.target())))
        with (
            patch(
                'studio_platform.backup_remote_cli.aws_client',
                side_effect=AssertionError('Must remain offline'),
            ),
            patch('sys.stdout', io.StringIO()) as output,
        ):
            self.assertEqual(
                main(['--target', str(targetfile), '--backup', str(source)]), 0
            )
            self.assertEqual(json.loads(output.getvalue())['cloud_operations'], 0)
            output.seek(0)
            output.truncate()
            self.assertEqual(
                main(['copy', '--target', str(targetfile), '--backup', str(source)]), 1
            )
            self.assertNotIn('https://', output.getvalue())

    def test_target_rejects_cross_account_kms_and_release_bucket(self):
        value = remote.asdict(self.target())
        for update in (
            {
                'kms_key_arn': value['kms_key_arn'].replace(
                    '123456789012', '999999999999'
                )
            },
            {'role_arn': value['role_arn'].replace('123456789012', '999999999999')},
            {'bucket': 'sixnine-platform-releases-example'},
            {'prefix': '../../private'},
        ):
            with self.assertRaises(remote.RemoteBackupError):
                remote.BackupTarget(**{**value, **update})

    def test_runtime_caller_wrong_account_stops_before_s3(self):
        calls = []

        def client(name, **kwargs):
            calls.append(name)
            self.assertEqual(kwargs['config'].retries['total_max_attempts'], 1)
            return SimpleNamespace(
                get_caller_identity=lambda: {'Account': '999999999999'}
            )

        with patch('boto3.Session', return_value=SimpleNamespace(client=client)):
            with self.assertRaisesRegex(
                remote.RemoteBackupError, 'backup_caller_account_mismatch'
            ):
                aws_client(self.target())
        self.assertEqual(calls, ['sts'])

    def test_explicit_role_assumption_stays_in_memory_and_verifies_scoped_caller(self):
        target = self.target()
        calls = []
        sessions = []
        expected = 'arn:aws:sts::123456789012:assumed-role/sixnine-business-backup/sixnine-backup-copy'
        secret = dict(
            AccessKeyId='synthetic-key',
            SecretAccessKey='synthetic-secret',
            SessionToken='synthetic-token',
            Expiration=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        def assume(**args):
            self.assertEqual(
                args,
                dict(
                    RoleArn=target.role_arn,
                    RoleSessionName='sixnine-backup-copy',
                    DurationSeconds=3600,
                ),
            )
            return dict(AssumedRoleUser=dict(Arn=expected), Credentials=secret)

        def session(**args):
            sessions.append(args)
            scoped = 'aws_session_token' in args

            def client(name, **kwargs):
                calls.append((scoped, name))
                self.assertEqual(kwargs['config'].retries['total_max_attempts'], 1)
                if name == 'sts':
                    self.assertEqual(
                        kwargs['endpoint_url'],
                        'https://sts.ap-southeast-1.amazonaws.com',
                    )
                    self.assertEqual(kwargs['config'].signature_version, 'v4')
                    return SimpleNamespace(
                        assume_role=assume,
                        get_caller_identity=lambda: dict(
                            Account=target.account_id, Arn=expected
                        ),
                    )
                self.assertTrue(scoped)
                self.assertEqual(kwargs['config'].signature_version, 's3v4')
                self.assertEqual(
                    kwargs['endpoint_url'], 'https://s3.ap-southeast-1.amazonaws.com'
                )
                return 'scoped-s3'

            return SimpleNamespace(client=client)

        with patch('boto3.Session', side_effect=session):
            self.assertEqual(aws_client(target), 'scoped-s3')
        self.assertEqual(calls, [(False, 'sts'), (True, 'sts'), (True, 's3')])
        self.assertEqual(sessions[1]['aws_session_token'], secret['SessionToken'])

    def test_wrong_assumed_role_or_expired_session_refuses_before_s3(self):
        target = self.target()
        expected = 'arn:aws:sts::123456789012:assumed-role/sixnine-business-backup/sixnine-backup-copy'
        for kind in ('returned-role', 'expired', 'verified-role'):
            role = dict(
                AssumedRoleUser=dict(
                    Arn=expected
                    if kind != 'returned-role'
                    else expected.replace('backup-copy', 'other')
                ),
                Credentials=dict(
                    AccessKeyId='fake-key',
                    SecretAccessKey='fake-secret',
                    SessionToken='fake-token',
                    Expiration=datetime.now(timezone.utc)
                    + timedelta(minutes=-1 if kind == 'expired' else 60),
                ),
            )
            calls = []

            def session(**args):
                scoped = 'aws_session_token' in args

                def client(name, **kwargs):
                    calls.append(name)
                    self.assertEqual(name, 'sts')
                    return SimpleNamespace(
                        assume_role=lambda **ignored: role,
                        get_caller_identity=lambda: dict(
                            Account=target.account_id,
                            Arn=expected
                            if not scoped or kind != 'verified-role'
                            else 'another-role',
                        ),
                    )

                return SimpleNamespace(client=client)

            with patch('boto3.Session', side_effect=session):
                with self.assertRaises(remote.RemoteBackupError):
                    aws_client(target)
            self.assertNotIn('s3', calls)

    def test_cli_never_prints_raw_runtime_exception(self):
        source = self.backup()
        targetfile = self.root / 'target.json'
        targetfile.write_text(json.dumps(remote.asdict(self.target())))
        with (
            patch(
                'studio_platform.backup_remote_cli.aws_client',
                side_effect=ProviderFailure(),
            ),
            patch('sys.stdout', io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        'copy',
                        '--execute',
                        '--target',
                        str(targetfile),
                        '--backup',
                        str(source),
                        '--receipts',
                        str(self.root / 'receipts'),
                    ]
                ),
                1,
            )
            self.assertNotIn('private.invalid', output.getvalue())
            self.assertNotIn('must-not-log', output.getvalue())

    def test_scheduled_transport_rejects_static_and_container_credentials_before_api(
        self,
    ):
        for method in (
            'env',
            'shared-credentials-file',
            'container-role',
            'assume-role',
        ):
            client = lambda *a, **k: self.fail('No AWS API before host-role check')
            with patch(
                'boto3.Session',
                return_value=SimpleNamespace(
                    client=client,
                    get_credentials=lambda: SimpleNamespace(method=method),
                ),
            ):
                with self.assertRaisesRegex(
                    remote.RemoteBackupError, '^backup_instance_role_required$'
                ):
                    aws_client(self.target(), instance_role_only=True)


if __name__ == '__main__':
    unittest.main()

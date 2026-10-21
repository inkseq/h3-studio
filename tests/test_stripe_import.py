"""LIVE credential import safety with synthetic secrets, fake AWS and no network."""

import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import types
import unittest
import sys
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from tools import import_stripe_credentials as importer

KEY = 'rk_live_' + 'synthetic' * 4
ACCOUNT = 'acct_Synthetic123'
IDENTITY = {'account_id': ACCOUNT, 'mode': 'live'}
DOCUMENT = {
    'version': 2,
    'mode': 'live',
    'account_id': ACCOUNT,
    'secret_key': KEY,
    'publishable_key': None,
    'webhook_secret': None,
    'webhook_endpoint_id': None,
}
EXISTING_VERSION = '11111111-1111-4111-8111-111111111111'


class NotFound(ClientError):
    def __init__(self):
        super().__init__(
            {'Error': {'Code': 'ResourceNotFoundException'}}, 'GetSecretValue'
        )


class FakeSecrets:
    exceptions = types.SimpleNamespace(ResourceNotFoundException=NotFound)

    def __init__(self, existing=None, *, container=False):
        self.exists = container or existing is not None
        self.versions = {EXISTING_VERSION: json.dumps(existing)} if existing else {}
        self.current = EXISTING_VERSION if existing else None
        self.writes, self.reads = [], []
        self.arn = f'arn:aws:secretsmanager:{importer.REGION}:{importer.ACCOUNT}:secret:{importer.SECRET}-Abc123'
        self.after_write = None
        self.deleted = False

    def describe_secret(self, **kwargs):
        assert kwargs == {'SecretId': importer.SECRET}
        if not self.exists:
            raise NotFound()
        return {
            'Name': importer.SECRET,
            'ARN': self.arn,
            'VersionIdsToStages': {key: ['AWSCURRENT'] for key in self.versions},
            **({'DeletedDate': 'not-a-secret'} if self.deleted else {}),
        }

    def get_secret_value(self, **kwargs):
        self.reads.append(kwargs)
        assert kwargs['SecretId'] == importer.SECRET
        version = kwargs.get('VersionId', self.current)
        if not self.exists or version not in self.versions:
            raise NotFound()
        return {
            'Name': importer.SECRET,
            'ARN': self.arn,
            'VersionId': version,
            'SecretString': self.versions[version],
        }

    def _write(self, kind, kwargs):
        self.writes.append((kind, kwargs))
        self.exists, self.current = True, kwargs['ClientRequestToken']
        self.versions[self.current] = kwargs['SecretString']
        if self.after_write:
            self.after_write()

    def create_secret(self, **kwargs):
        assert kwargs['Name'] == importer.SECRET
        if self.exists:
            raise ClientError(
                {'Error': {'Code': 'ResourceExistsException'}}, 'CreateSecret'
            )
        self._write('create', kwargs)

    def put_secret_value(self, **kwargs):
        assert kwargs['SecretId'] == importer.SECRET
        self._write('put', kwargs)


def session(api, account=importer.ACCOUNT):
    sts = Mock()
    sts.get_caller_identity.return_value = {'Account': account}
    value = Mock()
    value.client.side_effect = lambda service, **_kwargs: (
        sts if service == 'sts' else api
    )
    return value


class FakeDrop:
    def __init__(self, _path):
        self.key, self.fingerprint = KEY, [1, 2, 3, 4]
        self.deleted, self.changed = False, False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read_unchanged(self):
        if self.changed:
            raise importer.ImportErrorSafe('stripe_drop_changed')

    def delete_unchanged(self):
        self.read_unchanged()
        self.deleted = True


class StripeImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt_path = Path(self.temp.name) / 'receipt.json'
        self.patcher = patch.object(importer, 'RECEIPT', self.receipt_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.drop = FakeDrop(None)
        self.read_drop = Mock(return_value=self.drop)
        self.identity = Mock(return_value=IDENTITY)

    def run_import(self, api, **kwargs):
        return importer.import_existing(
            'production',
            session=session(api),
            read_identity=self.identity,
            read_drop=self.read_drop,
            **kwargs,
        )

    def test_account_and_target_are_checked_before_source_read(self):
        for target, account in [
            ('staging', importer.ACCOUNT),
            ('production', '000000000000'),
        ]:
            with self.assertRaises(importer.ImportErrorSafe):
                importer.import_existing(
                    target,
                    session=session(FakeSecrets(), account),
                    read_drop=self.read_drop,
                    read_identity=self.identity,
                )
        self.read_drop.assert_not_called()
        self.identity.assert_not_called()

    def test_import_then_exact_version_current_readback_durable_receipt_and_delete(
        self,
    ):
        api = FakeSecrets()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.run_import(api)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(json.loads(api.writes[0][1]['SecretString']), DOCUMENT)
        self.assertEqual(
            api.reads[-2:],
            [
                {'SecretId': importer.SECRET, 'VersionId': result['version_id']},
                {'SecretId': importer.SECRET, 'VersionStage': 'AWSCURRENT'},
            ],
        )
        self.assertTrue(self.drop.deleted)
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt['state'], 'source_deleted')
        self.assertEqual(receipt['operation_id'], result['version_id'])
        self.assertNotIn(KEY, self.receipt_path.read_text())
        self.assertNotIn('secret_key', self.receipt_path.read_text())

    def test_matching_existing_is_read_back_without_rotating(self):
        api = FakeSecrets(DOCUMENT)
        result = self.run_import(api)
        self.assertEqual(result['version_id'], EXISTING_VERSION)
        self.assertEqual(api.writes, [])
        self.assertTrue(self.drop.deleted)

    def test_missing_deleted_source_reconciles_same_version_without_stripe_or_write(
        self,
    ):
        api = FakeSecrets()
        first = self.run_import(api)
        self.read_drop.side_effect = FileNotFoundError()
        self.identity.reset_mock()
        source = Path(self.temp.name) / 'missing-drop'
        for state in ('verified', 'source_deleted'):
            receipt = json.loads(self.receipt_path.read_text())
            importer._receipt(self.receipt_path, {**receipt, 'state': state})
            with patch.object(importer, 'SOURCE', source):
                result = self.run_import(api)
            self.assertEqual(result, {**first, 'status': 'verified_source_absent'})
        self.identity.assert_not_called()
        self.assertEqual(len(api.writes), 1)

    def test_absent_source_cannot_reconcile_unverified_or_replaced_version(self):
        api = FakeSecrets()
        self.run_import(api)
        self.read_drop.side_effect = FileNotFoundError()
        source = Path(self.temp.name) / 'missing-drop'
        receipt = json.loads(self.receipt_path.read_text())
        importer._receipt(self.receipt_path, {**receipt, 'state': 'prepared'})
        with patch.object(importer, 'SOURCE', source):
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'absent_requires_review'
            ):
                self.run_import(api)
        importer._receipt(self.receipt_path, receipt)
        api.versions[EXISTING_VERSION] = json.dumps(DOCUMENT)
        api.current = EXISTING_VERSION
        with patch.object(importer, 'SOURCE', source):
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'current_version_changed'
            ):
                self.run_import(api)
        self.assertEqual(len(api.writes), 1)

    def test_source_reappearance_is_preserved_on_completion_retry(self):
        api = FakeSecrets()
        self.run_import(api)
        self.read_drop.side_effect = FileNotFoundError()
        source = Path(self.temp.name) / 'new-drop'
        source.write_text('new-owner-input')
        with patch.object(importer, 'SOURCE', source):
            with self.assertRaisesRegex(importer.ImportErrorSafe, 'source_reappeared'):
                self.run_import(api)
        self.assertEqual(source.read_text(), 'new-owner-input')

    def test_existing_different_or_old_test_version_never_overwritten(self):
        old = {
            'version': 1,
            'account_id': ACCOUNT,
            'secret_key': 'sk_test_' + 'x' * 32,
            'publishable_key': 'pk_test_' + 'x' * 32,
            'webhook_secret': None,
        }
        for existing in ({**DOCUMENT, 'secret_key': 'sk_live_' + 'x' * 32}, old):
            api = FakeSecrets(existing)
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'rotation_not_authorized'
            ):
                self.run_import(api)
            self.assertEqual(api.writes, [])
            self.assertFalse(self.drop.deleted)

    def test_existing_empty_container_history_and_pending_deletion_are_never_written(
        self,
    ):
        api = FakeSecrets(container=True)
        with self.assertRaisesRegex(importer.ImportErrorSafe, 'requires_review'):
            self.run_import(api)
        self.assertEqual(api.writes, [])
        self.assertFalse(self.drop.deleted)
        self.receipt_path.unlink()
        for pending in (False, True):
            api = FakeSecrets(DOCUMENT)
            api.current, api.deleted = None, pending
            with self.assertRaisesRegex(importer.ImportErrorSafe, 'requires_review'):
                self.run_import(api)
            self.assertEqual(api.writes, [])

    def test_concurrent_container_creation_cannot_be_overwritten(self):
        api = FakeSecrets()

        def concurrent_create(**kwargs):
            api.exists, api.current = True, EXISTING_VERSION
            api.versions[EXISTING_VERSION] = json.dumps(
                {**DOCUMENT, 'secret_key': 'rk_live_' + 'other' * 7}
            )
            raise NotFound()

        api.describe_secret = concurrent_create
        with self.assertRaises(ClientError):
            self.run_import(api)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.current, EXISTING_VERSION)
        self.assertFalse(self.drop.deleted)
        self.assertEqual(json.loads(self.receipt_path.read_text())['state'], 'prepared')

    def test_unknown_cloud_result_retains_operation_token_and_reconciles_without_rewrite(
        self,
    ):
        api = FakeSecrets()

        def fail():
            raise TimeoutError(KEY)

        api.after_write = fail
        with self.assertRaises(TimeoutError):
            self.run_import(api)
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt['state'], 'prepared')
        self.assertEqual(receipt['operation_id'], api.current)
        self.assertFalse(self.drop.deleted)
        api.after_write = None
        self.run_import(api)
        self.assertEqual(len(api.writes), 1)
        self.assertTrue(self.drop.deleted)

    def test_exact_version_or_current_mismatch_keeps_source(self):
        for exact in (True, False):
            api = FakeSecrets()
            original = api.get_secret_value

            def response(**kwargs):
                value = original(**kwargs)
                if api.writes and ('VersionId' in kwargs) == exact:
                    value['SecretString'] = json.dumps(
                        {**DOCUMENT, 'account_id': 'acct_Different123'}
                    )
                return value

            api.get_secret_value = response
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'readback_mismatch|current_version_changed'
            ):
                self.run_import(api)
            self.assertFalse(self.drop.deleted)

    def test_source_race_or_receipt_failure_never_deletes(self):
        api = FakeSecrets()
        api.after_write = lambda: setattr(self.drop, 'changed', True)
        with self.assertRaisesRegex(importer.ImportErrorSafe, 'drop_changed'):
            self.run_import(api)
        self.assertFalse(self.drop.deleted)
        self.assertEqual(json.loads(self.receipt_path.read_text())['state'], 'verified')
        self.receipt_path.unlink()
        self.drop.changed = False
        api = FakeSecrets()
        with patch.object(importer, '_receipt', side_effect=OSError(KEY)):
            with self.assertRaises(OSError):
                self.run_import(api)
        self.assertEqual(api.writes, [])
        self.assertFalse(self.drop.deleted)

    def test_verified_receipt_failure_preserves_original_intent_and_source(self):
        api = FakeSecrets()
        original = importer._receipt

        def receipt(path, value=None):
            if value is not None and value['state'] == 'verified':
                raise OSError(KEY)
            return original(path, value)

        with patch.object(importer, '_receipt', side_effect=receipt):
            with self.assertRaises(OSError):
                self.run_import(api)
        self.assertFalse(self.drop.deleted)
        self.assertEqual(json.loads(self.receipt_path.read_text())['state'], 'prepared')
        self.run_import(api)
        self.assertEqual(len(api.writes), 1)

    def test_identical_private_partial_receipt_can_finish_without_rewrite(self):
        self.run_import(FakeSecrets())
        value = json.loads(self.receipt_path.read_text())
        temporary = self.receipt_path.with_name(
            self.receipt_path.name + '.' + value['operation_id'] + '.tmp'
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            importer._private(fd, protect=True)
            os.write(fd, json.dumps(value).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        self.assertEqual(importer._receipt(self.receipt_path, value), value)
        self.assertFalse(temporary.exists())

    def test_wrong_destination_arn_refused(self):
        api = FakeSecrets(DOCUMENT)
        api.arn = api.arn.replace(importer.ACCOUNT, '000000000000')
        with self.assertRaisesRegex(
            importer.ImportErrorSafe, 'destination_unconfirmed'
        ):
            self.run_import(api)
        self.assertFalse(self.drop.deleted)

    def test_invalid_live_identity_fails_before_receipt_or_aws_write(self):
        self.identity.return_value = {'account_id': ACCOUNT, 'mode': 'test'}
        api = FakeSecrets()
        with self.assertRaisesRegex(importer.ImportErrorSafe, 'identity_unconfirmed'):
            self.run_import(api)
        self.assertEqual(api.writes, [])
        self.assertFalse(self.receipt_path.exists())

    def test_cli_provider_and_parser_exceptions_never_print_secrets(self):
        for error in (RuntimeError(KEY), ValueError(KEY), OSError(KEY)):
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(importer, 'import_existing', side_effect=error),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(importer.main(['--target', 'production']), 1)
            self.assertNotIn(KEY, output.getvalue() + errors.getvalue())
        with (
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit),
        ):
            importer.main(['--target', KEY])
        self.assertNotIn(KEY, errors.getvalue())

    def test_sdk_child_debug_console_and_stdout_are_suppressed_then_restored(self):
        import stripe
        from stripe import _util

        api = FakeSecrets()
        output, errors, logs = io.StringIO(), io.StringIO(), io.StringIO()
        child = logging.getLogger('botocore.endpoint.stripe_import_adversarial')
        old_handlers, old_level, old_propagate = (
            child.handlers[:],
            child.level,
            child.propagate,
        )
        old_disable = logging.root.manager.disable
        child.handlers, child.propagate = [logging.StreamHandler(logs)], False
        child.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        console_level = _util._console_log_level

        def noisy_write():
            child.debug('SecretString=%s', api.versions[api.current])
            _util.log_debug('SDK diagnostic', secret_key=KEY)
            _util.log_info('SDK diagnostic', secret_key=KEY)
            print(KEY)
            print(KEY, file=sys.stderr)

        api.after_write = noisy_write
        try:
            with (
                patch.object(stripe, 'log', 'debug'),
                patch.object(_util, 'STRIPE_LOG', 'debug'),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                self.run_import(api)
                self.assertEqual(stripe.log, 'debug')
                self.assertEqual(_util.STRIPE_LOG, 'debug')
                self.assertIs(_util._console_log_level, console_level)
                self.assertEqual(logging.root.manager.disable, logging.NOTSET)
            self.assertEqual(
                output.getvalue() + errors.getvalue() + logs.getvalue(), ''
            )
            self.assertEqual(child.level, logging.DEBUG)
        finally:
            child.handlers, child.propagate = old_handlers, old_propagate
            child.setLevel(old_level)
            logging.disable(old_disable)


class StripeDropTests(unittest.TestCase):
    def test_literal_single_key_only_no_expansion_duplicates_or_test_key(self):
        for raw in (KEY, '"' + KEY + '"', "'" + KEY + "'"):
            self.assertEqual(
                importer.parse_drop(
                    ('# comment\nSTRIPE_SECRET_KEY=' + raw + '\n').encode()
                ),
                KEY,
            )
        for raw in (
            '',
            'STRIPE_SECRET_KEY=$(command)',
            'STRIPE_SECRET_KEY=$KEY',
            'export STRIPE_SECRET_KEY=' + KEY,
            'STRIPE_SECRET_KEY=sk_test_' + 's' * 32,
            'STRIPE_SECRET_KEY=' + KEY + '\nOTHER=bad',
            'STRIPE_SECRET_KEY=' + KEY + '\nSTRIPE_SECRET_KEY=' + KEY,
        ):
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, '^stripe_drop_invalid$'
            ):
                importer.parse_drop(raw.encode())

    def test_links_and_changes_are_rejected_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'drop'
            path.write_text('STRIPE_SECRET_KEY=' + KEY)
            link = Path(directory) / 'hardlink'
            os.link(path, link)
            with self.assertRaisesRegex(importer.ImportErrorSafe, 'drop_invalid'):
                with importer.DropFile(path):
                    pass
            link.unlink()
            with importer.DropFile(path) as source:
                if os.name == 'nt':
                    with self.assertRaises(PermissionError):
                        path.write_text('STRIPE_SECRET_KEY=' + KEY + 'changed')
                    with self.assertRaises(PermissionError):
                        path.rename(path.with_suffix('.changed'))
                else:
                    path.write_text('STRIPE_SECRET_KEY=' + KEY + 'changed')
                    with self.assertRaisesRegex(
                        importer.ImportErrorSafe, 'drop_changed'
                    ):
                        source.read_unchanged()
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Exact-handle Windows deletion')
    def test_windows_delete_marks_only_same_locked_synthetic_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'drop'
            path.write_text('STRIPE_SECRET_KEY=' + KEY)
            with importer.DropFile(path) as source:
                source.delete_unchanged()
            self.assertFalse(path.exists())

    def test_read_only_sdk_requires_live_price_and_current_account(self):
        client = Mock()
        client.v1.accounts.retrieve_current.return_value = {
            'id': ACCOUNT,
            'object': 'account',
        }
        client.v1.prices.list.return_value = {
            'object': 'list',
            'data': [{'object': 'price', 'livemode': True}],
        }
        self.assertEqual(
            importer.inspect_live_key(KEY, client_factory=lambda _: client), IDENTITY
        )
        client.v1.accounts.retrieve_current.assert_called_once_with()
        client.v1.prices.list.assert_called_once_with({'limit': 1})
        for rows in ([], [{'object': 'price', 'livemode': False}]):
            client.v1.prices.list.return_value = {'object': 'list', 'data': rows}
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'identity_unconfirmed'
            ):
                importer.inspect_live_key(KEY, client_factory=lambda _: client)

    def test_restricted_key_missing_permission_names_only_read_operation(self):
        import stripe

        for field, expected in (
            ('accounts', 'account_read'),
            ('prices', 'prices_read'),
        ):
            client = Mock()
            client.v1.accounts.retrieve_current.return_value = {
                'id': ACCOUNT,
                'object': 'account',
            }
            method = (
                client.v1.accounts.retrieve_current
                if field == 'accounts'
                else client.v1.prices.list
            )
            method.side_effect = stripe.PermissionError(KEY)
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'permission_required_' + expected
            ) as raised:
                importer.inspect_live_key(KEY, client_factory=lambda _: client)
            self.assertNotIn(KEY, str(raised.exception))

    def test_pinned_sdk_objects_are_read_without_dict_get_or_network(self):
        import stripe

        self.assertEqual(stripe.VERSION, '16.0.0')
        price = stripe.Price.construct_from(
            {'object': 'price', 'id': 'price_Synthetic123', 'livemode': True}, None
        )
        prices = stripe.ListObject.construct_from(
            {'object': 'list', 'data': [price], 'has_more': False, 'url': '/v1/prices'},
            None,
        )
        self.assertIsInstance(prices['data'][0], stripe.Price)
        self.assertFalse(hasattr(prices, 'get'))
        for account_type in (stripe.Account, stripe.StripeObject):
            account = account_type.construct_from(
                {'object': 'account', 'id': ACCOUNT}, None
            )
            self.assertIsInstance(account, stripe.StripeObject)
            self.assertFalse(hasattr(account, 'get'))
            client = Mock()
            client.v1.accounts.retrieve_current.return_value = account
            client.v1.prices.list.return_value = prices
            with patch.object(
                stripe,
                'StripeClient',
                side_effect=AssertionError('unexpected network client'),
            ):
                self.assertEqual(
                    importer.inspect_live_key(KEY, client_factory=lambda _: client),
                    IDENTITY,
                )
            client.v1.accounts.retrieve_current.assert_called_once_with()
            client.v1.prices.list.assert_called_once_with({'limit': 1})

        for value in (False, None):
            price = stripe.Price.construct_from(
                {'object': 'price', 'id': 'price_Synthetic123', 'livemode': value}, None
            )
            client.v1.prices.list.return_value = stripe.ListObject.construct_from(
                {
                    'object': 'list',
                    'data': [price],
                    'has_more': False,
                    'url': '/v1/prices',
                },
                None,
            )
            with self.assertRaisesRegex(
                importer.ImportErrorSafe, 'identity_unconfirmed'
            ):
                importer.inspect_live_key(KEY, client_factory=lambda _: client)

    def test_direct_identity_sdk_console_is_suppressed_and_restored_on_error(self):
        import stripe
        from stripe import _util

        old_disable = logging.root.manager.disable
        console_level = _util._console_log_level
        output, errors = io.StringIO(), io.StringIO()

        def factory(_key):
            _util.log_debug('SDK diagnostic', key=KEY)
            print(KEY, file=sys.stderr)
            raise stripe.PermissionError(KEY)

        with (
            patch.object(stripe, 'log', 'debug'),
            patch.object(_util, 'STRIPE_LOG', 'debug'),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            with self.assertRaises(stripe.PermissionError):
                importer.inspect_live_key(KEY, client_factory=factory)
            self.assertEqual(stripe.log, 'debug')
            self.assertEqual(_util.STRIPE_LOG, 'debug')
            self.assertIs(_util._console_log_level, console_level)
            self.assertEqual(logging.root.manager.disable, old_disable)
        self.assertEqual(output.getvalue() + errors.getvalue(), '')


if __name__ == '__main__':
    unittest.main()

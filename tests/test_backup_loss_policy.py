"""Original roles remain actionable; filenames never suppress a loss."""

import unittest

from studio_platform.backup import _coverage
from studio_platform.backup_loss_policy import (
    classification,
    is_derivative,
    original_loss,
    valid_classification,
)
from studio_platform.backup_missing import missing_object


def item(*roles, key='owners/example/assets/id/preview.webp'):
    return {
        'key': key,
        'provider': 'r2',
        'size_bytes': 10,
        'references': [{'table': 'assets', 'role': role} for role in roles],
    }


class BackupLossPolicyTests(unittest.TestCase):
    def test_regenerable_roles_are_expected_without_deletion_proof(self):
        for role in ('model', 'display', 'preview', 'thumb', 'thumbnail', 'poster'):
            with self.subTest(role=role):
                missing = missing_object(item(role), None, {})
                self.assertEqual(missing['classification'], 'expected_derivative')
                coverage = _coverage([], [missing], classified=True)
                self.assertEqual(coverage['expected_missing_objects'], 1)
                self.assertEqual(coverage['status'], 'incomplete')
                self.assertFalse(coverage['loss_alert_required'])

    def test_original_role_wins_over_derivative_role_and_filename(self):
        for roles in (('original',), ('video',), ('thumb', 'original'), ('unknown',)):
            with self.subTest(roles=roles):
                value = item(*roles)
                self.assertFalse(is_derivative(value))
                self.assertEqual(classification(value, {}), 'unexpected_missing')

    def test_legacy_classification_remains_verifiable_without_receipt_rewrite(self):
        value = dict(item('thumb'), classification='unexpected_missing')
        self.assertTrue(valid_classification(value, {}))
        self.assertEqual(classification(value, {}), 'expected_derivative')
        self.assertEqual(value['classification'], 'unexpected_missing')

    def test_current_policy_ignores_derivatives_but_keeps_originals(self):
        thumb = dict(item('thumb'), classification='unexpected_missing')
        original = dict(item('original'), classification='unexpected_missing')

        def manifest(values):
            return {
                'coverage': {
                    'missing_objects': len(values),
                    'missing_bytes': sum(x['size_bytes'] for x in values),
                },
                'missing_objects': values,
            }

        self.assertFalse(original_loss(manifest([thumb])))
        self.assertTrue(original_loss(manifest([thumb, original])))

    def test_unreferenced_or_mismatched_evidence_cannot_hide_original_loss(self):
        self.assertFalse(is_derivative({'references': []}))
        with self.assertRaises(ValueError):
            original_loss(
                {
                    'coverage': {'missing_objects': 1, 'missing_bytes': 20},
                    'missing_objects': [item('thumb')],
                }
            )


if __name__ == '__main__':
    unittest.main()

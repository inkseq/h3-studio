"""Actual pinned CA data and rejected stale public CApath entries; no network."""

import re
import tempfile
import unittest
from pathlib import Path

import certifi

from deploy.tls.check_trust import (
    AFFECTED,
    UNAFFECTED,
    check_bundle,
    check_capath,
    fingerprints,
)


class TrustBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patched = Path(certifi.where()).read_bytes()
        # Public root from the old hash-pinned Certifi wheel, never installed.
        cls.compromised = (
            Path(__file__).parent / 'fixtures/emsign-ecc-g3.pem'
        ).read_bytes()

    def test_real_patched_certifi_retains_unaffected_roots(self):
        result = check_bundle(self.patched)
        self.assertFalse(result['affected_g3_present'])
        self.assertEqual(set(result['unaffected_roots_present']), {'G1', 'C1', 'C3'})

    def test_exact_compromised_fixture_is_rejected(self):
        self.assertEqual(fingerprints(self.compromised), {AFFECTED})
        with self.assertRaisesRegex(ValueError, 'tls_compromised_g3_still_trusted'):
            check_bundle(self.patched + self.compromised)

    def test_blanket_emsign_removal_is_not_a_passing_patch(self):
        blocks = re.findall(
            rb'-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----',
            self.patched,
            re.S,
        )
        missing_safe_root = b'\n'.join(
            block for block in blocks if UNAFFECTED['C1'] not in fingerprints(block)
        )
        with self.assertRaisesRegex(ValueError, 'tls_unaffected_emsign_root_missing'):
            check_bundle(missing_safe_root)

    def test_stale_hashed_capath_entry_is_rejected_even_with_clean_bundle(self):
        check_bundle(self.patched)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'deadbeef.0').write_bytes(self.compromised)
            with self.assertRaisesRegex(
                ValueError, 'tls_capath_compromised_g3_still_trusted'
            ):
                check_capath(directory)

    def test_public_hashed_entries_pass_without_reading_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'deadbeef.0').write_bytes(self.patched)
            (directory / 'private-key.key').write_bytes(b'not a certificate')
            self.assertEqual(check_capath(directory)['hashed_certificates'], 1)

    def test_empty_or_non_certificate_bundles_do_not_pass(self):
        for raw in (b'', b'-----BEGIN PRIVATE KEY-----\nnot-a-ca\n'):
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(ValueError, 'tls_ca_pem_missing'),
            ):
                check_bundle(raw)


if __name__ == '__main__':
    unittest.main()

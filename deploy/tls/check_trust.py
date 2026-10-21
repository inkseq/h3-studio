"""Offline build/acceptance check of the actual Python and OpenSSL CA stores.

No trust-store edits or network. Certifi2026.10.19 restores unaffected emSign
G1/C1/C3; only the compromised G3 anchor must be absent. Hashed OpenSSL entries
are checked too, since a stale CApath link can survive a bundle-only change.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import ssl

CERTIFI_VERSION = '2026.10.19'
AFFECTED = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b'
UNAFFECTED = {
    'G1': '40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367',
    'C1': '125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f',
    'C3': 'bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3',
}
OS_BUNDLE = Path('/etc/ssl/certs/ca-certificates.crt')
CA_DIRECTORY = Path('/etc/ssl/certs')


def fingerprints(raw):
    if len(raw) > 4 * 1024**2:
        raise ValueError('tls_ca_bundle_too_large')
    blocks = re.findall(
        rb'-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----', raw, re.S
    )
    if not blocks:
        raise ValueError('tls_ca_pem_missing')
    return {
        hashlib.sha256(ssl.PEM_cert_to_DER_cert(value.decode('ascii'))).hexdigest()
        for value in blocks
    }


def check_bundle(raw):
    roots = fingerprints(raw)
    if AFFECTED in roots:
        raise ValueError('tls_compromised_g3_still_trusted')
    if not set(UNAFFECTED.values()) <= roots:
        raise ValueError('tls_unaffected_emsign_root_missing')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=raw.decode('ascii'))
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'roots': len(roots),
            'affected_g3_present': False, 'unaffected_roots_present': list(UNAFFECTED)}


def check_capath(directory):
    """Read public hashed certificate targets, never an arbitrary/private file."""
    allowed = (directory.resolve(), Path('/usr/share/ca-certificates'),
               Path('/usr/local/share/ca-certificates'))
    entries = sorted(directory.iterdir())
    if len(entries) > 4096:
        raise ValueError('tls_capath_too_large')
    count = 0
    for entry in entries:
        if re.fullmatch(r'[0-9a-f]{8}\.[0-9]+', entry.name) is None:
            continue
        try:
            target = entry.resolve(strict=True)
        except FileNotFoundError:
            continue  # OpenSSL also ignores dangling links.
        if not any(target.is_relative_to(root) for root in allowed):
            raise ValueError('tls_capath_nonpublic_target')
        if (target.suffix not in {'.pem', '.crt'}
                and re.fullmatch(r'[0-9a-f]{8}\.[0-9]+', target.name) is None):
            raise ValueError('tls_capath_noncertificate_target')
        if not target.is_file() or target.stat().st_size > 1024**2:
            raise ValueError('tls_capath_invalid_certificate')
        if AFFECTED in fingerprints(target.read_bytes()):
            raise ValueError('tls_capath_compromised_g3_still_trusted')
        count += 1
    if not count:
        raise ValueError('tls_capath_empty')
    return {'hashed_certificates': count, 'affected_g3_present': False}


def main():
    import certifi

    if importlib.metadata.version('certifi') != CERTIFI_VERSION:
        raise ValueError('tls_certifi_version_mismatch')
    paths = ssl.get_default_verify_paths()
    if (not paths.cafile or Path(paths.cafile).resolve() != OS_BUNDLE.resolve()
            or not paths.capath or Path(paths.capath).resolve() != CA_DIRECTORY):
        raise ValueError('tls_default_verify_paths_unexpected')
    print(json.dumps({'state': 'actual_python_trust_checked',
        'certifi_version': CERTIFI_VERSION, 'openssl': ssl.OPENSSL_VERSION,
        'certifi': check_bundle(Path(certifi.where()).read_bytes()),
        'os_bundle': check_bundle(OS_BUNDLE.read_bytes()),
        'openssl_capath': check_capath(CA_DIRECTORY), 'network_calls': 0}, sort_keys=True))


if __name__ == '__main__':
    main()

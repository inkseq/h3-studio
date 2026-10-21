// Official Node22 security patch; this is a build/CI consumer, not product runtime.
import { X509Certificate } from 'node:crypto';
import { getCACertificates, rootCertificates } from 'node:tls';

const expectedVersion = '22.23.4';
const affected = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b';
const unaffected = [
  '40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367',
  '125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f',
  'bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3',
];
const fingerprints = (certificates) => new Set(certificates.map((pem) =>
  new X509Certificate(pem).fingerprint256.replaceAll(':', '').toLowerCase()));
const bundled = fingerprints(rootCertificates);
const defaults = fingerprints(getCACertificates('default'));
if (process.versions.node !== expectedVersion ||
    [bundled, defaults].some((roots) => roots.has(affected) ||
      !unaffected.every((fingerprint) => roots.has(fingerprint)))) {
  throw new Error('node_security_patch_or_trust_store_mismatch');
}
console.log(JSON.stringify({ state: 'node_build_trust_checked', node: process.versions.node,
  openssl: process.versions.openssl, bundled_roots: bundled.size, default_roots: defaults.size,
  bundled_affected_g3_present: false, default_affected_g3_present: false,
  bundled_unaffected_roots_retained: true, default_unaffected_roots_retained: true,
  network_calls: 0 }));

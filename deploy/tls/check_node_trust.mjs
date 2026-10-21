// Official Node22 security patch; this is a build/CI consumer, not product runtime.
import { X509Certificate } from 'node:crypto';
import { getCACertificates, rootCertificates } from 'node:tls';

const expectedVersion = '22.23.4';
const affected = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b';
const retainedG1 = '40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367';
// Official22.23.4 removes C1/C3 as well as G3. Do not inject them back.
const upstreamAbsent = [
  '125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f',
  'bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3',
];
const fingerprints = (certificates) => new Set(certificates.map((pem) =>
  new X509Certificate(pem).fingerprint256.replaceAll(':', '').toLowerCase()));
const bundled = fingerprints(rootCertificates);
const defaults = fingerprints(getCACertificates('default'));
const extra = fingerprints(getCACertificates('extra'));
if (process.versions.node !== expectedVersion ||
    bundled.has(affected) || !bundled.has(retainedG1) ||
    upstreamAbsent.some((fingerprint) => bundled.has(fingerprint)) ||
    defaults.has(affected) || !defaults.has(retainedG1) || extra.has(affected)) {
  throw new Error('node_security_patch_or_trust_store_mismatch');
}
// Report selected public mode flags only, never paths or raw environment values.
const options = process.env.NODE_OPTIONS || '';
const system = process.execArgv.includes('--use-system-ca') ||
  options.includes('--use-system-ca') || process.env.NODE_USE_SYSTEM_CA === '1';
const openssl = process.execArgv.includes('--use-openssl-ca') || options.includes('--use-openssl-ca');
console.log(JSON.stringify({ state: 'node_build_trust_checked', node: process.versions.node,
  openssl: process.versions.openssl, bundled_roots: bundled.size, default_roots: defaults.size,
  bundled_sha256_der: [...bundled].sort(), default_sha256_der: [...defaults].sort(),
  bundled_affected_g3_present: false, default_affected_g3_present: false,
  bundled_g1_retained: true, default_g1_retained: true,
  bundled_c1_c3_upstream_absent: true,
  default_c1_present: defaults.has(upstreamAbsent[0]), default_c3_present: defaults.has(upstreamAbsent[1]),
  extra_ca_configured: Boolean(process.env.NODE_EXTRA_CA_CERTS), extra_ca_roots: extra.size,
  extra_ca_affected_g3_present: false, use_system_ca: system, use_openssl_ca: openssl,
  default_equals_bundled: bundled.size === defaults.size && [...bundled].every((fp) => defaults.has(fp)),
  network_calls: 0 }));

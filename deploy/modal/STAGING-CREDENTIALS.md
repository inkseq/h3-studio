# Staging controller credentials

Refs #774 / #688. The CPU container cannot use the host EC2 role with the
existing IMDSv2 response hop limit of 1. Keep that isolation boundary.

Use the existing host role to read only AWSCURRENT of
`/sixnine/staging/modal`. The reviewed inert helper
`deploy/platform/modal_credentials.py` validates account, region, name, version
and the two existing token fields. It creates no secret/version and does not
rotate a differing runtime file. Install the reviewed helper and its pure
`studio_platform.modal_credentials` dependency through the existing gated
host-helper adoption. Do not copy credentials into a project env file.

Under a fresh staging release/image/source CAS, with zero jobs and leases,
demand OFF and Modal OFF, invoke `hydrate(client(), write_runtime,
target='staging')` once. Keep the original adoption journal, secret version
metadata and saved configuration bytes; never log the value. Its fixed output
is `/srv/sixnine/dstack-runtime/modal-credentials.json`, root:10001 mode0440.
The existing CPU runtime bind exposes that exact file at
`/dstack-runtime/modal-credentials.json`, outside business snapshots.

Add this explicit field to the existing version1 `modal.json`:

```json
{"credentials_file":"/dstack-runtime/modal-credentials.json"}
```

Retain the original complete HostSpec, version and enabled flag. A selected
file that is absent, malformed or unprotected refuses; it never falls back to
another secret source. The file path must be the fixed filename in that
controller work directory. The optional field is CPU configuration only and
never enters HostSpec, provider create requests or the GPU sandbox.

Read back existing-role hydration, exact runtime file protection, the pinned
App lookup and OFF configuration before any activation. Then use the existing
zero-work enable/restart gate and fresh CR-73 combined spend projection for
one original staging job. Charging remains OFF. Preserve each unknown original
outcome and reconcile read-only; do not repeat a create, generation or secret
write. After testing, close admission and stop the original hosts normally.

This change does not authorize production Modal or alter release/promotion
ownership, IAM policies, metadata settings, budgets, queues or settlement.

# CPU migration secret hydration

`migration_secrets_aws.py` is a host-only adjunct to the original
`runtime_secrets_aws.py`. It never changes the original business database,
bootstrap accounts, Google naming configuration or their secret mounts. Imports
are inert; Python standard library plus the existing host boto3 installation are
sufficient. It neither invokes providers nor transfers any key to a GPU.

The release owner securely seeds AWS Secrets Manager
`/sixnine/platform/migration` in `ap-southeast-1`, retaining existing provider
identities and the existing validated Grafana Cloud exporter configuration. The
closed JSON schema is:

```json
{
  "schema_version": 1,
  "providers": {
    "vastai": {
      "profile_id": "EXPLICIT_EXISTING_PROFILE_ID",
      "base_url": "https://console.vast.ai/api",
      "api_key": "SET_USING_PROTECTED_TRANSPORT"
    }
  },
  "grafana_cloud": {
    "schema_version": 1,
    "enabled": true,
    "endpoint": "https://otlp-gateway-REGION.grafana.net/otlp",
    "authorization": "SET_USING_PROTECTED_TRANSPORT",
    "service_name": "sixnine-worker",
    "service_version": "unknown",
    "environment": "staging"
  }
}
```

The uppercase placeholders cannot authenticate and must not be used as actual
configuration. An optional `runpod` entry uses its explicit `runpod--...`
profile and exact `https://api.runpod.io/graphql` base URL. At least one provider
is required; keys and destinations are validated together. Endpoint mismatch
fails without a fallback. This schema is not a new Registry profile store.

First infrastructure initialization requires a **completely absent**
`infrastructure` field, explicit `--initialize`, and a protected root-owned 0600
nonsecret receipt supplied through `--bootstrap-checks`. The release owner must
first read the dedicated PostgreSQL catalog to confirm both the intended
`sixnine_dstack` database and `dstack_control` role have no prior state; the helper
does not receive the business database administrator credential. Receipt fields
are `schema_version:1`, `infrastructure_database_absent:true`, `checked_at` (UTC
ISO timestamp within 300 seconds), `host_id` matching
`/var/lib/cloud/data/instance-id`, `hatchet_postgres_volume`,
`hatchet_config_volume`, `dstack_state_dir`, and `dstack_runtime_dir`.

The helper independently requires a successful Docker volume listing with both
selected volume names absent, absent/empty protected dstack state/runtime
directories, and no earlier infrastructure runtime mounts. Failed listing,
wrong host, expired receipt, existing volumes/state or partial infrastructure
requires recovery of original credentials. An approval receipt does not bypass
those presence checks. Initialize **before** creating the selected engine
volumes, dedicated database/role or server state. Do not delete old state merely
to make this check pass.

Initialization adds exactly `dstack_admin_token`, `dstack_database_url`,
`hatchet_database_password`, and `hatchet_administrator_password` to the
`infrastructure` dictionary and persists that complete managed version before
hydrating files. DSN is constrained to
`postgresql+asyncpg://dstack_control:...@db:5432/sixnine_dstack`; the release owner
separately provisions that least-privilege role/database with this identity.
An existing complete dictionary is always reused, including with `--initialize`.
A missing version cannot reconstruct provider credentials. Before any managed
write, a root/root 0600 provisional intent is durably fsynced at
`/var/lib/sixnine/migration-secret-initialization/intent.json` (protected 0700
directory). It retains the original seed VersionId, one fixed ClientRequestToken
and generated infrastructure; provider/Grafana credentials are **not** copied to
that journal. The original seed remains retrievable by its exact managed
VersionId. A lost acknowledgement or failed subsequent read leaves this intent
intact. Every later invocation reconciles that exact target VersionId and, when
necessary, retries only the identical payload/token; it never generates another
identity. The AWS token is the new version ID and makes retries idempotent.
This follows the documented [PutSecretValue idempotency contract](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_PutSecretValue.html).
Missing original version or conflicting current identity requires recovery.
Do not delete/archive this intent while initialization is unconfirmed. A host
lock and repeated managed-version read also guard local/observed concurrent
initialization; first bootstrap still has one release owner.

After the engine starts, root captures the existing Hatchet tenant API token
using the protected bootstrap path, persists it as optional `hatchet_api_token`
in this same secret, and rehydrates. The helper does not create broker tokens or
change an existing token. Revoke temporary `PutSecretValue` authority after
infrastructure/token persistence; recurring hydration only needs
`GetSecretValue` for the exact managed ARN. Existing encrypted-secret/KMS policy
must also allow the host instance role to read this ARN.

Run as root with only nonsecret arguments:

```sh
python3 /opt/sixnine-release/migration_secrets_aws.py \
  --initialize --bootstrap-checks /var/lib/sixnine/migration-bootstrap-approval.json
python3 /opt/sixnine-release/migration_secrets_aws.py
```

Protected CPU outputs beneath `/run/sixnine-secrets` match the overlay:

| File | Owner/group | Mode | Mount use |
| --- | --- | --- | --- |
| `dstack-server-config.yml` | root/root | 0600 | CPU dstack provider configuration, JSON-compatible YAML |
| `dstack_database_url` | root/root | 0600 | dedicated infrastructure DSN |
| `dstack_api_token` | root/10001 | 0640 | CPU dstack and app/controller |
| `grafana_cloud` | 10001/10001 | 0600 | closed CPU safe telemetry exporter |
| `hatchet/database-password` | root/root | 0600 | Hatchet PostgreSQL |
| `hatchet/administrator-password` | root/root | 0600 | Hatchet administrator |
| `hatchet_api_token` (when persisted) | 10001/10001 | 0600 | dispatcher/controller broker authentication |

Directories are root/root 0700. The entire output set is validated before any
write. Existing content, ownership and modes must match; repeated hydration keeps
Docker's bound inode. Differing bytes fail pending an explicit coordinated
rotation/release. Symlink, hardlink, unprotected parent and wrong-owner mounts
are rejected. App-only changes cannot replace the shared network namespace
without reconnecting its controller/engine members.

The release owner configures separate nonsecret native runtime, operator policy
and broker templates outside the checkout. The broker selects loopback HTTP
8888/gRPC 7077 and `/run/secrets/hatchet_api_token`; dstack selects 3000. Provider
configuration must be mounted only on CPU dstack. Keep the reviewed overlay's
`--log-level WARNING` and raw OTel enable variables absent. No secret-valued
`.env`, command argument, log statement or GPU run spec is required.

Scoped verification uses only synthetic secrets/fake AWS and isolated root
filesystem tests: `python3 -m unittest test_migration_secrets_aws -q`. Linux root
is needed to exercise real UID/mode, symlink/hardlink and bound-inode checks;
other platforms report those tests skipped. A successful local hydration test
is not a managed-secret deployment, provider rental or inference receipt.

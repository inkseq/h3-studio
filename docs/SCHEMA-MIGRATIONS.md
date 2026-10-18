# Platform schema baseline

Revision `20261019_0001` freezes the 59 platform tables / 444 columns at
`35b3b20abeaeeee8d985d898f41274fa86f32886`. It includes authentication,
connection, storage receipt and multipart tables. It imports no live models.
`schema_registry.metadata_families()` is the explicit current-model inventory;
it constructs metadata only, never a service, engine or provider.

This change introduces an **adoption operation**, not evidence that any live
database has already adopted it. Record the actual staging / production command
and result separately. Existing startup DDL remains for rollback compatibility;
it has not been replaced by a competing initializer. Future schema changes need
a new reviewed Alembic revision, not another ad-hoc startup ALTER. Removing all
legacy DDL entry points is a later, separately tested compatibility change.

## Original schema owners

| Metadata family | Defined in | Tables | Original initialization |
| --- | --- | ---: | --- |
| repository | `repository.py`; extended by `operator_capacity.py`, `capacity_market.py`, `dstack_workspace.py` | 38 | `Repository.create_schema()` |
| assets | `assets.py` | 1 | `AssetService` via `create_storage_schema`; repository startup |
| asset journal / quota / accounting | `storage_asset_journal.py` | 3 | journal via `create_storage_schema`; repository startup |
| multipart | `storage_multipart.py` | 2 | multipart service via `create_storage_schema`; repository startup |
| artifact output receipt | `artifact_writer.py` | 1 | writer via `create_storage_schema`; repository startup |
| batches | `batches.py` | 1 | `BatchService.__init__` directly `metadata.create_all` |
| guided edit receipts | `guided.py` | 1 | `Guided.__init__` transaction / startup advisory lock |
| project activity | `project_activity.py` | 1 | same Guided transaction |
| quick chat | `quick_chat.py` | 3 | service constructor transaction / startup advisory lock |
| authentication | `auth.py` | 5 | `Auth.__init__` transaction / startup advisory lock; ensures original documents table |
| Agent connection | `agent_connect.py` | 3 | service constructor transaction / startup advisory lock; ensures original documents table |

Other platform initialization callsites are `manage.main(init-db)` (repository,
auth, assets, batches, upload and multipart), `api.create_app`, `fleet`'s two
repository startup paths, `worker.main`, `render_cli.main`,
`tools/check_platform_stack.py`, and `tools/soak_platform.py`.
`backup.py:_create_business_schema` also creates the registered **isolated portable SQLite
recovery** schema; it is not a production migration entry point.

`Repository.create_schema()` retains seven explicit legacy operations:

1. Add `platform_instance_intents.provider`, server default `unknown`.
2. On PostgreSQL only, relax `hard_deadline NOT NULL`; preserve all old values.
3. Add `platform_registered_workers.drain_requested`, default `0`, then set
   existing `state='draining'` rows to `1`.
4. Add `platform_scaler_actions.application_idle_since`.
5. Add nullable `platform_budget_reservations.settled_at`; never invent dates.
6. Add nullable `platform_jobs.cancellation`.
7. Add nullable `platform_outbox.job_event_seq`, then ensure the original unique
   `platform_outbox_job_event_order` index.

The other `create_all` calls do not add existing-table columns. The repository
and storage/auth/guided/quick-chat paths use the original advisory transaction
lock `685939796868749721`; batch initialization and `manage`'s extra calls remain
listed compatibility paths, not silently rewritten in this baseline batch.

Out of scope: `server.py`'s legacy `uploads/jobs/auth_sessions` SQLite database,
`password_auth.py`'s legacy password database, and
`runtime_hosts/wangp_receipts.py`'s native worker-local `metadata/operations`
SQLite receipt database. They are not in the platform PostgreSQL database and
must not be stamped by this command. `deploy/platform/init_database.py`
provisions the existing DB / role / schema permissions, not business tables.
DBOS tables are not included or created.

## Verification and guarded adoption

Use the accepted application image and its existing protected database setting.
Never pass a DSN/password on the command line, invoke `manage init-db` for a
read-only inspection, or import `platform_app` to inspect a database.

```sh
python -m studio_platform.schema_baseline check
python -m studio_platform.schema_baseline sql --dialect postgresql
# Only after reviewed staging proof, a current verified backup, release lock,
# and the exact preceding check receipt:
python -m studio_platform.schema_baseline adopt --expected-fingerprint <64-hex-check-fingerprint>
```

`check` is PostgreSQL/public only and sets `READ ONLY` / a 30-second statement
timeout. It compares full types/lengths, nullability, literal server defaults,
PK column order, unique/FK/CHECK constraints, explicit indexes and options.
Named unique constraints are retained. It rejects unknown tables, application
schemas, views/materialized views, triggers, generated/identity columns,
nonordinary/unlogged/partitioned/RLS tables, unvalidated/deferred constraints,
custom column collations and invalid indexes. It reports only schema identity,
counts and static drift locations, never data or driver/DSN details.
PostgreSQL's default `FLOAT` / `DOUBLE PRECISION` spelling, default literal casts
and deparsed parentheses around the two simple AND-only CHECKs are explicitly
normalized. Arbitrary expressions are not normalized into equality.

Physical column order is intentionally not a schema change: production appended
`platform_jobs.cancellation` after `result`, while newer staging created it
before `result`. Both have the same typed, named columns and constraints. No
row rewrite or table rebuild is needed. Old SQLite files may retain the legacy
`hard_deadline NOT NULL`; that difference is reported, never silently repaired.

`adopt` is one caller-owned transaction under the **same original advisory
lock**, with a five-second lock timeout and 30-second statement timeout. The
operator must also hold the existing host `release.lock` to exclude startup / a
simultaneous release. It validates runtime metadata against frozen history,
checks the exact schema fingerprint, then:

- Matching populated database: Alembic creates only `alembic_version` and inserts
  `20261019_0001`. It does not execute the 59 CREATEs, update business rows,
  rewrite JSON/media or touch jobs, counters, settlements or credentials.
- Empty database: Alembic applies the explicit frozen CREATE TABLE / INDEX
  revision and records its version.
- Already-adopted exact database: return `already_adopted`, with no writes.
- Any drift, unknown version, malformed version table, stale fingerprint or
  post-operation mismatch: refuse and roll back the PostgreSQL transaction.

The command's before/after JSON is the operational receipt; retain it with the
normal protected release evidence. Generic online `alembic upgrade/stamp` has
no configured connection and is refused by `env.py`; only this verified caller
supplies its existing transaction. Destructive baseline downgrade is refused.
Restoring an accepted application/DB backup remains the rollback procedure.
There is no background migration daemon and adoption is not automatic startup.

Portable business backups recognize only the exact known `alembic_version`
shape/revision as bookkeeping. They exclude it alongside the existing excluded
auth tables; unknown/malformed revisions still refuse backup. A portable SQLite
recovery intentionally contains business tables only, so it must not claim the
full 59-table authentication-inclusive baseline. Initialize the normal full
schema and recheck/re-adopt the recovered PostgreSQL database explicitly. The
normal full PostgreSQL dump/rehearsal continues to retain the version table.
The no-Alembic `schema_version.py` reader ships in the preceding compatibility
release, before the writer. Before live adoption, verify both current and
previous rollback images include that reader; the writer must not be the first
release that teaches backup to recognize its new marker. The reader constant
and frozen revision must agree or adoption source refuses to load.

## Verification and package decisions

`test_schema_baseline` runs against temporary SQLite by default. In the existing
PostgreSQL CI service it uses only a new `baseline_test_<uuid>` schema under the
explicit loopback `sixnine_test` database, then removes that test schema. It
covers empty initialization, populated financial-row preservation, idempotency,
drift/CAS refusal and PostgreSQL concurrent adoption / transactional rollback.
It is explicitly included in the original PostgreSQL shard registry.

Alembic `1.20.0`, Mako `1.4.3` and MarkupSafe `3.0.4` are exact additions to the
existing lock; SQLAlchemy `2.0.54` and all previous pins remain unchanged. No new
native/GPU image or credentials are needed. The application Docker allowlist
includes only the exact migration source paths.

Primary references:
[Alembic changelog](https://alembic.sqlalchemy.org/en/latest/changelog.html),
[autogenerate limitations and multiple metadata collections](https://alembic.sqlalchemy.org/en/latest/autogenerate.html),
[existing connections / create-and-stamp cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html),
[Alembic PyPI metadata](https://pypi.org/project/alembic/1.20.0/).
Autogeneration produced the initial candidate source once; it is not runtime
permission to guess a migration or stamp an arbitrary populated database.

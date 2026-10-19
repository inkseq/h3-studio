"""Real isolated local PostgreSQL only; no app startup, providers or SDK executor."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

from studio_platform.backup import (
    backup_postgres_engine,
    restore_local,
    verify_local,
    dump_postgres,
)
from studio_platform.schema_baseline import (
    BaselineError,
    adopt_baseline,
    adopt_workflows,
    compare_schema,
    frozen_metadata,
)
from studio_platform.storage import LocalObjectStore
from studio_platform.workflow_backup import export, inspect_file, read, restore
from studio_platform.workflow_schema import (
    WORKFLOW_REVISION,
    WorkflowSchemaError,
    assert_execution_allowed,
    validate,
)

NATIVE_PG_DUMP = os.environ.get('PLATFORM_TEST_PG_DUMP', 'pg_dump')


@unittest.skipUnless(
    os.environ.get('PLATFORM_TEST_DATABASE_URL'),
    'Explicit isolated local PG URL required',
)
class WorkflowStateTests(unittest.TestCase):
    def setUp(self):
        parsed = make_url(os.environ['PLATFORM_TEST_DATABASE_URL'])
        if (
            parsed.host not in {'localhost', '127.0.0.1'}
            or parsed.database != 'sixnine_test'
        ):
            raise RuntimeError('test_database_must_be_explicit_local_sixnine_test')
        self.parsed = parsed
        self.bootstrap = create_engine(
            parsed, isolation_level='AUTOCOMMIT', hide_parameters=True
        )
        self.databases = []
        self.engines = []
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-state-')
        self.root = Path(self.temp.name)
        self.engine = self.database()
        frozen_metadata().create_all(self.engine)
        with self.engine.begin() as connection:
            before = compare_schema(connection)
            adopt_baseline(
                connection, expected_fingerprint=before['schema_fingerprint']
            )

    def database(self):
        # Own fresh database, not an existing test/product schema. The synthetic
        # CI role and local opt-in postgres role own only these UUID resources.
        name = 'workflow_test_' + uuid.uuid4().hex
        self.bootstrap.connect().close()
        with self.bootstrap.connect() as connection:
            connection.exec_driver_sql('CREATE DATABASE "' + name + '"')
        self.databases.append(name)
        engine = create_engine(self.parsed.set(database=name), hide_parameters=True)
        self.engines.append(engine)
        return engine

    def tearDown(self):
        for engine in self.engines:
            engine.dispose()
        for name in reversed(self.databases):
            with self.bootstrap.connect() as connection:
                connection.exec_driver_sql('DROP DATABASE "' + name + '"')
        self.bootstrap.dispose()
        self.temp.cleanup()

    def adopt(self, engine=None):
        with (engine or self.engine).begin() as connection:
            before = compare_schema(connection)
            return adopt_workflows(
                connection, expected_fingerprint=before['schema_fingerprint']
            )

    def seed(self):
        self.adopt()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO inkseq_workflows.workflow_status (workflow_uuid,status,name,application_version,application_name,attributes) VALUES ('original-workflow','PENDING','inkseq.generation.v1','inkseq-workflows-v1','inkseq-workflows','null'::jsonb)"
            )
            connection.exec_driver_sql(
                "INSERT INTO inkseq_workflows.workflow_input (workflow_uuid,inputs) VALUES ('original-workflow','not_deserialized:original-private-bytes')"
            )
            connection.exec_driver_sql(
                "INSERT INTO inkseq_workflows.operation_outputs (workflow_uuid,function_id,output) VALUES ('original-workflow',1,'original-step-output')"
            )
            connection.exec_driver_sql(
                "INSERT INTO inkseq_workflows.event_dispatch_kv (service_name,workflow_fn_name,key,value,update_seq,update_time) VALUES ('test','test','exact',NULL,12345678901234567890123456789012345678,12345678901234567890123.123456789012345)"
            )
            connection.exec_driver_sql(
                "INSERT INTO inkseq_workflows.workflow_schedules (schedule_id,schedule_name,workflow_name,schedule,status,context) VALUES ('schedule-original','qa','qa','* * * * *','ACTIVE','opaque')"
            )

    def test_additive_transaction_rolls_back_then_preserves_public_and_marker(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO platform_budget_accounts (id,tenant_id,owner_id,limit_microusd,reserved_microusd,spent_microusd) VALUES ('original','tenant','owner',1000000,123,456)"
            )
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.engine.begin() as connection:
                before = compare_schema(connection)
                adopt_workflows(
                    connection, expected_fingerprint=before['schema_fingerprint']
                )
                raise RuntimeError('rollback')
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                '20261019_0001',
            )
            self.assertIsNone(
                connection.exec_driver_sql(
                    "SELECT 1 FROM pg_namespace WHERE nspname='inkseq_workflows'"
                ).first()
            )
        self.adopt()
        with self.engine.begin() as connection:
            report = compare_schema(connection)
            self.assertTrue(report['matches'])
            self.assertEqual((report['table_count'], report['column_count']), (59, 444))
            self.assertEqual(report['recorded_revision'], WORKFLOW_REVISION)
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts WHERE id='original'"
                ).one(),
                (123, 456),
            )
            connection.exec_driver_sql(
                'ALTER TABLE inkseq_workflows.workflow_input ADD COLUMN unknown text'
            )
            with self.assertRaisesRegex(
                WorkflowSchemaError, 'workflow_schema_requires_review'
            ):
                validate(connection)

    def test_backup_same_snapshot_keeps_original_sdk_ids_precision_and_recovery_hold(
        self,
    ):
        self.seed()
        changed = False

        def after_cursor(conn, cursor, statement, params, context, many):
            nonlocal changed
            if (
                not changed
                and 'information_schema.tables WHERE table_schema=' in statement
            ):
                changed = True
                with self.engine.begin() as writer:
                    writer.exec_driver_sql(
                        "UPDATE inkseq_workflows.workflow_input SET inputs='new-after-snapshot'"
                    )

        event.listen(self.engine, 'after_cursor_execute', after_cursor)
        store = LocalObjectStore(self.root / 'objects')
        try:
            backup_postgres_engine(self.engine, store.root, self.root / 'backup')
        finally:
            event.remove(self.engine, 'after_cursor_execute', after_cursor)
        self.assertTrue(changed)
        manifest = verify_local(self.root / 'backup')
        sidecar = read(self.root / 'backup', manifest['workflow_state'])
        self.assertEqual(
            sidecar['tables']['workflow_input'][0]['inputs'],
            'not_deserialized:original-private-bytes',
        )
        self.assertEqual(
            sidecar['tables']['event_dispatch_kv'][0]['update_seq'],
            '12345678901234567890123456789012345678',
        )
        self.assertEqual(
            sidecar['tables']['event_dispatch_kv'][0]['update_time'],
            '12345678901234567890123.123456789012345',
        )
        self.assertEqual(sidecar['tables']['workflow_status'][0]['attributes'], 'null')
        restored = self.database()
        frozen_metadata().create_all(restored)
        with restored.begin() as connection:
            report = compare_schema(connection)
            adopted = adopt_baseline(
                connection, expected_fingerprint=report['schema_fingerprint']
            )
            adopt_workflows(
                connection, expected_fingerprint=adopted['after']['schema_fingerprint']
            )
            result = restore(
                connection, self.root / 'backup', manifest['workflow_state']
            )
            self.assertFalse(result['execution_enabled'])
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT workflow_uuid,status,attributes::text FROM inkseq_workflows.workflow_status'
                ).one(),
                ('original-workflow', 'PENDING', 'null'),
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT status FROM inkseq_workflows.workflow_schedules'
                ).scalar_one(),
                'PAUSED',
            )
            with self.assertRaisesRegex(
                WorkflowSchemaError, 'workflow_restored_execution_requires_review'
            ):
                assert_execution_allowed(connection)
            with self.assertRaisesRegex(
                WorkflowSchemaError, 'workflow_restore_target_not_empty'
            ):
                restore(connection, self.root / 'backup', manifest['workflow_state'])
        restore_local(self.root / 'backup', self.root / 'isolated')
        self.assertEqual(
            (self.root / 'backup/workflow-state.json').read_bytes(),
            (self.root / 'isolated/workflow-state.json').read_bytes(),
        )
        self.assertIn(
            'workflow_state',
            json.loads((self.root / 'isolated/RECOVERY-HOLD.json').read_bytes()),
        )

    def test_sdk_export_requires_same_readonly_repeatable_snapshot_and_hash(self):
        self.seed()
        output = self.root / 'sidecar'
        output.mkdir()
        with self.engine.begin() as connection:
            with self.assertRaisesRegex(
                WorkflowSchemaError, 'workflow_backup_same_snapshot_required'
            ):
                export(connection, output)
        with (
            self.engine.connect().execution_options(
                isolation_level='REPEATABLE READ'
            ) as connection,
            connection.begin(),
        ):
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            expected = export(connection, output)
        raw = (output / 'workflow-state.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected['sha256'])
        (output / 'workflow-state.json').write_bytes(
            raw.replace(b'original-step-output', b'tampered-step-output')
        )
        with self.assertRaisesRegex(
            WorkflowSchemaError, 'workflow_backup_checksum_mismatch'
        ):
            read(output, expected)

    @unittest.skipUnless(
        shutil.which(NATIVE_PG_DUMP), 'Local pinned PostgreSQL tools required'
    )
    def test_native_dump_uses_same_exported_snapshot_and_retains_sdk_companion(self):
        self.seed()
        source = self.root / 'synthetic-url'
        # Preserve the explicit CI/local password. Trust-only local clusters need
        # a nonempty synthetic value to exercise the protected URL-file contract.
        url = self.engine.url.set(
            password=self.engine.url.password or 'synthetic-local-only'
        ).render_as_string(hide_password=False)
        source.write_text(url)
        source.chmod(0o600)
        result = dump_postgres(
            source,
            self.root / 'native',
            pg_dump=NATIVE_PG_DUMP,
            include_workflows=True,
        )
        self.assertFalse(result['restore_verified'])
        manifest = json.loads((self.root / 'native/manifest.json').read_bytes())
        self.assertTrue(manifest['consistent_business_workflow_snapshot'])
        self.assertEqual(
            manifest['database_sha256'],
            hashlib.sha256(
                (self.root / 'native/database.dump').read_bytes()
            ).hexdigest(),
        )
        value = read(self.root / 'native', manifest['workflow_state'])
        self.assertEqual(
            value['tables']['workflow_status'][0]['workflow_uuid'], 'original-workflow'
        )
        self.assertFalse(manifest['authentication_included'])

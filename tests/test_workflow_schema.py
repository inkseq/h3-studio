"""No executor or migration runs while constructing the shared contract."""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine

from studio_platform.workflow_schema import (
    WORKFLOW_APPLICATION_VERSION,
    WorkflowSchemaError,
    dbos_client_config,
    dbos_config,
    frozen_catalog,
)


class WorkflowContractTests(unittest.TestCase):
    def test_config_has_explicit_stable_identity_and_no_startup_ddl(self):
        with patch.dict('sys.modules', {'dbos': None}):
            config = dbos_config(
                'postgresql+psycopg://unused@127.0.0.1/unused',
                executor_id='inkseq-workflows-staging',
            )
        self.assertEqual(config['application_version'], WORKFLOW_APPLICATION_VERSION)
        self.assertFalse(config['run_migrations'])
        self.assertIsNone(config['conductor_key'])
        self.assertEqual(config['sys_db_pool_size'], 5)
        self.assertEqual(config['notification_listener_polling_interval_sec'], 10)
        self.assertEqual(config['scheduler_polling_interval_sec'], 10)

    def test_producer_reuses_engine_without_connecting_or_sdk_import(self):
        engine = create_engine('postgresql+psycopg://unused@127.0.0.1/unused')
        try:
            with (
                patch.object(engine, 'connect', side_effect=AssertionError),
                patch.dict('sys.modules', {'dbos': None}),
            ):
                config = dbos_client_config(engine)
            self.assertIs(config['system_database_engine'], engine)
            self.assertTrue(config['lazy'])
            self.assertFalse(config['retry_connection_errors'])
            self.assertEqual(config['dbos_system_schema'], 'inkseq_workflows')
            self.assertNotIn('run_migrations', config)
        finally:
            engine.dispose()

    def test_config_refuses_unscoped_executor_or_non_postgresql(self):
        for url, executor in [('sqlite://', None), ('postgresql+psycopg://x', 'other')]:
            with self.assertRaises(WorkflowSchemaError):
                dbos_config(url, executor_id=executor)

    def test_frozen_family_is_exact_thirteen_sdk_tables_no_business_or_auth(self):
        tables = {
            r['relname'] for r in frozen_catalog()['relations'] if r['relkind'] == 'r'
        }
        self.assertEqual(len(tables), 13)
        self.assertIn('workflow_status', tables)
        self.assertIn('operation_outputs', tables)
        self.assertFalse(any(t.startswith('platform_') for t in tables))

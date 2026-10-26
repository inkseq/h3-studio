"""Original backup sidecar for one exact SDK schema; never an executor.

Business rows and SDK rows are read through the caller's SAME read-only snapshot.
Values use PostgreSQL text representations, retaining numeric precision and SQL
NULL versus JSON null. Serialized workflow payloads are never deserialized.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import os
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from .workflow_schema import (
    CATALOG_SHA256,
    INKSEQ_WORKFLOW_SCHEMA,
    WORKFLOW_APPLICATION_VERSION,
    WORKFLOW_SDK_VERSION,
    canonical,
    frozen_catalog,
    require,
    validate,
)

FILE = 'workflow-state.json'
FORMAT = 'inkseq-workflow-state-v1'
MAX_BYTES = 64 * 1024**2
MAX_ROWS = 1_000_000


def tables():
    frozen = frozen_catalog()
    names = sorted(r['relname'] for r in frozen['relations'] if r['relkind'] == 'r')
    return {
        name: {
            c['attname']: c['type'] for c in frozen['columns'] if c['relname'] == name
        }
        for name in names
    }


def descriptor(raw, value):
    return dict(
        format=FORMAT,
        file=FILE,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        schema=INKSEQ_WORKFLOW_SCHEMA,
        sdk_version=WORKFLOW_SDK_VERSION,
        catalog_sha256=CATALOG_SHA256,
        rows=sum(len(rows) for rows in value['tables'].values()),
        execution_restore='hold',
    )


def export(connection, directory, *, deadline=None):
    """Requires the original caller-owned REPEATABLE READ / READ ONLY tx."""
    from .backup import _write_json

    validate(connection)
    deadline = time.monotonic() + 60 if deadline is None else deadline
    require(connection.in_transaction(), 'workflow_backup_transaction_required')
    require(
        connection.exec_driver_sql('SHOW transaction_isolation').scalar_one()
        == 'repeatable read'
        and connection.exec_driver_sql('SHOW transaction_read_only').scalar_one()
        == 'on',
        'workflow_backup_same_snapshot_required',
    )
    value = dict(format=FORMAT, catalog_sha256=CATALOG_SHA256, tables={})
    total = 0
    for name, columns in tables().items():
        require(time.monotonic() < deadline, 'workflow_backup_time_limit')
        fields = ','.join('"' + c + '"::text AS "' + c + '"' for c in columns)
        relation = '"inkseq_workflows"."' + name + '"'
        count, size, largest = connection.exec_driver_sql(
            'SELECT count(*),COALESCE(sum(octet_length(row_to_json(t)::text)),0),'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
            'COALESCE(max(octet_length(row_to_json(t)::text)),0) FROM '
            '(SELECT ' + fields + ' FROM ' + relation + ') t'
        ).one()
        total += int(size)
        require(
            count <= MAX_ROWS and largest <= 16 * 1024**2 and total <= MAX_BYTES,
            'workflow_backup_size_limit',
        )
        rows = []
        with connection.exec_driver_sql(
            'SELECT row_to_json(t)::text FROM (SELECT '  # noqa: S608 -- identifiers from fixed schema metadata, not user input
            + fields
            + ' FROM '
            + relation
            + ') t',
            execution_options={'stream_results': True, 'max_row_buffer': 100},
        ) as source:
            while batch := source.fetchmany(100):
                require(time.monotonic() < deadline, 'workflow_backup_time_limit')
                rows.extend(json.loads(row[0]) for row in batch)
        require(len(rows) == count, 'workflow_backup_count_changed')
        # Canonical order does not depend on incidental heap/physical order.
        rows.sort(key=canonical)
        value['tables'][name] = rows
    raw = canonical(value)
    require(len(raw) <= MAX_BYTES, 'workflow_backup_size_limit')
    _write_json(directory / FILE, value)
    raw = (directory / FILE).read_bytes()
    require(len(raw) <= MAX_BYTES, 'workflow_backup_size_limit')
    return descriptor(raw, value)


def read(directory, expected):
    from .backup import _regular

    raw = _regular(directory / FILE, max_bytes=MAX_BYTES).read_bytes()
    value = json.loads(raw)
    require(
        isinstance(value, dict)
        and set(value) == {'format', 'catalog_sha256', 'tables'}
        and value['format'] == FORMAT
        and value['catalog_sha256'] == CATALOG_SHA256
        and isinstance(value['tables'], dict)
        and set(value['tables']) == set(tables()),
        'workflow_backup_format_requires_review',
    )
    for name, columns in tables().items():
        rows = value['tables'][name]
        require(
            isinstance(rows, list) and len(rows) <= MAX_ROWS,
            'workflow_backup_size_limit',
        )
        for row in rows:
            require(
                isinstance(row, dict)
                and set(row) == set(columns)
                and all(v is None or isinstance(v, str) for v in row.values()),
                'workflow_backup_row_requires_review',
            )
    require(
        value['tables']['dbos_migrations'] == [{'version': '123'}],
        'workflow_backup_sdk_revision_requires_review',
    )
    require(descriptor(raw, value) == expected, 'workflow_backup_checksum_mismatch')
    return value


def inspect_file(directory):
    from .backup import _regular

    raw = _regular(directory / FILE, max_bytes=MAX_BYTES).read_bytes()
    value = json.loads(raw)
    expected = descriptor(raw, value)
    read(directory, expected)
    return expected


def restore(connection, directory, expected):
    """Only a caller-owned isolated recovery transaction; keep original IDs.

    No schema initialization, automatic resume or credentials. A restored SDK
    catalog must already be exact and empty except the original version marker.
    The reserved SDK metadata row blocks producer/executor startup until an
    explicit domain/lease/receipt recovery review; this is not a cancellation.
    """
    value = read(directory, expected)
    validate(connection)
    require(connection.in_transaction(), 'workflow_restore_transaction_required')
    order = ['workflow_status'] + [
        n for n in tables() if n not in {'workflow_status', 'dbos_migrations'}
    ]
    for name in order:
        require(
            not connection.exec_driver_sql(
                'SELECT 1 FROM "inkseq_workflows"."' + name + '" LIMIT 1'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
            ).first(),
            'workflow_restore_target_not_empty',
        )
    for name in order:
        columns = tables()[name]
        fields = ','.join('"' + c + '"' for c in columns)
        casts = ','.join(
            'CAST(:v' + str(i) + ' AS ' + t + ')'
            for i, t in enumerate(columns.values())
        )
        sql = text(
            'INSERT INTO "inkseq_workflows"."'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
            + name
            + '" ('
            + fields
            + ') VALUES ('
            + casts
            + ')'
        )
        for row in value['tables'][name]:
            connection.execute(
                sql, {'v' + str(i): row[c] for i, c in enumerate(columns)}
            )
    connection.exec_driver_sql(
        "INSERT INTO inkseq_workflows.event_dispatch_kv "  # noqa: S608 -- trusted workflow version constant, not user input
        "(service_name,workflow_fn_name,key,value) VALUES "
        "('inkseq.recovery','"
        + WORKFLOW_APPLICATION_VERSION
        + "','restored-state','domain_and_receipt_review_required')"
    )
    connection.exec_driver_sql(
        "UPDATE inkseq_workflows.workflow_schedules SET status='PAUSED'"
    )
    return dict(
        state='restored_workflows_held',
        original_workflow_ids=True,
        original_rows=expected['rows'],
        execution_enabled=False,
    )


def main(argv=None):
    """Existing offline rehearsal's network-none disposable DB only."""
    parser = argparse.ArgumentParser(
        description='Restore pinned workflow state into an isolated rehearsal copy'
    )
    parser.add_argument('command', choices=['restore-rehearsal'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        from .backup import _regular, _digest
        from .settings import database_url_from_environment
        from .schema_baseline import adopt_baseline, adopt_workflows, compare_schema
        from .workflow_schema import schema_exists

        url = database_url_from_environment()
        parsed = make_url(url)
        require(
            parsed.drivername == 'postgresql+psycopg'
            and parsed.username == 'postgres'
            and parsed.password is None
            and parsed.host is None
            and parsed.port is None
            and parsed.database == 'rehearsal'
            and dict(parsed.query) == {'host': '/var/run/postgresql'}
            and os.environ.get('SIXNINE_GENERATION_ENABLED') == '0'
            and os.environ.get('SIXNINE_EXECUTION_BACKEND') == 'disabled',
            'workflow_restore_requires_offline_rehearsal',
        )
        directory = args.directory
        manifest = json.loads(
            _regular(
                directory / 'workflow-manifest.json', max_bytes=16 * 1024**2
            ).read_bytes()
        )
        require(
            manifest.get('kind') == 'postgres-database-only'
            and manifest.get('version') == 1
            and manifest.get('consistent_business_workflow_snapshot') is True
            and manifest.get('database_sha256')
            == _digest(_regular(directory / 'production.dump', max_bytes=8 * 1024**3)),
            'workflow_restore_original_dump_mismatch',
        )
        read(directory, manifest['workflow_state'])
        engine = create_engine(url, hide_parameters=True)
        with engine.begin() as connection:
            require(not schema_exists(connection), 'workflow_restore_target_not_empty')
            before = compare_schema(connection)
            adopted = adopt_baseline(
                connection, expected_fingerprint=before['schema_fingerprint']
            )
            adopt_workflows(
                connection, expected_fingerprint=adopted['after']['schema_fingerprint']
            )
            result = restore(connection, directory, manifest['workflow_state'])
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'code': 'workflow_rehearsal_restore_refused'}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from studio_platform import workflow_backup as backup
from studio_platform.workflow_backup_stream import inspect
from studio_platform.workflow_schema import canonical, WorkflowSchemaError


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def one(self):
        return self.value


class Rows:
    def __init__(self, values, repeats=1):
        self.values = iter(value for _ in range(repeats) for value in values)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetchmany(self, count):
        import itertools

        return [(json.dumps(value),) for value in itertools.islice(self.values, count)]


class Connection:
    def __init__(self, values, repeats=None):
        self.values, self.repeats = values, repeats or {}
        self.buffers = []

    def in_transaction(self):
        return True

    def exec_driver_sql(self, sql, **kwargs):
        if sql.startswith('SHOW'):
            return Result('on' if 'read_only' in sql else 'repeatable read')
        name = next(
            name for name in self.values if '"inkseq_workflows"."' + name + '"' in sql
        )
        rows = self.values[name]
        repeat = self.repeats.get(name, 1)
        sizes = [len(json.dumps(row).encode()) for row in rows]
        if sql.startswith('SELECT count'):
            return Result(
                (len(rows) * repeat, sum(sizes) * repeat, max(sizes, default=0))
            )
        self.buffers.append(kwargs['execution_options']['max_row_buffer'])
        return Rows(rows, repeat)


class StreamingWorkflowTests(unittest.TestCase):
    def value(self):
        tables = {name: [] for name in backup.tables()}
        tables['dbos_migrations'] = [{'version': '123'}]
        return dict(
            format=backup.FORMAT, catalog_sha256=backup.CATALOG_SHA256, tables=tables
        )

    def inspect(self, raw, **kwargs):
        return inspect(
            io.BytesIO(raw),
            limit=kwargs.get('limit', backup.MAX_BYTES),
            schemas=backup.tables(),
            row_limit=backup.MAX_ROWS,
            format_name=backup.FORMAT,
            catalog=backup.CATALOG_SHA256,
        )

    def test_export_retains_format_sort_duplicates_null_and_unicode(self):
        value = self.value()
        cols = backup.tables()['operation_outputs']
        row = {key: None for key in cols}
        row[next(iter(cols))] = '字幕🎬"\\' * 5000
        value['tables']['operation_outputs'] = [row, row, dict.fromkeys(cols, 'first')]
        with tempfile.TemporaryDirectory() as tmp, patch.object(backup, 'validate'):
            descriptor = backup.export(Connection(value['tables']), Path(tmp))
            expected = {
                **value,
                'tables': {
                    k: sorted(v, key=canonical) for k, v in value['tables'].items()
                },
            }
            self.assertEqual(
                (Path(tmp) / backup.FILE).read_bytes(), canonical(expected)
            )
            self.assertEqual(backup.read(Path(tmp), descriptor), expected)
            backup.verify(Path(tmp), descriptor)
            self.assertFalse(list(Path(tmp).glob('.workflow-sort-*')))
            with self.assertRaisesRegex(WorkflowSchemaError, 'checksum'):
                backup.verify(Path(tmp), {**descriptor, 'rows': 999})

    def test_legacy_whitespace_key_order_and_utf8_are_readable(self):
        value = self.value()
        row = dict.fromkeys(backup.tables()['operation_outputs'], '汉字🎬' * 2000)
        value['tables']['operation_outputs'] = [row]
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
        size, _, rows = self.inspect(raw)
        self.assertEqual((size, rows), (len(raw), 2))

    def test_malformed_or_truncated_or_oversize_never_verifies(self):
        value = self.value()
        raw = canonical(value)
        bad = [
            raw[:-1],
            raw + b'x',
            raw.replace(b'"version":"123"', b'"version":"999"'),
            raw.replace(b'"version":"123"', b'"version":"123","version":"123"'),
            raw.replace(b'"tables":{', b'"tables":{"unknown":[],'),
            raw.replace(b'"version":"123"', b'"version":123'),
        ]
        for item in bad:
            with (
                self.subTest(item=item[:50]),
                self.assertRaises((ValueError, WorkflowSchemaError)),
            ):
                self.inspect(item)
        with self.assertRaisesRegex(WorkflowSchemaError, 'size_limit'):
            self.inspect(raw, limit=len(raw) - 1)

    def test_production_sized_sidecar_over_16mib_has_bounded_memory(self):
        # Run this suite inside the 256 MiB test container; the input generator
        # never materializes the production-scale row count.
        value = self.value()
        row = dict.fromkeys(backup.tables()['operation_outputs'], 'x' * 16)
        value['tables']['operation_outputs'] = [row]
        connection = Connection(value['tables'], {'operation_outputs': 131000})
        with tempfile.TemporaryDirectory() as tmp, patch.object(backup, 'validate'):
            result = backup.export(
                connection, Path(tmp), deadline=time.monotonic() + 180
            )
            self.assertGreater(result['size_bytes'], 16 * 1024**2)
            self.assertEqual(result['rows'], 131001)
            backup.verify(Path(tmp), result)
            self.assertFalse(list(Path(tmp).glob('.workflow-sort-*')))
        self.assertTrue(all(1 <= n <= 100 for n in connection.buffers))

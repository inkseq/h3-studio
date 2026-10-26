"""Bounded sidecar IO; retain the v1 JSON wire format and canonical row order."""

import codecs
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager

from .workflow_schema import canonical, require


class Reader:
    def __init__(self, source, limit):
        self.source, self.limit = source, limit
        self.buffer = ''
        self.eof = False
        self.size = 0
        self.digest = hashlib.sha256()
        self.utf8 = codecs.getincrementaldecoder('utf-8')()
        self.decoder = json.JSONDecoder(object_pairs_hook=self.unique)

    @staticmethod
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'workflow_backup_duplicate_key')
            result[key] = value
        return result

    def more(self):
        block = self.source.read(65536)
        self.size += len(block)
        require(self.size <= self.limit, 'workflow_backup_size_limit')
        self.digest.update(block)
        self.eof = not block
        self.buffer += self.utf8.decode(block, final=self.eof)

    def peek(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer and not self.eof:
            self.more()
            self.buffer = self.buffer.lstrip()
        return self.buffer[:1]

    def token(self, token):
        require(self.peek() == token, 'workflow_backup_format_requires_review')
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                require(not self.eof, 'workflow_backup_format_requires_review')
                self.more()

    def keys(self):
        self.token('{')
        seen = set()
        if self.peek() != '}':
            while True:
                key = self.value()
                require(
                    type(key) is str and key not in seen,
                    'workflow_backup_duplicate_key',
                )
                seen.add(key)
                self.token(':')
                yield key
                if self.peek() == '}':
                    break
                self.token(',')
        self.token('}')


def inspect(source, *, limit, schemas, row_limit, format_name, catalog):
    reader = Reader(source, limit)
    root = {}
    counts = {}
    migration = []
    for key in reader.keys():
        require(
            key in {'format', 'catalog_sha256', 'tables'},
            'workflow_backup_format_requires_review',
        )
        if key != 'tables':
            root[key] = reader.value()
            continue
        for table in reader.keys():
            require(table in schemas, 'workflow_backup_format_requires_review')
            count = 0
            reader.token('[')
            if reader.peek() != ']':
                while True:
                    row = reader.value()
                    require(
                        isinstance(row, dict)
                        and set(row) == set(schemas[table])
                        and all(v is None or isinstance(v, str) for v in row.values()),
                        'workflow_backup_row_requires_review',
                    )
                    count += 1
                    require(count <= row_limit, 'workflow_backup_size_limit')
                    if table == 'dbos_migrations':
                        require(
                            count == 1, 'workflow_backup_sdk_revision_requires_review'
                        )
                        migration.append(row)
                    del row
                    if reader.peek() == ']':
                        break
                    reader.token(',')
            reader.token(']')
            counts[table] = count
    require(
        root == {'format': format_name, 'catalog_sha256': catalog}
        and set(counts) == set(schemas),
        'workflow_backup_format_requires_review',
    )
    require(
        migration == [{'version': '123'}],
        'workflow_backup_sdk_revision_requires_review',
    )
    require(not reader.peek(), 'workflow_backup_format_requires_review')
    return reader.size, reader.digest.hexdigest(), sum(counts.values())


@contextmanager
def sorted_rows(directory):
    # Disk-backed sorting avoids retaining all decoded rows or sort keys in RAM.
    # Only this invocation's private temporary spool is removed, never a backup.
    with tempfile.TemporaryDirectory(prefix='.workflow-sort-', dir=directory) as tmp:
        with closing(sqlite3.connect(os.path.join(tmp, 'sort.sqlite3'))) as db:
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('CREATE TABLE rows (value BLOB NOT NULL)')
            db.execute('CREATE INDEX row_order ON rows(value)')
            yield db


class Writer:
    def __init__(self, source, limit):
        self.source, self.limit, self.size = source, limit, 0

    def write(self, value):
        self.size += len(value)
        require(self.size <= self.limit, 'workflow_backup_size_limit')
        self.source.write(value)

    def value(self, value):
        self.write(canonical(value))

"""Read-only recognition of known schema bookkeeping, without Alembic.

Ship this reader before the baseline writer so the previous rollback image can
still create a business-only backup after adoption. Unknown markers are never
treated as known, exported, dropped, rewritten or silently stamped.
"""

import re

from sqlalchemy import MetaData, Table, inspect

BASELINE_REVISION = "20261019_0001"
WORKFLOW_REVISION = "20261019_0002"
IDENTITY_REVISION = "20261019_0003"
KNOWN_REVISIONS = frozenset({BASELINE_REVISION, WORKFLOW_REVISION, IDENTITY_REVISION})
WORKFLOW_REVISIONS = frozenset({WORKFLOW_REVISION, IDENTITY_REVISION})


class SchemaVersionError(Exception):
    """Static diagnostic code; never contains SQL, row values or connection URL."""


def read_schema_version(connection, *, schema=None):
    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names(schema=schema):
        return None
    columns = inspector.get_columns("alembic_version", schema=schema)
    primary = inspector.get_pk_constraint("alembic_version", schema=schema)
    if (
        len(columns) != 1
        or columns[0]["name"] != "version_num"
        or columns[0]["nullable"]
        or str(columns[0]["type"]).upper() != "VARCHAR(32)"
        or columns[0].get("default") is not None
        or columns[0].get("computed")
        or columns[0].get("identity")
        or primary.get("constrained_columns") != ["version_num"]
        or inspector.get_foreign_keys("alembic_version", schema=schema)
        or inspector.get_unique_constraints("alembic_version", schema=schema)
        or inspector.get_check_constraints("alembic_version", schema=schema)
        or inspector.get_indexes("alembic_version", schema=schema)
    ):
        raise SchemaVersionError("schema_version_table_requires_review")
    table = Table(
        "alembic_version", MetaData(), autoload_with=connection, schema=schema
    )
    values = list(connection.execute(table.select().limit(2)).scalars())
    if len(values) != 1 or values[0] not in KNOWN_REVISIONS:
        raise SchemaVersionError("schema_revision_requires_review")
    return values[0]


def read_sqlite_schema_version(db):
    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if table is None:
        return None
    columns = list(db.execute('PRAGMA table_info("alembic_version")'))
    expected_ddl = (
        r"CREATE TABLE alembic_version\s*\(\s*version_num VARCHAR\(32\) NOT NULL,"
        r"\s*CONSTRAINT alembic_version_pkc PRIMARY KEY \(version_num\)\s*\)"
    )
    if (
        len(columns) != 1
        or tuple(columns[0][1:]) != ("version_num", "VARCHAR(32)", 1, None, 1)
        or not re.fullmatch(expected_ddl, table[0], re.I)
        or any(
            row[3] != "pk" for row in db.execute('PRAGMA index_list("alembic_version")')
        )
    ):
        raise SchemaVersionError("schema_version_table_requires_review")
    values = list(db.execute("SELECT version_num FROM alembic_version LIMIT 2"))
    if len(values) != 1 or values[0][0] not in KNOWN_REVISIONS:
        raise SchemaVersionError("schema_revision_requires_review")
    return values[0][0]

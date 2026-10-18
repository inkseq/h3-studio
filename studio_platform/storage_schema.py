"""Serialize additive storage DDL with the platform startup migration lock."""


def create_storage_schema(engine, *schemas):
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(685939796868749721)"
            )
        elif engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise ValueError("Unsupported storage journal database")
        for schema in schemas:
            schema.create_all(connection)

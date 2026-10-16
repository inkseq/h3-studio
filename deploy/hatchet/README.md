# Historical Hatchet rollback data

Hatchet is retired by migration manifest v2 (`postgres-notify-v1`). New CPU
images contain no Hatchet SDK and the active Compose overlay has no broker,
broker database or dispatcher service. Existing immutable `hatchet-v1` job
route values remain accepted by the fenced PostgreSQL queue consumer.

Do not delete historical named volumes, Secrets Manager fields or protected
broker files. Rollback uses an independently approved original v1 artifact,
including its exact overlay and entrypoint, with the retained volumes and
credentials. It does not build or bootstrap a new broker from this checkout.
See ../dstack/RELEASE.md for the protected transition.

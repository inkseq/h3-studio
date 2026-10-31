"""Review/advice redaction within the existing proven content purge transaction."""


def redact(conn, tenant, owner, *, artifact_ids=(), project_ids=(), now):
    from .take_reviews import redact_reviews
    from .product_feedback import redact_feedback
    from .inky_store import redact_cache

    redact_reviews(
        conn, tenant, owner, artifact_ids=artifact_ids, project_ids=project_ids, now=now
    )
    redact_feedback(
        conn, tenant, owner, artifact_ids=artifact_ids, project_ids=project_ids, now=now
    )
    redact_cache(conn, tenant, owner, project_ids, now)

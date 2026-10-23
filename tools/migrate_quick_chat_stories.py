"""Bounded operator inventory/adoption; no schema initialization or public route.

Dry-run is default and read-only in both environments. Production apply is
intentionally unavailable until the owner approves the inventory/counts.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_platform.quick_chat import objects
from studio_platform.quick_chat_migration import (
    MigrationError,
    apply_staging,
    rollback_staging,
    preview,
    require,
)
from studio_platform.settings import Settings
from studio_platform.repository import request_hash


def run(args, settings):
    rolling_back = getattr(args, "rollback", False)
    modifying = args.apply or rolling_back
    require(
        args.environment == settings.deployment_target, "deployment_target_mismatch"
    )
    require(
        settings.public_origin
        == {
            "staging": "https://staging.inkseq.com",
            "production": "https://inkseq.com",
        }[args.environment],
        "deployment_origin_mismatch",
    )
    if args.apply:
        require(args.environment == "staging", "production_apply_not_authorized")
        require(
            len(args.owner) == 1
            and args.session
            and args.source_hash
            and args.project_version
            and args.actor_user_id,
            "apply_requires_exact_preview_and_admin",
        )
        require(args.reader_evidence, "deployed_readers_not_verified")
        evidence_path = Path(args.reader_evidence)
        require(
            evidence_path.is_file() and evidence_path.stat().st_size <= 16384,
            "deployed_readers_not_verified",
        )
        reader_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if rolling_back:
        require(args.environment == "staging", "production_rollback_not_authorized")
        require(
            len(args.owner) == 1
            and args.session
            and args.source_hash
            and args.actor_user_id
            and args.reason,
            "rollback_requires_original_receipt_and_admin",
        )
    url = make_url(settings.database_url)
    require(
        url.drivername in {"sqlite", "postgresql+psycopg"},
        "database_driver_unsupported",
    )
    if url.drivername == "sqlite":
        require(
            url.database and Path(url.database).is_file(), "existing_database_required"
        )
        if not modifying:
            url = url.set(
                database="file:" + str(Path(url.database).resolve()),
                query={"mode": "ro", "uri": "true"},
            )
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            with connection.begin():
                if connection.dialect.name == "postgresql" and not modifying:
                    connection.execute(
                        text(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                    )
                elif connection.dialect.name == "sqlite" and modifying:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                if modifying:
                    if args.apply:
                        outcome = apply_staging(
                            connection,
                            settings,
                            args.owner[0],
                            args.session,
                            source_hash=args.source_hash,
                            project_version=args.project_version,
                            actor_user_id=args.actor_user_id,
                            reader_evidence=reader_evidence,
                        )
                    else:
                        outcome = rollback_staging(
                            connection,
                            settings,
                            args.owner[0],
                            args.session,
                            source_hash=args.source_hash,
                            actor_user_id=args.actor_user_id,
                            reason=args.reason,
                        )
                    # Full archived user content stays in the protected existing
                    # document receipt, never in operator stdout/SSM logs.
                    return {
                        "environment": args.environment,
                        "result": {
                            key: outcome[key]
                            for key in (
                                "receipt_id",
                                "source_session_id",
                                "source_hash",
                                "project_id",
                                "before_project_version",
                                "after_project_version",
                                "before_project_hash",
                                "after_project_hash",
                                "archive_strategy",
                                "counts",
                                "actor_user_id",
                                "completed_at",
                                "replayed",
                                "rollback",
                                "current_project_hash",
                                "current_project_version",
                            )
                            if key in outcome
                        },
                    }
                results = []
                for owner in args.owner:
                    statement = (
                        select(objects.c.id)
                        .where(
                            objects.c.tenant == settings.tenant_id,
                            objects.c.owner == owner,
                            objects.c.kind == "session",
                        )
                        .order_by(objects.c.id)
                    )
                    if args.session:
                        statement = statement.where(objects.c.id == args.session)
                    ids = list(
                        connection.execute(statement.limit(args.limit + 1)).scalars()
                    )
                    require(len(ids) <= args.limit, "session_inventory_limit_exceeded")
                    for ident in ids:
                        results.append(
                            preview(
                                connection,
                                settings.tenant_id,
                                owner,
                                ident,
                                max_project_bytes=settings.max_project_bytes,
                            )
                        )
                return {
                    "environment": args.environment,
                    "mode": "read_only",
                    "sessions": results,
                    "accounts": {
                        owner: {
                            "sessions": sum(
                                result["owner"] == owner for result in results
                            ),
                            "ready": sum(
                                result["owner"] == owner and result["state"] == "ready"
                                for result in results
                            ),
                            "refused": sum(
                                result["owner"] == owner
                                and result["state"] == "refused"
                                for result in results
                            ),
                            "raw": {
                                key: sum(
                                    result["raw_counts"][key]
                                    for result in results
                                    if result["owner"] == owner
                                )
                                if all(
                                    result.get("raw_counts", {}).get(key) is not None
                                    for result in results
                                    if result["owner"] == owner
                                )
                                else None
                                for key in (
                                    "cards",
                                    "revisions",
                                    "takes",
                                    "persisted_selected",
                                    "turns",
                                    "text_turns",
                                )
                            },
                            "refusal_reasons": dict(
                                sorted(
                                    Counter(
                                        reason
                                        for result in results
                                        if result["owner"] == owner
                                        for reason in result.get("refusals", [])
                                    ).items()
                                )
                            ),
                            **{
                                key: sum(
                                    result.get("counts", {}).get(key, 0)
                                    for result in results
                                    if result["owner"] == owner
                                )
                                for key in (
                                    "canonical_shots",
                                    "retained_shots",
                                    "archived_shots",
                                    "removed_projections",
                                    "aliased_takes",
                                    "cards_with_choice",
                                    "cards_without_choice",
                                )
                            },
                        }
                        for owner in args.owner
                    },
                    "snapshot_hash": request_hash(results),
                    "count_scope": "raw covers every inventoried session (null means unknown); grouping covers ready sessions only",
                    "production_apply_authorized": False,
                }
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", choices=("staging", "production"), required=True
    )
    parser.add_argument("--owner", action="append", required=True)
    parser.add_argument("--session")
    parser.add_argument("--limit", type=int, default=500)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--source-hash")
    parser.add_argument("--project-version", type=int)
    parser.add_argument("--actor-user-id")
    parser.add_argument("--reason", help="Audited reason required for staging rollback")
    parser.add_argument(
        "--reader-evidence",
        help="Original staging reader self-check JSON; required for apply, not acceptance",
    )
    args = parser.parse_args(argv)
    try:
        require(1 <= args.limit <= 1000, "invalid_inventory_limit")
        result = run(args, Settings.from_environment())
    except MigrationError as error:
        print(json.dumps({"error": error.code}))
        return 2
    except Exception:
        # A driver exception may carry a connection URL or a content payload.
        print(
            json.dumps(
                {"error": "migration_operator_failed_read_original_state_before_retry"}
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

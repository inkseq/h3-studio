"""Apply ONE qualified production #481 row; never batch, replay or roll back.

Use only inside the separately reviewed host operation, after R then G promotion.
That operation supplies actual current/retained-reader proof and pins these files.
This tool validates those bindings, not the authenticity of operator-supplied JSON.
An existing result/DB receipt means reconcile its original outcome read-only.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_platform.quick_chat_migration import MigrationError, require
from studio_platform.quick_chat_production_migration import (
    SOURCE_FILES,
    apply_production_approved,
    load_manifest,
    validate_production_readers,
)
from studio_platform.settings import Settings


def _read(path, limit):
    source = Path(path)
    require(
        source.is_file() and not source.is_symlink() and source.stat().st_size <= limit,
        "production_operator_file_invalid",
    )
    raw = source.read_bytes()
    require(0 < len(raw) <= limit, "production_operator_file_invalid")
    return raw


def prepare(args, settings):
    # All cheap target, membership, source and reader checks precede a DB handle.
    require(
        settings.deployment_target == "production"
        and settings.public_origin == "https://inkseq.com",
        "production_environment_required",
    )
    manifest_raw = _read(args.manifest, 131072)
    manifest = load_manifest(manifest_raw, args.manifest_sha256)
    require(
        sum(
            (row["owner"], row["session_id"]) == (args.owner, args.session)
            for row in manifest["ready"]
        )
        == 1,
        "production_session_not_approved",
    )
    proof_raw = _read(args.reader_evidence, 16384)
    require(
        hashlib.sha256(proof_raw).hexdigest() == args.reader_evidence_sha256,
        "production_reader_proof_hash_mismatch",
    )
    import time

    proof = validate_production_readers(
        json.loads(proof_raw),
        manifest_sha256=args.manifest_sha256,
        tenant_id=settings.tenant_id,
        actor_user_id=args.actor_user_id,
        operation_id=args.operation_id,
        now=time.time(),
    )
    root = Path(__file__).resolve().parents[1]
    require(
        all(
            hashlib.sha256(_read(root / name, 262144)).hexdigest()
            == proof["source_sha256"][name]
            for name in SOURCE_FILES
        ),
        "production_entry_source_changed",
    )
    url = make_url(settings.database_url)
    require(url.drivername == "postgresql+psycopg", "production_postgres_required")
    return manifest_raw, proof, url


def _summary(outcome):
    # Deliberate closed fields: never before_project, messages, mapping or media.
    return {
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
            "production_approval",
            "source_rows_hash",
            "operational_closure_hash",
        )
    }


def execute(args, settings, prepared):
    manifest_raw, proof, url = prepared
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            outcome = apply_production_approved(
                connection,
                settings,
                args.owner,
                args.session,
                manifest_raw=manifest_raw,
                manifest_sha256=args.manifest_sha256,
                actor_user_id=args.actor_user_id,
                operation_id=args.operation_id,
                reader_evidence=proof,
            )
            # Projection failure also rolls back. Commit completes before returning.
            summary = _summary(outcome)
            json.dumps(summary, allow_nan=False)
        return summary
    finally:
        engine.dispose()


def _save(file, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    file.seek(0)
    file.write(raw)
    file.truncate()
    file.flush()
    os.fsync(file.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "owner",
        "session",
        "manifest",
        "manifest-sha256",
        "reader-evidence",
        "reader-evidence-sha256",
        "actor-user-id",
        "operation-id",
        "result",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    started = False
    result_file = None
    try:
        settings = Settings.from_environment()
        prepared = prepare(args, settings)
        # This receipt reserves the invocation before any DB access. Never reuse it.
        try:
            descriptor = os.open(
                args.result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            raise MigrationError("production_result_exists_reconcile_only") from None
        result_file = os.fdopen(descriptor, "wb")
        base = {
            "operation_id": args.operation_id,
            "environment": "production",
            "automatic_retry": False,
        }
        _save(result_file, {**base, "status": "started_reconcile_before_retry"})
        started = True
        outcome = execute(args, settings, prepared)
        # Persist the original committed result before stdout or host checks.
        result = {**base, "status": "committed", "result": outcome}
        _save(result_file, result)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception as error:
        # A connection/commit/output failure may have an unknown outcome. Never
        # declare rollback or provide a replay path from an exception alone.
        reconcile = started or (
            isinstance(error, MigrationError)
            and error.code == "production_result_exists_reconcile_only"
        )
        result = {
            "status": "outcome_requires_read_only_reconciliation"
            if reconcile
            else "refused_before_database",
            "error": error.code
            if isinstance(error, MigrationError)
            else "production_operator_failed",
            "automatic_retry": False,
        }
        # Do not overwrite a durable committed result if stdout subsequently fails.
        print(json.dumps(result))
        return 2
    finally:
        if result_file is not None:
            result_file.close()


if __name__ == "__main__":
    raise SystemExit(main())

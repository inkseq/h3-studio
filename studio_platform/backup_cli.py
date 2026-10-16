"""Explicit operator backup CLI, included in the reviewed CPU runtime image."""
from pathlib import Path
import argparse
import json

from .backup import backup_local, backup_postgres_local, backup_configured, verify_local, restore_local, dump_postgres


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private business-data backup; credentials and sessions excluded")
    actions = parser.add_subparsers(dest="action", required=True)
    configured = actions.add_parser("configured", help="Read-only PG and explicitly configured Local/R2 sources in the existing app container")
    configured.add_argument("--destination", required=True, type=Path)
    backup = actions.add_parser("local")
    backup.add_argument("--database", required=True, type=Path)
    backup.add_argument("--object-root", required=True, type=Path)
    backup.add_argument("--destination", required=True, type=Path)
    verify = actions.add_parser("verify")
    verify.add_argument("--backup", required=True, type=Path)
    restore = actions.add_parser("restore-local")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    postgres = actions.add_parser("postgres-database-only")
    postgres.add_argument("--url-file", required=True, type=Path)
    postgres.add_argument("--destination", required=True, type=Path)
    postgres.add_argument("--pg-dump", default="pg_dump")
    postgres.add_argument("--schema", default="public")
    portable = actions.add_parser("postgres-local", help="One PG read-only snapshot + Local media; isolated SQLite restoration")
    portable.add_argument("--url-file", required=True, type=Path)
    portable.add_argument("--object-root", required=True, type=Path)
    portable.add_argument("--destination", required=True, type=Path)
    portable.add_argument("--schema", default="public")
    portable.add_argument("--private-platform-network", action="store_true",
        help="Operator assertion: running inside reviewed private Compose network; only exact db:5432/sixnine identity allowed")
    args = parser.parse_args(argv)
    try:
        if args.action == "configured":
            result = backup_configured(args.destination)
        elif args.action == "local":
            result = backup_local(args.database, args.object_root, args.destination)
        elif args.action == "verify":
            manifest = verify_local(args.backup)
            result = {"state": "verified", "objects": len(manifest["objects"]), "media_bytes": manifest["media_bytes"]}
        elif args.action == "restore-local":
            result = restore_local(args.backup, args.destination)
        elif args.action == "postgres-local":
            result = backup_postgres_local(args.url_file, args.object_root, args.destination, schema=args.schema,
                                           private_platform_network=args.private_platform_network)
        else:
            result = dump_postgres(args.url_file, args.destination, pg_dump=args.pg_dump, schema=args.schema)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"state": "backup_or_recovery_refused", "detail": "Review private paths, version, integrity and operation scope; sources are not modified"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

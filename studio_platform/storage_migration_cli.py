"""Operator-only local/R2 media copy, without database or lifecycle operations."""
from pathlib import Path
import argparse
import json
import re

from .storage import S3ObjectStore
from .storage_config import S3StorageConfig, load_configured_storage_credentials
from .storage_migration import MigrationError, apply_plan, create_plan, verify_plan


def main(argv=None, *, credentials_loader=load_configured_storage_credentials,
         store_factory=S3ObjectStore):
    parser = argparse.ArgumentParser(description="Private immutable Local-to-R2 copy; local originals are never removed")
    parser.add_argument("action", choices=("plan", "apply", "verify"))
    parser.add_argument("--source-root", type=Path,
                        help="Existing LocalObjectStore root; required only for the offline plan")
    parser.add_argument("--receipt-dir", required=True, type=Path,
                        help="New private directory for plan; same directory for apply/verify")
    parser.add_argument("--endpoint", required=True, help="Exact nonsecret R2 S3 API HTTPS endpoint")
    parser.add_argument("--bucket", required=True, help="Explicit dedicated private bucket")
    parser.add_argument("--profile", default="cloudflare-r2--rig-root")
    parser.add_argument("--max-object-bytes", default=100 * 1024 ** 2, type=int)
    parser.add_argument("--credentials-file", type=Path,
                        help="Protected runtime file, never a raw credential argument; required for apply/verify")
    args = parser.parse_args(argv)
    if args.action == "plan" and (args.source_root is None or args.credentials_file is not None):
        parser.error("plan requires --source-root and does not load credentials")
    if args.action != "plan" and (args.credentials_file is None or args.source_root is not None):
        parser.error("apply/verify require --credentials-file; source is bound by the private plan")
    try:
        config = S3StorageConfig("r2", args.endpoint, "auto", args.bucket, "cloudflare-r2",
                                 args.profile, enabled=True, max_single_put_bytes=args.max_object_bytes)
        if args.action == "plan":
            result = create_plan(args.source_root, config, args.receipt_dir)
        else:
            credentials = credentials_loader(config, credentials_file=args.credentials_file)
            store = store_factory(config, credentials)
            result = apply_plan(args.receipt_dir, store) if args.action == "apply" else verify_plan(args.receipt_dir, store)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        detail = (str(error) if isinstance(error, MigrationError)
                  and re.fullmatch(r"migration_[a-z_]+", str(error)) else "migration_configuration_or_transport_failed")
        print(json.dumps({"state": "migration_refused", "reason_code": detail,
                          "local_copies_retained": True, "database_accessed": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

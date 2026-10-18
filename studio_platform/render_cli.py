"""Explicit local CPU-render worker entry point; no import-time services."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

from .control import WorkerControl, WorkerSpec
from .execution_policy import ExecutionPolicies
from .render_backend import CPURenderBackend
from .render_plans import CONFIGURATIONS, MODEL, POOL, RECIPE
from .repository import Repository
from .settings import Settings
from .storage import LocalObjectStore, S3ObjectStore
from .storage_config import S3StorageConfig, load_configured_storage_credentials
from .worker import WorkerRunner


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Opt-in CPU chapter rough cuts; GPU and supplier generation are unchanged"
    )
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--worker-id")
    parser.add_argument(
        "--instance-id",
        help="Stable operator-assigned CPU host identity, shared by workers on this host",
    )
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--confirmed-idle",
        action="store_true",
        help="Operator has checked no unresolved/orphaned render on this host",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--contract-version",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="3 for current caption-capable plans; 1/2 only for an explicitly separate legacy drain worker",
    )
    parser.add_argument(
        "--subtitle-font-profile",
        choices=("noto-cjk", "windows-yahei"),
        default="noto-cjk",
        help="Reviewed installed font profile; Windows preview must explicitly choose windows-yahei",
    )
    parser.add_argument(
        "--max-state-gib",
        type=int,
        default=8,
        help="Retained CPU attempt cache cap; default 8 GiB, no automatic deletion",
    )
    args = parser.parse_args(argv)
    if not args.enabled:
        print(json.dumps({"state": "disabled", "backend": "cpu-render"}))
        return 0
    if (
        not args.worker_id
        or not args.instance_id
        or args.work_dir is None
        or not args.work_dir.is_absolute()
    ):
        parser.error(
            "Enabled CPU rendering requires worker-id, instance-id and an absolute work-dir"
        )
    repo, control = None, None
    try:
        settings = Settings.from_environment()
        if not settings.render_enabled:
            print(
                json.dumps(
                    {
                        "state": "disabled",
                        "backend": "cpu-render",
                        "reason": "render_flag_off",
                    }
                )
            )
            return 0
        if args.data_dir:
            if not args.data_dir.is_absolute():
                raise ValueError("CPU data directory must be absolute")
            configured = (
                settings.database_url
                if (
                    os.environ.get("SIXNINE_DATABASE_URL")
                    or os.environ.get("SIXNINE_DATABASE_URL_FILE")
                )
                else ""
            )
            settings = replace(
                settings, data_dir=args.data_dir, database_url=configured
            )
        # Use exactly the API's reviewed store identity. No default AWS account,
        # provider fallback, credentials in args, or copies of the central loader.
        if settings.storage_provider == "local":
            store = LocalObjectStore(settings.data_dir / "objects")
        elif settings.storage_provider == "r2":
            cfg = S3StorageConfig(
                "r2",
                settings.storage_endpoint,
                settings.storage_region,
                settings.storage_bucket,
                "cloudflare-r2",
                settings.storage_profile,
                enabled=True,
            )
            credentials = load_configured_storage_credentials(
                cfg,
                credentials_file=settings.storage_credential_file,
                registry_root=os.environ.get("AI_REGISTRY_ROOT"),
            )
            store = S3ObjectStore(cfg, credentials)
        else:
            raise ValueError(
                "CPU worker storage requires an explicit reviewed runtime adapter"
            )
        repo = Repository(settings.database_url)
        repo.create_schema()
        if args.max_state_gib < 4:
            raise ValueError("CPU state cap must fit the 4 GiB per-attempt reservation")
        backend = CPURenderBackend(
            args.work_dir / "attempts",
            enabled=True,
            max_state_bytes=args.max_state_gib * 1024**3,
            subtitle_font_profile=args.subtitle_font_profile,
        )
        if args.contract_version >= 3:
            # Configuration v3 is a promise that the operator-installed renderer
            # can burn the fixed caption preset. Fail before reserving the slot.
            backend.assert_subtitle_ready()
        control = WorkerControl(repo)
        worker = control.register(
            WorkerSpec(
                args.worker_id,
                POOL,
                "local-cpu",
                args.instance_id,
                (),
                (RECIPE,),
                MODEL,
                CONFIGURATIONS[args.contract_version],
                backend="cpu-render",
            )
        )
        if worker["current_job_id"] is None and worker["state"] in {
            "registered",
            "ready",
            "unknown",
            "draining",
        }:
            control.mark_ready(
                args.worker_id, upstream_idle_confirmed=args.confirmed_idle
            )
        runner = WorkerRunner(
            repo,
            store,
            args.work_dir,
            backend=backend,
            control=control,
            submission_guard=ExecutionPolicies(settings, repo).submission_allowed,
        )
        if args.once:
            print(json.dumps(runner.run_once(args.worker_id, POOL)))
        else:
            runner.run_forever(args.worker_id, POOL)
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "state": "cpu_render_configuration_or_runtime_error",
                    "detail": "Review explicit CPU identity, readiness, storage and render flag; no secret values logged",
                }
            )
        )
        return 1
    finally:
        if args.once and control is not None:
            try:
                control.drain(args.worker_id)
            except Exception:
                pass  # Expiry remains UNKNOWN; never claim a lost DB is idle.
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Operational waiting-capacity entrypoint; intentionally has NO cloud adapter.

Disabled exits before importing Settings, opening a DB or reading policy files.
Dry-run only reads existing schema. Advance consumes trusted fleet qualification
records and rejects expired/revoked waits; it never creates/reconciles/destroys a VM.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import threading


def run(
    controller,
    approval_ids,
    *,
    mode,
    loop=False,
    interval_s=15,
    stop_event=None,
    emit=print,
):
    if mode not in ("dry-run", "advance") or not 1 <= interval_s <= 60:
        raise ValueError("invalid_capacity_runtime_mode")
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        for approval_id in approval_ids:
            if stop_event.is_set():
                break
            result = (
                controller.preview(approval_id)
                if mode == "dry-run"
                else controller.advance_once(approval_id)
            )
            emit(json.dumps(result, sort_keys=True))
        if not loop or stop_event.wait(interval_s):
            break
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local capacity activation; cloud bootstrap is not installed"
    )
    parser.add_argument(
        "--mode", choices=("disabled", "dry-run", "advance"), default="disabled"
    )
    parser.add_argument("--approval-id", action="append", default=[])
    parser.add_argument("--data-dir", type=Path)
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument("--loop", action="store_true")
    lifecycle.add_argument(
        "--once", action="store_true"
    )  # bounded once is also the default
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args(argv)
    if args.mode == "disabled":
        print(
            json.dumps(
                {
                    "state": "disabled",
                    "cloud_creation_enabled": False,
                    "provider_calls_enabled": False,
                }
            )
        )
        return 0
    repo, prior_handlers = None, {}
    try:
        if not 1 <= len(args.approval_id) <= 128 or len(set(args.approval_id)) != len(
            args.approval_id
        ):
            raise ValueError("explicit_capacity_approval_ids_required")
        if not 1 <= args.interval <= 60:
            raise ValueError("invalid_capacity_runtime_interval")
        from dataclasses import replace
        from .settings import Settings
        from .repository import Repository
        from .capacity import ColdStartCoordinator
        from .execution_policy import ExecutionPolicies

        settings = Settings.from_environment()
        if args.data_dir:
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
        # Existing state only. Never initialize a fresh DB or perform DDL here.
        if settings.database_url.startswith("sqlite:///"):
            from sqlalchemy.engine import make_url

            target = make_url(settings.database_url).database
            if not target or target == ":memory:" or not Path(target).is_file():
                raise ValueError("capacity_database_must_already_exist")
        repo = Repository(settings.database_url)
        policies = ExecutionPolicies(settings, repo)
        controller = ColdStartCoordinator(
            repo,
            enabled=args.mode == "advance",
            approval_guard=policies.capacity_approval_current,
            activation_guard=policies.activation_allowed,
        )
        stop_event = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                prior_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: stop_event.set())
        return run(
            controller,
            args.approval_id,
            mode=args.mode,
            loop=args.loop,
            interval_s=args.interval,
            stop_event=stop_event,
        )
    except Exception:
        print(
            json.dumps(
                {
                    "state": "capacity_configuration_or_runtime_error",
                    "cloud_creation_enabled": False,
                    "provider_calls_enabled": False,
                }
            )
        )
        return 1
    finally:
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())

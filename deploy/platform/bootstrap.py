#!/usr/bin/python3
"""Operator-only first-boot initialization; NEVER grant deploy sudo access here.

Install beside the independently reviewed root-owned release.py. This command
loads only an independently approved bundle, initializes the private database,
and obtains two passwords interactively. It does not publish the application.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import release


def management_status(directory, environment):
    raw = release.compose(directory, environment, "run", "--rm", "--no-deps", "-T", "app",
                          "python", "-m", "studio_platform.manage", "status")
    try:
        value = json.loads(raw)
        accounts = value["accounts"]
        release.require(isinstance(accounts, list) and len(accounts) <= 2 and len({v["username"] for v in accounts}) == len(accounts)
                        and all(set(v) == {"username", "configured", "disabled"} and v["username"] in {"superdan", "supervan"}
                                and v["configured"] is True and v["disabled"] is False for v in accounts),
                        "existing_account_state_requires_separate_administration")
        release.require(value["authentication"] == "password" and value["execution_backend"] == "disabled"
                        and value["generation_enabled"] is False and value["render_enabled"] is False
                        and value["storage_provider"] == "local", "bootstrap_runtime_policy_differs")
        return value
    except (ValueError, KeyError, TypeError):
        raise release.ReleaseError("bootstrap_status_unrecognized_no_details_logged") from None


def set_password(directory, environment, user):
    release.require(user in {"superdan", "supervan"} and sys.stdin.isatty() and sys.stdout.isatty(),
                    "interactive_operator_terminal_required")
    arguments = [release.DOCKER, "--host", "unix:///var/run/docker.sock", "compose", "--project-directory", str(directory),
                 "-f", str(directory / "compose.yaml"),
                 *release.deployment_target.database_compose_args(directory, environment),
                 "run", "--rm", "--no-deps", "app", "python", "-m",
                 "studio_platform.manage", "set-password", "--user", user]
    try:
        # The reviewed manage command hides password input, suppresses driver
        # errors and never receives a password in argv/environment. No capture
        # or pipe is permitted, so the operator sees its confirmation prompt.
        subprocess.run(arguments, env=environment, check=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise release.ReleaseError("account_initialization_incomplete_retry_same_commit") from None


def bootstrap_locked(root, commit):
    state_file = root / "release-state.json"
    state = {}
    if state_file.exists():
        release.regular(state_file, root_owned=True, maximum=16384)
        state = json.loads(state_file.read_text(encoding="utf-8"))
    release.require(state.get("current") is None and state.get("pending") in (None, commit),
                    "bootstrap_only_for_first_unpublished_release")
    release.approved_manifest(root, root / "incoming" / commit, commit)
    directory = release.prepare_bundle(root, commit)
    release.approved_manifest(root, directory, commit)
    environment = release.deployment_environment(root / "site.env", commit)
    dependencies = release.pinned_dependencies(environment)
    release.require(state.get("dependencies") in (None, dependencies), "dependency_change_requires_separate_maintenance")
    release.approved_configuration(directory, environment)
    state = {**state, "current": None, "pending": commit, "dependencies": dependencies,
             "status": "prepared_needs_accounts", "updated_at": time.time()}
    release.write_state(root, state)
    release.load_approved_image(root, directory, commit, environment)
    if release.deployment_target.database(environment)['mode'] == 'local':
        release.compose(directory, environment, "up", "-d", "db")
        release.compose(directory, environment, "run", "--rm", "db-init")
    release.compose(directory, environment, "run", "--rm", "--no-deps", "-T", "app",
                    "python", "-m", "studio_platform.manage", "init-db")
    status = management_status(directory, environment)
    configured = {item["username"] for item in status["accounts"]}
    for user in ("superdan", "supervan"):
        if user not in configured:
            set_password(directory, environment, user)
    status = management_status(directory, environment)
    release.require(status.get("auth_ready") is True, "accounts_not_ready_application_stays_unpublished")
    release.write_state(root, {**state, "status": "accounts_ready_waiting_release", "updated_at": time.time()})


def main():
    import fcntl
    try:
        release.require(len(sys.argv) == 2 and bool(release.SHA.fullmatch(sys.argv[1])), "one_commit_argument_required")
        release.require(sys.stdin.isatty() and sys.stdout.isatty(), "interactive_operator_terminal_required")
        release.check_host(release.ROOT)
        with (release.ROOT / "release.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise release.ReleaseError("another_release_is_in_progress") from None
            bootstrap_locked(release.ROOT, sys.argv[1])
        print("Accounts initialized; application/proxy have not been published. Run the approved release entry next.")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(str(error) if isinstance(error, release.ReleaseError) else "bootstrap_incomplete_details_suppressed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

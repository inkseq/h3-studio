"""Local administration, without public signup or plaintext secret files.

Run with the same SIXNINE_* non-secret settings / process-only DB connection as
the service. Passwords and machine tokens are read from an interactive terminal.
No command here starts GPU resources or contacts inference providers.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import warnings
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from .auth import Auth, USERS, PASSWORD_HASH, clients, accounts, sessions
from .repository import Repository
from .settings import Settings


def terminal_secret(prompt):
    if not sys.stdin.isatty():
        raise ValueError(
            "Interactive terminal required; do not put secrets in command arguments or files"
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sixnine private platform administration"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init-db",
        help="Initialize this platform's separate database; does not import legacy user data",
    )
    commands.add_parser(
        "status",
        help="Show non-secret authentication readiness and configuration flags",
    )
    passwords = commands.add_parser(
        "set-password", help="Set one account password and revoke its sessions"
    )
    passwords.add_argument("--user", required=True, choices=sorted(USERS))
    disable = commands.add_parser(
        "disable-user",
        help="Disable an account and revoke sessions and machine clients",
    )
    disable.add_argument("--user", required=True, choices=sorted(USERS))
    registration = commands.add_parser(
        "register-client",
        help="Register a high-entropy token by its hash, scoped to explicit projects",
    )
    registration.add_argument("--id", required=True)
    registration.add_argument("--owner", choices=sorted(USERS), required=True)
    registration.add_argument("--project", action="append", required=True)
    registration.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=[
            "projects:read",
            "assets:read",
            "assets:write",
            "jobs:read",
            "jobs:write",
        ],
    )
    revocation = commands.add_parser("revoke-client")
    revocation.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    repo = None
    try:
        settings = Settings.from_environment()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        repo = Repository(settings.database_url)
        repo.create_schema()
        auth = Auth(
            repo.engine,
            tenant=settings.tenant_id,
            mode=settings.auth_mode,
            session_seconds=settings.session_seconds,
        )
        if args.command == "init-db":
            # Metadata-only initialization. No object-store SDK is constructed.
            from .assets import metadata as assets_metadata
            from .batches import metadata as batch_metadata
            from .storage_asset_journal import _schema as upload_metadata
            from .storage_multipart import _metadata as multipart_metadata

            assets_metadata.create_all(repo.engine)
            batch_metadata.create_all(repo.engine)
            upload_metadata.create_all(repo.engine)
            multipart_metadata.create_all(repo.engine)
            print(
                "Platform database initialized; legacy data unchanged; inference remains controlled by explicit service settings"
            )
        elif args.command == "status":
            with repo.engine.connect() as conn:
                client_rows = [
                    dict(row)
                    for row in conn.execute(
                        select(clients.c.id, clients.c.owner, clients.c.disabled).where(
                            clients.c.tenant == settings.tenant_id
                        )
                    ).mappings()
                ]
                account_rows = conn.execute(
                    select(
                        accounts.c.username,
                        accounts.c.password_hash,
                        accounts.c.disabled,
                    ).where(accounts.c.tenant == settings.tenant_id)
                ).all()
            print(
                json.dumps(
                    {
                        "tenant": settings.tenant_id,
                        "authentication": settings.auth_mode,
                        "auth_ready": auth.ready(),
                        "execution_backend": settings.execution_backend,
                        "generation_enabled": settings.generation_enabled,
                        "storage_provider": settings.storage_provider,
                        "render_enabled": settings.render_enabled,
                        "accounts": [
                            {
                                "username": name,
                                "configured": bool(PASSWORD_HASH.fullmatch(encoded)),
                                "disabled": bool(disabled),
                            }
                            for name, encoded, disabled in account_rows
                        ],
                        "clients": client_rows,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "set-password":
            password = terminal_secret(
                f"{args.user} new password (12+ characters, max 72 UTF-8 bytes): "
            )
            confirmation = terminal_secret("Confirm password: ")
            if password != confirmation:
                raise ValueError("Passwords did not match; no account change was made")
            auth.set_password(args.user, password)
            del password, confirmation
            print(f"{args.user}: password saved; previous browser sessions revoked")
        elif args.command == "disable-user":
            with repo.transaction() as conn:
                conn.execute(
                    update(accounts)
                    .where(
                        accounts.c.tenant == settings.tenant_id,
                        accounts.c.username == args.user,
                    )
                    .values(disabled=1)
                )
                conn.execute(
                    delete(sessions).where(
                        sessions.c.tenant == settings.tenant_id,
                        sessions.c.username == args.user,
                    )
                )
                conn.execute(
                    update(clients)
                    .where(
                        clients.c.tenant == settings.tenant_id,
                        clients.c.owner == args.user,
                    )
                    .values(disabled=1)
                )
            print(
                f"{args.user}: account disabled; sessions and machine clients revoked; projects retained"
            )
        elif args.command == "register-client":
            token = terminal_secret(
                "Existing high-entropy service token (hidden; never written in plaintext): "
            )
            auth.register_client(args.id, token, args.owner, args.project, args.scope)
            del token
            print(
                f"{args.id}: scoped service client saved; any earlier token for this ID is revoked"
            )
        elif args.command == "revoke-client":
            with repo.transaction() as conn:
                conn.execute(
                    update(clients)
                    .where(
                        clients.c.tenant == settings.tenant_id, clients.c.id == args.id
                    )
                    .values(disabled=1)
                )
            print("Service client revoked if present")
        return 0
    except (ValueError, getpass.GetPassWarning) as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Database operation failed; connection details were suppressed",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Administration cancelled", file=sys.stderr)
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())

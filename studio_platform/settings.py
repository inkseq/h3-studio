"""Explicit service settings; defaults never enable GPU or cloud creation."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
import re
from pathlib import Path
import stat
from urllib.parse import urlsplit


def public_origin_from_environment():
    """Prefer the product-neutral name; a conflicting legacy alias is invalid."""
    current = os.environ.get("PUBLIC_ORIGIN")
    legacy = os.environ.get("SIXNINE_PUBLIC_ORIGIN")
    if (
        current is not None
        and legacy is not None
        and current.rstrip("/") != legacy.rstrip("/")
    ):
        raise ValueError("PUBLIC_ORIGIN conflicts with SIXNINE_PUBLIC_ORIGIN")
    return (current if current is not None else legacy or "").rstrip("/")


def _public_https_origin(value):
    if not isinstance(value, str):
        raise ValueError("PUBLIC_ORIGIN must be one exact public HTTPS origin")
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed/out-of-range authorities before matching Host.
    except ValueError:
        raise ValueError(
            "PUBLIC_ORIGIN must be one exact public HTTPS origin"
        ) from None
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
    if (
        value != value.strip()
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        or address is not None
        and not address.is_global
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != "https://" + parsed.netloc
        or "*" in value
    ):
        raise ValueError("PUBLIC_ORIGIN must be one exact public HTTPS origin")
    return parsed


def database_url_from_environment():
    """Load the DSN from an explicitly protected runtime file, never log it."""
    value = os.environ.get("SIXNINE_DATABASE_URL", "")
    filename = os.environ.get("SIXNINE_DATABASE_URL_FILE", "")
    if value and filename:
        raise ValueError("Configure only one database credential source")
    if not filename:
        return value
    path = Path(filename)
    if not path.is_absolute():
        raise ValueError("Database credential file must be an absolute path")
    try:
        # Follow an explicit runtime mount, but validate the actual open file.
        # This avoids a stat/read race and never reflects file content in errors.
        with path.open("rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Invalid database credential file")
            if os.name != "nt" and metadata.st_mode & (stat.S_IRWXO | stat.S_IWGRP):
                raise ValueError("Database credential file permissions are too broad")
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError("Invalid database credential file")
        result = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
        if (
            not result
            or result != result.strip()
            or any(c in result for c in ("\n", "\r", "\x00"))
        ):
            raise ValueError("Invalid database credential file")
        return result
    except (OSError, UnicodeError):
        raise ValueError("Database credential file is unavailable or invalid") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str = field(repr=False, default="")
    public_origin: str = ""
    deployment_target: str = "production"
    local_ui_origins: tuple[str, ...] = ()
    auth_mode: str = "password"
    generation_enabled: bool = False
    execution_backend: str = "disabled"
    tenant_id: str = "sixnine"
    max_upload_bytes: int = 512 * 1024 * 1024
    max_project_bytes: int = 8 * 1024 * 1024
    session_seconds: int = 7 * 86400
    storage_provider: str = "local"
    storage_bucket: str = ""
    storage_profile: str = ""
    storage_endpoint: str = ""
    storage_region: str = ""
    storage_credential_file: Path | None = None
    frontend_dir: Path | None = None
    frontend_release_dir: Path | None = None
    execution_policy_file: Path | None = None
    execution_profiles_file: Path | None = None
    default_deployment_profile_id: str | None = None
    operator_capacity_owners: tuple[str, ...] = ()
    operator_admin_owners: tuple[str, ...] = ()
    storyboard_owners: tuple[str, ...] = ()
    stage_query_config_file: Path | None = None
    render_enabled: bool = False
    recovery_backends: tuple[str, ...] = ()
    generation_queue: str = "postgres"
    title_config_file: Path | None = None
    workos_config_file: Path | None = None
    stripe_config_file: Path | None = None
    stripe_mode: str = "off"
    title_use_central: bool = False
    # Chat-first story building (#422): on unless INKSEQ_STORY_CHAT=0 (owner enabled it for
    # production). It reuses the title text credential; without one /quick-chat keeps the
    # original Quick Create. Pictures stay behind their own audited admin switch.
    story_chat_enabled: bool = False
    # Chat turns per Agent/Telegram connection per 24 h (#531); bounds model cost.
    story_chat_connection_daily_turns: int = 200
    public_origin_aliases: tuple[str, ...] = ()
    account_deletion_test_owners: tuple[str, ...] = ()
    sentry_dsn: str = ""
    # Internal derivation only; there is no independently overridable Sentry
    # environment. Unlike the legacy deployment default, unset means no send.
    sentry_environment: str = ""

    def __post_init__(self):
        from .error_monitoring import valid_dsn

        if (
            not isinstance(self.account_deletion_test_owners, tuple)
            or len(self.account_deletion_test_owners) > 20
            or any(
                not isinstance(owner, str) or not re.fullmatch(r"u_[0-9a-f]{32}", owner)
                for owner in self.account_deletion_test_owners
            )
        ):
            raise ValueError("Invalid disposable account deletion fixtures")
        if self.deployment_target not in {"production", "staging"}:
            raise ValueError("Unsupported deployment target")
        if (
            not valid_dsn(self.sentry_dsn)
            or type(self.sentry_environment) is not str
            or self.sentry_environment not in {"", "staging", "production"}
            or self.sentry_environment
            and self.sentry_environment != self.deployment_target
        ):
            raise ValueError("Invalid API error monitoring configuration")
        if self.stripe_mode not in {"off", "test", "live"}:
            raise ValueError("Unsupported Stripe mode")
        if self.stripe_config_file is not None:
            path = Path(self.stripe_config_file)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("Stripe credential path must be absolute")
            object.__setattr__(self, "stripe_config_file", path)
        if self.stripe_mode == "test" and (
            self.deployment_target != "staging"
            or self.public_origin != "https://staging.inkseq.com"
            or self.public_origin_aliases
            or self.stripe_config_file is None
        ):
            raise ValueError(
                "Stripe test mode requires its isolated staging configuration"
            )
        if self.stripe_mode == "live" and (
            self.deployment_target != "production"
            or self.public_origin != "https://inkseq.com"
            or not set(self.public_origin_aliases)
            <= {"https://sixnine.art", "https://www.sixnine.art"}
            or self.stripe_config_file is None
        ):
            raise ValueError("Stripe live mode requires its production configuration")
        if self.generation_queue not in {"postgres", "dbos"}:
            raise ValueError("Unsupported generation queue")
        if self.generation_queue == "dbos" and not self.database_url.startswith(
            "postgresql+psycopg:"
        ):
            raise ValueError("DBOS requires the original PostgreSQL database")
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.frontend_dir is not None:
            object.__setattr__(self, "frontend_dir", Path(self.frontend_dir).resolve())
        if (
            self.stage_query_config_file is not None
            and not Path(self.stage_query_config_file).is_absolute()
        ):
            raise ValueError("Stage query credential path must be absolute")
        if self.storage_credential_file is not None:
            path = Path(self.storage_credential_file)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or self.storage_provider != "r2"
            ):
                raise ValueError(
                    "Storage credential file requires R2 and an absolute protected path"
                )
            object.__setattr__(self, "storage_credential_file", path)
        if (
            self.title_config_file is not None
            and not Path(self.title_config_file).is_absolute()
        ):
            raise ValueError("Title credential path must be absolute")
        if self.workos_config_file is not None:
            path = Path(self.workos_config_file)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("WorkOS credential path must be absolute")
            object.__setattr__(self, "workos_config_file", path)
        if type(self.title_use_central) is not bool or (
            self.title_config_file is not None and self.title_use_central
        ):
            raise ValueError("Configure only one explicit title credential source")
        if self.public_origin and self.title_use_central:
            raise ValueError(
                "Public title generation requires the protected runtime credential source"
            )
        if self.frontend_release_dir is not None:
            from .frontend import validate_release_directory

            object.__setattr__(
                self,
                "frontend_release_dir",
                validate_release_directory(self.frontend_release_dir),
            )
        if (
            self.execution_policy_file is not None
            and not Path(self.execution_policy_file).is_absolute()
        ):
            raise ValueError("Execution policy path must be absolute")
        if (
            self.execution_profiles_file is not None
            and not Path(self.execution_profiles_file).is_absolute()
        ):
            raise ValueError("Execution profiles path must be absolute")
        if self.default_deployment_profile_id is not None:
            from .runtime_catalog import get_profile

            get_profile(self.default_deployment_profile_id)
        if (
            not isinstance(self.operator_capacity_owners, tuple)
            or len(set(self.operator_capacity_owners))
            != len(self.operator_capacity_owners)
            or any(
                not isinstance(v, str) or v not in {"superdan", "supervan"}
                for v in self.operator_capacity_owners
            )
        ):
            raise ValueError(
                "Operator capacity identities must be explicit existing accounts"
            )
        if (
            not isinstance(self.operator_admin_owners, tuple)
            or len(set(self.operator_admin_owners)) != len(self.operator_admin_owners)
            or any(
                not isinstance(v, str) or v not in self.operator_capacity_owners
                for v in self.operator_admin_owners
            )
        ):
            raise ValueError(
                "Operator administrators must be an explicit subset of capacity accounts"
            )
        from .auth import USERS

        if (
            not isinstance(self.storyboard_owners, tuple)
            or any(
                not isinstance(v, str) or v not in USERS for v in self.storyboard_owners
            )
            or len(set(self.storyboard_owners)) != len(self.storyboard_owners)
        ):
            raise ValueError("Storyboard identities must be explicit existing accounts")
        if not self.database_url:
            object.__setattr__(
                self,
                "database_url",
                "sqlite:///" + (self.data_dir / "platform.sqlite3").as_posix(),
            )
        if self.auth_mode not in {"password", "local-test"}:
            raise ValueError("Use password or explicit local-test authentication")
        if (
            type(self.session_seconds) is not int
            or not 1 <= self.session_seconds <= 365 * 86400
        ):
            raise ValueError("Invalid browser session duration")
        if (
            not isinstance(self.public_origin_aliases, tuple)
            or any(not isinstance(origin, str) for origin in self.public_origin_aliases)
            or len(set(self.public_origin_aliases)) != len(self.public_origin_aliases)
            or self.public_origin in self.public_origin_aliases
            or self.public_origin_aliases
            and not self.public_origin
        ):
            raise ValueError(
                "Public origin aliases require distinct explicit origins and a canonical origin"
            )
        if self.public_origin:
            _public_https_origin(self.public_origin)
            for alias in self.public_origin_aliases:
                _public_https_origin(alias)
            if self.auth_mode != "password":
                raise ValueError("Public deployments require password authentication")
            if self.execution_backend == "mock":
                raise ValueError("Public deployments cannot present mock generation")
            if self.local_ui_origins:
                raise ValueError(
                    "Public deployments cannot include local preview origin exceptions"
                )
        for origin in self.local_ui_origins:
            local = urlsplit(origin)
            if (
                local.scheme != "http"
                or local.hostname not in {"localhost", "127.0.0.1"}
                or local.username
                or local.password
                or local.path
                or local.query
                or local.fragment
            ):
                raise ValueError("Local UI origins must be exact loopback HTTP origins")
        if self.execution_backend not in {
            "disabled",
            "mock",
            "comfy-worker",
            "wangp-worker",
        }:
            raise ValueError("Unsupported execution backend")
        if (
            not isinstance(self.recovery_backends, tuple)
            or any(not isinstance(value, str) for value in self.recovery_backends)
            or len(set(self.recovery_backends)) != len(self.recovery_backends)
            or any(
                value not in {"comfy-worker", "wangp-worker"}
                for value in self.recovery_backends
            )
        ):
            raise ValueError("Recovery backends must explicitly name real engines")
        if self.generation_enabled and self.execution_backend == "disabled":
            raise ValueError("Generation requires an explicitly configured backend")
        if self.storage_provider not in {"local", "r2", "s3", "hippius"}:
            raise ValueError("Unsupported storage provider")
        if self.storage_provider != "local" and not all(
            (self.storage_bucket, self.storage_endpoint, self.storage_region)
        ):
            raise ValueError(
                "Remote storage requires explicit bucket, endpoint and region"
            )
        if self.max_upload_bytes < 1 or self.max_project_bytes < 1024:
            raise ValueError("Invalid request limits")

    def public_origin_for_host(self, host):
        """Resolve only a configured exact authority, never a forwarded header."""
        return next(
            (
                origin
                for origin in (self.public_origin, *self.public_origin_aliases)
                if origin and urlsplit(origin).netloc.lower() == host.lower()
            ),
            None,
        )

    @classmethod
    def from_environment(cls):
        default = Path(__file__).resolve().parents[1] / ".platform-data"
        session_seconds = os.environ.get("INKSEQ_BROWSER_SESSION_SECONDS")
        legacy_session_seconds = os.environ.get("SIXNINE_SESSION_SECONDS")
        if (
            session_seconds is not None
            and legacy_session_seconds is not None
            and session_seconds != legacy_session_seconds
        ):
            raise ValueError("Conflicting browser session duration settings")
        duration = (
            session_seconds if session_seconds is not None else legacy_session_seconds
        )
        if duration is not None and (not duration.isascii() or not duration.isdigit()):
            raise ValueError("Invalid browser session duration")
        return cls(
            data_dir=Path(os.environ.get("SIXNINE_DATA", str(default))),
            database_url=database_url_from_environment(),
            public_origin=public_origin_from_environment(),
            deployment_target=os.environ.get("INKSEQ_DEPLOYMENT_TARGET", "production"),
            sentry_dsn=os.environ.get("INKSEQ_SENTRY_DSN", ""),
            sentry_environment=os.environ.get("INKSEQ_DEPLOYMENT_TARGET", ""),
            account_deletion_test_owners=tuple(
                filter(
                    None,
                    os.environ.get("INKSEQ_ACCOUNT_DELETION_TEST_OWNERS", "").split(
                        ","
                    ),
                )
            ),
            public_origin_aliases=tuple(os.environ["PUBLIC_ORIGIN_ALIASES"].split(","))
            if os.environ.get("PUBLIC_ORIGIN_ALIASES")
            else (),
            local_ui_origins=tuple(
                x.strip()
                for x in os.environ.get("SIXNINE_LOCAL_UI_ORIGINS", "").split(",")
                if x.strip()
            ),
            auth_mode=os.environ.get("SIXNINE_AUTH_MODE", "password"),
            session_seconds=int(duration) if duration is not None else 7 * 86400,
            generation_enabled=os.environ.get("SIXNINE_GENERATION_ENABLED", "0") == "1",
            execution_backend=os.environ.get("SIXNINE_EXECUTION_BACKEND", "disabled"),
            generation_queue=os.environ.get("INKSEQ_GENERATION_QUEUE", "postgres"),
            storage_provider=os.environ.get("SIXNINE_STORAGE_PROVIDER", "local"),
            storage_bucket=os.environ.get("SIXNINE_STORAGE_BUCKET", ""),
            storage_profile=os.environ.get("SIXNINE_STORAGE_PROFILE", ""),
            storage_endpoint=os.environ.get("SIXNINE_STORAGE_ENDPOINT", ""),
            storage_region=os.environ.get("SIXNINE_STORAGE_REGION", ""),
            storage_credential_file=Path(os.environ["SIXNINE_STORAGE_CREDENTIAL_FILE"])
            if os.environ.get("SIXNINE_STORAGE_CREDENTIAL_FILE")
            else None,
            frontend_dir=Path(os.environ["SIXNINE_FRONTEND_DIR"])
            if os.environ.get("SIXNINE_FRONTEND_DIR")
            else None,
            frontend_release_dir=Path(os.environ["SIXNINE_FRONTEND_RELEASE_DIR"])
            if os.environ.get("SIXNINE_FRONTEND_RELEASE_DIR")
            else None,
            execution_policy_file=Path(os.environ["SIXNINE_EXECUTION_POLICY_FILE"])
            if os.environ.get("SIXNINE_EXECUTION_POLICY_FILE")
            else None,
            execution_profiles_file=Path(os.environ["SIXNINE_EXECUTION_PROFILES_FILE"])
            if os.environ.get("SIXNINE_EXECUTION_PROFILES_FILE")
            else None,
            default_deployment_profile_id=os.environ.get(
                "SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"
            )
            or None,
            operator_capacity_owners=tuple(
                x.strip()
                for x in os.environ.get("SIXNINE_OPERATOR_CAPACITY_OWNERS", "").split(
                    ","
                )
                if x.strip()
            ),
            operator_admin_owners=tuple(
                x.strip()
                for x in os.environ.get("SIXNINE_OPERATOR_ADMIN_OWNERS", "").split(",")
                if x.strip()
            ),
            storyboard_owners=tuple(
                x.strip()
                for x in os.environ.get("SIXNINE_STORYBOARD_OWNERS", "").split(",")
                if x.strip()
            ),
            stage_query_config_file=Path(os.environ["SIXNINE_STAGE_QUERY_CONFIG_FILE"])
            if os.environ.get("SIXNINE_STAGE_QUERY_CONFIG_FILE")
            else None,
            render_enabled=os.environ.get("SIXNINE_RENDER_ENABLED", "0") == "1",
            recovery_backends=tuple(
                x.strip()
                for x in os.environ.get("SIXNINE_RECOVERY_BACKENDS", "").split(",")
                if x.strip()
            ),
            title_config_file=Path(os.environ["SIXNINE_TITLE_CONFIG_FILE"])
            if os.environ.get("SIXNINE_TITLE_CONFIG_FILE")
            else None,
            workos_config_file=Path(os.environ["INKSEQ_WORKOS_CONFIG_FILE"])
            if os.environ.get("INKSEQ_WORKOS_CONFIG_FILE")
            else None,
            stripe_config_file=Path(os.environ["INKSEQ_STRIPE_CONFIG_FILE"])
            if os.environ.get("INKSEQ_STRIPE_CONFIG_FILE")
            else None,
            stripe_mode=os.environ.get("INKSEQ_STRIPE_MODE", "off"),
            title_use_central=os.environ.get("SIXNINE_TITLE_USE_CENTRAL", "0") == "1",
            story_chat_enabled=os.environ.get("INKSEQ_STORY_CHAT") != "0",
        )

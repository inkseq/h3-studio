"""Explicit service settings; defaults never enable GPU or cloud creation."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit


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
        if not result or result != result.strip() or any(c in result for c in ("\n", "\r", "\x00")):
            raise ValueError("Invalid database credential file")
        return result
    except (OSError, UnicodeError):
        raise ValueError("Database credential file is unavailable or invalid") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str = field(repr=False, default="")
    public_origin: str = ""
    local_ui_origins: tuple[str, ...] = ()
    auth_mode: str = "password"
    generation_enabled: bool = False
    execution_backend: str = "disabled"
    tenant_id: str = "sixnine"
    max_upload_bytes: int = 512 * 1024 * 1024
    max_project_bytes: int = 8 * 1024 * 1024
    session_seconds: int = 12 * 3600
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
    title_config_file: Path | None = None
    title_use_central: bool = False

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.frontend_dir is not None:
            object.__setattr__(self, "frontend_dir", Path(self.frontend_dir).resolve())
        if self.stage_query_config_file is not None and not Path(self.stage_query_config_file).is_absolute():
            raise ValueError("Stage query credential path must be absolute")
        if self.storage_credential_file is not None:
            path = Path(self.storage_credential_file)
            if not path.is_absolute() or ".." in path.parts or self.storage_provider != "r2":
                raise ValueError("Storage credential file requires R2 and an absolute protected path")
            object.__setattr__(self, "storage_credential_file", path)
        if self.title_config_file is not None and not Path(self.title_config_file).is_absolute():
            raise ValueError("Title credential path must be absolute")
        if type(self.title_use_central) is not bool or (self.title_config_file is not None and self.title_use_central):
            raise ValueError("Configure only one explicit title credential source")
        if self.public_origin and self.title_use_central:
            raise ValueError("Public title generation requires the protected runtime credential source")
        if self.frontend_release_dir is not None:
            from .frontend import validate_release_directory
            object.__setattr__(self, "frontend_release_dir", validate_release_directory(self.frontend_release_dir))
        if self.execution_policy_file is not None and not Path(self.execution_policy_file).is_absolute():
            raise ValueError("Execution policy path must be absolute")
        if self.execution_profiles_file is not None and not Path(self.execution_profiles_file).is_absolute():
            raise ValueError("Execution profiles path must be absolute")
        if self.default_deployment_profile_id is not None:
            from .runtime_catalog import get_profile
            get_profile(self.default_deployment_profile_id)
        if (not isinstance(self.operator_capacity_owners, tuple)
                or len(set(self.operator_capacity_owners)) != len(self.operator_capacity_owners)
                or any(not isinstance(v, str) or v not in {"superdan", "supervan"} for v in self.operator_capacity_owners)):
            raise ValueError("Operator capacity identities must be explicit existing accounts")
        if (not isinstance(self.operator_admin_owners, tuple)
                or len(set(self.operator_admin_owners)) != len(self.operator_admin_owners)
                or any(not isinstance(v, str) or v not in self.operator_capacity_owners
                       for v in self.operator_admin_owners)):
            raise ValueError("Operator administrators must be an explicit subset of capacity accounts")
        from .auth import USERS
        if (not isinstance(self.storyboard_owners, tuple)
                or any(not isinstance(v, str) or v not in USERS for v in self.storyboard_owners)
                or len(set(self.storyboard_owners)) != len(self.storyboard_owners)):
            raise ValueError("Storyboard identities must be explicit existing accounts")
        if not self.database_url:
            object.__setattr__(self, "database_url", "sqlite:///" + (self.data_dir / "platform.sqlite3").as_posix())
        if self.auth_mode not in {"password", "local-test"}:
            raise ValueError("Use password or explicit local-test authentication")
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or "*" in self.public_origin):
                raise ValueError("PUBLIC_ORIGIN must be one exact HTTPS origin")
            if self.auth_mode != "password":
                raise ValueError("Public deployments require password authentication")
            if self.execution_backend == "mock":
                raise ValueError("Public deployments cannot present mock generation")
            if self.local_ui_origins:
                raise ValueError("Public deployments cannot include local preview origin exceptions")
        for origin in self.local_ui_origins:
            local = urlsplit(origin)
            if (local.scheme != "http" or local.hostname not in {"localhost", "127.0.0.1"}
                    or local.username or local.password or local.path or local.query or local.fragment):
                raise ValueError("Local UI origins must be exact loopback HTTP origins")
        if self.execution_backend not in {"disabled", "mock", "comfy-worker", "wangp-worker"}:
            raise ValueError("Unsupported execution backend")
        if (not isinstance(self.recovery_backends, tuple)
                or any(not isinstance(value, str) for value in self.recovery_backends)
                or len(set(self.recovery_backends)) != len(self.recovery_backends)
                or any(value not in {"comfy-worker", "wangp-worker"} for value in self.recovery_backends)):
            raise ValueError("Recovery backends must explicitly name real engines")
        if self.generation_enabled and self.execution_backend == "disabled":
            raise ValueError("Generation requires an explicitly configured backend")
        if self.storage_provider not in {"local", "r2", "s3", "hippius"}:
            raise ValueError("Unsupported storage provider")
        if self.storage_provider != "local" and not all((self.storage_bucket, self.storage_endpoint, self.storage_region)):
            raise ValueError("Remote storage requires explicit bucket, endpoint and region")
        if self.max_upload_bytes < 1 or self.max_project_bytes < 1024:
            raise ValueError("Invalid request limits")

    @classmethod
    def from_environment(cls):
        default = Path(__file__).resolve().parents[1] / ".platform-data"
        return cls(
            data_dir=Path(os.environ.get("SIXNINE_DATA", str(default))),
            database_url=database_url_from_environment(),
            public_origin=os.environ.get("SIXNINE_PUBLIC_ORIGIN", "").rstrip("/"),
            local_ui_origins=tuple(x.strip() for x in os.environ.get("SIXNINE_LOCAL_UI_ORIGINS", "").split(",") if x.strip()),
            auth_mode=os.environ.get("SIXNINE_AUTH_MODE", "password"),
            generation_enabled=os.environ.get("SIXNINE_GENERATION_ENABLED", "0") == "1",
            execution_backend=os.environ.get("SIXNINE_EXECUTION_BACKEND", "disabled"),
            storage_provider=os.environ.get("SIXNINE_STORAGE_PROVIDER", "local"),
            storage_bucket=os.environ.get("SIXNINE_STORAGE_BUCKET", ""),
            storage_profile=os.environ.get("SIXNINE_STORAGE_PROFILE", ""),
            storage_endpoint=os.environ.get("SIXNINE_STORAGE_ENDPOINT", ""),
            storage_region=os.environ.get("SIXNINE_STORAGE_REGION", ""),
            storage_credential_file=Path(os.environ["SIXNINE_STORAGE_CREDENTIAL_FILE"]) if os.environ.get("SIXNINE_STORAGE_CREDENTIAL_FILE") else None,
            frontend_dir=Path(os.environ["SIXNINE_FRONTEND_DIR"]) if os.environ.get("SIXNINE_FRONTEND_DIR") else None,
            frontend_release_dir=Path(os.environ["SIXNINE_FRONTEND_RELEASE_DIR"]) if os.environ.get("SIXNINE_FRONTEND_RELEASE_DIR") else None,
            execution_policy_file=Path(os.environ["SIXNINE_EXECUTION_POLICY_FILE"]) if os.environ.get("SIXNINE_EXECUTION_POLICY_FILE") else None,
            execution_profiles_file=Path(os.environ["SIXNINE_EXECUTION_PROFILES_FILE"]) if os.environ.get("SIXNINE_EXECUTION_PROFILES_FILE") else None,
            default_deployment_profile_id=os.environ.get("SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID") or None,
            operator_capacity_owners=tuple(x.strip() for x in os.environ.get("SIXNINE_OPERATOR_CAPACITY_OWNERS", "").split(",") if x.strip()),
            operator_admin_owners=tuple(x.strip() for x in os.environ.get("SIXNINE_OPERATOR_ADMIN_OWNERS", "").split(",") if x.strip()),
            storyboard_owners=tuple(x.strip() for x in os.environ.get("SIXNINE_STORYBOARD_OWNERS", "").split(",") if x.strip()),
            stage_query_config_file=Path(os.environ["SIXNINE_STAGE_QUERY_CONFIG_FILE"]) if os.environ.get("SIXNINE_STAGE_QUERY_CONFIG_FILE") else None,
            render_enabled=os.environ.get("SIXNINE_RENDER_ENABLED", "0") == "1",
            recovery_backends=tuple(x.strip() for x in os.environ.get("SIXNINE_RECOVERY_BACKENDS", "").split(",") if x.strip()),
            title_config_file=Path(os.environ["SIXNINE_TITLE_CONFIG_FILE"]) if os.environ.get("SIXNINE_TITLE_CONFIG_FILE") else None,
            title_use_central=os.environ.get("SIXNINE_TITLE_USE_CENTRAL", "0") == "1",
        )

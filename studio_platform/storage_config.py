"""Explicit storage configuration. Importing this module never loads credentials."""
from __future__ import annotations

import importlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


class StorageConfigurationError(ValueError):
    """Configuration is missing or conflicts with the selected provider."""


@dataclass(frozen=True)
class StorageCapabilities:
    conditional_create: bool
    presigned_download: bool
    presigned_upload: bool
    browser_cors_management: bool
    browser_upload_verified: bool = False
    multipart_implemented: bool = False
    experimental: bool = False


LOCAL_CAPABILITIES = StorageCapabilities(True, False, False, False)
PROVIDER_CAPABILITIES = {
    "r2": StorageCapabilities(True, True, True, True),
    "aws-s3": StorageCapabilities(True, True, True, True),
    # Documented S3 reads/writes, but no conditional PUT or verified browser CORS.
    "hippius": StorageCapabilities(False, True, False, False, experimental=True),
}


def _endpoint(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise StorageConfigurationError("An explicit HTTPS storage endpoint is required")
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username
                 and not parts.password and not parts.query and not parts.fragment
                 and parts.path in ("", "/") and parts.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise StorageConfigurationError("Storage endpoint must be HTTPS without credentials, path or query")
    return "https://" + parts.hostname.lower()


@dataclass(frozen=True)
class S3StorageConfig:
    provider: str
    endpoint_url: str
    region: str
    bucket: str
    service: str
    profile: str
    enabled: bool = False
    # Application limit, not a claim about the provider's maximum object size.
    max_single_put_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_CAPABILITIES:
            raise StorageConfigurationError("Unknown storage provider; add a reviewed adapter explicitly")
        normalized = _endpoint(self.endpoint_url)
        object.__setattr__(self, "endpoint_url", normalized)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket or ""):
            raise StorageConfigurationError("Bucket must be an explicit lowercase DNS-safe name")
        if not self.service or not self.profile or not self.region:
            raise StorageConfigurationError("Service, profile and region must be explicit")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) for x in (self.service, self.profile)):
            raise StorageConfigurationError("Invalid service or profile identifier")
        host = urlsplit(normalized).hostname
        if self.provider == "r2":
            if (self.service != "cloudflare-r2" or self.region != "auto"
                    or not re.fullmatch(r"[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com", host)):
                raise StorageConfigurationError("R2 requires its verified API host, cloudflare-r2 service and auto region")
        elif self.provider == "hippius":
            if self.service != "hippius-s3" or normalized != "https://s3.hippius.com" or self.region != "decentralized":
                raise StorageConfigurationError("Hippius requires hippius-s3, s3.hippius.com and decentralized region")
        else:
            suffix = "amazonaws.com.cn" if self.region.startswith("cn-") else "amazonaws.com"
            if (self.service != "aws-s3" or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.region)
                    or host != f"s3.{self.region}.{suffix}"):
                raise StorageConfigurationError("AWS S3 requires an explicit matching regional S3 endpoint")
        if type(self.enabled) is not bool:
            raise StorageConfigurationError("enabled must be a boolean")
        if type(self.max_single_put_bytes) is not int or not 1 <= self.max_single_put_bytes <= 100 * 1024 * 1024:
            raise StorageConfigurationError("This initial adapter supports at most 100 MiB per PUT")

    @property
    def capabilities(self) -> StorageCapabilities:
        return PROVIDER_CAPABILITIES[self.provider]


class S3Credentials:
    """Process-only credentials; repr/str and implicit serialization hide values."""
    __slots__ = ("_access", "_secret", "_token")

    def __init__(self, access_key_id: str, secret_access_key: str, session_token: str | None = None):
        if not all(isinstance(x, str) and x and not any(c.isspace() for c in x)
                   for x in (access_key_id, secret_access_key)):
            raise StorageConfigurationError("Storage credentials are missing or invalid")
        if session_token is not None and (not isinstance(session_token, str) or not session_token
                                          or any(c.isspace() for c in session_token)):
            raise StorageConfigurationError("Storage session credential is invalid")
        self._access, self._secret, self._token = access_key_id, secret_access_key, session_token

    def __repr__(self) -> str:
        return "S3Credentials(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("Storage credentials must not be serialized")


@dataclass(frozen=True)
class CredentialFields:
    access_key_id: str
    secret_access_key: str
    endpoint: str
    session_token: str | None = None

    def __post_init__(self):
        for field in (self.access_key_id, self.secret_access_key, self.endpoint, self.session_token):
            if field is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", field):
                raise StorageConfigurationError("Credential references must be environment field names")


R2_CREDENTIAL_FIELDS = CredentialFields("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT")


def load_runtime_storage_credentials(config: S3StorageConfig, filename: Path | str) -> S3Credentials:
    """Read one explicit protected Linux runtime identity; no central fallback.

    The host hydrates this file from its approved encrypted secret source. Its
    bucket and endpoint binding must match the API/worker's nonsecret settings.
    Credential values are never accepted through CLI/environment parameters.
    """
    failure = "Protected storage credentials are unavailable or invalid"
    if not config.enabled or config.provider != "r2":
        raise StorageConfigurationError(failure)
    try:
        path = Path(filename)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError
        # Match protected runtime readers: reject links/reparse points and
        # validate the opened inode instead of relying on a prior stat alone.
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                    or (os.name != "nt" and (info.st_uid != 0 or info.st_mode & 0o037))):
                raise ValueError
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        payload = json.loads(raw, object_pairs_hook=unique_pairs)
        required = {"schema_version", "service", "profile", "endpoint_url", "region", "bucket",
                    "access_key_id", "secret_access_key"}
        if (not isinstance(payload, dict) or not required <= set(payload)
                or set(payload) - required - {"session_token"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or any(payload[field] != getattr(config, field)
                       for field in ("service", "profile", "endpoint_url", "region", "bucket"))):
            raise ValueError
        if "session_token" in payload and payload["session_token"] is None:
            raise ValueError
        for field in ("access_key_id", "secret_access_key", "session_token"):
            if field in payload and (not isinstance(payload[field], str) or not 1 <= len(payload[field]) <= 8192
                    or any(ord(c) < 33 or ord(c) == 127 for c in payload[field])):
                raise ValueError
        return S3Credentials(payload["access_key_id"], payload["secret_access_key"], payload.get("session_token"))
    except Exception:
        # JSON/OS/secret errors can echo input or private paths; only a static
        # configuration error may cross this boundary.
        raise StorageConfigurationError(failure) from None


def load_configured_storage_credentials(config: S3StorageConfig, *, credentials_file: Path | str | None = None,
                                        registry_root: Path | str | None = None) -> S3Credentials:
    """Select the explicit runtime file or existing workstation central loader."""
    if credentials_file is not None:
        return load_runtime_storage_credentials(config, credentials_file)
    return load_storage_credentials(config, fields=R2_CREDENTIAL_FIELDS, registry_root=registry_root)


def load_storage_credentials(config: S3StorageConfig, *, fields: CredentialFields,
                             registry_root: Path | str | None = None,
                             loader: Callable | None = None) -> S3Credentials:
    """Reuse the central loader; never read .env, wallets, CLI profiles or print values.

    The caller supplies a trusted absolute registry path or an injected load_api
    callable. No global environment is changed. No provider request is made.
    """
    if not config.enabled:
        raise StorageConfigurationError("Cloud storage is disabled")
    if loader is None:
        if registry_root is None or not Path(registry_root).is_absolute():
            raise StorageConfigurationError("An explicit absolute central registry path is required")
        root = Path(registry_root).resolve(strict=True)
        expected = root / "api_registry.py"
        if not expected.is_file():
            raise StorageConfigurationError("Central API loader was not found")
        existing = sys.modules.get("api_registry")
        if existing is not None and Path(getattr(existing, "__file__", "")).resolve() != expected:
            raise StorageConfigurationError("A different central API loader is already imported")
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module("api_registry")
        finally:
            sys.path.remove(str(root))
        loader = module.load_api
    try:
        loaded = loader(config.service, profile=config.profile)
        if loaded.service != config.service or loaded.profile != config.profile:
            raise StorageConfigurationError("Central profile does not match the selected storage identity")
        env = loaded.env
        reference_endpoint = env.get(fields.endpoint)
        if not reference_endpoint or _endpoint(reference_endpoint) != config.endpoint_url:
            raise StorageConfigurationError("Central profile endpoint conflicts with storage configuration")
        if loaded.base_url and _endpoint(loaded.base_url) != config.endpoint_url:
            raise StorageConfigurationError("Central base URL conflicts with storage configuration")
        if fields.session_token and not env.get(fields.session_token):
            raise StorageConfigurationError("The selected profile is missing its required session credential")
        credentials = S3Credentials(env.get(fields.access_key_id), env.get(fields.secret_access_key),
                                    env.get(fields.session_token) if fields.session_token else None)
        if config.provider == "hippius" and not credentials._access.startswith("hip_"):
            raise StorageConfigurationError("Hippius requires an S3 access key, not wallet credentials")
        return credentials
    except StorageConfigurationError:
        raise
    except Exception:
        # Errors from a secret loader may contain configuration values.
        raise StorageConfigurationError("Central storage credentials could not be loaded") from None

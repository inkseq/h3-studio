"""Explicit Linux runtime source for the already selected central Lium profile.

Secrets Manager is the authorized encrypted runtime backend, not a copy of the
Windows registry loader. Inject AwsLiumLoader into LiumProvider(loader=...). No
imports, construction, files or environment changes load a credential. Only a
matching explicit load reads one pinned managed secret version into memory.
Never enable botocore HTTP/debug response logging in this process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import threading

from .lium_identity import BASE_URL, KEY_VARIABLE, PROFILE, SERVICE


REGION = "ap-southeast-1"
SECRET_NAME = "/sixnine/platform/lium"
SECRET_ARN = re.compile(
    r"arn:aws:secretsmanager:ap-southeast-1:[0-9]{12}:secret:/sixnine/platform/lium-[A-Za-z0-9]{6}"
)
VERSION_ID = re.compile(r"[A-Za-z0-9-]{32,64}")


class RuntimeCredentialError(ValueError):
    """Only static error codes leave the credential boundary."""


@dataclass(frozen=True)
class LiumRuntimeConfig:
    service: str = SERVICE
    profile: str = PROFILE
    base_url: str = BASE_URL
    primary_key_variable: str = KEY_VARIABLE
    api_key: str = field(default="", repr=False)


def _client():
    # The operator supplies the EC2 role credential chain, never static AWS
    # secrets in this module, an image, or a GPU host. Explicit region/endpoint
    # prevent environment endpoint overrides from changing the destination.
    import boto3
    from botocore.config import Config

    return boto3.client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url="https://secretsmanager.ap-southeast-1.amazonaws.com",
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )


class AwsLiumLoader:
    """One exact ARN/version, reused per process; rotation needs a new instance.

    Use a trusted operator configuration for secret_arn and version_id. The
    explicit VersionId avoids switching a running controller to another account
    after a mutable AWSCURRENT change. This class performs no writes to AWS.
    """

    service, profile, base_url, key_variable = SERVICE, PROFILE, BASE_URL, KEY_VARIABLE
    secret_name, secret_arn_pattern, config_type = (
        SECRET_NAME,
        SECRET_ARN,
        LiumRuntimeConfig,
    )

    def __init__(self, secret_arn, version_id, *, client_factory=None):
        if (
            not isinstance(secret_arn, str)
            or not self.secret_arn_pattern.fullmatch(secret_arn)
            or not isinstance(version_id, str)
            or not VERSION_ID.fullmatch(version_id)
        ):
            raise RuntimeCredentialError("lium_runtime_secret_identity_invalid")
        self._arn, self._version = secret_arn, version_id
        self._factory = client_factory or _client
        self._client = self._config = None
        self._lock = threading.Lock()

    def __call__(self, service, *, profile):
        if service != self.service or profile != self.profile:
            raise RuntimeCredentialError("lium_runtime_service_profile_mismatch")
        with self._lock:
            if self._config is not None:
                return self._config
            try:
                if self._client is None:
                    self._client = self._factory()
                response = self._client.get_secret_value(
                    SecretId=self._arn, VersionId=self._version
                )
                if (
                    not isinstance(response, dict)
                    or response.get("ARN") != self._arn
                    or response.get("Name") != self.secret_name
                    or response.get("VersionId") != self._version
                    or "SecretBinary" in response
                ):
                    raise RuntimeCredentialError(
                        "lium_runtime_secret_response_identity_mismatch"
                    )
                raw = response.get("SecretString")
                if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
                    raise RuntimeCredentialError("lium_runtime_secret_payload_invalid")

                # Reject duplicate JSON keys instead of quietly choosing one.
                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise RuntimeCredentialError(
                                "lium_runtime_secret_duplicate_field"
                            )
                        value[key] = item
                    return value

                value = json.loads(raw, object_pairs_hook=unique)
                expected = {
                    "schema_version",
                    "service",
                    "profile",
                    "base_url",
                    "primary_key_variable",
                    "api_key",
                }
                if (
                    not isinstance(value, dict)
                    or set(value) != expected
                    or type(value["schema_version"]) is not int
                    or value["schema_version"] != 1
                    or value["service"] != self.service
                    or value["profile"] != self.profile
                    or value["base_url"] != self.base_url
                    or value["primary_key_variable"] != self.key_variable
                ):
                    raise RuntimeCredentialError(
                        "lium_runtime_profile_metadata_mismatch"
                    )
                token = value["api_key"]
                if (
                    not isinstance(token, str)
                    or not 1 <= len(token) <= 8192
                    or token != token.strip()
                    or any(c in token for c in "\r\n\x00")
                ):
                    raise RuntimeCredentialError("lium_runtime_key_invalid")
                self._config = self.config_type(api_key=token)
                return self._config
            except RuntimeCredentialError:
                raise
            except Exception:
                # SDK/JSON errors may contain response data. No fallback to
                # DPAPI, environment API keys, another secret/profile or URL.
                raise RuntimeCredentialError(
                    "lium_runtime_secret_unavailable_or_invalid"
                ) from None

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = self._config = None

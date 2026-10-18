"""Pinned Linux identity for the existing central Targon account; no fallback."""

from dataclasses import dataclass, field
import json
import re

from .lium_runtime_aws import AwsLiumLoader, RuntimeCredentialError

SERVICE = 'targon'
PROFILE = 'targon--rig-root'
BASE_URL = 'https://api.targon.com'
KEY_VARIABLE = 'TARGON_API_KEY'
SECRET_NAME = '/sixnine/platform/targon'
SECRET_ARN = re.compile(
    r'arn:aws:secretsmanager:ap-southeast-1:[0-9]{12}:secret:/sixnine/platform/targon-[A-Za-z0-9]{6}'
)


@dataclass(frozen=True)
class TargonRuntimeConfig:
    service: str = SERVICE
    profile: str = PROFILE
    base_url: str = BASE_URL
    primary_key_variable: str = KEY_VARIABLE
    api_key: str = field(default='', repr=False)


class AwsTargonLoader(AwsLiumLoader):
    service, profile, base_url, key_variable = SERVICE, PROFILE, BASE_URL, KEY_VARIABLE
    secret_name, secret_arn_pattern, config_type = (
        SECRET_NAME,
        SECRET_ARN,
        TargonRuntimeConfig,
    )


def stdin_targon_loader(secret_arn, version_id, stream):
    try:
        raw = stream.read(24577)
        if len(raw) > 24576:
            raise ValueError

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        envelope = json.loads(raw, object_pairs_hook=unique)
        if (
            set(envelope) != {'secret_arn', 'version_id', 'payload'}
            or envelope['secret_arn'] != secret_arn
            or envelope['version_id'] != version_id
        ):
            raise ValueError

        class MemorySecret:
            def get_secret_value(self, **kwargs):
                return {
                    'ARN': secret_arn,
                    'Name': SECRET_NAME,
                    'VersionId': version_id,
                    'SecretString': json.dumps(envelope['payload']),
                }

            def close(self):
                envelope.clear()

        loader = AwsTargonLoader(secret_arn, version_id, client_factory=MemorySecret)
        loader(SERVICE, profile=PROFILE)
        return loader
    except Exception:
        raise RuntimeCredentialError('targon_runtime_envelope_invalid') from None

"""Small immutable artifact tuple; manifests bind images, archives and native sources."""
from __future__ import annotations

import hashlib
import json
import re

SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate(value):
    if (type(value) is not dict or set(value) != {'version', 'commit', 'kind', 'run_id', 'run_attempt', 'manifests'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['commit'], str) or not SHA.fullmatch(value['commit'])
            or value['kind'] not in {'platform', 'frontend'}
            or any(type(value[key]) is not int or value[key] <= 0 for key in ('run_id', 'run_attempt'))):
        raise ValueError('release_candidate_invalid')
    names = {'platform', 'migration', 'frontend'} if value['kind'] == 'platform' else {'frontend'}
    if (type(value['manifests']) is not dict or set(value['manifests']) != names
            or any(not isinstance(item, str) or not DIGEST.fullmatch(item) for item in value['manifests'].values())):
        raise ValueError('release_candidate_manifest_identity_invalid')
    return value


def acceptance(value, candidate):
    validate(candidate)
    if (type(value) is not dict or set(value) != {'version', 'state', 'commit', 'candidate_sha256',
            'staging_instance_id', 'checks', 'evidence'}
            or type(value['version']) is not int or value['version'] != 1
            or value['state'] != 'accepted' or value['commit'] != candidate['commit']
            or value['candidate_sha256'] != digest(encode(candidate))
            or not isinstance(value['staging_instance_id'], str)
            or not re.fullmatch(r'i-[0-9a-f]{17}', value['staging_instance_id'])
            or value['checks'] != {name: 'passed' for name in ('browser_agent', 'isolation', 'migration_rehearsal')}
            or not isinstance(value['evidence'], str) or not re.fullmatch(r'https://github\.com/inkseq/h3-studio/issues/[0-9]+(?:#issuecomment-[0-9]+)?', value['evidence'])):
        raise ValueError('staging_acceptance_required')
    return value

"""Closed host identities shared by the existing release and secret helpers.

The root-installed helper and protected host record select an environment;
neither an incoming bundle nor a web request can select another origin/secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

ROOT = Path('/srv/sixnine')
PROFILES = {
    'production': {
        'origin': 'https://inkseq.com',
        'aliases': 'https://www.sixnine.art,https://sixnine.art',
        'caddy': 'Caddyfile', 'secret_root': '/sixnine/platform',
        'r2_profile': 'cloudflare-r2--rig-root', 'r2_bucket': 'sixnine-media',
        'vast_profile': 'vastai--sixnine-dstack-20261011',
        'dstack_project': 'sixnine',
    },
    'staging': {
        'origin': 'https://staging.inkseq.com', 'aliases': '',
        'caddy': 'Caddyfile.staging', 'secret_root': '/sixnine/staging',
        'r2_profile': 'cloudflare-r2--sixnine-staging-20261015',
        'r2_bucket': 'sixnine-staging-media',
        'vast_profile': 'vastai--sixnine-staging-20261015',
        'dstack_project': 'sixnine-staging',
    },
}


def profile(name='production'):
    if type(name) is not str or name not in PROFILES:
        raise ValueError('deployment_target_invalid')
    return dict(PROFILES[name], name=name)


def environment(values):
    return profile(values.get('INKSEQ_DEPLOYMENT_TARGET', 'production'))


def host(root=ROOT, *, legacy=False):
    """Read only a protected record. Legacy absence is explicit, prod-only."""
    path = Path(root) / 'deployment-target.json'
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or path.parent.is_symlink()
            or os.name != 'nt' and (parent.st_uid != 0 or parent.st_mode & 0o022)):
        raise ValueError('deployment_target_parent_not_protected')
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
            raise ValueError('deployment_target_record_not_protected')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        if legacy:
            return profile()
        raise ValueError('deployment_target_record_required') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2048
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o077)):
            raise ValueError('deployment_target_record_not_protected')
        raw = os.read(fd, 2049)
        if len(raw) != info.st_size:
            raise ValueError('deployment_target_record_changed')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('deployment_target_duplicate_field')
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
    finally:
        os.close(fd)
    if (type(value) is not dict or set(value) != {'version', 'target', 'instance_id'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['instance_id'], str)
            or not re.fullmatch(r'i-[0-9a-f]{17}', value['instance_id'])):
        raise ValueError('deployment_target_record_invalid')
    return dict(profile(value['target']), instance_id=value['instance_id'])


def site_environment(root, values):
    selected = environment(values)
    sealed = host(root, legacy='INKSEQ_DEPLOYMENT_TARGET' not in values)
    if selected['name'] != sealed['name']:
        raise ValueError('deployment_target_site_mismatch')
    return {
        'INKSEQ_DEPLOYMENT_TARGET': selected['name'],
        'INKSEQ_PUBLIC_ORIGIN': selected['origin'],
        'INKSEQ_PUBLIC_ORIGIN_ALIASES': selected['aliases'],
        'INKSEQ_CADDY_PROFILE': selected['caddy'],
    }

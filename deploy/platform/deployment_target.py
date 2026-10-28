"""Closed host identities shared by the existing release and secret helpers.

The root-installed helper and protected host record select an environment;
neither an incoming bundle nor a web request can select another origin/secret.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

ROOT = Path('/srv/sixnine')
STANDARD_RESOURCES = 'standard-4g'
COMPACT_RESOURCES = 'staging-2g-swap2g'
COMPACT_INSTANCE = 'i-07805df7bd6984bd9'
COMPACT_PARENT = 'inkseq-staging-workload.slice'
COMPACT_CGROUP = Path('/sys/fs/cgroup/inkseq.slice/inkseq-staging.slice') / COMPACT_PARENT
COMPACT_SWAP = ROOT / 'staging.swap'
MIB = 1024**2
COMPACT_SERVICE_MIB = {'db': 384, 'db-init': 128, 'caddy': 96, 'app': 512,
    'dstack': 768, 'hatchet-db': 256, 'hatchet': 384, 'dispatch': 128, 'controller': 640}
COMPACT_UNITS = {
    '/etc/systemd/system/inkseq-staging-workload.slice': '7d5b6b0b2a7f5114ddc79cce167b0a429d93733a2bd4b2deda9274fd01357bdb',
    '/etc/systemd/system/srv-sixnine-staging.swap.swap': '0086edba93a00323a6793a4b69cf1095dad72cb7f2fba8b6bf41ee9644fb3235',
    '/etc/systemd/system/docker.service.d/30-inkseq-staging-resources.conf': '59041ba093db73b0e69d918a507ae75cf650cb45773831cd9094392483605d42',
    '/etc/systemd/system/inkseq-staging-secrets.service': 'de44b8c3918b50be2390062f26b49b9e557535388ca4b6deaaba646d45ab4f2e',
}
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
        # CR-20261016-18 explicitly shares the existing provider account/key.
        # Stage project/DB/SSH and budgets remain separate; this is no longer
        # a provider-account credential isolation boundary.
        'vast_profile': 'vastai--sixnine-dstack-20261011',
        'dstack_project': 'sixnine-staging',
    },
}


def profile(name='production'):
    if type(name) is not str or name not in PROFILES:
        raise ValueError('deployment_target_invalid')
    return dict(PROFILES[name], name=name)


def environment(values):
    return profile(values.get('INKSEQ_DEPLOYMENT_TARGET', 'production'))


def resources(values):
    """Closed resource choice; swap is separate from the physical RAM floor."""
    selected = environment(values)
    name = values.get('INKSEQ_RESOURCE_PROFILE', STANDARD_RESOURCES)
    parent = values.get('INKSEQ_CPU_CGROUP_PARENT', '')
    if name == STANDARD_RESOURCES and parent == '':
        return {'name': name, 'minimum_host_mib': 3584, 'resident_mib': None,
                'swap_mib': 0, 'cgroup_parent': ''}
    if (name == COMPACT_RESOURCES and selected['name'] == 'staging'
            and parent == COMPACT_PARENT):
        return {'name': name, 'minimum_host_mib': 1792, 'resident_mib': 1280,
                'swap_mib': 2048, 'cgroup_parent': parent}
    raise ValueError('deployment_resource_profile_invalid')


def resource_environment(sealed):
    name = sealed.get('resource_profile', STANDARD_RESOURCES)
    if name == STANDARD_RESOURCES:
        return {}
    if (name != COMPACT_RESOURCES or sealed['name'] != 'staging'
            or sealed.get('instance_id') != COMPACT_INSTANCE):
        raise ValueError('deployment_resource_host_invalid')
    return {'INKSEQ_RESOURCE_PROFILE': name, 'INKSEQ_CPU_CGROUP_PARENT': COMPACT_PARENT}


def validate_resource_metadata(values, metadata, *, memory_bytes):
    """Pure compact-host checks, shared by preflight and migration acceptance."""
    selected = resources(values)
    if selected['name'] == STANDARD_RESOURCES:
        return selected
    def need(ok, code):
        if not ok:
            raise ValueError(code)
    need(type(memory_bytes) is int and memory_bytes >= selected['minimum_host_mib']*MIB,
         'staging_compact_physical_memory_below_floor')
    need(type(metadata) is dict and metadata.get('instance_id') == COMPACT_INSTANCE,
         'staging_compact_wrong_host')
    need(metadata.get('cgroup_driver') == 'systemd' and metadata.get('cgroup_version') == '2'
         and metadata.get('memory_max') == 1280*MIB and metadata.get('swap_max') == 2048*MIB,
         'staging_compact_parent_limits_unconfirmed')
    swap = metadata.get('swap_file', {})
    need(swap.get('regular') is True and swap.get('uid') == 0 and swap.get('gid') == 0
         and swap.get('mode') == 0o600 and swap.get('links') == 1
         and swap.get('bytes') == 2048*MIB and swap.get('allocated_bytes', 0) >= 2048*MIB,
         'staging_compact_swap_file_not_protected')
    # mkswap reserves one 4-KiB page on this fixed x86-64 host.
    need(metadata.get('swaps') == [{'path': str(COMPACT_SWAP), 'kind': 'file',
         'bytes': 2048*MIB-4096, 'priority': 10}], 'staging_compact_swap_not_active')
    need(type(metadata.get('disk_free_bytes')) is int and metadata['disk_free_bytes'] >= 3*1024**3,
         'staging_compact_release_disk_headroom')
    need(type(metadata.get('swappiness')) is int and 1 <= metadata['swappiness'] <= 100,
         'staging_compact_swap_disabled')
    need(metadata.get('unit_hashes') == COMPACT_UNITS, 'staging_compact_boot_units_changed')
    required = {COMPACT_PARENT, 'srv-sixnine-staging.swap.swap', 'inkseq-staging-secrets.service'}
    need(required <= set(metadata.get('docker_requires', []))
         and required <= set(metadata.get('docker_after', [])), 'staging_compact_boot_order_invalid')
    return selected


def resource_metadata():
    """Read fixed nonsecret Linux paths; never create swap, units or cgroups."""
    def protected(path, *, read=False):
        path = Path(path)
        for parent in path.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('staging_compact_parent_not_protected')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('staging_compact_file_not_protected')
            if read:
                if info.st_size > 16384:
                    raise ValueError('staging_compact_unit_too_large')
                raw = os.read(fd, 16385)
                if len(raw) != info.st_size:
                    raise ValueError('staging_compact_unit_changed')
                return hashlib.sha256(raw).hexdigest()
            return {'regular': True, 'uid': info.st_uid, 'gid': info.st_gid,
                    'mode': stat.S_IMODE(info.st_mode), 'links': info.st_nlink,
                    'bytes': info.st_size, 'allocated_bytes': info.st_blocks*512}
        finally:
            os.close(fd)
    def command(args):
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=15, check=True, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                'LANG': 'C.UTF-8', 'DOCKER_CONFIG': '/opt/sixnine-release/docker-config'}).stdout.strip()
    docker = command(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info',
                      '--format', '{{.CgroupDriver}} {{.CgroupVersion}} {{.MemTotal}}']).split()
    if len(docker) != 3:
        raise ValueError('staging_compact_cgroup_metadata_invalid')
    props = command(['/usr/bin/systemctl', 'show', 'docker.service', '--property=Requires,After'])
    props = dict(line.split('=', 1) for line in props.splitlines())
    swaps = []
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        path, kind, size, _used, priority = line.split()
        swaps.append({'path': path, 'kind': kind, 'bytes': int(size)*1024, 'priority': int(priority)})
    return {'instance_id': Path('/var/lib/cloud/data/instance-id').read_text().strip(),
        'cgroup_driver': docker[0], 'cgroup_version': docker[1], 'memory_bytes': int(docker[2]),
        'memory_max': int((COMPACT_CGROUP/'memory.max').read_text()),
        'swap_max': int((COMPACT_CGROUP/'memory.swap.max').read_text()),
        'swap_file': protected(COMPACT_SWAP), 'swaps': swaps,
        'disk_free_bytes': shutil.disk_usage(ROOT).free,
        'swappiness': int(Path('/proc/sys/vm/swappiness').read_text()),
        'unit_hashes': {name: protected(name, read=True) for name in COMPACT_UNITS},
        'docker_requires': props.get('Requires', '').split(), 'docker_after': props.get('After', '').split()}


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
    if (type(value) is not dict or set(value) not in ({'version', 'target', 'instance_id'},
            {'version', 'target', 'instance_id', 'resource_profile'})
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['instance_id'], str)
            or not re.fullmatch(r'i-[0-9a-f]{17}', value['instance_id'])):
        raise ValueError('deployment_target_record_invalid')
    selected = dict(profile(value['target']), instance_id=value['instance_id'])
    if 'resource_profile' in value:
        selected['resource_profile'] = value['resource_profile']
        resource_environment(selected)
    return selected


def inky_environment(values):
    """Existing staging credentials only; this grants no production authority."""
    selected = values.get('INKSEQ_INKY_CONFIG_FILE', '')
    if selected not in {'', '/run/sixnine-secrets/inky'} or (
        selected and values.get('INKSEQ_DEPLOYMENT_TARGET') != 'staging'
    ):
        raise ValueError('migration_inky_configuration_invalid')


def inky_mounts(document, *, target):
    """Check references only, never read keys or execute an incoming helper."""
    profile(target)
    services = document['services']
    secrets = document.get('secrets', {})
    app = services['app']
    selected = app.get('environment', {}).get('INKSEQ_INKY_CONFIG_FILE', '')
    if selected not in {'', '/run/secrets/inky'} or selected and target != 'staging':
        raise ValueError('migration_inky_configuration_invalid')
    mounted = False
    for name, service in services.items():
        if name != 'app' and 'INKSEQ_INKY_CONFIG_FILE' in service.get('environment', {}):
            raise ValueError('migration_inky_outside_app')
        for mount in service.get('secrets', []):
            source = mount.get('source') if isinstance(mount, dict) else mount
            destination = mount.get('target', source) if isinstance(mount, dict) else source
            if source == 'inky':
                if name != 'app' or destination not in {'inky', '/run/secrets/inky'} or mounted:
                    raise ValueError('migration_inky_mount_invalid')
                mounted = True
            elif destination in {'inky', '/run/secrets/inky'}:
                raise ValueError('migration_inky_mount_alias')
    configured = secrets.get('inky')
    if configured is None:
        if selected or mounted:
            raise ValueError('migration_inky_configuration_invalid')
        return  # Retained rollback releases need no new secret reference.
    expected = '/run/sixnine-secrets/inky' if selected else secrets['execution_profiles']['file']
    if (set(configured) - {'file', 'name'} or configured.get('file') != expected
            or configured.get('name', 'sixnine-platform_inky') != 'sixnine-platform_inky'
            or not mounted):
        raise ValueError('migration_inky_configuration_invalid')


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
        **resource_environment(sealed),
    }

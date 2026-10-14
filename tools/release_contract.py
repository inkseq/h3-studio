"""Conservative source fingerprints for independently released UI/API/workers.

These are compatibility gates, not evidence of testing or model qualification.
Unknown runtime modules participate automatically. Changes to this policy also
change both fingerprints, so narrowing it requires one drained deployment.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
FIXED = (
    'platform_app.py', 'comfy_workflow.py', 'comfy-object-info.json',
    'requirements.lock.txt', 'Dockerfile.platform', 'tools/release_contract.py',
    'deploy/platform/compose.yaml', 'deploy/platform/check_config.py',
    'deploy/platform/Caddyfile', 'deploy/platform/init_database.py',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1.json',
    'deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1.json',
    'deploy/wangp/profiles/h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2.json',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1.json',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2.json',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3.json',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4.json',
    'deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2.json',
    'deploy/wangp/profiles/h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5.json',
    'deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3.json',
)
AGENT_FILES = ('skills/sixnine-yingxu/SKILL.md', 'skills/sixnine-yingxu/scripts/sixnine.py')
# These modules serve static files or describe Agent discovery; neither runs
# worker code or owns database/job schemas. api.py remains deliberately included.
WORKER_EXCLUSIONS = frozenset(('studio_platform/frontend.py', 'studio_platform/agent_discovery.py', *AGENT_FILES))


def source_paths(root=ROOT):
    root = Path(root)
    module_dir = root / 'studio_platform'
    if module_dir.is_symlink() or not module_dir.is_dir():
        raise ValueError('Invalid runtime source directory')
    paths = set(FIXED) | set(AGENT_FILES)
    for path in module_dir.rglob('*.py'):
        if '__pycache__' not in path.parts:
            paths.add(path.relative_to(root).as_posix())
    return sorted(paths)


def digest_sources(root, paths):
    root = Path(root).resolve()
    digest = hashlib.sha256(b'sixnine-release-contract-v1\n')
    for name in sorted(paths):
        path = root / name
        for part in (path, *path.parents):
            if part == root:
                break
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked runtime source is forbidden')
        if not path.is_file() or path.stat().st_size > 16 * 1024**2:
            raise ValueError('Runtime source is missing or too large')
        raw = path.read_bytes().replace(b'\r\n', b'\n')
        digest.update(name.encode('utf-8') + b'\0' + str(len(raw)).encode('ascii') + b'\0' + raw + b'\0')
    return digest.hexdigest()


def build_contracts(root=ROOT):
    names = source_paths(root)
    return {'version': 1, 'api_compatibility': digest_sources(root, names),
            'worker_compatibility': digest_sources(root, set(names) - WORKER_EXCLUSIONS),
            'frontend_contract': 'sixnine-web-v1'}

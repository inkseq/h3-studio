#!/usr/bin/python3
"""One explicitly authorized public GPU job, resumable without duplicate submits.

Operator-only AWS host procedure. Password and temporary PAT remain in memory.
Default does nothing. Existing output/receipts stay on the host for review.

Authorized operator, from the deployed release directory:
  sudo python3 deploy/platform/verify_public_generation.py submit-authorized
  sudo python3 deploy/platform/verify_public_generation.py status
  sudo python3 deploy/platform/verify_public_generation.py collect
An interrupted submission is recovered by its original plan; an unknown outcome
is never automatically resubmitted using a replacement PAT. Keep receipt.json.
Browser verification here means a browser-session API read, not a rendered UI.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import verify_public_api as public

ROOT = Path('/srv/sixnine/gpu-acceptance/public-verification')
PROJECT = 'production-h3-acceptance-20261004'
KEY_NAME = 'Temporary production GPU acceptance 20261004'
PROMPT = 'A golden retriever walks through a sunlit garden, flowers sway gently, birds chirp, cinematic slow camera movement.'
RECIPE = 'h3-base-fl2va-v1'
# Mirrors studio_platform.quality_views.PUBLIC_CONTROLS: what a creator view echoes back.
PUBLIC_CONTROLS = frozenset({'duration', 'seed', 'resolution', 'aspect_ratio', 'generate_audio', 'width', 'height'})
SUBMISSION_KEY = 'public-gpu-job-20261004'


def check_file(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not path.is_symlink()


def save(root, state):
    target = root/'receipt.json'
    temporary = root/'receipt.pending.json'
    check_file(target)
    check_file(temporary)
    with temporary.open('w', encoding='utf-8') as out:
        json.dump(state, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(target)
    if os.name == 'posix':
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@contextmanager
def authenticated(*, prepare=False):
    accounts = public.runtime_secrets_aws.value(public.runtime_secrets_aws.client(), public.runtime_secrets_aws.ACCOUNTS)
    browser, agent = public.client(), public.client()
    key_id = None
    logged_in = False
    key_name = KEY_NAME+' '+uuid.uuid4().hex[:12]
    headers = {}
    try:
        public.request(browser, 'POST', '/api/auth/login', {'username': 'superdan', 'password': accounts['superdan']})
        logged_in = True
        # Story creation is browser-authorized; the temporary agent may access
        # only this one acceptance project, including during result recovery.
        if prepare:
            prepare_story(browser, {})
        value = public.request(browser, 'POST', '/v1/api-keys', {
            'name': key_name, 'scopes': ['projects:read', 'projects:write', 'jobs:read', 'jobs:write'],
            'all_projects': False, 'project_ids': [PROJECT], 'expires_in_days': 1}, expected=201)
        key_id = value['key']['id']
        headers = {'Authorization': 'Bearer '+value['api_key']}
        del value
        yield browser, agent, headers
    finally:
        try:
            if key_id:
                public.request(browser, 'DELETE', '/v1/api-keys/'+key_id)
            elif logged_in:
                # Even a lost key-creation response must not leave that key
                # knowingly active. Its unguessable run name is not a secret.
                for key in public.request(browser, 'GET', '/v1/api-keys')['api_keys']:
                    if key['name'] == key_name and key['revoked_at'] is None:
                        public.request(browser, 'DELETE', '/v1/api-keys/'+key['id'])
        finally:
            try:
                if logged_in:
                    public.request(browser, 'POST', '/api/auth/logout')
            finally:
                accounts.clear()
                headers.clear()


def prepare_story(agent, headers):
    projects = public.request(agent, 'GET', '/v1/projects', headers=headers)['projects']
    if not any(p['id'] == PROJECT for p in projects):
        public.request(agent, 'POST', '/v1/projects', {
            'id': PROJECT, 'title': '公网 H3 实测 · 花园金毛',
            'logline': '真实公网 API → GPU → 视频和独立音轨 → 镜头采用。原版 BF16、50 步、768P，验收后保留。'},
            {**headers, 'Idempotency-Key': 'public-gpu-story-20261004'}, expected=201)
    project = public.request(agent, 'GET', '/v1/projects/'+PROJECT, headers=headers)
    entities = project['project']['entities']
    if not entities:
        actions = [
            {'op': 'entity.create', 'entity': {'id': 'chapter', 'type': 'chapter', 'title': '第一章：花园清晨'}},
            {'op': 'entity.create', 'entity': {'id': 'scene', 'type': 'scene', 'parentId': 'chapter', 'title': '阳光下的花园'}},
            {'op': 'entity.create', 'entity': {'id': 'shot', 'type': 'shot', 'parentId': 'scene', 'title': '金毛穿过花园',
                'data': {'seconds': 5, 'prompt': PROMPT}}}]
        project = public.request(agent, 'POST', '/v1/projects/'+PROJECT+'/actions',
            {'expected_version': project['version'], 'actions': actions},
            {**headers, 'Idempotency-Key': 'public-gpu-structure-20261004'})
    shot = next(e for e in project['project']['entities'] if e['id'] == 'shot')
    assert shot['type'] == 'shot' and shot['parentId'] == 'scene' and shot['data']['prompt'] == PROMPT
    assert shot['data']['seconds'] == 5
    return project, shot


def check_state(state):
    if not state:
        return
    assert state.get('project_id') == PROJECT and state.get('submission_key') == SUBMISSION_KEY
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,160}', state.get('plan_id', ''))
    body = state['request']
    assert body['recipe_id'] == RECIPE and body['prompt'] == PROMPT
    assert body['client_ref']['project_id'] == PROJECT and body['client_ref']['shot_id'] == 'shot'
    assert body['controls'] == {'duration': 5, 'resolution': '768P', 'aspect_ratio': '16:9', 'steps': 50,
        'seed': '42004', 'generate_audio': True, 'video_decode': 'tiled', 'encoder_device': 'cpu'}
    if state.get('job_id'):
        assert re.fullmatch(r'[A-Za-z0-9_-]{1,160}', state['job_id'])


def check_job(job, state):
    assert not job['simulation'] and job['project_id'] == PROJECT and job['plan_id'] == state['plan_id']
    assert job['recipe_id'] == 'video-fl-v1' and job['client_ref']['shot_id'] == 'shot'
    check_effective(job['effective_request'], state['request'])
    if state.get('job_id'):
        assert job['id'] == state['job_id']


def check_effective(effective, requested):
    # Agent readback projects the retained BF16 route to public quality. The
    # original request recipe, saved body and submission key remain unchanged.
    assert effective['tier'] == 'refine' and effective['mode'] == 'fl'
    assert 'model' not in effective and 'backend' not in effective
    assert effective['prompt'] == PROMPT
    # Creators see only what they author; steps and other tuning stay internal (#898).
    assert all(effective[key] == value for key, value in requested['controls'].items()
               if key in PUBLIC_CONTROLS)
    assert 'steps' not in effective


def recover_submission(root, state, agent, headers):
    # Job idempotency includes PAT actor identity. A replacement PAT must NEVER
    # blindly replay the paid POST, even with the original plan and key.
    matches = []
    exhausted = False
    for offset in range(0, 1000, 100):
        rows = public.request(agent, 'GET', '/v1/jobs?client_project_id='+PROJECT+
            '&limit=100&offset='+str(offset), headers=headers)['jobs']
        matches.extend(job for job in rows if job['plan_id'] == state['plan_id'])
        if len(rows) < 100:
            exhausted = True
            break
    if not exhausted or len(matches) != 1:
        state.update(status='submission_unknown', recovery='no_unique_matching_job_no_resubmit')
        save(root, state)
        return state
    job = public.request(agent, 'GET', '/v1/jobs/'+matches[0]['id'], headers=headers)
    check_job(job, state)
    state.update(job_id=job['id'], status=job['status'], recovery='recovered_by_original_plan')
    save(root, state)
    return state


def submit(root, state, agent, headers):
    check_state(state)
    if state.get('job_id'):
        check_job(public.request(agent, 'GET', '/v1/jobs/'+state['job_id'], headers=headers), state)
        return state
    if state.get('submission_started'):
        return recover_submission(root, state, agent, headers)
    if not state.get('plan_id'):
        health = public.request(agent, 'GET', '/healthz')
        assert health['auth_ready'] and health['generation_enabled'] and health['execution_backend'] in ('enabled', 'comfy-worker')
        project, shot = prepare_story(agent, headers)
        body = {'client_ref': {'project_id': PROJECT, 'shot_id': 'shot', 'shot_version': shot['version']},
            'recipe_id': RECIPE, 'prompt': PROMPT,
            'controls': {'duration': 5, 'resolution': '768P', 'aspect_ratio': '16:9', 'steps': 50,
                'seed': '42004', 'generate_audio': True, 'video_decode': 'tiled', 'encoder_device': 'cpu'}}
        plan = public.request(agent, 'POST', '/v1/generation-plans', body, headers, expected=201)
        if plan['status'] != 'ready' or plan['simulation'] or plan['blockers']:
            return {'state': 'blocked', 'blockers': plan['blockers'], 'generation_submitted': False}
        check_effective(plan['effective_request'], body)
        state.update(plan_id=plan['plan_id'], project_id=PROJECT, request=body,
            submission_key=SUBMISSION_KEY, started_at=datetime.now(timezone.utc).isoformat())
        save(root, state)
    state['submission_started'] = True
    save(root, state)
    for attempt in range(2):
        try:
            job = public.request(agent, 'POST', '/v1/jobs', {'plan_id': state['plan_id']},
                {**headers, 'Idempotency-Key': state['submission_key']}, expected=202)
            break
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            if attempt:
                raise
    check_job(job, state)
    state.update(job_id=job['id'], status=job['status'])
    save(root, state)
    return state


def decode_arguments(path, kind):
    assert kind in ('video', 'audio')
    return ['-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode', '-threads', '2',
        '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0' if kind == 'video' else '0:a:0',
        *(['-map', '0:a?'] if kind == 'video' else []), '-threads', '2', '-f', 'null', '-']


def local_decode(path, kind):
    """Injectable offline-test decoder; production CLI always uses Docker."""
    result = subprocess.run(['ffmpeg', *decode_arguments(path, kind)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    assert result.returncode == 0


def trusted_release():
    directory = Path('/opt/sixnine-release')
    for parent in (directory.parent, directory):
        info = parent.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    for name in ('check_config', 'release'):
        path = directory/(name+'.py')
        check_file(path)
        info = path.stat()
        assert info.st_uid == 0 and not info.st_mode & 0o022
        if name in sys.modules:
            assert Path(sys.modules[name].__file__) == path
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module('release')
    finally:
        sys.path.pop(0)


def docker_decode(path, kind):
    """Only the independently approved installed image may parse this media.

    Never pass runtime environment, application data, secrets, socket, or network
    into the disposable decoder. The single media bind is read-only.
    """
    release = trusted_release()
    assert release.ROOT == Path('/srv/sixnine') and release.DOCKER == '/usr/bin/docker'
    check_file(path)
    scaling_parent = Path('/srv/sixnine/gpu-scaler/public-verification')
    allowed_parent = path.parent == ROOT or (path.parent.parent == scaling_parent
        and re.fullmatch(r'shot-[1-6]', path.parent.name))
    assert allowed_parent and path.name in ('generated.mp4', 'generated.flac')
    release.regular(Path(release.DOCKER), root_owned=True)
    state_path = release.ROOT/'release-state.json'
    release.regular(state_path, root_owned=True, maximum=16384)
    state = json.loads(state_path.read_text(encoding='utf-8'))
    commit = state.get('current')
    assert isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit)
    assert state.get('status') == 'app_ready'
    directory = release.ROOT/'releases'/commit
    release.approved_manifest(release.ROOT, directory, commit)
    manifest_path = directory/'release-manifest.json'
    release.regular(manifest_path, root_owned=True, maximum=16384)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['commit'] == commit and manifest['image'] == 'sixnine-platform:'+commit
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', manifest['image_id'])
    archive = directory/'image.tar.gz'
    release.regular(archive, root_owned=True, maximum=2*1024**3)
    identities = release.validate_image_archive(archive, manifest)
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
        'DOCKER_CONFIG': '/opt/sixnine-release/docker-config'}
    image_id = release.command(['image', 'inspect', manifest['image'], '--format', '{{.Id}}'],
        environment=environment, timeout=20).decode('ascii').strip()
    # Classic Docker reports the config digest; containerd can report the OCI
    # manifest/index digest. Every allowed ID must come from the same verified
    # archive descriptor graph bound to the independently approved release.
    assert image_id in identities
    # Run by immutable ID and prohibit a pull after the inspection.
    release.command(['run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--memory', '512m',
        '--memory-swap', '512m', '--cpus', '0.5', '--pids-limit', '64', '--user', '0:0',
        '--mount', 'type=bind,src='+str(path)+',dst=/input'+path.suffix+',readonly',
        '--entrypoint', '/usr/bin/ffmpeg', image_id, *decode_arguments('/input'+path.suffix, kind)],
        environment=environment, timeout=120)


def download(root, agent, headers, artifact, *, decoder=None):
    kind = artifact['kind']
    assert kind in ('video', 'audio')
    suffix = '.mp4' if kind == 'video' else '.flac'
    path = root/('generated'+suffix)
    size, expected_hash = artifact['size_bytes'], artifact['sha256']
    assert type(size) is int and 0 < size <= 256*1024**2
    assert re.fullmatch(r'[0-9a-f]{64}', expected_hash)
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,160}', artifact['id'])
    assert artifact['mime'] == ('video/mp4' if kind == 'video' else 'audio/flac')
    assert artifact['content_url'] == '/v1/artifacts/'+artifact['id']+'/content'
    check_file(path)
    if not path.exists():
        temporary = root/('generated'+suffix+'.part')
        # A partial download may be overwritten; no user file can occupy this
        # operator-owned directory, and final outputs are never overwritten.
        check_file(temporary)
        req = urllib.request.Request(public.ORIGIN+artifact['content_url'], headers={'Origin': public.ORIGIN, **headers})
        digest, total = hashlib.sha256(), 0
        with agent.open(req, timeout=60) as response, temporary.open('wb') as out:
            assert response.code == 200
            while chunk := response.read(1024**2):
                total += len(chunk)
                assert total <= size
                digest.update(chunk)
                out.write(chunk)
        assert total == size and digest.hexdigest() == expected_hash
        temporary.replace(path)
    assert path.is_file() and not path.is_symlink() and path.stat().st_size == size
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024**2):
            digest.update(chunk)
    assert digest.hexdigest() == expected_hash
    (decoder or docker_decode)(path, kind)
    return {'artifact_id': artifact['id'], 'kind': kind, 'filename': path.name,
        'size_bytes': size, 'sha256': expected_hash, 'full_decode': True}


def collect(root, state, browser, agent, headers, *, decoder=None):
    check_state(state)
    assert state.get('job_id') and state.get('project_id') == PROJECT
    job = public.request(agent, 'GET', '/v1/jobs/'+state['job_id'], headers=headers)
    check_job(job, state)
    state['status'] = job['status']
    if job['status'] != 'succeeded':
        save(root, state)
        return state
    artifacts = job['artifacts']
    assert len(artifacts) == 2 and {a['kind'] for a in artifacts} == {'video', 'audio'}
    assert all(a['job_id'] == state['job_id'] for a in artifacts)
    state['outputs'] = [download(root, agent, headers, a, decoder=decoder) for a in artifacts]
    video = next(a for a in artifacts if a['kind'] == 'video')
    audio = next(a for a in artifacts if a['kind'] == 'audio')
    project = public.request(agent, 'GET', '/v1/projects/'+PROJECT, headers=headers)
    shot = next(e for e in project['project']['entities'] if e['id'] == 'shot')
    if (shot['data'].get('selectedAssetId') != 'result-'+video['id']
            or not shot['data'].get('selectedVideoRange')):
        # This is a dedicated acceptance story. Refuse to replace an operator's
        # later choice or soundtrack while retrying the acceptance procedure.
        # Completion may already have selected this exact first result. Trim
        # and soundtrack acceptance is still explicit and not implied by that.
        assert shot['data'].get('selectedAssetId') in (None, '', 'result-'+video['id'])
        assert not shot['data'].get('selectedVideoRange')
        assert not project['project'].get('journey', {}).get('soundTracks', {}).get('chapter')
        actions = [
            {'op': 'artifact.adopt', 'artifact_id': video['id'], 'shot_id': 'shot', 'select': True},
            {'op': 'artifact.adopt', 'artifact_id': audio['id']},
            {'op': 'sound.set', 'chapter_id': 'chapter', 'mode': 'mixed', 'tracks': []},
            {'op': 'shot.trim', 'shot_id': 'shot', 'start': 0, 'end': 5},
            {'op': 'sound.generated', 'shot_id': 'shot'}]
        project = public.request(agent, 'POST', '/v1/projects/'+PROJECT+'/actions',
            {'expected_version': project['version'], 'actions': actions},
            {**headers, 'Idempotency-Key': 'public-gpu-adoption-20261004'})
    entities = {e['id']: e for e in project['project']['entities']}
    shot = entities['shot']
    assert entities['result-'+video['id']]['data']['cloudArtifactId'] == video['id']
    assert entities['result-'+audio['id']]['data']['cloudArtifactId'] == audio['id']
    trim = shot['data']['selectedVideoRange']
    assert trim['assetId'] == 'result-'+video['id'] and trim['start'] == 0 and trim['end'] == 5
    tracks = project['project']['journey']['soundTracks']['chapter']
    assert len(tracks) == 1 and tracks[0]['assetId'] == 'result-'+audio['id']
    assert tracks[0]['shotId'] == 'shot' and tracks[0]['generatedFrom']['jobId'] == state['job_id']
    assert tracks[0]['generatedFrom']['videoArtifactId'] == video['id']
    assert tracks[0]['generatedFrom']['audioArtifactId'] == audio['id']
    assert tracks[0]['start'] == 0 and tracks[0]['end'] == 5 and not tracks[0]['muted']
    assert public.request(browser, 'GET', '/v1/projects/'+PROJECT) == {**project, 'created_by': {}}  # Ordinary PAT: no assistant attribution.
    state.update(adopted=True, browser_api_same_document=True, project_version=project['version'],
        completed_at=datetime.now(timezone.utc).isoformat())
    save(root, state)
    return state


def run(mode, root=ROOT):
    if mode not in ('submit-authorized', 'status', 'collect'):
        return {'state': 'disabled', 'generation_submitted': False}
    if not __debug__:
        raise RuntimeError('Verification requires Python assertions enabled')
    if getattr(os, 'geteuid', lambda: -1)() != 0:
        raise RuntimeError('Operator root required')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert not root.is_symlink() and root.stat().st_uid == 0 and not root.stat().st_mode & 0o077
    import fcntl
    check_file(root/'verification.lock')
    check_file(root/'receipt.json')
    with (root/'verification.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root/'receipt.json').read_text()) if (root/'receipt.json').exists() else {}
        check_state(state)
        with authenticated(prepare=mode == 'submit-authorized' and not state) as (browser, agent, headers):
            if mode == 'submit-authorized':
                return submit(root, state, agent, headers)
            if mode == 'collect':
                return collect(root, state, browser, agent, headers)
            assert state.get('job_id')
            job = public.request(agent, 'GET', '/v1/jobs/'+state['job_id'], headers=headers)
            check_job(job, state)
            return {'job_id': job['id'], 'status': job['status'], 'artifacts': len(job['artifacts'])}


if __name__ == '__main__':
    try:
        print(json.dumps(run(sys.argv[1] if len(sys.argv) == 2 else ''), ensure_ascii=False))
    except Exception:
        print('Public GPU verification incomplete; preserve receipt and reconcile; details suppressed', file=sys.stderr)
        raise SystemExit(1)

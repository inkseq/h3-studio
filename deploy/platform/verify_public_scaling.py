#!/usr/bin/python3
"""Six explicit public H3 jobs; durable plan/job intent, never cloud rental.

Default is disabled. Run as the authorized root operator on the AWS CPU host.
Modes: prepare, submit-authorized, status, collect, watch-collect. Passwords, sessions,
and temporary project-limited PAT values stay in memory. A replacement PAT
reconciles the original plan and NEVER blindly replays an uncertain paid POST.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import uuid

import verify_public_api as public
import verify_public_generation as single

ROOT = Path('/srv/sixnine/gpu-scaler/public-verification')
PROJECT = 'production-scaling-20261004'
RECIPE = 'h3-base-fl2va-v1'
KEY_NAME = 'Temporary scaling acceptance 20261004'
PROMPTS = (
    'A red ceramic teapot on a wooden table, slow cinematic camera move, gentle ambient sound.',
    'A golden retriever walks through a sunlit garden, flowers sway gently, birds chirp.',
    'A small sailboat on a calm blue lake, sunlight on the water, gentle waves.',
    'A silver train enters a foggy station at sunrise, the camera slowly pans, soft train ambience.',
    'A robot made from wooden blocks waves in a colorful studio, cheerful soft mechanical clicks.',
    'An astronaut slowly walks across a moonlike plain, cinematic wide shot, ambient space music.',
)
SHOTS = tuple('shot-'+str(i) for i in range(1, 7))


def now():
    return datetime.now(timezone.utc).isoformat()


def controls(index):
    return {'duration': 5, 'resolution': '768P', 'aspect_ratio': '16:9', 'steps': 50,
        'seed': str(52000+index), 'generate_audio': True, 'video_decode': 'tiled', 'encoder_device': 'cpu'}


def initial_state():
    return {'version': 1, 'project_id': PROJECT, 'recipe_id': RECIPE, 'created_at': now(),
        'shots': [{'shot_id': shot, 'submission_key': 'public-scaling-20261004-'+shot} for shot in SHOTS],
        'key_intents': []}


def check_state(state):
    assert state['version'] == 1 and state['project_id'] == PROJECT and state['recipe_id'] == RECIPE
    assert [x['shot_id'] for x in state['shots']] == list(SHOTS)
    assert len(state.get('key_intents', [])) <= 100
    for key in state.get('key_intents', []):
        assert re.fullmatch(re.escape(KEY_NAME)+r' [0-9a-f]{32}', key['name'])
    for index, item in enumerate(state['shots']):
        assert item['submission_key'] == 'public-scaling-20261004-'+SHOTS[index]
        for field in ('plan_id', 'job_id'):
            if item.get(field):
                assert re.fullmatch(r'[A-Za-z0-9_-]{1,160}', item[field])
        if item.get('job_id'):
            assert item.get('plan_id') and item.get('submission_started_at')
        if item.get('plan_creation_started_at'):
            body = item['request']
            assert body['recipe_id'] == RECIPE and body['prompt'] == PROMPTS[index]
            assert body['client_ref']['project_id'] == PROJECT and body['client_ref']['shot_id'] == SHOTS[index]
            assert type(body['client_ref']['shot_version']) is int and body['client_ref']['shot_version'] >= 1
            assert body['controls'] == controls(index)


def save(root, state):
    check_state(state)
    single.save(root, state)


def prepare_story(browser):
    projects = public.request(browser, 'GET', '/v1/projects')['projects']
    if not any(p['id'] == PROJECT for p in projects):
        public.request(browser, 'POST', '/v1/projects', {'id': PROJECT,
            'title': '公网自动扩容实测 · 六个镜头',
            'logline': '六条真实HTTPS任务，验证生产队列按需求扩容、视频回写、独立音轨与用户隔离。'},
            {'Idempotency-Key': 'public-scaling-story-20261004'}, expected=201)
    project = public.request(browser, 'GET', '/v1/projects/'+PROJECT)
    entities = {e['id']: e for e in project['project']['entities']}
    expected = [
        {'id': 'chapter', 'type': 'chapter', 'title': '第一章：六个世界'},
        {'id': 'scene', 'type': 'scene', 'parentId': 'chapter', 'title': '并行生成验收'},
        *[{'id': shot, 'type': 'shot', 'parentId': 'scene', 'title': '镜头 '+str(index+1),
            'data': {'seconds': 5, 'prompt': PROMPTS[index]}} for index, shot in enumerate(SHOTS)]]
    for item in expected:
        if item['id'] in entities:
            existing = entities[item['id']]
            assert existing['type'] == item['type'] and existing.get('parentId') == item.get('parentId')
            if item['type'] == 'shot':
                assert existing['data']['seconds'] == 5 and existing['data']['prompt'] == item['data']['prompt']
    missing = [x for x in expected if x['id'] not in entities]
    if missing:
        project = public.request(browser, 'POST', '/v1/projects/'+PROJECT+'/actions',
            {'expected_version': project['version'], 'actions': [{'op': 'entity.create', 'entity': x} for x in missing]},
            {'Idempotency-Key': 'public-scaling-structure-'+str(project['version'])})
    journey = project['project'].get('journey', {})
    if journey.get('sound', {}).get('mode') != 'mixed':
        assert not journey.get('soundTracks', {}).get('chapter')
        project = public.request(browser, 'POST', '/v1/projects/'+PROJECT+'/actions',
            {'expected_version': project['version'], 'actions': [
                {'op': 'sound.set', 'chapter_id': 'chapter', 'mode': 'mixed', 'tracks': []}]},
            {'Idempotency-Key': 'public-scaling-sound-'+str(project['version'])})
    return project


@contextmanager
def authenticated(root, state, *, prepare=False):
    accounts = public.runtime_secrets_aws.value(public.runtime_secrets_aws.client(), public.runtime_secrets_aws.ACCOUNTS)
    browser, agent, outsider = public.client(), public.client(), public.client()
    key_id = None
    logged_in = other_logged_in = False
    key_name = None
    headers = {}
    try:
        public.request(browser, 'POST', '/api/auth/login', {'username': 'superdan', 'password': accounts['superdan']})
        logged_in = True
        # Recover key-creation or process-termination uncertainty without
        # persisting a token or leaving old test keys knowingly active.
        names = {x['name'] for x in state['key_intents']}
        for key in public.request(browser, 'GET', '/v1/api-keys')['api_keys']:
            if key['name'] in names and key['revoked_at'] is None:
                assert not key['all_projects'] and key['project_ids'] == [PROJECT]
                public.request(browser, 'DELETE', '/v1/api-keys/'+key['id'])
        if prepare:
            prepare_story(browser)
        key_name = KEY_NAME+' '+uuid.uuid4().hex
        state['key_intents'].append({'name': key_name, 'started_at': now()})
        save(root, state)
        value = public.request(browser, 'POST', '/v1/api-keys', {'name': key_name,
            'scopes': ['projects:read', 'projects:write', 'jobs:read', 'jobs:write'],
            'all_projects': False, 'project_ids': [PROJECT], 'expires_in_days': 1}, expected=201)
        key_id = value['key']['id']
        headers = {'Authorization': 'Bearer '+value['api_key']}
        del value
        public.request(outsider, 'POST', '/api/auth/login', {'username': 'supervan', 'password': accounts['supervan']})
        other_logged_in = True
        yield browser, agent, headers, outsider
    finally:
        try:
            if key_id:
                public.request(browser, 'DELETE', '/v1/api-keys/'+key_id)
            elif logged_in and key_name:
                for key in public.request(browser, 'GET', '/v1/api-keys')['api_keys']:
                    if key['name'] == key_name and key['revoked_at'] is None:
                        public.request(browser, 'DELETE', '/v1/api-keys/'+key['id'])
            if key_name:
                next(x for x in state['key_intents'] if x['name'] == key_name)['revoked_at'] = now()
                save(root, state)
        finally:
            try:
                if logged_in:
                    public.request(browser, 'POST', '/api/auth/logout')
                if other_logged_in:
                    public.request(outsider, 'POST', '/api/auth/logout')
            finally:
                accounts.clear()
                headers.clear()


def check_effective(effective, requested):
    assert effective['model'] == 'MiniMax-H3-Base-BF16' and effective['mode'] == 'fl'
    assert effective['prompt'] == requested['prompt'] and effective['backend'] == 'comfy-local'
    assert all(effective[key] == value for key, value in requested['controls'].items())


def check_job(job, item):
    assert not job['simulation'] and job['project_id'] == PROJECT and job['plan_id'] == item['plan_id']
    assert job['recipe_id'] == RECIPE and job['client_ref']['shot_id'] == item['shot_id']
    check_effective(job['effective_request'], item['request'])
    if item.get('job_id'):
        assert item['job_id'] == job['id']


def recover(root, state, item, agent, headers):
    matches, exhausted = [], False
    for offset in range(0, 1000, 100):
        rows = public.request(agent, 'GET', '/v1/jobs?client_project_id='+PROJECT+
            '&limit=100&offset='+str(offset), headers=headers)['jobs']
        matches.extend(x for x in rows if x['plan_id'] == item['plan_id'])
        if len(rows) < 100:
            exhausted = True
            break
    if not exhausted or len(matches) != 1:
        item.update(status='submission_unknown', recovery='no_unique_original_plan_match_no_resubmit')
        save(root, state)
        return False
    job = public.request(agent, 'GET', '/v1/jobs/'+matches[0]['id'], headers=headers)
    check_job(job, item)
    item.update(job_id=job['id'], status=job['status'], recovery='recovered_by_original_plan')
    save(root, state)
    return True


def submit(root, state, agent, headers):
    check_state(state)
    health = public.request(agent, 'GET', '/healthz')
    assert health['auth_ready'] and health['generation_enabled'] and health['execution_backend'] == 'comfy-worker'
    project = public.request(agent, 'GET', '/v1/projects/'+PROJECT, headers=headers)
    entities = {e['id']: e for e in project['project']['entities']}
    for index, item in enumerate(state['shots']):
        if item.get('job_id'):
            job = public.request(agent, 'GET', '/v1/jobs/'+item['job_id'], headers=headers)
            check_job(job, item)
            item['status'] = job['status']
            continue
        if item.get('submission_started_at'):
            if not recover(root, state, item, agent, headers):
                return state
            continue
        if not item.get('plan_id'):
            if item.get('plan_creation_started_at'):
                item['status'] = 'plan_creation_unknown_no_resubmit'
                save(root, state)
                return state
            shot = entities[item['shot_id']]
            assert shot['type'] == 'shot' and shot['data']['prompt'] == PROMPTS[index]
            body = {'client_ref': {'project_id': PROJECT, 'shot_id': item['shot_id'], 'shot_version': shot['version']},
                'recipe_id': RECIPE, 'prompt': PROMPTS[index], 'controls': controls(index)}
            # A plan is not a charged generation. Still preserve its uncertain
            # creation rather than manufacture an apparently recovered plan ID.
            item.update(plan_creation_started_at=now(), request=body, status='plan_creating')
            save(root, state)
            plan = public.request(agent, 'POST', '/v1/generation-plans', body, headers, expected=201)
            item.update(plan_id=plan['plan_id'], plan_status=plan['status'])
            save(root, state)
            if plan['status'] != 'ready' or plan['simulation'] or plan['blockers']:
                item['status'] = 'plan_blocked'
                save(root, state)
                return state
            check_effective(plan['effective_request'], body)
            item['plan_verified'] = True
            save(root, state)
        assert item.get('plan_verified') is True
        item['submission_started_at'] = now()
        save(root, state)
        # Only same live PAT can retry this idempotency scope.
        for attempt in range(2):
            try:
                job = public.request(agent, 'POST', '/v1/jobs', {'plan_id': item['plan_id']},
                    {**headers, 'Idempotency-Key': item['submission_key']}, expected=202)
                break
            except (TimeoutError, ConnectionError, urllib.error.URLError):
                if attempt:
                    raise
        check_job(job, item)
        item.update(job_id=job['id'], status=job['status'])
        save(root, state)
    return state


def collect(root, state, browser, agent, headers, outsider, *, decoder=None):
    check_state(state)
    for item in state['shots']:
        if not item.get('job_id'):
            continue
        job = public.request(agent, 'GET', '/v1/jobs/'+item['job_id'], headers=headers)
        check_job(job, item)
        item['status'] = job['status']
        if job['status'] != 'succeeded':
            continue
        artifacts = job['artifacts']
        assert len(artifacts) == 2 and {x['kind'] for x in artifacts} == {'video', 'audio'}
        assert all(x['job_id'] == item['job_id'] for x in artifacts)
        directory = root/item['shot_id']
        directory.mkdir(mode=0o700, exist_ok=True)
        assert not directory.is_symlink() and directory.is_dir()
        previous = {x['artifact_id']: x for x in item.get('outputs', [])}
        outputs = []
        for artifact in artifacts:
            proof = previous.get(artifact['id'], {})
            identical_decoded = proof.get('full_decode') is True and all(
                proof.get(k) == artifact[k] for k in ('kind', 'size_bytes', 'sha256'))
            # Each poll still reads and hashes the complete saved file. A
            # decoder proof for exactly those immutable bytes need not launch
            # another Docker parser while the remaining shots are running.
            verify = (lambda *_: None) if identical_decoded else decoder
            output = single.download(directory, agent, headers, artifact, decoder=verify)
            output['decode_evidence'] = 'reused_identical_hash' if identical_decoded else 'decoded_this_run'
            outputs.append(output)
        item['outputs'] = outputs
        video = next(x for x in artifacts if x['kind'] == 'video')
        audio = next(x for x in artifacts if x['kind'] == 'audio')
        project = public.request(agent, 'GET', '/v1/projects/'+PROJECT, headers=headers)
        shot = next(e for e in project['project']['entities'] if e['id'] == item['shot_id'])
        tracks = project['project'].get('journey', {}).get('soundTracks', {}).get('chapter', [])
        if shot['data'].get('selectedAssetId') != 'result-'+video['id']:
            assert not shot['data'].get('selectedAssetId')
            assert not any(t.get('shotId') == item['shot_id'] for t in tracks)
            project = public.request(agent, 'POST', '/v1/projects/'+PROJECT+'/actions',
                {'expected_version': project['version'], 'actions': [
                    {'op': 'artifact.adopt', 'artifact_id': video['id'], 'shot_id': item['shot_id'], 'select': True},
                    {'op': 'artifact.adopt', 'artifact_id': audio['id']},
                    {'op': 'shot.trim', 'shot_id': item['shot_id'], 'start': 0, 'end': 5},
                    {'op': 'sound.generated', 'shot_id': item['shot_id']}]},
                {**headers, 'Idempotency-Key': 'public-scaling-adopt-'+item['shot_id']})
        entities = {e['id']: e for e in project['project']['entities']}
        shot = entities[item['shot_id']]
        assert entities['result-'+video['id']]['data']['cloudArtifactId'] == video['id']
        assert entities['result-'+audio['id']]['data']['cloudArtifactId'] == audio['id']
        trim = shot['data']['selectedVideoRange']
        assert all(trim[key] == value for key, value in
            {'assetId': 'result-'+video['id'], 'cloudArtifactId': video['id'], 'start': 0, 'end': 5}.items())
        tracks = [t for t in project['project']['journey']['soundTracks']['chapter'] if t.get('shotId') == item['shot_id']]
        assert len(tracks) == 1
        track = tracks[0]
        assert track['assetId'] == 'result-'+audio['id'] and not track['muted']
        assert track['generatedFrom']['jobId'] == item['job_id']
        assert track['generatedFrom']['videoArtifactId'] == video['id'] and track['generatedFrom']['audioArtifactId'] == audio['id']
        assert track['start'] == 0 and track['end'] == 5
        assert public.request(browser, 'GET', '/v1/projects/'+PROJECT) == {**project, 'created_by': {}}  # Ordinary PAT: no assistant attribution.
        public.request(outsider, 'GET', '/v1/projects/'+PROJECT, expected=404)
        public.request(outsider, 'GET', '/v1/jobs/'+item['job_id'], expected=404)
        for artifact in artifacts:
            public.request(outsider, 'GET', artifact['content_url'], expected=404)
        item.update(adopted=True, browser_api_same_document=True, cross_owner_denied=True,
            project_version=project['version'], completed_at=now())
        save(root, state)
    save(root, state)
    return state


def compact(state):
    return {'project_id': PROJECT, 'submitted': sum(bool(x.get('job_id')) for x in state['shots']),
        'adopted': sum(bool(x.get('adopted')) for x in state['shots']),
        'shots': [{k: x.get(k) for k in ('shot_id', 'job_id', 'status', 'adopted')} for x in state['shots']]}


def prepare(root, state):
    """Create the fixed story before operator policy; no plan, job or health gate."""
    with authenticated(root, state, prepare=True):
        return {**compact(state), 'state': 'prepared', 'generation_submitted': False}


def run(mode, root=ROOT):
    if mode not in ('prepare', 'submit-authorized', 'status', 'collect', 'watch-collect'):
        return {'state': 'disabled', 'generation_submitted': False}
    if not __debug__:
        raise RuntimeError('Verification requires Python assertions enabled')
    if getattr(os, 'geteuid', lambda: -1)() != 0:
        raise RuntimeError('Operator root required')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert not root.is_symlink() and root.stat().st_uid == 0 and not root.stat().st_mode & 0o077
    import fcntl
    single.check_file(root/'verification.lock')
    single.check_file(root/'receipt.json')
    with (root/'verification.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((root/'receipt.json').read_text(encoding='utf-8')) if (root/'receipt.json').exists() else initial_state()
        check_state(state)
        save(root, state)
        if mode == 'prepare':
            return prepare(root, state)
        with authenticated(root, state, prepare=mode == 'submit-authorized') as (browser, agent, headers, outsider):
            if mode == 'submit-authorized':
                return compact(submit(root, state, agent, headers))
            if mode == 'status':
                for item in state['shots']:
                    if item.get('job_id'):
                        job = public.request(agent, 'GET', '/v1/jobs/'+item['job_id'], headers=headers)
                        check_job(job, item)
                        item['status'] = job['status']
                save(root, state)
                return compact(state)
            deadline = time.monotonic()+7200
            previous = None
            while True:
                collect(root, state, browser, agent, headers, outsider)
                result = compact(state)
                if mode == 'collect' or result['adopted'] == 6 or any(x.get('status') in ('failed', 'cancelled') for x in state['shots']):
                    return result
                if time.monotonic() >= deadline:
                    return {**result, 'state': 'monitor_timeout_preserve_receipt'}
                if result != previous:
                    print(json.dumps(result), flush=True)
                    previous = result
                time.sleep(30)


if __name__ == '__main__':
    try:
        print(json.dumps(run(sys.argv[1] if len(sys.argv) == 2 else ''), ensure_ascii=False))
    except Exception:
        print('Public scaling verification incomplete; preserve receipt and reconcile; details suppressed', file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/python3
"""Operator acceptance through public TLS. Secrets remain in process memory.

Creates one clearly labelled acceptance story per configured account and revokes
the temporary test keys. No generation, cloud creation or password changes.
Run on the authorized AWS control host only after publication.
"""
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import io
import json
import sys
import urllib.error
import urllib.request
import uuid
import zipfile

import runtime_secrets_aws

ORIGIN = 'https://inkseq.com'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Unexpected API redirect')


def client():
    return urllib.request.build_opener(NoRedirect(),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(opener, method, path, body=None, headers=None, expected=200, binary=False):
    if not path.startswith('/v1/') and path not in ('/healthz', '/api/auth/login', '/api/auth/logout'):
        raise RuntimeError('Unapproved verification path')
    merged = {'Origin': ORIGIN, **(headers or {})}
    if body is not None:
        merged['Content-Type'] = 'application/json'
    req = urllib.request.Request(ORIGIN + path, method=method, headers=merged,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        result = opener.open(req, timeout=30)
    except urllib.error.HTTPError as error:
        result = error
    with result:
        if result.code != expected:
            raise RuntimeError('Unexpected verification status')
        data = result.read(9*1024**2)
        return data if binary else json.loads(data)


def verify():
    accounts = runtime_secrets_aws.value(runtime_secrets_aws.client(), runtime_secrets_aws.ACCOUNTS)
    browser, agent = {}, client()
    keys, stories, checks = [], {}, []
    run = uuid.uuid4().hex
    try:
        health = request(agent, 'GET', '/healthz')
        assert health['auth_ready'] and not health['generation_enabled']
        request(agent, 'GET', '/v1/projects', expected=401)
        checks.append('public_tls_and_unauthenticated_denial')
        for username in ('superdan', 'supervan'):
            browser[username] = client()
            request(browser[username], 'POST', '/api/auth/login',
                    {'username': username, 'password': accounts[username]})
            value = request(browser[username], 'POST', '/v1/api-keys', {
                'name': 'Temporary release acceptance ' + run[:8],
                'scopes': ['projects:read', 'projects:create', 'projects:write', 'assets:read', 'assets:write'],
                'all_projects': True, 'project_ids': [], 'expires_in_days': 1}, expected=201)
            headers = {'Authorization': 'Bearer ' + value['api_key']}
            keys.append((username, value['key']['id'], headers))
            request(agent, 'GET', '/v1/api-keys', headers=headers, expected=403)
            body = {'title': 'Agent API 上线验收 · ' + username,
                    'logline': '通过公开 HTTPS API 创建；测试 Key 已撤销。可在网页继续编辑这个故事。'}
            creation_headers = {**headers, 'Idempotency-Key': 'acceptance-' + run + '-' + username}
            created = request(agent, 'POST', '/v1/projects', body, creation_headers, expected=201)
            replayed = request(agent, 'POST', '/v1/projects', body, creation_headers, expected=201)
            assert replayed == created
            ident = created['id']
            stories[username] = ident
            actions = [
                {'op': 'entity.create', 'entity': {'id': 'chapter-one', 'type': 'chapter', 'title': '第一章：来信'}},
                {'op': 'entity.create', 'entity': {'id': 'scene-one', 'type': 'scene', 'parentId': 'chapter-one', 'title': '清晨海边'}},
                {'op': 'entity.create', 'entity': {'id': 'shot-one', 'type': 'shot', 'parentId': 'scene-one',
                    'title': '邮差抵达', 'data': {'seconds': 5, 'prompt': '清晨海边，邮差递出一封信。'}}}]
            edited = request(agent, 'POST', f'/v1/projects/{ident}/actions',
                {'expected_version': 1, 'actions': actions},
                {**headers, 'Idempotency-Key': 'edit-' + run + '-' + username})
            assert edited['version'] == 2 and len(edited['project']['entities']) == 3
            # Browser and Agent must read the same persistent document.
            assert request(browser[username], 'GET', f'/v1/projects/{ident}') == edited
            request(agent, 'POST', f'/v1/projects/{ident}/actions',
                {'expected_version': 1, 'actions': actions}, headers, expected=409)
            exported = request(agent, 'GET', f'/v1/projects/{ident}/export?format=json', headers=headers)
            assert ident in json.dumps(exported)
            skill = request(agent, 'GET', '/v1/agent-skill.zip', headers=headers, binary=True)
            with zipfile.ZipFile(io.BytesIO(skill)) as archive:
                assert len(archive.namelist()) == 2
                assert all('..' not in name for name in archive.namelist())
            checks.append(username + '_login_key_creation_story_actions_export_skill_browser_consistency')
        for username, _, headers in keys:
            other = 'supervan' if username == 'superdan' else 'superdan'
            request(agent, 'GET', '/v1/projects/' + stories[other], headers=headers, expected=404)
        checks.append('cross_account_story_isolation')
    finally:
        for username, ident, headers in keys:
            request(browser[username], 'DELETE', '/v1/api-keys/' + ident)
            request(agent, 'GET', '/v1/projects', headers=headers, expected=401)
        for opener in browser.values():
            request(opener, 'POST', '/api/auth/logout')
        accounts.clear()
    checks.append('all_test_keys_revoked_and_sessions_logged_out')
    return {'verified_at': datetime.now(timezone.utc).isoformat(), 'origin': ORIGIN,
        'checks': checks, 'stories': stories, 'generation_called': False,
        'credentials_emitted': False}


if __name__ == '__main__':
    try:
        print(json.dumps(verify(), ensure_ascii=False))
    except BaseException:
        print('Public API acceptance failed; credentials and response details suppressed', file=sys.stderr)
        raise SystemExit(1)

"""Push one reviewed batch and observe its single automatic main staging run.
Never cancel CI or dispatch duplicate preparation. A prior intent blocks replay.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ci_changes import DOCUMENTS
from tools.native_source_retention import INPUTS as RETENTION_INPUTS, binding as retention_binding
REPO, WORKFLOW = 'inkseq/h3-studio', '.github/workflows/ci.yml'
API = f'repos/{REPO}/actions/workflows/ci.yml'
SHA = re.compile(r'[0-9a-f]{40}')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def call(root, *args, payload=None):
    try:
        result = subprocess.run(list(args), cwd=root, input=payload, capture_output=True, timeout=60, check=False)
        require(result.returncode == 0, 'Command failed or outcome unknown; inspect receipt/GitHub before any retry')
        return result.stdout.decode('utf-8')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RuntimeError('Command outcome unknown; inspect receipt/GitHub before any retry') from None


def api(root, endpoint, payload=None):
    args = ['gh', 'api', '--hostname', 'github.com', endpoint]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    raw = call(root, *args, payload=None if payload is None else json.dumps(payload).encode())
    return json.loads(raw) if raw.strip() else None


def local(root, expected=None):
    require(Path(call(root, 'git', 'rev-parse', '--show-toplevel').strip()).resolve() == root.resolve(), 'Wrong repository root')
    require(call(root, 'git', 'branch', '--show-current').strip() == 'main', 'Local branch must be main')
    head = call(root, 'git', 'rev-parse', 'HEAD').strip()
    require(SHA.fullmatch(head) and (expected is None or expected == head), 'Commit must equal exact local main HEAD')
    allowed = {f'{prefix}{REPO}{suffix}' for prefix in ('https://github.com/', 'git@github.com:', 'ssh://git@github.com/') for suffix in ('', '.git')}
    for extra in ([], ['--push']):
        origins = call(root, 'git', 'remote', 'get-url', *extra, '--all', 'origin').strip().splitlines()
        require(len(origins) == 1 and origins[0] in allowed, 'Origin must be the reviewed repository without embedded credentials')
    raw = call(root, 'git', 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none')
    require(not raw or raw.endswith('\0'), 'Incomplete source status')
    for entry in raw.split('\0')[:-1]:
        require(len(entry) >= 4 and entry[2] == ' ' and not set(entry[:2]) & {'R', 'C', 'U'} and entry[3:] in DOCUMENTS,
                'Source/configuration is dirty; only explicit non-runtime document edits may remain local')
    return head


def remote(root, commit):
    require(call(root, 'git', 'ls-remote', '--heads', 'origin', 'refs/heads/main').split() == [commit, 'refs/heads/main'], 'Remote main changed; stop before dispatch')
    ref = api(root, f'repos/{REPO}/git/ref/heads/main')
    require(ref.get('ref') == 'refs/heads/main' and ref.get('object', {}).get('sha') == commit, 'GitHub main changed; stop before dispatch')


def match(run, wid, commit, event):
    return (isinstance(run, dict) and type(run.get('id')) is int and run['id'] > 0 and run.get('workflow_id') == wid
            and run.get('path') == WORKFLOW and run.get('event') == event and run.get('head_sha') == commit
            and run.get('head_branch') == 'main' and (run.get('repository') or {}).get('full_name') == REPO
            and (run.get('head_repository') or {}).get('full_name') == REPO)


def runs(root, wid, commit, event):
    found, page, seen = [], 1, set()
    while True:
        body = api(root, API + f'/runs?event={event}&branch=main&head_sha={commit}&per_page=100&page={page}')
        require(type(body.get('total_count')) is int and 0 <= body['total_count'] <= 1000, 'Run inventory exceeds safe bound')
        rows = body['workflow_runs']
        for row in rows:
            require(row['id'] not in seen, 'Run inventory changed; reconcile before preparation')
            seen.add(row['id'])
            if match(row, wid, commit, event):
                found.append(row)
        if len(rows) < 100:
            require(len(seen) >= body['total_count'], 'Run inventory incomplete')
            return found
        page += 1
        require(page <= 11, 'Run inventory incomplete')


def prepare(kind, commit=None, *, root=ROOT, sleep=time.sleep, retain_native_sources=None):
    root = Path(root).resolve()
    require(kind in ('platform', 'frontend') and (commit is None or SHA.fullmatch(commit)), 'Invalid kind or exact commit')
    try:
        retained = retention_binding(retain_native_sources or {})
    except ValueError:
        raise RuntimeError('Invalid explicit native source retention binding') from None
    require(retained is None, 'Automatic main staging uses the reviewed repository native-retention variables; do not dispatch another build')
    commit = local(root, commit)
    store = root / '.release-prepares'
    store.mkdir(exist_ok=True)
    require(not store.is_symlink() and not getattr(store, 'is_junction', lambda: False)(), 'Receipt store must not be linked')
    attempt = store / (commit + '-' + kind)
    require(not attempt.exists() and not attempt.is_symlink(), 'Prior intent exists: inspect .release-prepares and GitHub; do not repeat push/cancel/dispatch')
    attempt.mkdir()  # Exclusive persistent guard; a crash never permits blind retry.
    receipt = {'repository': REPO, 'workflow': WORKFLOW, 'commit': commit, 'kind': kind, 'cancelled_run_ids': []}
    if retained is not None:
        receipt['retain_native_sources'] = retained
    def record(phase, **fields):
        receipt.update(fields, phase=phase)
        temporary = attempt / 'receipt.next'
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(receipt, output, indent=2); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, attempt / 'receipt.json')
    record('validated')
    workflow = api(root, API)
    require(workflow.get('path') == WORKFLOW and workflow.get('state') == 'active' and type(workflow.get('id')) is int, 'Reviewed workflow unavailable')
    wid = workflow['id']
    record('push_started', workflow_id=wid)
    call(root, 'git', 'push', 'origin', commit + ':refs/heads/main')
    remote(root, commit)
    record('push_confirmed')
    push_runs = []
    for index in range(7):
        push_runs = runs(root, wid, commit, 'push')
        if push_runs:
            break
        if index < 6:
            sleep(2)  # GitHub run inventory is eventually consistent after push.
    require(push_runs, 'Push check is not visible yet; no dispatch sent. Inspect GitHub before continuing')
    local(root, commit); remote(root, commit)
    item = max(push_runs, key=lambda value: value['id'])
    record('automatic_main_run_observed', run_id=item['id'], observed_head_sha=item['head_sha'])
    return {**receipt, 'url': f'https://github.com/{REPO}/actions/runs/{item["id"]}',
        'receipt': str(attempt / 'receipt.json'), 'completed': False, 'approved': False,
        'deployed': False, 'automatic_staging': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('platform', 'frontend'), required=True)
    parser.add_argument('--commit')
    parser.add_argument('--retain-native-sources-run')
    parser.add_argument('--retain-native-sources-commit')
    parser.add_argument('--retain-native-sources-index-sha256')
    args = parser.parse_args(argv)
    try:
        retained = dict(zip(RETENTION_INPUTS, (args.retain_native_sources_run,
            args.retain_native_sources_commit, args.retain_native_sources_index_sha256)))
        print(json.dumps(prepare(args.kind, args.commit, retain_native_sources=retained), indent=2)); return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        print('Preparation incomplete; inspect receipt/GitHub; do not repeat unknown mutations', file=sys.stderr)
    print(f'Read only: gh run list --repo {REPO} --workflow ci.yml', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())

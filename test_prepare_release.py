"""One-shot preparation contract; every subprocess/network call is synthetic."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools import prepare_release as tool

SHA, OTHER, WID = 'a' * 40, 'b' * 40, 42


def run(ident, **overrides):
    return dict(id=ident, workflow_id=WID, path=tool.WORKFLOW, event='push', head_sha=SHA,
                head_branch='main', repository={'full_name': tool.REPO},
                head_repository={'full_name': tool.REPO}, status='in_progress', **overrides)


class FakeCommands:
    def __init__(self, root):
        self.root, self.calls, self.pushes = root, [], [run(10)]
        self.status, self.branch, self.remote_reads = ' M SCALING.zh-CN.md\0', 'main', 0
        self.race, self.fail_cancel, self.fail_dispatch, self.dispatched = False, False, False, False
        self.complete_at_refresh, self.fresh = False, True
        self.empty_push_reads = 0

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        out = ''
        if args[0] == 'git':
            op = args[1:]
            if op == ['rev-parse', '--show-toplevel']: out = str(self.root)
            elif op == ['rev-parse', 'HEAD']: out = SHA
            elif op == ['branch', '--show-current']: out = self.branch
            elif op[:2] == ['remote', 'get-url']: out = 'https://github.com/' + tool.REPO + '.git'
            elif op[0] == 'status': out = self.status
            elif op[0] == 'push': self.pushed = True
            elif op[0] == 'ls-remote':
                self.remote_reads += 1
                out = (OTHER if self.race and self.remote_reads > 1 else SHA) + '\trefs/heads/main\n'
            else: raise AssertionError(args)
        elif args[:4] == ['gh', 'api', '--hostname', 'github.com']:
            endpoint = args[4]
            if endpoint == tool.API: out = {'id': WID, 'path': tool.WORKFLOW, 'state': 'active'}
            elif '/git/ref/' in endpoint: out = {'ref': 'refs/heads/main', 'object': {'sha': SHA}}
            elif endpoint.endswith('/cancel'):
                if self.fail_cancel: raise subprocess.TimeoutExpired(args, 60)
            elif endpoint.endswith('/dispatches'):
                self.dispatched = True
                self.dispatch_body = json.loads(kwargs['input'])
                if self.fail_dispatch: raise subprocess.TimeoutExpired(args, 60)
            elif '/runs?' in endpoint:
                rows = self.pushes if 'event=push&' in endpoint else (
                    [run(20, **{}) | {'event': 'workflow_dispatch'}] if self.dispatched and self.fresh else [])
                if 'event=push&' in endpoint and self.empty_push_reads:
                    self.empty_push_reads -= 1
                    rows = []
                out = {'total_count': len(rows), 'workflow_runs': rows}
            elif endpoint.endswith('/runs/10'):
                out = run(10) | ({'status': 'completed'} if self.complete_at_refresh else {})
            elif endpoint.endswith('/runs/20'): out = run(20) | {'event': 'workflow_dispatch'}
            else: raise AssertionError(args)
        else: raise AssertionError(args)
        raw = json.dumps(out).encode() if isinstance(out, dict) else out.encode()
        return subprocess.CompletedProcess(args, 0, raw, b'never print arbitrary subprocess output')

    def mutations(self):
        return [args for args in self.calls if args[:2] == ['git', 'push'] or '--method' in args]


class PrepareReleaseTests(unittest.TestCase):
    def test_explicit_native_retention_is_frozen_in_preparation_receipt_and_dispatch(self):
        values = {"retain_native_sources_run": "42", "retain_native_sources_commit": OTHER,
            "retain_native_sources_index_sha256": "c" * 64}
        result = tool.prepare('platform', SHA, root=self.root, sleep=lambda _: None, retain_native_sources=values)
        self.assertEqual(result['retain_native_sources'], values)
        self.assertEqual(self.fake.dispatch_body['inputs'], dict(values, deploy='true', release_kind='platform'))
        receipt = json.loads((self.root / '.release-prepares' / (SHA + '-platform') / 'receipt.json').read_text())
        self.assertEqual(receipt['retain_native_sources'], values)

    def test_incomplete_or_frontend_retention_refuses_before_mutation(self):
        values = {"retain_native_sources_run": "42", "retain_native_sources_commit": OTHER,
            "retain_native_sources_index_sha256": "c" * 64}
        for kind, selected in (('frontend', values), ('platform', {'retain_native_sources_run': '42'})):
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                tool.prepare(kind, SHA, root=self.root, sleep=lambda _: None, retain_native_sources=selected)
        self.assertEqual(self.fake.mutations(), [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fake = FakeCommands(self.root)
        self.patch = patch.object(tool.subprocess, 'run', side_effect=self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def prepare(self):
        return tool.prepare('frontend', SHA, root=self.root, sleep=lambda _: None)

    def receipt(self):
        return json.loads((self.root / '.release-prepares' / (SHA + '-frontend') / 'receipt.json').read_text())

    def test_only_exact_unfinished_push_is_cancelled_then_one_dispatch(self):
        self.fake.pushes += [run(11) | {'head_sha': OTHER}, run(12) | {'event': 'pull_request'},
            run(13) | {'repository': {'full_name': 'other/repo'}}, run(14) | {'path': '.github/workflows/other.yml'},
            run(15) | {'status': 'completed'}, run(16) | {'event': 'workflow_dispatch'}]
        result = self.prepare()
        self.assertEqual(result['run_id'], 20)
        self.assertEqual(result['observed_head_sha'], SHA)
        self.assertFalse(result['completed'])
        self.assertFalse(result['deployed'])
        self.assertEqual(len(self.fake.mutations()), 3)
        self.assertEqual(self.fake.dispatch_body, {'ref': 'main', 'inputs': {'deploy': 'true', 'release_kind': 'frontend'}})
        self.assertEqual(self.receipt()['cancelled_run_ids'], [10])
        with self.assertRaisesRegex(RuntimeError, 'Prior intent'):
            self.prepare()
        self.assertEqual(len(self.fake.mutations()), 3)

    def test_runtime_dirty_source_and_nonmain_refuse_before_any_mutation(self):
        for status, branch in [('?? tools/new.py\0', 'main'), (' M AGENTS.md\0', 'main'),
                               ('R  README.md\0studio_platform/api.py\0', 'main'), ('', 'topic')]:
            self.fake.status, self.fake.branch = status, branch
            with self.assertRaises(RuntimeError): self.prepare()
        self.assertEqual(self.fake.mutations(), [])

    def test_remote_main_race_stops_before_dispatch(self):
        self.fake.race = True
        with self.assertRaisesRegex(RuntimeError, 'Remote main changed'): self.prepare()
        self.assertFalse(self.fake.dispatched)
        self.assertNotEqual(self.receipt()['phase'], 'dispatch_started')

    def test_refresh_completed_push_is_not_cancelled(self):
        self.fake.complete_at_refresh = True
        self.prepare()
        self.assertEqual(len(self.fake.mutations()), 2)
        self.assertEqual(self.receipt()['cancelled_run_ids'], [])

    def test_uncertain_cancel_preserves_intent_and_blocks_retry(self):
        self.fake.fail_cancel = True
        with self.assertRaisesRegex(RuntimeError, 'outcome unknown'): self.prepare()
        self.assertEqual(self.receipt()['phase'], 'cancel_started')
        self.assertFalse(self.fake.dispatched)
        before = len(self.fake.mutations())
        with self.assertRaisesRegex(RuntimeError, 'Prior intent'): self.prepare()
        self.assertEqual(len(self.fake.mutations()), before)

    def test_uncertain_dispatch_is_never_repeated(self):
        self.fake.fail_dispatch = True
        with self.assertRaisesRegex(RuntimeError, 'outcome unknown'): self.prepare()
        self.assertEqual(self.receipt()['phase'], 'dispatch_started')
        before = len(self.fake.mutations())
        with self.assertRaisesRegex(RuntimeError, 'Prior intent'): self.prepare()
        self.assertEqual(len(self.fake.mutations()), before)

    def test_acknowledged_dispatch_without_visible_run_does_not_resend(self):
        self.fake.fresh = False
        with self.assertRaisesRegex(RuntimeError, 'run not observed'): self.prepare()
        self.assertEqual(self.receipt()['phase'], 'dispatch_acknowledged')
        self.assertEqual(sum(args[4].endswith('/dispatches') for args in self.fake.mutations() if args[0] == 'gh'), 1)

    def test_late_push_visibility_is_observed_before_cancel_and_dispatch(self):
        self.fake.empty_push_reads = 2
        self.prepare()
        self.assertEqual(self.receipt()['cancelled_run_ids'], [10])
        self.assertEqual(len(self.fake.mutations()), 3)

    def test_invisible_push_never_starts_duplicate_preparation(self):
        self.fake.empty_push_reads = 20
        with self.assertRaisesRegex(RuntimeError, 'not visible yet'):
            self.prepare()
        self.assertFalse(self.fake.dispatched)
        self.assertEqual(len(self.fake.mutations()), 1)


if __name__ == '__main__':
    unittest.main()

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

    def test_push_is_observed_without_cancellation_or_second_workflow(self):
        result = self.prepare()
        self.assertEqual(result['run_id'], 10)
        self.assertTrue(result['automatic_staging'])
        self.assertFalse(result['deployed'])
        self.assertEqual(len(self.fake.mutations()), 1)
        self.assertEqual(self.fake.mutations()[0][:2], ['git', 'push'])
        with self.assertRaisesRegex(RuntimeError, 'Prior intent'):
            self.prepare()
        self.assertEqual(len(self.fake.mutations()), 1)

    def test_runtime_dirty_source_and_nonmain_refuse_before_mutation(self):
        for status, branch in [('?? tools/new.py\0', 'main'), (' M AGENTS.md\0', 'main'), ('', 'topic')]:
            self.fake.status, self.fake.branch = status, branch
            with self.assertRaises(RuntimeError): self.prepare()
        self.assertEqual(self.fake.mutations(), [])

    def test_unknown_remote_main_is_not_cancelled_or_dispatched(self):
        self.fake.race = True
        with self.assertRaisesRegex(RuntimeError, 'Remote main changed'): self.prepare()
        self.assertFalse(self.fake.dispatched)
        self.assertEqual(len(self.fake.mutations()), 1)

    def test_explicit_retention_cannot_dispatch_duplicate_build(self):
        values = {'retain_native_sources_run': '42', 'retain_native_sources_commit': OTHER,
            'retain_native_sources_index_sha256': 'c'*64}
        with self.assertRaisesRegex(RuntimeError, 'repository native-retention'):
            tool.prepare('platform', SHA, root=self.root, retain_native_sources=values)
        self.assertEqual(self.fake.mutations(), [])

    def test_late_push_visibility_is_observed_without_resubmitting(self):
        self.fake.empty_push_reads = 2
        self.prepare()
        self.assertEqual(len(self.fake.mutations()), 1)

    def test_invisible_push_keeps_intent_and_never_dispatches(self):
        self.fake.empty_push_reads = 20
        with self.assertRaisesRegex(RuntimeError, 'not visible yet'): self.prepare()
        self.assertFalse(self.fake.dispatched)
        self.assertEqual(len(self.fake.mutations()), 1)
        with self.assertRaisesRegex(RuntimeError, 'Prior intent'): self.prepare()


if __name__ == '__main__':
    unittest.main()

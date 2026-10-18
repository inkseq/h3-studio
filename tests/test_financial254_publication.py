"""Reject trust-boundary failures in the single financial hotfix lane."""

import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
import hashlib

from tools.financial254_publication import (
    BASE,
    BRANCH,
    REPOSITORY,
    WORKFLOW,
    binding,
    ci_proof,
    trusted_context,
    extract_verified_archive,
    validate_downloads,
    main,
)


class Financial254PublicationTests(unittest.TestCase):
    def setUp(self):
        self.commit = '1' * 40
        self.value = dict(
            version=1,
            enabled=True,
            issue=254,
            repository=REPOSITORY,
            source_branch=BRANCH,
            base_commit=BASE,
            source_commit=self.commit,
            source_tree='2' * 40,
            diff_sha256='3' * 64,
            changed_paths=['studio_platform/repository.py'],
        )
        self.env = dict(
            GITHUB_REPOSITORY=REPOSITORY,
            GITHUB_REF='refs/heads/main',
            GITHUB_EVENT_NAME='workflow_dispatch',
            GITHUB_SHA='4' * 40,
            GITHUB_WORKFLOW_SHA='4' * 40,
            GITHUB_WORKFLOW_REF=REPOSITORY + '/' + WORKFLOW + '@refs/heads/main',
        )
        self.run = dict(
            id=42,
            repository=dict(full_name=REPOSITORY),
            head_repository=dict(full_name=REPOSITORY),
            event='workflow_dispatch',
            path='.github/workflows/ci.yml',
            head_branch=BRANCH,
            head_sha=self.commit,
            status='completed',
            conclusion='success',
            run_attempt=1,
        )
        names = [
            'changes',
            'test',
            'frontend',
            'containers',
            'macos-helper (macos-15)',
            'macos-helper (macos-15-intel)',
        ]
        names += [
            f'{suite} ({i})' for suite in ('python', 'postgres') for i in range(6)
        ]
        rows = [
            dict(
                id=i + 1,
                run_id=42,
                head_sha=self.commit,
                run_attempt=1,
                name=n,
                status='completed',
                conclusion='success',
            )
            for i, n in enumerate(names)
        ]
        rows.extend(
            dict(
                id=i,
                run_id=42,
                head_sha=self.commit,
                run_attempt=1,
                name=name,
                status='completed',
                conclusion='skipped',
            )
            for i, name in [(99, 'publish-aws'), (100, 'frontend-tools')]
        )
        self.jobs = [dict(total_count=len(rows), jobs=rows)]
        items = [
            dict(
                id=i + 100,
                name=f'sixnine-{kind}-{self.commit}',
                expired=False,
                size_in_bytes=1024,
                digest='sha256:' + '5' * 64,
                workflow_run=dict(id=42, head_sha=self.commit, head_branch=BRANCH),
            )
            for i, kind in enumerate(('platform', 'migration', 'frontend'))
        ]
        self.artifacts = [dict(total_count=3, artifacts=items)]

    def check(self):
        return ci_proof(self.value, self.run, self.jobs, self.artifacts, 42)

    def test_real_branch_provenance_is_not_labeled_main(self):
        trusted_context(self.env)
        proof = self.check()
        self.assertEqual(proof['source_branch'], BRANCH)
        self.assertEqual(proof['run_id'], 42)
        self.assertIn('not_main_source', proof['attribution'])

    def test_fork_branch_workflow_or_unfinished_run_refused(self):
        for field, value in [
            ('head_repository', {'full_name': 'other/fork'}),
            ('head_branch', 'main'),
            ('head_sha', '6' * 40),
            ('path', '.github/workflows/other.yml'),
            ('event', 'push'),
            ('conclusion', 'failure'),
            ('status', 'in_progress'),
        ]:
            with self.subTest(field=field):
                run = copy.deepcopy(self.run)
                run[field] = value
                with self.assertRaises(ValueError):
                    ci_proof(self.value, run, self.jobs, self.artifacts, 42)

    def test_partial_missing_failed_duplicate_or_split_job_refused(self):
        variants = []
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'].pop()
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'].pop(0)
        x[0]['total_count'] -= 1
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'][3]['conclusion'] = 'cancelled'
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'][3]['name'] = 'test'
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'][3]['head_sha'] = '7' * 40
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'][3]['run_attempt'] = 2
        variants.append(x)
        x = copy.deepcopy(self.jobs)
        x[0]['jobs'][-2]['conclusion'] = 'success'
        variants.append(x)
        for jobs in variants:
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                ci_proof(self.value, self.run, jobs, self.artifacts, 42)

    def test_complete_multi_page_job_list_accepted(self):
        rows = self.jobs[0]['jobs']
        pages = [
            dict(total_count=len(rows), jobs=rows[:9]),
            dict(total_count=len(rows), jobs=rows[9:]),
        ]
        self.assertEqual(
            ci_proof(self.value, self.run, pages, self.artifacts, 42)['run_id'], 42
        )

    def test_missing_expired_duplicate_or_split_artifact_refused(self):
        variants = []
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'].pop()
        variants.append(x)
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'][0]['expired'] = True
        variants.append(x)
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'][0]['workflow_run']['head_sha'] = '8' * 40
        variants.append(x)
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'][0]['workflow_run']['id'] = 43
        variants.append(x)
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'][0]['name'] = 'other'
        variants.append(x)
        x = copy.deepcopy(self.artifacts)
        x[0]['artifacts'].append(copy.deepcopy(x[0]['artifacts'][0]))
        x[0]['total_count'] = 4
        variants.append(x)
        for artifacts in variants:
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                ci_proof(self.value, self.run, self.jobs, artifacts, 42)

    def test_non_main_or_non_trusted_publication_context_refused(self):
        for field, value in [
            ('GITHUB_REF', 'refs/heads/' + BRANCH),
            ('GITHUB_REPOSITORY', 'other/fork'),
            ('GITHUB_EVENT_NAME', 'push'),
            ('GITHUB_WORKFLOW_SHA', '9' * 40),
            (
                'GITHUB_WORKFLOW_REF',
                REPOSITORY + '/' + WORKFLOW + '@refs/heads/' + BRANCH,
            ),
        ]:
            env = self.env | {field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                trusted_context(env)

    def test_binding_is_fail_closed_and_paths_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'binding.json'
            path.write_text(json.dumps(self.value))
            self.assertEqual(binding(path), self.value)
            for patch in [
                dict(enabled=False),
                dict(source_commit='UNBOUND'),
                dict(changed_paths=[]),
                dict(changed_paths=['../secret']),
                dict(base_commit='0' * 40),
                dict(extra=True),
            ]:
                path.write_text(json.dumps(self.value | patch))
                with self.subTest(patch=patch), self.assertRaises(ValueError):
                    binding(path)

    def test_archive_digest_and_traversal_checked_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'artifact.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('image.tar.gz', b'test bytes')
            digest = 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                extract_verified_archive(archive, root / 'wrong', 'sha256:' + '0' * 64)
            self.assertFalse((root / 'wrong').exists())
            extract_verified_archive(archive, root / 'ok', digest)
            self.assertEqual((root / 'ok/image.tar.gz').read_bytes(), b'test bytes')
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../outside', b'bad')
            digest = 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                extract_verified_archive(archive, root / 'bad', digest)
            self.assertFalse((root / 'bad').exists())

    def test_changed_consumed_artifact_is_refused_before_any_validator_or_publisher(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind in self.check()['artifacts']:
                (root / (kind + '.verified-artifact.zip')).write_bytes(b'changed')
            with patch('tools.check_platform_bundle.verify') as verify:
                with self.assertRaisesRegex(ValueError, 'consumed_artifact_split'):
                    validate_downloads(self.check(), root)
                verify.assert_not_called()

    def test_candidate_preflight_failure_makes_no_publisher_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, value in [
                ('run', self.run),
                ('jobs', self.jobs),
                ('artifacts', self.artifacts),
            ]:
                p = Path(tmp) / name
                p.write_text(json.dumps(value))
                paths.extend(['--' + name, str(p)])
            with (
                patch.dict('os.environ', self.env),
                patch(
                    'tools.financial254_publication.binding', return_value=self.value
                ),
                patch('tools.financial254_publication.source_proof'),
                patch(
                    'tools.financial254_publication.validate_downloads',
                    side_effect=ValueError('invalid bytes'),
                ),
                patch('tools.publish_staging_candidate.publish') as publish,
            ):
                with self.assertRaisesRegex(ValueError, 'invalid bytes'):
                    main(['candidate', '--run-id', '42', '--source', tmp, *paths])
                publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()

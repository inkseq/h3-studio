"""CPU advisory inspection: no GPU, cloud calls, generation retries or charging."""

import copy
from dataclasses import replace
import json
import subprocess
import unittest
from unittest.mock import patch
from sqlalchemy import select

from studio_platform.subtitle_quality import (
    assess,
    dialogue_lines,
    dialogue_digests,
    evidence,
    public_quality,
    validate_quality,
)
from studio_platform.runtime_hosts.subtitle_ocr import inspect_video
from studio_platform.queue import TaskQueue
from studio_platform.repository import artifacts
from test_platform_repository import LedgerCase
import test_platform_wangp_delivery as delivery_fixtures
import test_storyboard_versions as version_fixtures


class SubtitleQualityTests(unittest.TestCase):
    def samples(
        self, *, text='今天我们一起回家', box=(0.2, 0.83, 0.8, 0.91), motion=0.04
    ):
        return [
            {'time': t, 'motion': motion, 'texts': [(box, text, 0.96)]}
            for t in (0, 0.5, 1, 1.5)
        ]

    def test_dialogue_tags_and_unicode_without_public_prompt(self):
        lines = dialogue_lines(
            'A sign says welcome. (S1) <d>[Chinese] 今天我们一起回家。</d> '
            '<d>[English] Let’s go home!</d>'
        )
        self.assertEqual(lines, ['今天我们一起回家', 'lets go home'.replace(' ', '')])
        self.assertEqual(dialogue_lines('No spoken line'), [])
        self.assertEqual(dialogue_lines('<d>好</d>'), [])
        result = assess(self.samples(), dialogue_digests('<d>今天我们一起回家</d>'))
        self.assertEqual(result['quality_flags'], ['burned_subtitles'])
        self.assertEqual(
            result['quality_checks']['burned_subtitles']['times_s'], [0, 0.5, 1, 1.5]
        )
        self.assertNotIn('今天', json.dumps(result, ensure_ascii=False))
        validate_quality(result, 2)

    def test_signs_motion_hold_and_matching_negative_controls(self):
        cases = [
            self.samples(text='欢迎光临商店'),
            self.samples(motion=0),
            self.samples(box=(0.01, 0.83, 0.21, 0.91)),
            self.samples(box=(0.2, 0.4, 0.8, 0.48)),
            self.samples()[:2],
        ]
        moving = self.samples()
        for i, sample in enumerate(moving):
            sample['texts'] = [
                (
                    (0.15 + i * 0.05, 0.83, 0.65 + i * 0.05, 0.91),
                    '今天我们一起回家',
                    0.96,
                )
            ]
        cases.append(moving)
        for samples in cases:
            with self.subTest(samples=samples):
                self.assertEqual(
                    assess(samples, dialogue_digests('<d>今天我们一起回家</d>')),
                    evidence('checked'),
                )

    def test_strict_public_evidence_rejects_unbounded_or_private_payload(self):
        good = evidence('checked', confidence=0.95, times=[0, 0.5, 1])
        bad = []
        for key, value in [
            ('confidence', float('nan')),
            ('confidence', True),
            ('times_s', [0, 0.5, float('inf')]),
            ('times_s', [0, 0.5, 5]),
            ('times_s', [0, 0, 1]),
            ('times_s', [1, 0.5, 0]),
            ('times_s', [False, 0.5, 1]),
            ('status', []),
            ('text', 'private'),
        ]:
            item = copy.deepcopy(good)
            item['quality_checks']['burned_subtitles'][key] = value
            bad.append(item)
        bad.extend([{'quality_flags': []}, {**good, 'prompt': 'private'}])
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate_quality(item, 2)
        self.assertEqual(public_quality({}), evidence('not_checked'))

    def test_child_timeout_failure_and_absent_dependencies_are_advisory(self):
        with patch('subprocess.run') as run:
            self.assertEqual(inspect_video('test.mp4', [], 5), evidence('no_dialogue'))
            run.assert_not_called()
        for outcome, expected in [
            (subprocess.TimeoutExpired('ocr', 45), 'timeout'),
            (OSError('private local path'), 'failed'),
            (subprocess.CompletedProcess([], 1, ''), 'failed'),
            (subprocess.CompletedProcess([], 0, 'not json'), 'failed'),
            (
                subprocess.CompletedProcess([], 0, json.dumps(evidence('unavailable'))),
                'unavailable',
            ),
        ]:
            with self.subTest(expected=expected):
                kwargs = (
                    {'side_effect': outcome}
                    if isinstance(outcome, Exception)
                    else {'return_value': outcome}
                )
                with patch('subprocess.run', **kwargs) as run:
                    self.assertEqual(
                        inspect_video('test.mp4', ['hello'], 5), evidence(expected)
                    )
                    self.assertEqual(run.call_args.kwargs['timeout'], 45)
                    self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)


class SubtitleDeliveryTests(unittest.TestCase):
    setUp = delivery_fixtures.DeliveryTests.setUp
    export = delivery_fixtures.DeliveryTests.export
    upload = delivery_fixtures.DeliveryTests.upload
    prepare = delivery_fixtures.DeliveryTests.prepare

    def test_private_context_keeps_only_hashes_and_legacy_replay_stays_legacy(self):
        from studio_platform.inference.wangp_contract import canonical_json

        prepared = replace(
            self.prepared,
            job_id='second-job',
            attempt_tag='second-attempt',
            settings_json=canonical_json(
                {'prompt': '<d>[Chinese] 今天我们一起回家</d>'}
            ),
        )
        self.journal.claim(prepared, 'new-incarnation')
        self.journal.save_delivery_context(prepared)
        context = self.journal.delivery_context(prepared.operation_id)
        self.assertEqual(
            context['dialogue_digests'], dialogue_digests('<d>今天我们一起回家</d>')
        )
        self.assertNotIn('今天', json.dumps(context, ensure_ascii=False))
        with self.journal._connection() as db:
            raw = json.loads(
                db.execute(
                    'SELECT receipt FROM operations WHERE operation_id=?',
                    (prepared.operation_id,),
                ).fetchone()[0]
            )
            raw['_delivery_context'].pop('dialogue_digests')
            db.execute(
                'UPDATE operations SET receipt=? WHERE operation_id=?',
                (json.dumps(raw), prepared.operation_id),
            )
        self.journal.save_delivery_context(prepared)
        self.assertNotIn(
            'dialogue_digests', self.journal.delivery_context(prepared.operation_id)
        )

    def test_warning_is_sealed_once_and_prepare_replay_does_not_run_ocr(self):
        warning = evidence('checked', confidence=0.94, times=[0, 0.5, 1])
        with patch(
            'studio_platform.runtime_hosts.subtitle_ocr.inspect_video',
            return_value=warning,
        ) as ocr:
            first = self.prepare()
            second = self.prepare()
            self.assertEqual(first, second)
            ocr.assert_called_once()
        self.assertEqual(first['state'], 'prepared')
        self.assertEqual(
            first['roles']['video']['evidence']['quality_flags'], ['burned_subtitles']
        )
        self.assertNotIn('quality_flags', first['roles']['audio']['evidence'])
        self.assertEqual(self.exports, 1)
        self.assertEqual(self.host.inspect(self.prepared.operation_id), self.original)

    def test_detector_bug_never_loses_valid_export_or_resubmits(self):
        with patch(
            'studio_platform.runtime_hosts.subtitle_ocr.inspect_video',
            side_effect=RuntimeError('bug'),
        ):
            result = self.prepare()
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual(
            result['roles']['video']['evidence']['quality_checks'],
            evidence('failed')['quality_checks'],
        )
        self.assertEqual(self.exports, 1)


class SubtitleCommitTests(LedgerCase):
    def test_ledger_preserves_warning_without_cost_or_job_retry(self):
        self.job(cost=0)
        queue = TaskQueue(self.repo)
        lease = queue.claim('worker', 'test-pool').lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, 'native-task')
        queue.begin_collection(lease)
        spec = {
            'kind': 'video',
            'object_key': 'test/video.mp4',
            'size_bytes': 42,
            'sha256': 'a' * 64,
            'validated': True,
            'duration_s': 5,
            **evidence('checked', confidence=0.93, times=[0, 0.5, 1]),
        }
        for bad in [{**spec, 'kind': 'audio'}, {**spec, 'quality_checks': {}}]:
            with self.assertRaises(ValueError):
                queue.complete(lease, [bad], actual_cost_microusd=0)
        result = queue.complete(lease, [spec], actual_cost_microusd=0)
        self.assertEqual(result['status'], 'succeeded')
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(artifacts.c.metadata)).scalar_one(), spec
            )


class SubtitleVersionTests(unittest.TestCase):
    setUp = version_fixtures.StoryboardVersionTests.setUp
    login = version_fixtures.StoryboardVersionTests.login
    setup_project = version_fixtures.StoryboardVersionTests.setup_project
    key = version_fixtures.StoryboardVersionTests.key
    edit = version_fixtures.StoryboardVersionTests.edit
    source = version_fixtures.StoryboardVersionTests.source
    configure = version_fixtures.StoryboardVersionTests.configure
    prepare = version_fixtures.StoryboardVersionTests.prepare
    preflight = version_fixtures.StoryboardVersionTests.preflight
    completed = version_fixtures.StoryboardVersionTests.completed
    current = version_fixtures.StoryboardVersionTests.current

    def test_versions_expose_warning_and_legacy_not_checked(self):
        from sqlalchemy import update

        self.prepare()
        plan = self.preflight(copies=1).json()['items'][0]['plan_id']
        _, artifact = self.completed(plan)
        path = '/v1/projects/story-one/shots/shot-one/versions'
        old = self.client.get(path).json()['versions'][0]
        self.assertEqual(
            old['quality_checks'], evidence('not_checked')['quality_checks']
        )
        warning = evidence('checked', confidence=0.92, times=[0, 0.5, 1])
        with self.app.state.repository.engine.begin() as conn:
            metadata = conn.execute(
                select(artifacts.c.metadata).where(artifacts.c.id == artifact)
            ).scalar_one()
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == artifact)
                .values(metadata={**metadata, **warning})
            )
        new = self.client.get(path).json()['versions'][0]
        self.assertEqual(new['quality_flags'], ['burned_subtitles'])
        self.assertEqual(new['quality_checks'], warning['quality_checks'])


if __name__ == '__main__':
    unittest.main()

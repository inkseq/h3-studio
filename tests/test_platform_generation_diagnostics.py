"""Specific safe diagnostics at preflight and public standalone helper seams."""

import importlib.util
from pathlib import Path
import unittest
import httpx
import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import select, update
import test_platform_repository as ledger

from studio_platform.generation_diagnostics import (
    preflight_diagnostic,
    WANGP_VALIDATION_CODES,
)


def failure_request():
    from studio_platform.runtime_catalog import PROFILE_IDS

    return {
        'deployment_profile_id': PROFILE_IDS[-1],
        'request': {
            'prompt': 'PRIVATE prompt https://secret.invalid',
            'mode': 'ref',
            'duration': 7,
            'resolution': '768P',
            'aspect_ratio': '16:9',
            'seed': '123',
            'steps': 40,
            'sampler_name': 'er_sde',
            'denoise': 0.75,
            'shift_video': 9,
            'shift_audio': 3,
            'generate_audio': True,
            'export_crf': 18,
            'video_conditioning': 'denoise',
            'audio_conditioning': 'soundtrack',
            'image_refs_relative_size': 80,
            'audio_refinement': 'enabled',
            'mask_mode': 'shared_timestep',
            'inputs': {
                'images': ['image-PRIVATE'],
                'videos': ['video-PRIVATE'],
                'audios': ['audio-PRIVATE'],
                'first_frame': 'image-PRIVATE',
                'last_frame': None,
            },
            'video_audio': {'video-PRIVATE': True},
            'guides': [
                {'media_id': 'video-PRIVATE', 'time_seconds': 0, 'use_audio': True}
            ],
            'video_excerpts': [{'time_seconds': 2, 'duration_seconds': 3}],
        },
        'output_spec': {'width': 1344, 'height': 768, 'frames': 175, 'fps': 24},
        'assets': {
            'image-PRIVATE': {'metadata': {'kind': 'image', 'path': 'PRIVATE'}},
            'video-PRIVATE': {
                'object_key': 'PRIVATE',
                'metadata': {'kind': 'video', 'duration': 3, 'source_duration': 999},
            },
            'audio-PRIVATE': {'metadata': {'kind': 'audio', 'duration': 2}},
        },
    }


spec = importlib.util.spec_from_file_location(
    'diagnostic_public_helper',
    Path(__file__).resolve().parents[1] / 'skills/inkseq/scripts/sixnine.py',
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class DiagnosticTests(unittest.TestCase):
    def test_runtime_summary_uses_selected_metadata_and_closed_values_only(self):
        from studio_platform.generation_diagnostics import (
            safe_failure_summary,
            failure_log_context,
        )
        from studio_platform.telemetry import _context

        job = {
            'request': failure_request(),
            'execution_plan': {},
            'error_code': 'wangp_generation_cuda_out_of_memory',
        }
        original = copy.deepcopy(job)
        summary = safe_failure_summary(
            job,
            {
                'machine_id': '19492',
                'provider': 'vast',
                # Fixed: a random UUID can contain the '999' sentinel below.
                'node_id': '00000000-0000-4000-8000-000000000001',
                'offer_id': '123',
                'provider_instance_id': '456',
                'url': 'PRIVATE',
            },
        )
        self.assertEqual(summary['stage'], 'generate')
        self.assertEqual(summary['inputs']['videos']['total_duration_seconds'], 3)
        self.assertEqual(summary['inputs']['audios']['total_duration_seconds'], 2)
        self.assertEqual(
            summary['inputs']['guides'],
            [{'kind': 'video', 'time_seconds': 0, 'use_audio': True}],
        )
        self.assertEqual(summary['controls']['mask_mode'], 'shared_timestep')
        self.assertEqual(summary['controls']['sampler_name'], 'er_sde')
        self.assertEqual(summary['output']['duration_s'], 7)
        self.assertEqual(summary['host']['machine_id'], '19492')
        self.assertFalse(summary['automatic_generation_retry'])
        self.assertNotIn('PRIVATE', json.dumps(summary))
        self.assertNotIn('999', json.dumps(summary))
        self.assertEqual(job, original)
        context = _context(
            {
                **failure_log_context(job),
                'sampler_name': 'PRIVATE',
                'guide_kinds': ['PRIVATE'],
                'guide_times_s': [float('nan')],
                'seed': 'PRIVATE',
                'prompt': 'PRIVATE',
            }
        )
        self.assertNotIn('PRIVATE', json.dumps(context))
        self.assertNotIn('guide_times_s', context)
        self.assertEqual(context['video_duration_s'], 3)
        self.assertEqual(context['video_ref_durations_s'], [3])

    def test_preflight_preserves_known_codes_and_discards_private_or_dynamic_text(self):
        for code in WANGP_VALIDATION_CODES:
            result = preflight_diagnostic(ValueError(code))
            self.assertEqual(result['error_code'], code)
            self.assertEqual(result['error_stage'], 'preflight')
        for text in (
            'wangp_private_token_SECRET',
            'PRIVATE /media/a.png https://x/?token=SECRET',
            'wangp_invalid_inputs SECRET',
        ):
            result = preflight_diagnostic(ValueError(text))
            self.assertEqual(result['error_code'], 'preflight_rejected')
            self.assertNotIn(text, str(result))
        result = preflight_diagnostic(
            ValueError('首尾帧配方不能混用全能参考；请明确切换配方或解除关联')
        )
        self.assertEqual(result['error_code'], 'incompatible_fl_ref_inputs')

    def test_helper_http_failure_exposes_only_known_code_and_stage_and_never_retries(
        self,
    ):
        for code in (
            'wangp_adapter_ref_first_last_unmapped',
            'wangp_generation_cuda_out_of_memory',
            'wangp_invalid_excerpts',
            'wangp_selected_video_has_no_audio',
            'incompatible_fl_ref_inputs',
            'wangp_private_token_SECRET',
        ):
            response = httpx.Response(
                422,
                json={
                    'code': code,
                    'error_stage': 'preflight',
                    'message': 'SECRET private prompt /media/file https://x/?token=SECRET',
                    'token': 'SECRET',
                },
            )
            with self.assertRaises(ValueError) as error:
                helper.check_response(response)
            text = str(error.exception)
            self.assertIn('HTTP 422', text)
            self.assertNotIn('SECRET', text)
            if 'private_token' not in code:
                self.assertIn('code=' + code, text)
                self.assertIn('stage=preflight', text)
        for body in ('PRIVATE SECRET', '[' * 20000):
            with self.assertRaises(ValueError) as error:
                helper.check_response(httpx.Response(500, text=body))
            self.assertNotIn(body, str(error.exception))
        with self.assertRaisesRegex(ValueError, 'HTTP 503'):
            helper.check_response(
                httpx.Response(503, stream=httpx.ByteStream(b'PRIVATE SECRET'))
            )

    def test_business_api_preflight_uses_static_diagnostic_projection(self):
        import asyncio, json, tempfile
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from starlette.requests import Request

        with tempfile.TemporaryDirectory(prefix='sixnine-diagnostic-') as folder:
            app = create_app(
                Settings(
                    data_dir=Path(folder),
                    auth_mode='local-test',
                    generation_enabled=False,
                )
            )
            request = Request(
                {
                    'type': 'http',
                    'path': '/v1/generations/preflight',
                    'headers': [],
                    'scheme': 'http',
                    'server': ('localhost', 8899),
                }
            )
            handler = app.exception_handlers[ValueError]
            known = asyncio.run(handler(request, ValueError('wangp_invalid_inputs')))
            self.assertEqual(
                json.loads(known.body)['error_code'], 'wangp_invalid_inputs'
            )
            private = asyncio.run(
                handler(request, ValueError('PRIVATE SECRET /media/a.png'))
            )
            self.assertNotIn('SECRET', private.body.decode())
            self.assertEqual(private.status_code, 422)
            app.state.repository.close()


class FailureNoticeTests(ledger.LedgerCase):
    def make_job(self, scope=None):
        scope = scope or self.scope
        request = failure_request()
        plan = self.repo.create_plan(
            scope,
            request,
            {'pool': 'test-pool', 'backend': 'wangp-worker', 'enabled': True},
            expires_at=self.now + 1000,
            estimated_cost_microusd=100000 if scope == self.scope else 0,
        )
        return self.repo.create_job(
            scope,
            plan['id'],
            uuid.uuid4().hex,
            budget_account_ids=['owner-budget'] if scope == self.scope else [],
        )

    def fail_job(self, job, code='wangp_generation_cuda_out_of_memory'):
        from studio_platform.repository import jobs

        with self.repo.transaction() as connection:
            self.repo._job(connection, job['id'], lock=True)
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(
                    status='failed',
                    error_code=code,
                    result={'actual_cost_microusd': None, 'billing_status': 'pending'},
                )
            )
            self.repo._emit(
                connection,
                'job.failed',
                job['id'],
                {
                    'job_id': job['id'],
                    'failure_context': {
                        'machine_id': '19492',
                        'provider': 'vast',
                        'url': 'PRIVATE',
                    },
                },
            )

    def failures(self):
        from studio_platform.repository import outbox

        with self.repo.engine.connect() as connection:
            return list(
                connection.execute(
                    select(outbox)
                    .where(outbox.c.event_type == 'job.failed')
                    .order_by(outbox.c.job_event_seq)
                ).mappings()
            )

    def test_concurrent_same_kind_deduplicates_notice_not_failures_and_owner_isolated(
        self,
    ):
        from studio_platform.repository import outbox
        from studio_platform.auth import Principal
        from studio_platform.job_events import JobEvents
        from studio_platform.generation_services import GenerationAccess, GenerationRead

        own = [self.make_job(), self.make_job()]
        barrier = threading.Barrier(2)

        def fail(job):
            barrier.wait(timeout=5)
            self.fail_job(job)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(fail, own))
        foreign = self.make_job(self.other)
        self.fail_job(foreign)
        records = self.failures()
        self.assertEqual(len(records), 3)
        notices = [row['payload']['public_state']['failure_notice'] for row in records]
        self.assertEqual([n['new_kind'] for n in notices], [True, False, True])
        self.assertEqual(notices[0]['id'], notices[1]['id'])
        self.assertNotEqual(notices[0]['id'], notices[2]['id'])
        self.assertEqual(records[0]['id'], notices[0]['id'])
        self.assertEqual(len({row['id'] for row in records}), 3)
        original_ids = [row['id'] for row in records]
        self.fail_job(self.make_job(), 'wangp_generation_media_decode_failed')
        self.assertEqual([row['id'] for row in self.failures()[:3]], original_ids)
        feed = JobEvents(self.repo, self.scope.tenant_id).read(
            Principal('superdan', 'browser')
        )
        failed = [row for row in feed['events'] if row['event_type'] == 'job.failed']
        self.assertEqual(len(failed), 3)
        self.assertEqual(sum(row['failure_notice']['new_kind'] for row in failed), 2)
        self.assertNotIn('PRIVATE', json.dumps(failed))
        row = self.repo.get_job(self.scope, own[0]['id'])
        read = GenerationRead(
            self.repo, GenerationAccess(self.repo, self.scope.tenant_id)
        )
        visible = read.public_job(row)
        self.assertEqual(visible['failure_diagnostic']['host']['machine_id'], '19492')
        self.assertEqual(visible['failure_notice'], {'id': notices[0]['id']})
        self.assertEqual(row['result']['billing_status'], 'pending')
        self.assertIsNone(row['result']['actual_cost_microusd'])
        with self.repo.engine.connect() as connection:
            self.assertTrue(
                all(
                    row['job_event_seq'] is not None
                    for row in connection.execute(
                        select(outbox).where(outbox.c.event_type == 'job.failed')
                    ).mappings()
                )
            )

    def test_rollback_does_not_consume_first_kind_or_touch_original_request(self):
        job = self.make_job()
        original = self.repo.get_job(self.scope, job['id'])
        sequence = self.repo._sequence_job_events

        def interrupted(connection):
            sequence(connection)
            raise RuntimeError('rollback-after-notice')

        with patch.object(self.repo, '_sequence_job_events', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'rollback-after-notice'):
                self.fail_job(job)
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.repo.get_job(self.scope, job['id']), original)
        self.fail_job(job)
        row = self.failures()[0]
        self.assertTrue(row['payload']['public_state']['failure_notice']['new_kind'])
        self.assertEqual(
            self.repo.get_job(self.scope, job['id'])['request'], original['request']
        )

    def test_real_worker_failure_logs_controls_and_preserves_one_attempt(self):
        from studio_platform.worker import WorkerRunner, Outcome
        from studio_platform.storage import LocalObjectStore
        from studio_platform.telemetry import StageTelemetry
        from test_platform_telemetry import RecordingLogger, RecordingMeter
        from opentelemetry.trace import NonRecordingSpan, SpanContext

        class Backend:
            kind, enabled, slot_key = 'wangp-worker', True, 'diagnostic-only-test'

            def prepare(self, *_args):
                return {}

            def submit(self, *_args):
                return 'original-task'

            def poll(self, *_args):
                return Outcome(
                    'failed',
                    'original-task',
                    None,
                    'wangp_generation_cuda_out_of_memory',
                )

        logger = RecordingLogger()
        tracer = type(
            'Tracer',
            (),
            {
                'start_span': lambda *_args, **_kwargs: NonRecordingSpan(
                    SpanContext(1, 2, False)
                )
            },
        )()
        telemetry = StageTelemetry(logger=logger, tracer=tracer, meter=RecordingMeter())
        job = self.make_job()
        runner = WorkerRunner(
            self.repo,
            LocalObjectStore(Path(self.temp.name) / 'objects'),
            Path(self.temp.name) / 'worker',
            backend=Backend(),
            submission_guard=lambda _job: True,
            telemetry=telemetry,
            telemetry_context={'provider': 'vast', 'machine_id': '19492'},
        )
        self.assertEqual(runner.run_once('worker', 'test-pool')['state'], 'failed')
        row = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(row['attempt_no'], 1)
        self.assertEqual(
            row['result']['failure_diagnostic']['host']['machine_id'], '19492'
        )
        self.assertEqual(
            row['result']['failure_diagnostic']['controls']['sampler_name'], 'er_sde'
        )
        self.assertIsNone(row['result']['actual_cost_microusd'])
        failures = [
            e
            for e in logger.events
            if e.get('event') == 'stage_finished' and e.get('outcome') == 'failure'
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['video_duration_s'], 3)
        self.assertEqual(failures[0]['machine_id'], '19492')
        self.assertNotIn('PRIVATE', json.dumps(logger.events))

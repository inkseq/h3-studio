import test from 'node:test';
import assert from 'node:assert/strict';
import { jobMessage, capacityWaitMessage } from './cloud-model.js';

const CODES = [
  'capacity_no_matching_gpu',
  'capacity_inventory_check_failed',
  'capacity_rental_reconciliation',
  'capacity_configuration_unavailable',
  'capacity_bootstrap_repair_required',
  'capacity_queued_task_repair_required',
  'capacity_budget_or_limit',
  'capacity_gpu_starting',
  'capacity_provider_preparing',
  'capacity_provider_configuring_ssh',
  'capacity_provider_preparation_failed',
  'capacity_provider_preparation_timeout',
  'capacity_provider_preparation_retry_limit',
  'capacity_gpu_busy',
  'capacity_searching_gpu',
  'unrecognized',
];

test('the wait message never names machines, providers, models or budgets', () => {
  for (const locale of ['zh', 'en'])
    for (const error_code of CODES) {
      const job = {
        status: 'waiting_capacity',
        error_code,
        message: 'provider-payload-must-not-be-displayed',
        phase: 'loading_model',
      };
      const before = structuredClone(job),
        message = capacityWaitMessage(job, locale);
      assert.ok(message.length > 10, error_code);
      assert.equal(jobMessage(job, locale), message);
      assert.doesNotMatch(
        message,
        /GPU|显存|机器|租|供应商|模型|provider|machine|rental|VRAM|model|SSH|capacity_|provider-payload/i,
      );
      assert.deepEqual(job, before);
    }
});

test('the wait message shows only while a job waits', () => {
  const held = { status: 'queued', error_code: 'capacity_queued_task_repair_required' };
  assert.match(jobMessage(held), /任务已保留/);
  assert.equal(capacityWaitMessage({ ...held, status: 'failed' }), '');
  for (const status of ['succeeded', 'failed', 'cancelled', 'running'])
    assert.equal(capacityWaitMessage({ status, error_code: 'capacity_no_matching_gpu' }), '');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { jobMessage, capacityWaitMessage } from './cloud-model.js';

test('capacity wait explains inventory, billing uncertainty and preparation separately', () => {
  const waiting = (error_code) => ({ status: 'waiting_capacity', error_code });
  assert.match(jobMessage(waiting('capacity_no_matching_gpu')), /没有符合.*可用 GPU/);
  assert.match(jobMessage(waiting('capacity_inventory_check_failed')), /无法确认 GPU 库存/);
  assert.match(jobMessage(waiting('capacity_rental_reconciliation')), /暂停再次租机/);
  assert.match(jobMessage(waiting('capacity_gpu_starting')), /加载模型/);
  assert.equal(
    jobMessage(waiting('capacity_bootstrap_repair_required')),
    'GPU 环境准备失败，正在修复；原任务已保留，无需重新提交。',
  );
  assert.equal(
    capacityWaitMessage(waiting('capacity_bootstrap_repair_required')),
    'GPU 环境准备失败，正在修复；原任务已保留，无需重新提交。',
  );
  assert.match(jobMessage(waiting('capacity_budget_or_limit')), /不会自动提高预算/);
  assert.match(jobMessage(waiting('unrecognized')), /等待计算容量/);
  assert.equal(
    capacityWaitMessage({ status: 'succeeded', error_code: 'capacity_no_matching_gpu' }),
    '',
  );
  const held = { status: 'queued', error_code: 'capacity_queued_task_repair_required' };
  assert.match(jobMessage(held), /暂停接新任务.*任务仍保留/);
  assert.equal(capacityWaitMessage({ ...held, status: 'failed' }), '');
});

test('provider preparation phases preserve the original job without promising model progress or replacement', () => {
  const phases = [
    ['capacity_provider_preparing', /正在准备机器/, /尚未开始加载模型或生成/],
    ['capacity_provider_configuring_ssh', /正在配置远程连接/, /尚未开始加载模型或生成/],
    ['capacity_provider_preparation_failed', /准备机器失败/, /机器回收与费用仍需核对/],
    ['capacity_provider_preparation_timeout', /准备机器超时/, /机器回收与费用仍需核对/],
    ['capacity_provider_preparation_retry_limit', /已暂停自动租机/, /等待管理员处理/],
  ];
  const messages = [];
  for (const [error_code, phase, detail] of phases) {
    const job = {
      id: 'original-job',
      status: 'waiting_capacity',
      error_code,
      message: 'provider-payload-must-not-be-displayed',
      phase: 'loading_model',
    };
    const before = structuredClone(job),
      message = jobMessage(job);
    assert.match(message, phase);
    assert.match(message, detail);
    assert.match(message, /原任务.*保留/);
    assert.match(message, /无需重新提交/);
    assert.doesNotMatch(
      message,
      /provider-payload|capacity_|正在加载模型|正在生成|自动更换|自动重试|自动重新租机/,
    );
    assert.equal(capacityWaitMessage(job), message);
    assert.deepEqual(job, before);
    for (const status of ['succeeded', 'failed', 'cancelled', 'running']) {
      assert.equal(capacityWaitMessage({ ...job, status }), '');
    }
    messages.push(message);
  }
  assert.equal(new Set(messages).size, phases.length);
});

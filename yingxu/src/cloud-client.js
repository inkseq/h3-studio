/* Same-origin application API only. This module never holds provider credentials. */
import { readLocale, translate } from './i18n/locale.js';
import { CREDITS_CHANGED, INSUFFICIENT_CREDITS, insufficientCredits } from './credits.js';
// The credits chip and the not-enough-credits prompt listen for these (h3-studio#306).
function announce(type, detail) {
  if (typeof globalThis.dispatchEvent === 'function' && typeof CustomEvent === 'function')
    globalThis.dispatchEvent(new CustomEvent(type, { detail }));
}
function uiLocale(account) {
  try {
    return readLocale(globalThis.localStorage, account);
  } catch {
    return 'en';
  }
}
const ui = (key, account, values) => translate(uiLocale(account), key, values);
function defaultSpendingConfirmation(detail, account) {
  return (
    typeof globalThis.confirm === 'function' &&
    globalThis.confirm(
      translate(uiLocale(account), 'errors.spendingConfirmationRequired', {
        amount: new Intl.NumberFormat('en-US', {
          style: 'currency',
          currency: 'USD',
          maximumFractionDigits: 6,
        }).format(detail.estimate_microusd / 1_000_000),
      }),
    )
  );
}
export class CloudError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = 'CloudError';
    this.status = status;
    this.detail = detail;
  }
}
export function safeCloudPath(path) {
  if (
    typeof path !== 'string' ||
    !/^\/(?:v1\/|api\/auth\/)/.test(path) ||
    path.includes('\\') ||
    /[\r\n]/.test(path)
  )
    throw new CloudError(ui('client.pathScope'));
  const parsed = new URL(path, 'https://studio.invalid');
  if (
    parsed.origin !== 'https://studio.invalid' ||
    parsed.username ||
    parsed.password ||
    parsed.hash ||
    !/^\/(?:v1\/|api\/auth\/)/.test(parsed.pathname)
  )
    throw new CloudError(ui('client.pathInvalid'));
  return parsed.pathname + parsed.search;
}

function abortError() {
  const error = new Error('Aborted');
  error.name = 'AbortError';
  return error;
}
function waitForRetry(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const stopped = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', stopped);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', stopped);
      resolve();
    }, ms);
    signal.addEventListener('abort', stopped, { once: true });
  });
}

function readAdmission(getAccount = () => null) {
  let active = 0,
    downloads = 0;
  const waiting = [];
  const room = (blob) => active < 4 && (!blob || downloads < 2);
  function begin(blob) {
    active++;
    if (blob) downloads++;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      active--;
      if (blob) downloads--;
      drain();
    };
  }
  function drain() {
    for (let i = 0; i < waiting.length;) {
      const entry = waiting[i];
      if (!room(entry.blob)) {
        i++;
        continue;
      }
      waiting.splice(i, 1);
      entry.signal.removeEventListener('abort', entry.cancel);
      entry.resolve(begin(entry.blob));
    }
  }
  return (blob, signal) => {
    if (signal.aborted) throw abortError();
    if (room(blob)) return begin(blob);
    if (waiting.length >= 128)
      throw new CloudError(ui('client.readQueue', getAccount()), { status: 429 });
    return new Promise((resolve, reject) => {
      const entry = {
        blob,
        signal,
        resolve,
        reject,
        cancel: () => {
          const at = waiting.indexOf(entry);
          if (at >= 0) waiting.splice(at, 1);
          signal.removeEventListener('abort', entry.cancel);
          reject(abortError());
        },
      };
      waiting.push(entry);
      signal.addEventListener('abort', entry.cancel, { once: true });
    });
  };
}

function retryDelay(response) {
  const raw = response.headers.get('Retry-After');
  if (!raw) return 3000;
  const seconds = /^\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : NaN;
  const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(raw) - Date.now();
  return Number.isFinite(delay) && delay >= 0 && delay <= 30000 ? delay : null;
}

export function createCloudClient({
  fetcher = (...args) => fetch(...args),
  onUnauthorized = () => {},
  timeoutMs = 30000,
  uploadTimeoutMs = 900000,
  downloadTimeoutMs = 300000,
  confirmSpending = defaultSpendingConfirmation,
} = {}) {
  let epoch = 0,
    account = null;
  const inflight = new Set(),
    admitRead = readAdmission(() => account);
  function reset() {
    epoch++;
    for (const controller of inflight) controller.abort();
    inflight.clear();
  }
  async function request(
    path,
    { method = 'GET', body, key, blob = false, anonymous = false, signal } = {},
  ) {
    const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
    const encodedBody = body !== undefined && !multipart ? JSON.stringify(body) : body;
    const target = safeCloudPath(path),
      current = epoch,
      expectedAccount = account,
      controller = new AbortController();
    inflight.add(controller);
    const cancelRead = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener('abort', cancelRead, { once: true });
    const headers = {
      ...(body !== undefined && !multipart ? { 'Content-Type': 'application/json' } : {}),
      ...(key ? { 'Idempotency-Key': key } : {}),
      ...(expectedAccount && !anonymous ? { 'X-Expected-Account': expectedAccount } : {}),
    };
    let timedOut = false,
      releaseRead = null;
    const duration = blob ? downloadTimeoutMs : multipart ? uploadTimeoutMs : timeoutMs,
      deadline = Date.now() + duration;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, duration);
    try {
      // Only reads are queued/retried. A write retains its original explicit
      // confirmation/idempotency behavior, including after a 429 or timeout.
      if (method === 'GET') {
        const slot = admitRead(blob, controller.signal);
        releaseRead = typeof slot === 'function' ? slot : await slot;
      }
      let spendingConfirmed = false;
      for (let attempt = 0; ; attempt++) {
        if (current !== epoch) throw new CloudError(ui('client.accountChanged', expectedAccount));
        if (controller.signal.aborted) throw abortError();
        const response = await fetcher(target, {
          method,
          headers,
          credentials: 'same-origin',
          cache: 'no-store',
          signal: controller.signal,
          ...(body !== undefined ? { body: encodedBody } : {}),
        });
        if (current !== epoch) throw new CloudError(ui('client.accountChanged', expectedAccount));
        const responseAccount = response.headers.get('X-Authenticated-Account');
        if (
          expectedAccount &&
          !anonymous &&
          responseAccount &&
          responseAccount !== expectedAccount
        ) {
          onUnauthorized();
          throw new CloudError(ui('client.identityChanged', expectedAccount), { status: 401 });
        }
        if (!response.ok) {
          let detail;
          try {
            detail = await response.json();
          } catch {
            detail = null;
          }
          if (detail?.code === 'account_context_changed') {
            onUnauthorized();
            throw new CloudError(ui('client.contextChanged', expectedAccount), { status: 401 });
          }
          if (response.status === 401 && !anonymous) onUnauthorized();
          const short = insufficientCredits(response.status, detail);
          if (short) announce(INSUFFICIENT_CREDITS, short);
          // This closed response proves admission refused before a reservation.
          // Only the human's explicit approval permits one exact-key retry.
          if (
            method === 'POST' &&
            response.status === 409 &&
            detail?.code === 'spending_confirmation_required' &&
            !spendingConfirmed &&
            typeof detail.plan_id === 'string' &&
            Number.isSafeInteger(detail.estimate_microusd) &&
            detail.estimate_microusd > 0
          ) {
            if (await confirmSpending(detail, expectedAccount)) {
              if (current !== epoch) throw abortError();
              if (controller.signal.aborted) throw abortError();
              await request('/v1/account/spending-confirmations', {
                method: 'POST',
                body: { plan_ids: detail.plan_ids || [detail.plan_id] },
                signal: controller.signal,
              });
              if (current !== epoch) throw abortError();
              spendingConfirmed = true;
              continue;
            }
          }
          if (method === 'GET' && response.status === 429 && attempt < 2) {
            const delay = retryDelay(response);
            if (delay !== null && Date.now() + delay < deadline) {
              await waitForRetry(delay, controller.signal);
              continue;
            }
          }
          const reason =
            response.status === 409 && detail?.detail === 'capacity_drain_repreflight'
              ? ui('client.capacityDrain', expectedAccount)
              : typeof detail?.detail === 'string'
                ? detail.detail
                : typeof detail?.message === 'string'
                  ? detail.message
                  : response.status === 409
                    ? ui('client.versionChanged', expectedAccount)
                    : response.status === 404
                      ? ui('client.notFound', expectedAccount)
                      : response.status === 401
                        ? ui('client.login', expectedAccount)
                        : ui('client.status', expectedAccount, { status: response.status });
          const budgetKey = [
            'user_daily_limit_reached',
            'connection_daily_limit_reached',
            'spending_usage_unconfirmed',
            'spending_settings_changed',
            'spending_settings_invalid',
            'admin_hourly_price_limit_reached',
            'dstack_test_runtime_limit_reached',
            'dstack_test_daily_limit_reached',
            'assistant_quote_unavailable',
            'spending_confirmation_consumed',
          ].includes(detail?.code)
            ? 'errors.' + detail.code
            : null;
          const localized = budgetKey
            ? translate(uiLocale(expectedAccount), budgetKey)
            : detail?.messages?.[uiLocale(expectedAccount)] || reason;
          throw new CloudError(localized, { status: response.status, detail });
        }
        const result = blob
          ? await response.blob()
          : response.status === 204
            ? null
            : await response.json();
        if (current !== epoch) throw new CloudError(ui('client.accountChanged', expectedAccount));
        if (method !== 'GET' && !anonymous) announce(CREDITS_CHANGED);
        return result;
      }
    } catch (error) {
      if (error instanceof CloudError) throw error;
      if (timedOut)
        throw new CloudError(
          ui(
            multipart
              ? 'client.uploadTimeout'
              : method === 'GET'
                ? blob
                  ? 'client.mediaTimeout'
                  : 'client.readTimeout'
                : 'client.writeTimeout',
            expectedAccount,
          ),
        );
      if (error.name === 'AbortError') throw new CloudError(ui('client.aborted', expectedAccount));
      throw new CloudError(ui('client.offline', expectedAccount));
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancelRead);
      inflight.delete(controller);
      if (releaseRead) releaseRead();
    }
  }
  function nativeContentURL(path, { download = false, rendition = null } = {}) {
    if (!account) throw new CloudError(ui('client.mediaAccount', account));
    if (
      typeof path !== 'string' ||
      !/^\/v1\/(?:assets|artifacts)\/[A-Za-z0-9_-]{1,200}\/content$/.test(path.split('?')[0]) ||
      typeof download !== 'boolean'
    )
      throw new CloudError(ui('client.mediaPath', account));
    const parsed = new URL(safeCloudPath(path), 'https://studio.invalid'),
      entries = [...parsed.searchParams];
    if (
      entries.length > 1 ||
      entries.some(
        ([key, value]) =>
          !(
            (key === 'download' && value === '1') ||
            (key === 'rendition' && ['thumb', 'preview'].includes(value))
          ),
      ) ||
      (rendition !== null && !['thumb', 'preview'].includes(rendition))
    )
      throw new CloudError(ui('client.mediaQuery', account));
    const role = parsed.searchParams.get('rendition') || rendition,
      wantsDownload = download || parsed.searchParams.has('download');
    if (role && (wantsDownload || (rendition && role !== rendition)))
      throw new CloudError(ui('client.mediaQuery', account));
    const query = new URLSearchParams({
      expected_account: account,
      ...(role ? { rendition: role } : wantsDownload ? { download: '1' } : {}),
    });
    return parsed.pathname + '?' + query;
  }
  const id = (value) => encodeURIComponent(value);
  return {
    request,
    reset,
    setAccount(value) {
      const next = typeof value === 'string' && value ? value : null;
      if (next !== account) {
        account = next;
        reset();
      }
    },
    authConfig: () => request('/api/auth/config', { anonymous: true }),
    me: () => request('/api/auth/me', { anonymous: true }),
    login: (username, password) =>
      request('/api/auth/login', { method: 'POST', body: { username, password }, anonymous: true }),
    logout: () => request('/api/auth/logout', { method: 'POST', anonymous: true }),
    projects: ({ limit = 100, offset = 0 } = {}) =>
      request(`/v1/projects?limit=${limit}&offset=${offset}`),
    createProject: (project, key) =>
      request('/v1/projects', { method: 'POST', body: { project }, key }),
    project: (projectId) => request(`/v1/projects/${id(projectId)}`),
    saveProject: (projectId, project, version) =>
      request(`/v1/projects/${id(projectId)}`, {
        method: 'PUT',
        body: { expected_version: version, project },
      }),
    capabilities: () => request('/v1/capabilities'),
    generationDraft: (projectId, shotId) =>
      request(`/v1/projects/${id(projectId)}/shots/${id(shotId)}/generation-draft`),
    savedPlan: (projectId, shotId, body, key) =>
      request(`/v1/projects/${id(projectId)}/shots/${id(shotId)}/generation-plans`, {
        method: 'POST',
        body,
        key,
      }),
    saveFrame: (projectId, artifactId, body, key) =>
      request(`/v1/projects/${id(projectId)}/artifacts/${id(artifactId)}/frames`, {
        method: 'POST',
        body,
        key,
      }),
    generationAvailability: () => request('/v1/generation-availability'),
    changePassword: (old_password, new_password) =>
      request('/v1/auth/password', { method: 'POST', body: { old_password, new_password } }),
    apiKeys: () => request('/v1/api-keys'),
    createApiKey: (body) => request('/v1/api-keys', { method: 'POST', body }),
    revokeApiKey: (keyId) => request(`/v1/api-keys/${id(keyId)}`, { method: 'DELETE' }),
    projectMeta: (projectId) => request(`/v1/projects/${id(projectId)}/meta`),
    projectActivity: (projectId, { limit = 50, beforeVersion } = {}) =>
      request(
        `/v1/projects/${id(projectId)}/activity?limit=${limit}${beforeVersion === undefined ? '' : `&before_version=${beforeVersion}`}`,
      ),
    upload: (file, projectId, assetId, key) => {
      const body = new FormData();
      body.append('file', file, file.name || 'reference');
      body.append('client_project_id', projectId);
      if (assetId) body.append('client_asset_id', assetId);
      return request('/v1/assets', { method: 'POST', body, key });
    },
    asset: (assetId) => request(`/v1/assets/${id(assetId)}`),
    assets: (projectId) => request(`/v1/assets?client_project_id=${id(projectId)}`),
    storageUsage: () => request('/v1/storage-usage'),
    resumeAsset: (assetId) => request(`/v1/assets/${id(assetId)}/resume`, { method: 'POST' }),
    derivative: (assetId, range, key) =>
      request(`/v1/assets/${id(assetId)}/derivatives`, {
        method: 'POST',
        body: { start: range.start, end: range.end },
        key,
      }),
    plan: (payload, key) => request('/v1/generation-plans', { method: 'POST', body: payload, key }),
    renderPlan: (payload) => request('/v1/render-plans', { method: 'POST', body: payload }),
    submit: (planId, key) =>
      request('/v1/jobs', { method: 'POST', body: { plan_id: planId }, key }),
    jobs: (projectId, { limit = 100, offset = 0 } = {}) =>
      request(`/v1/jobs?client_project_id=${id(projectId)}&limit=${limit}&offset=${offset}`),
    activitySummary: (projectId) =>
      request(`/v1/activity-summary?client_project_id=${id(projectId)}`),
    job: (jobId) => request(`/v1/jobs/${id(jobId)}`),
    cancel: (jobId) => request(`/v1/jobs/${id(jobId)}/cancel`, { method: 'POST' }),
    batch: (projectId, planIds, key) =>
      request('/v1/batches', {
        method: 'POST',
        body: { client_project_id: projectId, plan_ids: planIds },
        key,
      }),
    batches: (projectId, { limit = 10, offset = 0 } = {}) =>
      request(`/v1/batches?client_project_id=${id(projectId)}&limit=${limit}&offset=${offset}`),
    getBatch: (batchId) => request(`/v1/batches/${id(batchId)}`),
    cancelBatch: (batchId) => request(`/v1/batches/${id(batchId)}/cancel`, { method: 'POST' }),
    artifacts: (jobId) => request(`/v1/jobs/${id(jobId)}/artifacts`),
    nativeContentURL,
    download: (path, { signal } = {}) => request(path, { blob: true, signal }),
  };
}

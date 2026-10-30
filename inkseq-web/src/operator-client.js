import { createCloudClient } from './cloud-client.js';

const ROOT = '/v1/operator/capacity';
const id = (value) => encodeURIComponent(value);

/** Cookie-authenticated operator projection. No provider credentials in the browser. */
export function createOperatorClient(options = {}) {
  const api = createCloudClient(options);
  return {
    ...api,
    state: () => api.request(ROOT + '/state'),
    catalog: () => api.request(ROOT + '/catalog'),
    marketRefresh: () => api.request(ROOT + '/market-refreshes', { method: 'POST', body: {} }),
    candidates: ({ model_id, mode, ttl_seconds }) =>
      api.request(ROOT + '/candidates?' + new URLSearchParams({ model_id, mode, ttl_seconds })),
    offers: ({
      runtime_profile_id,
      mode,
      gpu_type,
      gpu_count,
      provider,
      node_count,
      ttl_seconds,
      filters,
    }) => {
      const query = new URLSearchParams({ runtime_profile_id, mode, gpu_type, gpu_count });
      for (const [key, value] of Object.entries({ provider, node_count, ttl_seconds }))
        if (value !== undefined) query.set(key, value);
      if (filters !== undefined) query.set('filters', JSON.stringify(filters));
      return api.request(ROOT + '/offers?' + query);
    },
    preview: (selection) => api.request(ROOT + '/previews', { method: 'POST', body: selection }),
    start: (body, key) => api.request(ROOT + '/starts', { method: 'POST', body, key }),
    drain: (nodeId, body, key) =>
      api.request(ROOT + `/nodes/${id(nodeId)}/drain`, { method: 'POST', body, key }),
    stop: (nodeId, body, key) =>
      api.request(ROOT + `/nodes/${id(nodeId)}/stop`, { method: 'POST', body, key }),
    manualReview: (nodeId, body, key) =>
      api.request(ROOT + `/nodes/${id(nodeId)}/manual-review`, { method: 'POST', body, key }),
    extensionPreview: (nodeId, body) =>
      api.request(ROOT + `/nodes/${id(nodeId)}/extension-previews`, { method: 'POST', body }),
    extend: (nodeId, body, key) =>
      api.request(ROOT + `/nodes/${id(nodeId)}/extensions`, { method: 'POST', body, key }),
    updatePolicy: (body) => api.request(ROOT + '/policy', { method: 'PUT', body }),
  };
}

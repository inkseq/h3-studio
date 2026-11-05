import { useMemo } from 'react';
import { createCloudClient } from './cloud-client.js';

export function useClient(account) {
  return useMemo(() => {
    const client = createCloudClient();
    client.setAccount(account);
    return client;
  }, [account]);
}

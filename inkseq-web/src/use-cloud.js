import { useSyncExternalStore } from 'react';
import { lastAccount } from './account-access.js';
import { cloudController as cloud } from './cloud-controller.js';

export const useCloud = () => useSyncExternalStore(cloud.subscribe, cloud.getState, cloud.getState);
// Who to draw the page frame for: the account, or while the sign-in read is out, the last one.
export function useFrameAccount(account) {
  const { sessionReady } = useCloud();
  return account || (sessionReady ? null : lastAccount());
}

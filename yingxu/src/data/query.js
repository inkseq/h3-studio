// One data layer for page reads and writes (h3-studio#514 phase 2, R-C). TanStack Query keeps
// each request's own loading/error/pending state, caches reads and refetches after writes, so
// pages no longer hold a page-wide `busy` that disables everything.
// Account isolation: every key starts with the account, and the whole cache is cleared when the
// signed-in account changes, so one account's data can never show for another.
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: 0 },
  },
});

export const accountKey = (account, ...parts) => ['account', account || '', ...parts];

export function installAccountScope(controller, client = queryClient) {
  let last = controller.getState().account || null;
  return controller.subscribe(() => {
    const account = controller.getState().account || null;
    if (account === last) return;
    last = account;
    client.clear();
  });
}

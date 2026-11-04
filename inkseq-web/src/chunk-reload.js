// An old tab can ask for a code file that a newer release removed. It cannot go on without that
// file, so reload once for the new one (#1023). Twice within a minute means reloading did not
// help, and the page shows its normal error instead of looping.
const KEY = 'inkseq.chunkReloadAt';
const WINDOW = 60 * 1000;

export const isChunkError = (error) =>
  /dynamically imported module|importing a module script failed|unable to preload/i.test(
    String(error?.message || error || ''),
  );

export function reloadOnce({
  storage = globalThis.sessionStorage,
  reload = () => globalThis.location.reload(),
  now = Date.now(),
} = {}) {
  try {
    if (now - Number(storage.getItem(KEY) || 0) < WINDOW) return false;
    storage.setItem(KEY, String(now));
  } catch {
    // Without storage a reload could not be limited, so leave the error page to the person.
    return false;
  }
  reload();
  return true;
}

export function installChunkReload(target = globalThis) {
  target.addEventListener?.('vite:preloadError', (event) => {
    if (reloadOnce()) event.preventDefault();
  });
}

// What is typed in the Quick Create box survives a refresh, per account and story.
const draftKey = (account, story) => `inkseq.storyChat.draft:${account || ''}:${story || 'new'}`;
const DRAFT_MAX = 20000;

export function readComposerDraft(account, story, storage = globalThis.localStorage) {
  try {
    return storage.getItem(draftKey(account, story)) || '';
  } catch {
    return '';
  }
}

export function writeComposerDraft(account, story, text, storage = globalThis.localStorage) {
  try {
    if (text) storage.setItem(draftKey(account, story), text.slice(0, DRAFT_MAX));
    else storage.removeItem(draftKey(account, story));
  } catch {
    // Browser storage is optional; the box still works for this visit.
  }
}

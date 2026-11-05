// Build step (#1022): the first script carries only the words its own pages can show. The rest
// load with the pages that need them. Runs in Node during `vite build`; the catalogs in source and
// in tests stay whole. A key this scan misses is not lost: translate() fetches the rest and redraws.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const STATIC_IMPORT =
  /^\s*(?:import|export)\s[^;'"]*?from\s+['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]/gm;
const DYNAMIC_IMPORT = /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g;
// A quoted key, or the fixed start of a key built at run time ('a.' + b, `a.${b}`).
const KEY_TOKEN = /['"`]([A-Za-z][\w-]*(?:\.[\w-]+)*\.?)(['"`]\s*\+|\$\{|['"`])/g;
const OPS_PAGES = ['P4OperatorConsole.jsx', 'AdminHome.jsx'];

const isCatalog = (file) => /Catalogs\.js$|[\\/]i18n[\\/]catalogs\.js$/.test(file);

function imports(file, pattern) {
  const out = [];
  for (const match of fs.readFileSync(file, 'utf8').matchAll(pattern)) {
    const source = match[1] || match[2];
    if (!source?.startsWith('.') || source.includes('?')) continue;
    const target = path.resolve(path.dirname(file), source);
    if (/\.jsx?$/.test(target) && fs.existsSync(target)) out.push(target);
  }
  return out;
}

export function reachable(entry, { dynamic = false, stop = [] } = {}) {
  const seen = new Set();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file) || stop.includes(path.basename(file))) continue;
    seen.add(file);
    queue.push(...imports(file, STATIC_IMPORT));
    if (dynamic) queue.push(...imports(file, DYNAMIC_IMPORT));
  }
  return [...seen].filter((file) => !isCatalog(file));
}

export function referencedKeys(files, keys) {
  const known = new Set(keys);
  const found = new Set();
  const starts = new Set();
  for (const file of files) {
    for (const match of fs.readFileSync(file, 'utf8').matchAll(KEY_TOKEN)) {
      const [, token, close] = match;
      if (known.has(token)) found.add(token);
      if (token.endsWith('.') || close.endsWith('+') || close === '${') starts.add(token);
    }
  }
  for (const key of keys) for (const start of starts) if (key.startsWith(start)) found.add(key);
  return found;
}

/** Shell: what the first script's pages use. App: what signed-in pages add. Ops: all the rest. */
export function partitionKeys(srcDir, keys) {
  const main = path.join(srcDir, 'main.jsx');
  const shell = referencedKeys(reachable(main), keys);
  const signedIn = referencedKeys(reachable(main, { dynamic: true, stop: OPS_PAGES }), keys);
  const tiers = { shell: [], app: [], ops: [] };
  for (const key of keys)
    tiers[shell.has(key) ? 'shell' : signedIn.has(key) ? 'app' : 'ops'].push(key);
  return tiers;
}

const words = (catalogs, keys) => ({
  en: Object.fromEntries(keys.map((key) => [key, catalogs.en[key]])),
  zh: Object.fromEntries(keys.map((key) => [key, catalogs.zh[key]])),
});
const asParse = (value) => `JSON.parse(${JSON.stringify(JSON.stringify(value))})`;

const SHELL_ID = '\0inkseq-words-shell';
const APP_SOURCE = 'inkseq-words-app';
const OPS_SOURCE = 'inkseq-words-ops';

export function catalogTiers() {
  let root;
  let modules;
  return {
    name: 'inkseq-catalog-tiers',
    apply: 'build',
    enforce: 'pre',
    configResolved(config) {
      root = config.root;
    },
    async buildStart() {
      const srcDir = path.join(root, 'src');
      const url = pathToFileURL(path.join(srcDir, 'i18n', 'catalogs.js')).href;
      const { catalogs } = await import(url);
      const tiers = partitionKeys(srcDir, Object.keys(catalogs.en));
      modules = {
        [SHELL_ID]: `const first = ${asParse(words(catalogs, tiers.shell))};
export const catalogs = first;
const load = {
  app: () => import('${APP_SOURCE}'),
  ops: () => import('${OPS_SOURCE}'),
};
const started = {};
let waiting = 2;
function fetchTier(tier) {
  started[tier] ||= load[tier]().then(
    ({ default: more }) => {
      Object.assign(first.en, more.en);
      Object.assign(first.zh, more.zh);
      waiting -= 1;
    },
    (error) => {
      started[tier] = null;
      throw error;
    },
  );
  return started[tier];
}
export const catalogsPending = () => waiting > 0;
export const loadCatalogs = (tier = 'app') =>
  tier === 'ops' ? Promise.all([fetchTier('app'), fetchTier('ops')]) : fetchTier('app');
export const loadAllCatalogs = () => Promise.all([fetchTier('app'), fetchTier('ops')]);
`,
        ['\0' + APP_SOURCE]: `export default ${asParse(words(catalogs, tiers.app))};\n`,
        ['\0' + OPS_SOURCE]: `export default ${asParse(words(catalogs, tiers.ops))};\n`,
      };
    },
    resolveId(source, importer) {
      if (source === APP_SOURCE || source === OPS_SOURCE) return '\0' + source;
      if (source === './catalogs.js' && importer?.replaceAll('\\', '/').endsWith('/i18n/locale.js'))
        return SHELL_ID;
      return null;
    },
    load(id) {
      return modules?.[id] ?? null;
    },
  };
}

import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';
import * as prettier from 'prettier';

const app = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repository = resolve(app, '..');
const sourceFolder = relative(repository, app).replaceAll('\\', '/');
const git = (...args) =>
  execFileSync('git', args, { cwd: repository, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
if (process.argv.length !== 2) throw new Error('This check accepts no command arguments');
const base = process.env.FORMAT_BASE_SHA || git('merge-base', 'HEAD', 'origin/main').trim();
if (!/^[0-9a-f]{40}$/.test(base) || /^0+$/.test(base))
  throw new Error('An existing exact base commit is required');
git('cat-file', '-e', base + '^{commit}');
const configFiles = new Set([
  'package.json',
  '.prettierrc.json',
  'eslint.config.js',
  'tools/check-changed.mjs',
  'tools/layout-audit.mjs',
]);
const runtime = (file) => /^src\/.*\.(?:js|jsx|css)$/.test(file) || file === 'vite.config.js';
const files = git('diff', '--name-only', '--diff-filter=ACMR', '-z', base, '--', sourceFolder)
  .split('\0')
  .filter(Boolean)
  .map((file) => file.slice(sourceFolder.length + 1))
  .filter((file) => runtime(file) || configFiles.has(file));
let failed = false;
for (const file of files) {
  const absolute = resolve(app, file),
    source = await readFile(absolute, 'utf8');
  const options = { ...(await prettier.resolveConfig(absolute)), filepath: absolute };
  if (!(await prettier.check(source, options))) {
    console.error('Unformatted: ' + file);
    failed = true;
  }
}
const lintFiles = files.filter((file) => /\.(?:js|jsx|mjs)$/.test(file));
if (lintFiles.length) {
  const eslint = new ESLint({ cwd: app });
  const results = await eslint.lintFiles(lintFiles.map((file) => resolve(app, file)));
  if (results.some((result) => result.errorCount || result.warningCount)) {
    console.error(await (await eslint.loadFormatter('stylish')).format(results));
    failed = true;
  }
}
if (failed) process.exitCode = 1;
else console.log(`Checked ${files.length} changed canonical files.`);

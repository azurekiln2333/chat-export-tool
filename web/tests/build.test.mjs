/**
 * Built-app checks for the Vue frontend.
 *
 * This never runs the bundler: it inspects the source manifest and the already
 * built web/dist, so it adds no dependency and stays fast. Run `npm run build`
 * before `npm test`; a missing build is reported as a failure because every
 * component is expected to be present.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

// URL-based paths keep this file independent of the host path separator.
const webDir = new URL('..', import.meta.url);
const packageJson = new URL('package.json', webDir);
const sourceIndex = new URL('index.html', webDir);
const distIndex = new URL('dist/index.html', webDir);
const distAssets = new URL('dist/assets/', webDir);

test('web frontend is a Vue 3 + Vite project', () => {
  assert.ok(fs.existsSync(packageJson), 'web/package.json is missing');
  const manifest = JSON.parse(fs.readFileSync(packageJson, 'utf8'));
  const dependencies = { ...(manifest.dependencies || {}), ...(manifest.devDependencies || {}) };
  const vue = dependencies.vue || dependencies['@vue/runtime-core'];
  assert.ok(vue, 'vue is not a dependency');
  assert.match(String(vue), /(^|\D)3\./, 'expected Vue 3');
  assert.ok(dependencies.vite, 'vite is not a dependency');
  assert.ok(dependencies['@vitejs/plugin-vue'], 'plugin-vue is not a dependency');
  assert.equal(manifest.type, 'module', 'the package must be ESM');
  assert.ok(manifest.engines && manifest.engines.node, 'the Node engine range must be declared');
  assert.ok(manifest.scripts && manifest.scripts.build, 'no build script');
  assert.ok(manifest.scripts && manifest.scripts.start, 'no start script');

  assert.ok(fs.existsSync(sourceIndex), 'web/index.html is missing');
  const html = fs.readFileSync(sourceIndex, 'utf8');
  const lang = html.match(/<html[^>]*\blang="([^"]+)"/i);
  assert.equal(lang && lang[1].toLowerCase(), 'zh-cn', 'the reader UI must be Simplified Chinese');
  assert.match(html, /<div id="app">/, 'the Vue mount point is missing');
});

test('built app uses a relative base and never an absolute root URL', () => {
  assert.ok(fs.existsSync(distIndex), 'web/dist is not built; run npm run build first');
  const html = fs.readFileSync(distIndex, 'utf8');
  assert.ok(html.length > 0);
  assert.ok(!/\s(?:src|href)="\/[^/]/.test(html),
    'the built page uses an absolute root URL; Vite base must be "./"');
  const lang = html.match(/<html[^>]*\blang="([^"]+)"/i);
  if (lang) assert.equal(lang[1].toLowerCase(), 'zh-cn', 'the reader UI must stay Simplified Chinese');

  assert.ok(fs.existsSync(distAssets), 'dist/assets is missing');
  const bundles = fs.readdirSync(distAssets).filter((name) => /\.(js|css)$/.test(name));
  assert.ok(bundles.length >= 1, 'no built JS/CSS bundle found');

  // The bundle must be self-contained: no runtime CDN or absolute host.
  for (const bundle of bundles.filter((name) => name.endsWith('.js'))) {
    const code = fs.readFileSync(new URL(bundle, distAssets), 'utf8');
    assert.ok(!/https?:\/\/(?:unpkg|cdn\.jsdelivr|cdnjs)/i.test(code),
      `${bundle} references a CDN; assets must be bundled locally`);
  }
});

test('media and API helpers are relative to the document base', () => {
  assert.ok(fs.existsSync(distAssets), 'web/dist is not built; run npm run build first');
  const code = fs.readdirSync(distAssets).filter((name) => name.endsWith('.js'))
    .map((name) => fs.readFileSync(new URL(name, distAssets), 'utf8')).join('\n');
  assert.ok(!/fetch\((['"`])\/(?:api|media)\//.test(code),
    'API/media requests must not use an absolute root path');
  assert.ok(!/https?:\/\/[^/'"`]+\/(?:api|media)\//.test(code),
    'API/media requests must not hardcode a host');
});
// Writes the licences of every package bundled into static/js/editor.js next to it,
// as their MIT and ISC licences require. Run by `npm run build`, after esbuild.

import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

const meta = JSON.parse(readFileSync('build-meta.json', 'utf8'));
const packages = new Map();
for (const input of Object.keys(meta.inputs)) {
  const match = /^(.*node_modules\/(@[^/]+\/)?[^/]+)\//.exec(input);
  if (!match || packages.has(match[1])) { continue; }
  const manifest = JSON.parse(readFileSync(join(match[1], 'package.json'), 'utf8'));
  const licenceFile = readdirSync(match[1]).find((name) => /^(licen[cs]e|copying)/i.test(name));
  packages.set(match[1], {
    name: manifest.name,
    version: manifest.version,
    license: manifest.license,
    text: licenceFile ? readFileSync(join(match[1], licenceFile), 'utf8').trim() : `(no licence file; package.json says ${manifest.license})`,
  });
}

const sorted = [...packages.values()].sort((a, b) => a.name.localeCompare(b.name));
const out = join('..', '..', 'static', 'js', 'editor.js.LICENSES.txt');
writeFileSync(out, [
  'Third-party software in editor.js (the Chai & Trails post editor), and their licences.',
  '',
  ...sorted.flatMap((pkg) => [`${'='.repeat(72)}`, `${pkg.name} ${pkg.version} (${pkg.license})`, '-'.repeat(72), pkg.text, '']),
].join('\n'));
console.log(`Wrote ${sorted.length} licences to ${out}`);
if (!existsSync(dirname(out))) { process.exit(1); }

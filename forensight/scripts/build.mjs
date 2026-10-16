import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const root = process.cwd();
const output = join(root, 'dist');
const apiBase = (process.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
if (process.env.VERCEL && (!apiBase || !apiBase.startsWith('https://') || /localhost|127\.0\.0\.1/i.test(apiBase))) {
  throw new Error('Set VITE_API_BASE_URL to the public HTTPS backend URL in Vercel project settings (not localhost).');
}

await rm(output, { recursive: true, force: true });
await mkdir(join(output, 'assets'), { recursive: true });
await cp(join(root, 'frontend', 'index.html'), join(output, 'index.html'));
await cp(join(root, 'frontend', 'style.css'), join(output, 'assets', 'style.css'));
const source = await readFile(join(root, 'frontend', 'app.js'), 'utf8');
const configured = source.replace("const API_BASE = window.FORENSIGHT_API_BASE_URL || '';", `const API_BASE = ${JSON.stringify(apiBase)};`);
if (configured === source) throw new Error('API base URL marker missing from frontend/app.js');
await writeFile(join(output, 'assets', 'app.js'), configured);
console.log(`Built static frontend in dist (API base: ${apiBase || 'same origin'})`);

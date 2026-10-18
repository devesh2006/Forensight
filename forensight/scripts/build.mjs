import { cp, mkdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const root = process.cwd();
const output = join(root, 'dist');
await rm(output, { recursive: true, force: true });
await mkdir(join(output, 'assets'), { recursive: true });
await cp(join(root, 'frontend', 'index.html'), join(output, 'index.html'));
for (const file of ['style.css', 'app.js', 'api.js', 'system-status.js']) {
  await cp(join(root, 'frontend', file), join(output, 'assets', file));
}
const apiUrl = (process.env.VITE_API_URL || '').trim().replace(/\/+$/, '');
await writeFile(join(output, 'assets', 'config.js'), `window.FORENSIGHT_CONFIG = { apiUrl: ${JSON.stringify(apiUrl)} };\n`);
console.log('Built static FORENSIGHT frontend in dist.');

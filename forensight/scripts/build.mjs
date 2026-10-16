import { cp, mkdir, rm } from 'node:fs/promises';
import { join } from 'node:path';

const root = process.cwd();
const output = join(root, 'dist');
await rm(output, { recursive: true, force: true });
await mkdir(join(output, 'assets'), { recursive: true });
await cp(join(root, 'frontend', 'index.html'), join(output, 'index.html'));
for (const file of ['style.css', 'app.js', 'demo-data.js']) {
  await cp(join(root, 'frontend', file), join(output, 'assets', file));
}
console.log('Built static FORENSIGHT frontend in dist.');

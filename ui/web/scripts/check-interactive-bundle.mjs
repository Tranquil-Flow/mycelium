import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const output = mkdtempSync(path.join(tmpdir(), 'mycelium-browser-stage-'));
const tsc = path.join(root, 'node_modules', '.bin', process.platform === 'win32' ? 'tsc.cmd' : 'tsc');

try {
  const result = spawnSync(
    tsc,
    [
      '--ignoreConfig',
      'src/interactive/pixelStage.ts',
      'src/interactive/contracts.ts',
      '--target',
      'es2022',
      '--module',
      'es2022',
      '--moduleResolution',
      'bundler',
      '--lib',
      'es2022,dom',
      '--skipLibCheck',
      '--outDir',
      output,
    ],
    { cwd: root, encoding: 'utf8' },
  );
  if (result.status !== 0) {
    process.stderr.write(result.stdout);
    process.stderr.write(result.stderr);
    process.exitCode = result.status ?? 1;
  } else {
    const generated = readFileSync(path.join(output, 'pixelStage.js'));
    const committed = readFileSync(path.resolve(root, '..', '..', 'mycelium_interactive', 'static', 'pixelStage.js'));
    if (!generated.equals(committed)) {
      console.error('interactive browser stage bundle drift');
      process.exitCode = 1;
    } else {
      console.log('interactive browser stage bundle OK');
    }
  }
} finally {
  rmSync(output, { recursive: true, force: true });
}

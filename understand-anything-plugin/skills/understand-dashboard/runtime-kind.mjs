#!/usr/bin/env node
import { execFileSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** A tracked checkout must run its own dashboard, including committed fork changes. */
export function dashboardRuntimeKind(pluginRoot) {
  try {
    execFileSync('git', ['ls-files', '--error-unmatch', '--', 'packages/dashboard/vite.config.ts'], {
      cwd: realpathSync(pluginRoot), stdio: 'ignore', timeout: 3000,
    });
    return 'checkout';
  } catch {
    return 'installed';
  }
}

let isCli = false;
try { isCli = Boolean(process.argv[1]) && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url)); } catch {}
if (isCli) {
  if (!process.argv[2]) { process.stderr.write('Usage: runtime-kind.mjs <plugin-root>\n'); process.exitCode = 1; }
  else process.stdout.write(dashboardRuntimeKind(process.argv[2]) + '\n');
}

import { afterEach, describe, expect, it } from 'vitest';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(new URL('../../../understand-anything-plugin/skills/understand/refresh-relationships.mjs', import.meta.url));
const roots = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'ua-relink-test-'));
  roots.push(root);
  const put = (path, text) => { const dest = join(root, path); mkdirSync(dirname(dest), { recursive: true }); writeFileSync(dest, text); };
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const sources = {
    'package.json': '{"name":"@sample/ui","dependencies":{"@sveltejs/kit":"2.0.0"}}',
    'packages/shared/package.json': '{"name":"@sample/shared","exports":{".":"./entry.ts"}}',
    'packages/shared/entry.ts': 'export const value = 42;\n',
    'svelte.config.js': 'export default { kit: {} };\n',
    'src/routes/+layout.svelte': '<slot />\n',
    'src/routes/+page.svelte': '<script lang="ts">import {value} from "@sample/shared";</script><p>{value}</p>\n',
    'src/routes/+page.server.ts': 'export const load = () => ({ready:true});\n',
    'docs/start.md': '[Shared entry](../packages/shared/entry.ts)\n',
  };
  for (const [path, contents] of Object.entries(sources)) put(path, contents);
  git('init'); git('config', 'user.email', 'fixture@example.test'); git('config', 'user.name', 'Fixture');
  git('add', '.'); git('commit', '-m', 'Fixture baseline');
  const nodes = Object.keys(sources).map(filePath => {
    const type = filePath.endsWith('.md') ? 'document' : filePath.endsWith('.json') ? 'config' : 'file';
    return { id: `${type}:${filePath}`, type, filePath, name: filePath, summary: 'Fixture file', tags: ['fixture'], complexity: 'simple' };
  });
  const graph = { version: '1.0.0', kind: 'codebase', project: { name: 'Fixture', description: 'Test codebase',
    languages: ['typescript'], frameworks: ['SvelteKit'], analyzedAt: '2026-01-01T00:00:00Z', gitCommitHash: git('rev-parse', 'HEAD') },
    nodes, edges: [], layers: [{ id: 'layer:test', name: 'Test', description: 'Fixture sources', nodeIds: nodes.map(n => n.id) }],
    tour: [{ order: 1, title: 'Start', description: 'Fixture tour', nodeIds: [nodes[0].id] }] };
  put('.ua/input.json', JSON.stringify(graph));
  return { root, put, graph, input: join(root, '.ua/input.json'), candidate: join(root, '.ua/candidate.json') };
}

function run(f, input = f.input, output = f.candidate, options = []) {
  return spawnSync(process.execPath, [script, f.root, input, output, ...options], {
    encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
}

describe('source relationship refresh', () => {
  it('rebuilds imports, framework wiring and document links and keeps its input unchanged', () => {
    const f = fixture(); const original = readFileSync(f.input, 'utf8');
    const scan = join(f.root, '.ua/scan.json');
    const result = run(f, f.input, f.candidate, ['--scan-output', scan]);
    expect(result.status, result.stderr).toBe(0);
    expect(readFileSync(f.input, 'utf8')).toBe(original);
    const candidate = JSON.parse(readFileSync(f.candidate, 'utf8'));
    expect(candidate.nodes).toEqual(f.graph.nodes);
    expect(candidate.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({ source: 'file:src/routes/+page.svelte', target: 'file:packages/shared/entry.ts', type: 'imports' }),
      expect.objectContaining({ source: 'file:src/routes/+layout.svelte', target: 'file:src/routes/+page.svelte', type: 'contains', derivedBy: 'framework-links' }),
      expect.objectContaining({ source: 'document:docs/start.md', target: 'file:packages/shared/entry.ts', type: 'documents', derivedBy: 'source-references' }),
    ]));
    expect(JSON.parse(readFileSync(scan, 'utf8')).importMap['src/routes/+page.svelte']).toEqual(['packages/shared/entry.ts']);
    const second = join(f.root, '.ua/second.json');
    const again = run(f, f.candidate, second);
    expect(again.status, again.stderr).toBe(0);
    expect(JSON.parse(readFileSync(second, 'utf8')).edges).toEqual(candidate.edges);
  });

  it('rejects a dirty source tree without creating a candidate', () => {
    const f = fixture();
    f.put('packages/shared/entry.ts', 'export const value = 9;\n');
    const result = run(f);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('Graph is dirty');
    expect(existsSync(f.candidate)).toBe(false);
  });

  it('rejects an inventory change instead of silently dropping excluded files', () => {
    const f = fixture();
    const result = run(f, f.input, f.candidate, ['--exclude', 'docs/*']);
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain('inventory differs');
    expect(existsSync(f.candidate)).toBe(false);
  });
});

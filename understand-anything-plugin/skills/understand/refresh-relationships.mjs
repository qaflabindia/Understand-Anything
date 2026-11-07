#!/usr/bin/env node
/** Recompute source-derived relationships on a fresh graph, without publishing it. */
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, renameSync, mkdtempSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { KnowledgeGraphSchema, getGraphFreshness } from '../../packages/core/dist/index.js';

const here = dirname(fileURLToPath(import.meta.url));
const fileTypes = new Set(['file', 'config', 'document', 'service', 'pipeline', 'table', 'schema', 'resource', 'endpoint']);
const readJson = path => JSON.parse(readFileSync(path, 'utf8'));

function run(command, args) {
  const result = spawnSync(command, args, { stdio: ['ignore', 'inherit', 'inherit'], windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${basename(args[0] ?? command)} failed (${result.signal ?? result.status})`);
}

function runPython(args) {
  for (const [command, prefix] of [['python3', []], ['python', []], ['py', ['-3']]]) {
    try { run(command, [...prefix, ...args]); return; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  throw new Error('Python 3 is required to rebuild relationships');
}

export function runRelationshipMerge(root, input, scan, output) {
  runPython([join(here, 'merge-batch-graphs.py'), root, '--refresh-relationships', input, scan, output]);
}

function canonical(path) {
  try { return realpathSync(path); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return join(realpathSync(dirname(resolve(path))), basename(path));
  }
}

function writeAtomic(path, data) {
  const temp = `${path}.tmp-${process.pid}`;
  try {
    writeFileSync(temp, JSON.stringify(data, null, 2) + '\n', { flag: 'wx' });
    renameSync(temp, path);
  } finally { rmSync(temp, { force: true }); }
}

function validate(graph) {
  const parsed = KnowledgeGraphSchema.safeParse(graph);
  if (!parsed.success) throw new Error(`Invalid knowledge graph: ${parsed.error.issues[0]?.path.join('.')}`);
  if (graph.kind && graph.kind !== 'codebase') throw new Error('Relationship refresh requires a codebase graph');
  const ids = new Set(graph.nodes.map(node => node.id));
  if (ids.size !== graph.nodes.length) throw new Error('Duplicate graph node IDs');
  const edges = new Set();
  for (const edge of graph.edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) throw new Error('Dangling graph relationship');
    const key = JSON.stringify([edge.source, edge.target, edge.type, edge.direction]);
    if (edges.has(key)) throw new Error('Duplicate graph relationship');
    edges.add(key);
  }
  for (const item of [...graph.layers, ...graph.tour]) {
    if (item.nodeIds.some(id => !ids.has(id))) throw new Error('Dangling layer or tour reference');
  }
}

async function requireFresh(root, graph) {
  const status = await getGraphFreshness(root, { graphCommitHash: graph.project.gitCommitHash });
  if (status.status !== 'fresh') throw new Error(`Graph is ${status.status}; run /understand to analyze changed source first`);
  return status.headCommitHash;
}

async function main() {
  const { positionals, values } = parseArgs({ allowPositionals: true, options: {
    'scan-output': { type: 'string' }, exclude: { type: 'string' },
  } });
  if (positionals.length !== 3) {
    throw new Error('Usage: refresh-relationships.mjs <project-root> <input-graph> <candidate-output> [--scan-output <path>] [--exclude <patterns>]');
  }
  const [root, input, output] = positionals.map(canonical);
  const scanOutput = values['scan-output'] ? canonical(values['scan-output']) : null;
  if (input === output || (scanOutput && [input, output].includes(scanOutput))) {
    throw new Error('Input graph, candidate and scan output must be distinct paths');
  }
  const graph = readJson(input);
  validate(graph);
  const head = await requireFresh(root, graph);
  const temp = mkdtempSync(join(tmpdir(), 'ua-relationship-refresh-'));
  try {
    const scanPath = join(temp, 'scan.json');
    const importInput = join(temp, 'import-input.json');
    const importOutput = join(temp, 'import-output.json');
    const candidatePath = join(temp, 'candidate.json');
    const exclusions = values.exclude ? ['--exclude', values.exclude] : [];
    run(process.execPath, [join(here, 'scan-project.mjs'), root, scanPath, '--exclude-analysis-data', ...exclusions]);
    const scan = readJson(scanPath);
    if (!scan.scriptCompleted || scan.failures?.length) throw new Error('Project scan did not complete cleanly');
    const inventory = new Set(scan.files.map(file => file.path));
    const mapped = new Set(graph.nodes.filter(node => fileTypes.has(node.type) && node.filePath).map(node => node.filePath));
    if (inventory.size !== mapped.size || [...inventory].some(path => !mapped.has(path))) {
      throw new Error('Project inventory differs from the graph; run /understand for a structural update');
    }
    writeFileSync(importInput, JSON.stringify({ projectRoot: root, files: scan.files }));
    run(process.execPath, [join(here, 'extract-import-map.mjs'), importInput, importOutput]);
    const imports = readJson(importOutput);
    if (!imports.scriptCompleted || imports.failures?.length) throw new Error('Import extraction did not complete cleanly');
    scan.importMap = imports.importMap;
    writeFileSync(scanPath, JSON.stringify(scan));
    runRelationshipMerge(root, input, scanPath, candidatePath);
    const candidate = readJson(candidatePath);
    validate(candidate);
    if (await requireFresh(root, graph) !== head) throw new Error('Project HEAD changed during the relationship refresh');
    candidate.project = { ...graph.project, gitCommitHash: head, analyzedAt: new Date().toISOString() };
    if (scanOutput) writeAtomic(scanOutput, { ...graph.project, ...scan });
    writeAtomic(output, candidate);
    console.log(JSON.stringify({ nodes: candidate.nodes.length, edgesBefore: graph.edges.length,
      edgesAfter: candidate.edges.length, scannedFiles: inventory.size, resolvedImports: imports.stats.totalEdges }));
  } finally { rmSync(temp, { recursive: true, force: true }); }
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch(error => { console.error(`refresh-relationships: ${error.message}`); process.exitCode = 1; });
}

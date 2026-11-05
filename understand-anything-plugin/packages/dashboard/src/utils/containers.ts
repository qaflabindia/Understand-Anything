import type {
  GraphNode,
  GraphEdge,
} from "@understand-anything/core/types";

export interface DerivedContainer {
  id: string;
  name: string;
  nodeIds: string[];
  strategy: "folder" | "prefix";
}

export interface DeriveResult {
  containers: DerivedContainer[];
  ungrouped: string[];
}

const MIN_BUCKET_COUNT = 2;
const MIN_NODES_FOR_SUPPRESSION = 3;
const MAX_CONTAINER_SIZE = 20;
const MIN_PREFIX_SPLIT_SIZE = 3;
const ROOT_BUCKET = "~";

/**
 * Longest common prefix of the *directory* portion of paths (each with its
 * trailing `/`), trimmed to a `/` boundary, so files directly in the shared
 * parent (`risk/grc.ts` next to `risk/handlers/a.ts`) land in the root bucket.
 */
function commonPrefix(paths: string[]): string {
  if (paths.length === 0) return "";
  const dirs = paths.map((p) => {
    const slash = p.lastIndexOf("/");
    return slash >= 0 ? p.slice(0, slash + 1) : "";
  });
  let prefix = dirs[0];
  for (const d of dirs) {
    while (!d.startsWith(prefix)) {
      prefix = prefix.slice(0, -1);
      if (!prefix) return "";
    }
  }
  const lastSlash = prefix.lastIndexOf("/");
  return lastSlash >= 0 ? prefix.slice(0, lastSlash + 1) : "";
}

function firstSegment(path: string): string {
  const slash = path.indexOf("/");
  return slash >= 0 ? path.slice(0, slash) : path;
}

function groupByFolder(
  nodes: GraphNode[],
): { groups: Map<string, string[]>; rooted: string[] } {
  const withPath = nodes.filter((n) => n.filePath);
  const lcp = commonPrefix(withPath.map((n) => n.filePath!));
  const groups = new Map<string, string[]>();
  const rooted: string[] = [];
  for (const n of nodes) {
    if (!n.filePath) {
      rooted.push(n.id);
      continue;
    }
    const stripped = n.filePath.slice(lcp.length);
    if (!stripped.includes("/")) {
      rooted.push(n.id);
      continue;
    }
    const seg = firstSegment(stripped);
    const arr = groups.get(seg) ?? [];
    arr.push(n.id);
    groups.set(seg, arr);
  }
  return { groups, rooted };
}

function filenameWords(path: string): string[] {
  const base = path.slice(path.lastIndexOf("/") + 1);
  const words = base.match(/[A-Z0-9]+(?![a-z])|[A-Z]?[a-z0-9]+/g);
  return words ? words.map((w) => w.toLowerCase()) : [base.toLowerCase()];
}

/** The first `depth` words of the filename, e.g. depth 2: grc-risk-scoring.ts → grc-risk. */
function filePrefix(path: string, depth: number): string {
  const words = filenameWords(path);
  return words.slice(0, Math.min(depth, Math.max(1, words.length - 1))).join("-");
}

function groupByPrefix(
  nodes: GraphNode[],
  ids: string[],
  depth: number,
): Map<string, string[]> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const groups = new Map<string, string[]>();
  for (const id of ids) {
    const n = byId.get(id)!;
    const key = n.filePath ? filePrefix(n.filePath, depth) : ROOT_BUCKET;
    const arr = groups.get(key) ?? [];
    arr.push(id);
    groups.set(key, arr);
  }
  return groups;
}

/**
 * One level of grouping: folder boxes below the nodes' common prefix, or
 * filename-prefix boxes when the nodes all sit in one flat folder.
 */
function bucket(nodes: GraphNode[], prefixDepth = 1): DerivedContainer[] {
  const { groups, rooted } = groupByFolder(nodes);
  const bucketCount = groups.size + (rooted.length > 0 ? 1 : 0);

  if (bucketCount >= MIN_BUCKET_COUNT) {
    const containers: DerivedContainer[] = [...groups.entries()].map(([seg, ids]) => ({
      id: `container:${seg}`,
      name: seg,
      nodeIds: ids,
      strategy: "folder" as const,
    }));
    if (rooted.length > 0) {
      containers.push({
        id: `container:${ROOT_BUCKET}`,
        name: ROOT_BUCKET,
        nodeIds: rooted,
        strategy: "folder" as const,
      });
    }
    return containers;
  }

  // Flat folder: no sub-folders to name boxes after, so group by the
  // shared filename prefix (agent-*.ts, task-*.ts) instead.
  const ids = rooted.length > 0 ? rooted : [...groups.values()][0];
  const prefixes = groupByPrefix(nodes, ids, prefixDepth);
  if (prefixes.size < MIN_BUCKET_COUNT) return [];
  return [...prefixes.entries()].map(([prefix, members]) => ({
    id: `container:prefix-${prefix}`,
    name: prefix === ROOT_BUCKET ? ROOT_BUCKET : `${prefix}*`,
    nodeIds: members,
    strategy: "prefix" as const,
  }));
}

function lastSegment(name: string): string {
  return name.slice(name.lastIndexOf("/") + 1);
}

const OTHER_SUFFIX = " (other)";

function childName(parent: DerivedContainer, child: DerivedContainer): string {
  // A deeper prefix (grc-risk*) already spells out its parent (grc*).
  if (parent.strategy === "prefix" && child.strategy === "prefix") return child.name;
  const p = lastSegment(parent.name.replace(OTHER_SUFFIX, ""));
  if (p === ROOT_BUCKET) return child.name;
  // Files sitting directly in the parent folder are named after it.
  if (child.name === ROOT_BUCKET) return p;
  return `${p}/${child.name}`;
}

function otherName(name: string): string {
  return name === ROOT_BUCKET || name.endsWith(OTHER_SUFFIX) ? name : `${name}${OTHER_SUFFIX}`;
}

/**
 * Re-bucket a box that is too big to read at a glance, recursing until every
 * box fits MAX_CONTAINER_SIZE or cannot be divided further. A prefix box is
 * re-split on the next filename word (grc* → grc-risk*, grc-evidence*).
 * Sub-groups too small to be worth a box are kept together in an "(other)"
 * box instead of scattering into loose nodes or two-file boxes.
 */
function splitOversized(
  container: DerivedContainer,
  byId: Map<string, GraphNode>,
  prefixDepth: number,
): DerivedContainer[] {
  if (container.nodeIds.length <= MAX_CONTAINER_SIZE) return [container];
  const nextDepth = container.strategy === "prefix" ? prefixDepth + 1 : 1;
  const children = bucket(
    container.nodeIds.map((id) => byId.get(id)!),
    nextDepth,
  );
  const minSize = (c: DerivedContainer) =>
    c.strategy === "prefix" ? MIN_PREFIX_SPLIT_SIZE : MIN_BUCKET_COUNT;
  const kept = children.filter((c) => c.nodeIds.length >= minSize(c));
  if (kept.length === 0) return [container];

  const leftovers = children
    .filter((c) => c.nodeIds.length < minSize(c))
    .flatMap((c) => c.nodeIds);
  if (kept.some((c) => c.nodeIds.length === container.nodeIds.length)) {
    return [container];
  }

  const result = kept.flatMap((child) =>
    splitOversized(
      {
        id: `${container.id}/${child.id.slice("container:".length)}`,
        name: childName(container, child),
        nodeIds: child.nodeIds,
        strategy: child.strategy,
      },
      byId,
      nextDepth,
    ),
  );
  if (leftovers.length > 0) {
    result.push({ ...container, name: otherName(container.name), nodeIds: leftovers });
  }
  return result;
}

/**
 * Splitting can name two boxes alike: a folder's own leftovers and the
 * leftovers of its own-files box are both "components (other)".
 */
function mergeSameName(containers: DerivedContainer[]): DerivedContainer[] {
  const byName = new Map<string, DerivedContainer>();
  for (const c of containers) {
    const existing = byName.get(c.name);
    if (existing) existing.nodeIds = [...existing.nodeIds, ...c.nodeIds];
    else byName.set(c.name, { ...c });
  }
  return [...byName.values()];
}

export function deriveContainers(
  nodes: GraphNode[],
  _edges: GraphEdge[],
): DeriveResult {
  if (nodes.length === 0) {
    return { containers: [], ungrouped: [] };
  }

  const byId = new Map(nodes.map((n) => [n.id, n]));
  let containers = mergeSameName(
    bucket(nodes).flatMap((c) => splitOversized(c, byId, 1)),
  );

  // Suppress single-child containers (their child becomes ungrouped).
  // Skip suppression for tiny layers — with so few nodes, even single-item
  // boxes carry useful folder context that shouldn't be discarded.
  const ungrouped: string[] = [];
  if (nodes.length >= MIN_NODES_FOR_SUPPRESSION) {
    containers = containers.filter((c) => {
      if (c.nodeIds.length === 1) {
        ungrouped.push(c.nodeIds[0]);
        return false;
      }
      return true;
    });
  }

  return { containers, ungrouped };
}

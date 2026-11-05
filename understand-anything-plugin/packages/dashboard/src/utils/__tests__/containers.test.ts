import { describe, it, expect } from "vitest";
import { deriveContainers } from "../containers";
import type { GraphNode } from "@understand-anything/core/types";

function node(id: string, filePath?: string): GraphNode {
  return {
    id,
    type: "file",
    name: id,
    filePath,
    summary: "",
    complexity: "simple",
    tags: [],
  } as GraphNode;
}

describe("deriveContainers — folder strategy", () => {
  it("groups nodes by first folder segment after LCP", () => {
    const nodes = [
      node("a", "src/auth/login.go"),
      node("b", "src/auth/oauth.go"),
      node("c", "src/cart/cart.go"),
      node("d", "src/cart/checkout.go"),
    ];
    const { containers, ungrouped } = deriveContainers(nodes, []);
    expect(ungrouped).toEqual([]);
    expect(containers).toHaveLength(2);
    const names = containers.map((c) => c.name).sort();
    expect(names).toEqual(["auth", "cart"]);
    const auth = containers.find((c) => c.name === "auth")!;
    expect(auth.strategy).toBe("folder");
    expect(auth.nodeIds.sort()).toEqual(["a", "b"]);
  });

  it("strips deep LCP", () => {
    const nodes = [
      node("a", "monorepo/backend/src/auth/login.go"),
      node("b", "monorepo/backend/src/cart/cart.go"),
    ];
    const { containers } = deriveContainers(nodes, []);
    const names = containers.map((c) => c.name).sort();
    expect(names).toEqual(["auth", "cart"]);
  });

  it("collapses nested folders into the first segment", () => {
    const nodes = [
      node("a", "auth/handlers/oauth.go"),
      node("b", "auth/services/token.go"),
      node("c", "cart/cart.go"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.find((c) => c.name === "auth")?.nodeIds.sort()).toEqual(["a", "b"]);
  });

  it("places nodes without filePath in '~' container", () => {
    const nodes = [
      node("a", "auth/login.go"),
      node("b", "auth/oauth.go"),
      node("c"),
      node("d"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.find((c) => c.name === "~")?.nodeIds.sort()).toEqual(["c", "d"]);
  });

  it("suppresses single-child containers (single child becomes ungrouped)", () => {
    const nodes = [
      node("a", "auth/login.go"),
      node("b", "auth/oauth.go"),
      node("c", "cart/cart.go"),
    ];
    const { containers, ungrouped } = deriveContainers(nodes, []);
    // 'cart' has only 1 child → suppressed
    expect(containers.find((c) => c.name === "cart")).toBeUndefined();
    expect(ungrouped).toContain("c");
    // 'auth' kept
    expect(containers.find((c) => c.name === "auth")?.nodeIds.sort()).toEqual(["a", "b"]);
  });

  it("returns flat (no containers) when total nodes < 8", () => {
    const nodes = [
      node("a", "auth/x.go"),
      node("b", "cart/y.go"),
      node("c", "logs/z.go"),
    ];
    const { containers, ungrouped } = deriveContainers(nodes, []);
    expect(containers).toHaveLength(0);
    expect(ungrouped.sort()).toEqual(["a", "b", "c"]);
  });
});

describe("deriveContainers — no anonymous clusters", () => {
  it("groups a flat folder by filename prefix", () => {
    const nodes = [
      node("a1", "services/agent-runner.ts"),
      node("a2", "services/agent-memory.ts"),
      node("t1", "services/task.service.ts"),
      node("t2", "services/taskQueue.ts"),
      node("s", "services/skill.ts"),
    ];
    const { containers, ungrouped } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual(["agent*", "task*"]);
    for (const c of containers) expect(c.strategy).toBe("prefix");
    expect(ungrouped).toEqual(["s"]);
  });

  it("keeps folder boxes when one folder holds > 70%", () => {
    const nodes = [
      ...Array.from({ length: 8 }, (_, i) => node(`big${i}`, `big/file${i}.go`)),
      node("a", "small/a.go"),
      node("b", "small/b.go"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual(["big", "small"]);
  });

  it("keeps parent-level files in a '~' box next to folder boxes", () => {
    const nodes = [
      node("r1", "risk/grc.ts"),
      node("r2", "risk/scoring.ts"),
      node("h1", "risk/handlers/a.ts"),
      node("h2", "risk/handlers/b.ts"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual(["handlers", "~"]);
  });

  it("keeps all-caps filename prefixes whole", () => {
    const nodes = [
      node("a", "docs/LENS_SETUP.md"),
      node("b", "docs/LENS-ROADMAP.md"),
      node("c", "docs/risk-model.md"),
      node("d", "docs/risk-packs.md"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual(["lens*", "risk*"]);
  });

  it("never emits Cluster names", () => {
    const nodes = Array.from({ length: 10 }, (_, i) => node(`n${i}`, `services/n${i}.go`));
    const { containers } = deriveContainers(nodes, []);
    for (const c of containers) expect(c.name).not.toMatch(/^Cluster /);
  });
});

describe("deriveContainers — oversized boxes split deeper", () => {
  const many = (n: number, path: (i: number) => string, prefix: string) =>
    Array.from({ length: n }, (_, i) => node(`${prefix}${i}`, path(i)));

  it("splits a big folder box into its sub-folders", () => {
    const nodes = [
      ...many(15, (i) => `src/capabilities/handlers/h${i}.ts`, "h"),
      ...many(10, (i) => `src/capabilities/registry/r${i}.ts`, "r"),
      ...many(3, (i) => `src/lib/l${i}.ts`, "l"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual([
      "capabilities/handlers",
      "capabilities/registry",
      "lib",
    ]);
  });

  it("re-splits a big prefix box on the next filename word", () => {
    const nodes = [
      ...many(12, (i) => `risk/grc-risk-${i}.ts`, "rk"),
      ...many(12, (i) => `risk/grc-evidence-${i}.ts`, "ev"),
      ...many(3, (i) => `risk/flag-${i}.ts`, "fl"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual([
      "flag*",
      "grc-evidence*",
      "grc-risk*",
    ]);
  });

  it("keeps sub-groups under three files together in an (other) box", () => {
    const nodes = [
      ...many(5, (i) => `handlers/code-${i}.ts`, "c"),
      ...many(20, (i) => `handlers/v${i}-thing.ts`, "v"),
      node("x", "lib/x.ts"),
      node("y", "lib/y.ts"),
    ];
    const { containers, ungrouped } = deriveContainers(nodes, []);
    const names = containers.map((c) => c.name).sort();
    expect(names).toEqual(["handlers (other)", "handlers/code*", "lib"]);
    expect(containers.find((c) => c.name === "handlers (other)")!.nodeIds).toHaveLength(20);
    expect(ungrouped).toEqual([]);
  });

  it("names split boxes by their full folder path from the layer root", () => {
    const nodes = [
      ...many(12, (i) => `lixedin/server/src/routes/r${i}.ts`, "r"),
      ...many(12, (i) => `lixedin/server/src/services/s${i}.ts`, "s"),
      ...many(3, (i) => `lixedin/ui/src/u${i}.tsx`, "u"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.map((c) => c.name).sort()).toEqual([
      "server/src/routes",
      "server/src/services",
      "ui",
    ]);
  });

  it("leaves a big box whole when nothing divides it", () => {
    const nodes = [
      ...many(25, (i) => `handlers/${String.fromCharCode(97 + i)}x.ts`, "u"),
      ...many(3, (i) => `lib/l${i}.ts`, "l"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(containers.find((c) => c.name === "handlers")!.nodeIds).toHaveLength(25);
  });

  it("gives every box a unique id and name", () => {
    const nodes = [
      ...many(14, (i) => `components/agent-${i}.tsx`, "ca"),
      ...many(14, (i) => `components/task-${i}.tsx`, "ct"),
      ...many(14, (i) => `pages/agent-${i}.tsx`, "pa"),
      ...many(14, (i) => `pages/task-${i}.tsx`, "pt"),
    ];
    const { containers } = deriveContainers(nodes, []);
    expect(new Set(containers.map((c) => c.id)).size).toBe(containers.length);
    expect(new Set(containers.map((c) => c.name)).size).toBe(containers.length);
    for (const c of containers) expect(c.nodeIds.length).toBeLessThanOrEqual(20);
  });
});

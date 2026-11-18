import { describe, expect, it } from "vitest";

import type { Evidence, IRGraph } from "@uicodeanalysis/contracts";

import { collectFocusNodeIds, resolveVisibleNodeIds, sliceGraph } from "./focus";

const evidence: [Evidence, ...Evidence[]] = [{ source: "static", confidence: 1 }];

function graph(): IRGraph {
  return {
    version: "1.1",
    nodes: ["A", "B", "C", "D", "E"].map((id) => ({
      id,
      kind: "component" as const,
      name: id,
      attrs: {},
      evidence,
    })),
    edges: [
      { id: "ab", from: "A", to: "B", kind: "renders", evidence },
      { id: "bc", from: "B", to: "C", kind: "calls", evidence },
      { id: "cd", from: "C", to: "D", kind: "renders", evidence },
    ],
  };
}

describe("collectFocusNodeIds", () => {
  it("expands along edges in both directions", () => {
    const ids = collectFocusNodeIds(graph(), "B", new Set(["A", "B", "C", "D"]), 1);

    // 上游 A 与下游 C 都在，跳数以外的 D 不在
    expect(ids).toEqual(new Set(["B", "A", "C"]));
  });

  it("respects the allowed filter", () => {
    const ids = collectFocusNodeIds(graph(), "B", new Set(["B", "C"]), 2);

    expect(ids).toEqual(new Set(["B", "C"]));
  });

  it("returns empty when the seed is filtered out", () => {
    expect(collectFocusNodeIds(graph(), "B", new Set(["A"]))).toEqual(new Set());
  });

  it("caps the result at the node limit", () => {
    const ids = collectFocusNodeIds(graph(), "B", new Set(["A", "B", "C", "D"]), 3, 2);

    expect(ids.size).toBe(2);
    expect(ids.has("B")).toBe(true);
  });
});

describe("sliceGraph", () => {
  it("keeps only nodes in the set and edges between them", () => {
    const sliced = sliceGraph(graph(), new Set(["B", "C"]));

    expect(sliced.nodes.map((node) => node.id)).toEqual(["B", "C"]);
    expect(sliced.edges.map((edge) => edge.id)).toEqual(["bc"]);
  });

  it("supports exact server-provided focus ids without expanding neighbors", () => {
    const allowed = new Set(["A", "B", "C", "D"]);
    const exact = new Set(["B", "D"]);
    const visible = new Set([...exact].filter((nodeId) => allowed.has(nodeId)));
    const sliced = sliceGraph(graph(), visible);

    expect(sliced.nodes.map((node) => node.id)).toEqual(["B", "D"]);
    expect(sliced.edges).toEqual([]);
  });
});

describe("resolveVisibleNodeIds", () => {
  const allowed = new Set(["A", "B", "C", "D", "E"]);

  it("uses the exact subgraph when focus mode is on", () => {
    const visible = resolveVisibleNodeIds(graph(), {
      allowed,
      selectedNodeId: "B",
      exactNodeIds: new Set(["B", "D"]),
      focusMode: true,
    });

    // 不做邻域扩展：C 是 B 的直接邻居，但不属于后端给的子图
    expect(visible).toEqual(new Set(["B", "D"]));
  });

  it("intersects with the search/kind filter", () => {
    const visible = resolveVisibleNodeIds(graph(), {
      allowed: new Set(["B", "C"]),
      selectedNodeId: "B",
      exactNodeIds: new Set(["B", "D"]),
      focusMode: true,
    });

    expect(visible).toEqual(new Set(["B"]));
  });

  it("falls back to neighborhood focus when the filter removes the whole subgraph", () => {
    const visible = resolveVisibleNodeIds(graph(), {
      allowed: new Set(["C", "D"]),
      selectedNodeId: null,
      exactNodeIds: new Set(["A", "B"]),
      focusMode: true,
    });

    // 退回到过滤结果的第一个节点 C 的邻域，而不是渲染空图
    expect(visible.has("C")).toBe(true);
    expect(visible.size).toBeGreaterThan(0);
  });

  it("keeps the full filtered set when focus mode is off", () => {
    const visible = resolveVisibleNodeIds(graph(), {
      allowed,
      selectedNodeId: "B",
      exactNodeIds: new Set(["B"]),
      focusMode: false,
    });

    expect(visible).toBe(allowed);
  });

  it("expands neighbors when no exact ids are given", () => {
    const visible = resolveVisibleNodeIds(graph(), {
      allowed,
      selectedNodeId: "B",
      exactNodeIds: null,
      focusMode: true,
      depth: 1,
    });

    expect(visible).toEqual(new Set(["B", "A", "C"]));
  });
});

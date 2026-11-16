import { describe, expect, it } from "vitest";

import type { IRGraph } from "@uicodeanalysis/contracts";

import { buildFlowGraph } from "./layout";

const graph: IRGraph = {
  version: "1.1",
  nodes: [
    {
      id: "src/Table.tsx#Table",
      kind: "component",
      name: "Table",
      attrs: { file: "src/Table.tsx" },
      evidence: [
        {
          source: "static",
          confidence: 1,
          file: "src/Table.tsx",
          loc: { line: 8, col: 1 },
        },
      ],
    },
    {
      id: "src/Table.tsx#Row",
      kind: "component",
      name: "Row",
      attrs: { file: "src/Table.tsx" },
      evidence: [{ source: "static", confidence: 0.9, file: "src/Table.tsx" }],
    },
    {
      id: "api:/orders",
      kind: "api",
      name: "GET /orders",
      attrs: {},
      evidence: [{ source: "runtime", confidence: 1, traceId: "trc_1" }],
    },
  ],
  edges: [
    {
      id: "renders",
      from: "src/Table.tsx#Table",
      to: "src/Table.tsx#Row",
      kind: "renders",
      evidence: [{ source: "static", confidence: 1 }],
    },
    {
      id: "requests",
      from: "src/Table.tsx#Row",
      to: "api:/orders",
      kind: "requests",
      evidence: [
        { source: "runtime", confidence: 1, traceId: "trc_1" },
        { source: "ai", confidence: 0.5 },
      ],
    },
    {
      id: "ai",
      from: "src/Table.tsx#Table",
      to: "api:/orders",
      kind: "calls",
      evidence: [{ source: "ai", confidence: 0.55 }],
    },
  ],
};

describe("buildFlowGraph", () => {
  it("keeps every component declared in the same file", () => {
    const { nodes } = buildFlowGraph(graph);
    const ids = nodes.map((node) => node.id);

    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    expect(ids).toContain("src/Table.tsx#Table");
    expect(ids).toContain("src/Table.tsx#Row");
  });

  it("produces finite dagre positions", () => {
    const { nodes } = buildFlowGraph(graph);

    for (const node of nodes) {
      expect(Number.isFinite(node.position.x)).toBe(true);
      expect(Number.isFinite(node.position.y)).toBe(true);
    }
  });

  it("styles runtime edges solid and AI edges dashed and animated", () => {
    const { edges } = buildFlowGraph(graph);
    const runtimeEdge = edges.find((edge) => edge.id === "requests");
    const aiEdge = edges.find((edge) => edge.id === "ai");

    expect(runtimeEdge?.animated).toBeFalsy();
    expect(runtimeEdge?.style).toMatchObject({ strokeDasharray: undefined });

    expect(aiEdge?.animated).toBe(true);
    expect(aiEdge?.style).toMatchObject({ strokeDasharray: "6 4" });
    expect(aiEdge?.label).toBe("调用");
  });
});

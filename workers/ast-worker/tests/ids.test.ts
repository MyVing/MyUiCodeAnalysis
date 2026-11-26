/**
 * 业务 id 定长收敛：超长节点 id 不能把行表 `VARCHAR(512)` 顶爆。
 */

import { describe, expect, it } from "vitest";

import { canonicalId, MAX_NODE_ID_LENGTH } from "../src/ids.js";

describe("canonicalId", () => {
  it("未超长时原样返回", () => {
    const id = "src/pages/UserList.tsx#UserList/jsx:button/handleExport";
    expect(canonicalId(id)).toBe(id);
  });

  it("超长时收敛到固定上限并保留可读前缀", () => {
    const full = `src/${"nested/".repeat(80)}View.tsx#View/jsx:button/handleSubmit`;
    const id = canonicalId(full);
    expect(id.length).toBe(MAX_NODE_ID_LENGTH);
    expect(id.startsWith("src/nested/")).toBe(true);
    expect(id).toContain("~");
  });

  it("确定性：同输入永远同输出，不同输入不会碰撞到同一结果", () => {
    const base = "a".repeat(MAX_NODE_ID_LENGTH);
    const first = canonicalId(`${base}#One`);
    const second = canonicalId(`${base}#One`);
    const other = canonicalId(`${base}#Two`);
    expect(first).toBe(second);
    expect(first).not.toBe(other);
  });

  it("幂等：已收敛的 id 再处理不变化", () => {
    const full = `src/${"deep/".repeat(100)}X.tsx#X`;
    const once = canonicalId(full);
    expect(canonicalId(once)).toBe(once);
  });

  it("节点上限保证最长边 id 不超过 512", () => {
    const longestKind = "navigates";
    const nodeId = "x".repeat(MAX_NODE_ID_LENGTH);
    const edgeId = `e_${longestKind}_${nodeId}__${nodeId}`;
    expect(edgeId.length).toBeLessThanOrEqual(512);
  });
});

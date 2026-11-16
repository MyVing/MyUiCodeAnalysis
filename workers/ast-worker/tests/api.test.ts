/**
 * API 链路分析测试。
 *
 * 重点守住四件事：
 *  1. URL 出现在**封装器的调用点**，而不是 `fetch` 所在处——只扫 fetch 会拿到
 *     一堆 `` `${url}${query}` ``，那没有任何信息
 *  2. 封装器自己**不产** api 节点（它体内只有参数），由调用点负责
 *  3. 识别 HTTP 客户端靠「名字 + 符号来自项目外」，不是纯名字匹配：
 *     项目里自己写的 `fetch` 包装不能被当成浏览器 fetch
 *  4. 链路要连成 `组件 ──calls──> 函数 ──requests──> 接口`
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { IREdge } from "@uicodeanalysis/contracts";

import { analyzeApi, type ApiAnalysisResult } from "../src/api.js";
import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

function analyze(rootPath: string): ApiAnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  return analyzeApi({
    rootPath,
    project,
    definitionIndex: discovery.definitionIndex,
  });
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): ApiAnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-api-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir);
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

/** 取一条边；不存在直接失败，避免断言静默跳过。 */
function edgeOf(result: ApiAnalysisResult, kind: IREdge["kind"], to: string): IREdge {
  const edge = result.edges.find((e) => e.kind === kind && e.to === to);
  if (!edge) {
    const actual = result.edges.map((e) => `${e.kind}:${e.to}`).join("\n");
    throw new Error(`缺少 ${kind} 边指向 ${to}；实际边：\n${actual}`);
  }
  return edge;
}

function apiIds(result: ApiAnalysisResult): string[] {
  return result.apiNodes.map((n) => n.id).sort();
}

describe("API 链路（真实样本仓）", () => {
  let result: ApiAnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("从服务层封装的调用点还原出接口，而不是 `${url}${query}`", () => {
    expect(apiIds(result)).toEqual(["api:GET:/api/users"]);

    const node = result.apiNodes[0];
    expect(node?.name).toBe("GET /api/users");
    expect(node?.attrs).toMatchObject({ method: "GET", url: "/api/users" });
    // URL 字面量在 UserList.tsx:21 的实参里，不在 request.ts 的 fetch 处
    expect(node?.file).toBe("src/pages/UserList.tsx");
    expect(node?.loc).toEqual({ line: 21, col: 10 });
  });

  it("封装器自身不产 api 节点：它的 URL 只是参数", () => {
    // request({ url }) 被调用时才产出接口；request.ts 里的 fetch 不该单独成点
    expect(result.apiNodes.every((n) => n.file !== "src/services/request.ts")).toBe(true);
  });

  it("链路成环：组件 calls 函数，函数 requests 接口", () => {
    const fnId = "src/pages/UserList.tsx#fetchUsers";
    const apiId = "api:GET:/api/users";

    const requests = edgeOf(result, "requests", apiId);
    expect(requests.from).toBe(fnId);
    expect(requests.evidence[0]).toMatchObject({ source: "static", confidence: 0.9 });
    expect(requests.evidence[0].loc?.line).toBe(21);

    const calls = edgeOf(result, "calls", fnId);
    expect(calls.from).toBe("src/pages/UserList.tsx#UserList");
    // 调用发生在 useEffect 内，归属仍是最外层组件
    expect(calls.evidence[0].loc?.line).toBe(33);
  });

  it("请求链路上的函数以 fn 节点入图，带 file / loc", () => {
    expect(result.fnNodes.map((n) => n.id)).toEqual(["src/pages/UserList.tsx#fetchUsers"]);

    const fn = result.fnNodes[0];
    expect(fn?.kind).toBe("fn");
    expect(fn?.file).toBe("src/pages/UserList.tsx");
    expect(fn?.loc).toEqual({ line: 19, col: 1 });
    expect(fn?.evidence[0]).toMatchObject({ source: "static", confidence: 1 });
  });

  it("请求点是语法事实，但经参数转手后置信度低于直接字面量", () => {
    // 0.9：URL 与请求点的对应关系靠参数流转推出，不是直接读到的
    expect(result.apiNodes[0]?.evidence[0].confidence).toBe(0.9);
    expect(result.apiNodes[0]?.evidence[0].confidence).toBeLessThan(1);
  });

  it("所有边端点都在本模块产出的节点集合里", () => {
    const ids = new Set([
      ...result.apiNodes.map((n) => n.id),
      ...result.fnNodes.map((n) => n.id),
    ]);
    for (const edge of result.edges) {
      // from 可能是组件 id（由 components 模块产出），只校验 to
      expect(ids.has(edge.to)).toBe(true);
      expect(edge.evidence.length).toBeGreaterThan(0);
    }
  });
});

describe("API 链路的边界（合成项目）", () => {
  it("直接调用 fetch 且 URL 是字面量时置信度为 1", () => {
    const result = analyzeSynthetic({
      "src/Direct.tsx": `
        export function Direct() {
          fetch("/api/direct", { method: "POST" });
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:POST:/api/direct"]);
    expect(result.apiNodes[0]?.evidence[0].confidence).toBe(1);
    // 组件自己发请求，没有中间函数
    expect(result.fnNodes).toEqual([]);
    expect(edgeOf(result, "requests", "api:POST:/api/direct").from).toBe("src/Direct.tsx#Direct");
  });

  it("项目内自定义的同名 fetch 不被当成 HTTP 客户端", () => {
    const result = analyzeSynthetic({
      "src/lib/fetch.ts": `
        export function fetch(url: string) { return url; }
      `,
      "src/Use.tsx": `
        import { fetch } from "./lib/fetch";
        export function Use() {
          fetch("/api/fake");
          return <div />;
        }
      `,
    });

    // 名字对上了，但符号在项目内 —— 这是项目自己的函数，不是浏览器 fetch
    expect(apiIds(result)).toEqual([]);
  });

  it("URL 通过局部变量转一手时置信度降到 0.8", () => {
    const result = analyzeSynthetic({
      "src/ViaVar.tsx": `
        export function ViaVar() {
          const url = "/api/indirect";
          fetch(url);
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/indirect"]);
    expect(result.apiNodes[0]?.evidence[0].confidence).toBe(0.8);
  });

  it("带插值的 URL 按形态归一，不同 id 归到同一个接口", () => {
    const result = analyzeSynthetic({
      "src/Dynamic.tsx": `
        export function Dynamic({ id }: { id: number }) {
          fetch(\`/api/users/\${id}\`);
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/users/${id}"]);
  });

  it("URL 完全由变量拼成、追不到字面量时不产节点", () => {
    const result = analyzeSynthetic({
      "src/Opaque.tsx": `
        declare const baseUrl: string;
        export function Opaque() {
          fetch(baseUrl + "/users");
          return <div />;
        }
      `,
    });

    // 只追变量初始化表达式；declare 的常量没有初始化值，追不到
    expect(apiIds(result)).toEqual([]);
  });

  it("同一个接口被同一组件多处调用时只产一个节点、一条边，证据合并", () => {
    const result = analyzeSynthetic({
      "src/Dup.tsx": `
        export function Dup() {
          fetch("/api/dup");
          return <div onClick={() => fetch("/api/dup")} />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/dup"]);
    const requests = result.edges.filter((e) => e.kind === "requests");
    expect(requests).toHaveLength(1);
    expect(requests[0]?.evidence.map((e) => e.loc?.line).sort()).toEqual([3, 4]);
  });

  it("方法不同则接口不同：同一路径的 GET 与 POST 是两个节点", () => {
    const result = analyzeSynthetic({
      "src/Methods.tsx": `
        export function Methods() {
          fetch("/api/items");
          fetch("/api/items", { method: "POST" });
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/items", "api:POST:/api/items"]);
  });

  it("封装器的默认方法参与判定：调用点不传 method 时取解构默认值", () => {
    const result = analyzeSynthetic({
      "src/http.ts": `
        export async function get<T>(options: { url: string }): Promise<T> {
          const { url } = options;
          return fetch(url) as Promise<T>;
        }
      `,
      "src/Caller.tsx": `
        import { get } from "./http";
        export function Caller() {
          get({ url: "/api/default" });
          return <div />;
        }
      `,
    });

    // 封装器体内 fetch 的 URL 就是参数本身
    expect(apiIds(result)).toEqual(["api:GET:/api/default"]);
  });

  it("外部依赖的方法调用（axios.post）按方法名推断 HTTP 方法", () => {
    const result = analyzeSynthetic({
      "node_modules/axios/index.d.ts": `
        declare const axios: { post(url: string, data?: unknown): Promise<unknown> };
        export default axios;
      `,
      "src/Axios.tsx": `
        import axios from "axios";
        export function Axios() {
          axios.post("/api/axios");
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:POST:/api/axios"]);
  });

  it("封装器之间的转发也追得动：两层封装各转一手", () => {
    const result = analyzeSynthetic({
      "src/http.ts": `
        export function send(options: { url: string }) {
          return fetch(options.url);
        }
      `,
      "src/api.ts": `
        import { send } from "./http";
        export function getUser(options: { path: string }) {
          return send({ url: options.path });
        }
      `,
      "src/Page.tsx": `
        import { getUser } from "./api";
        export function Page() {
          getUser({ path: "/api/user" });
          return <div />;
        }
      `,
    });

    // getUser 的 URL 来自自己的参数，它自己不产 api 节点；
    // 真正决定 URL 的是 Page 里的调用点，所以接口在这里产出
    expect(apiIds(result)).toEqual(["api:GET:/api/user"]);

    const requests = edgeOf(result, "requests", "api:GET:/api/user");
    expect(requests.from).toBe("src/Page.tsx#Page");

    // 封装器是「实现细节」而不是「业务函数」：链路上的 getUser / send 都不成节点。
    // fn 节点只留给组件调用、自己不属于封装器的那些函数（如 fetchUsers）。
    expect(result.fnNodes).toEqual([]);
  });

  it("非请求的普通调用不产任何节点或边", () => {
    const result = analyzeSynthetic({
      "src/Plain.tsx": `
        declare function formatDate(v: string): string;
        export function Plain() {
          formatDate("2024");
          return <div />;
        }
      `,
    });

    expect(result.apiNodes).toEqual([]);
    expect(result.fnNodes).toEqual([]);
    expect(result.edges).toEqual([]);
  });
});

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
import { analyzeHooks } from "../src/hooks.js";
import { createFullProject } from "../src/project.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

function analyze(rootPath: string): ApiAnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  // 与 index.ts 同序：hooks 先于 api，api 才能读到 hook 回调体归属
  const hooks = analyzeHooks({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  return analyzeApi({
    rootPath,
    project,
    definitionIndex: discovery.definitionIndex,
    hookCallbackOwners: hooks.callbackOwners,
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

describe("外部服务对象与 URL 常量拼接", () => {
  const cvmModule = `
    import { CvmService } from "@lu14/cvm2-web-react";

    export const postHttp = CvmService.post;

    export function mockLogin(data: any) {
      return CvmService.post(\`/cmvcweb/v1/localSin\`, data, { mask: true });
    }

    export function save(data: any) {
      return postHttp("/svc/b", data);
    }

    const BAS_CFG_SVC = "/bas";
    const V1 = "/v1";
    const getMenuConfigUrl = BAS_CFG_SVC + V1 + "/front/getMenuConfig";

    export function getMenuConfig() {
      return CvmService.post(getMenuConfigUrl, {});
    }

    const PREFIX = "/cmvcweb" + V1;

    export function localSin() {
      return CvmService.get(\`\${PREFIX}/localSin\`);
    }
  `;

  it("外部服务对象的直调（模板 URL）能产出接口", () => {
    const result = analyzeSynthetic({ "src/services/cvm.ts": cvmModule });
    expect(apiIds(result)).toContain("api:POST:/cmvcweb/v1/localsin");
  });

  it("别名导出的客户端调用能识别，且方法是 POST 而不是 GET", () => {
    const result = analyzeSynthetic({ "src/services/cvm.ts": cvmModule });
    const node = result.apiNodes.find((candidate) => candidate.id === "api:POST:/svc/b");
    expect(node).toBeDefined();
    expect(node?.attrs).toMatchObject({ method: "POST", url: "/svc/b" });
  });

  it("常量 + 拼接的 URL 能求值", () => {
    const result = analyzeSynthetic({ "src/services/cvm.ts": cvmModule });
    expect(apiIds(result)).toContain("api:POST:/bas/v1/front/getmenuconfig");
  });

  it("模板前缀由常量拼接时仍能求值", () => {
    const result = analyzeSynthetic({ "src/services/cvm.ts": cvmModule });
    expect(apiIds(result)).toContain("api:GET:/cmvcweb/v1/localsin");
  });

  it("外部通用库的 get(obj, path) 不会被误判成客户端", () => {
    const result = analyzeSynthetic({
      "src/utils/pick.ts": `
        import _ from "lodash";
        export function pick(source: any) {
          return _.get(source, "a.b");
        }
      `,
    });
    expect(result.apiNodes).toHaveLength(0);
  });

  it("项目内定义的 router.get 不会被误判成客户端", () => {
    const result = analyzeSynthetic({
      "src/nav.ts": `
        const router = { get: (path: string) => path };
        export function nav() {
          return router.get("/users");
        }
      `,
    });
    expect(result.apiNodes).toHaveLength(0);
  });

  it("既有封装器链路不回归", () => {
    const result = analyzeSynthetic({
      "src/services/http.ts": `
        export async function request(url: string, init?: any) {
          return fetch(url, init);
        }
      `,
      "src/pages/User.tsx": `
        import { request } from "../services/http";
        export function User() {
          const load = () => request("/api/user");
          return <button onClick={load}>load</button>;
        }
      `,
    });
    expect(apiIds(result)).toContain("api:GET:/api/user");
  });
});

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

    // 插值名不进 URL：`${id}` / `${userId}` / `${key}` 指向的是同一个接口，
    // 保留变量名反而会让同一接口按调用处的变量名分裂成多个节点。
    // 原始写法记在 urlRaw，回查时仍能看到代码里到底写了什么。
    expect(apiIds(result)).toEqual(["api:GET:/api/users/:param"]);
    expect(result.apiNodes[0]?.attrs.urlRaw).toBe("`/api/users/${id}`");
  });

  it("URL 归一化：origin / query / hash / 尾斜杠去掉，数字与 UUID 段归一成 :param", () => {
    const result = analyzeSynthetic({
      "src/Normalize.tsx": `
        export function Normalize({ id }: { id: string }) {
          fetch("https://api.example.com/api/users/42?page=1#top");
          fetch("/api//users/7/");
          fetch("/api/users/123");
          fetch("/api/users/550e8400-e29b-41d4-a716-446655440000");
          fetch(\`/api/users/\${id}\`);
          return <div />;
        }
      `,
    });

    // 五种写法全部指向同一个接口：绝对地址、查询串、锚点、重复斜杠、尾斜杠
    // 都不改变接口身份；数字 / UUID / 插值都是同一资源的不同实例
    expect(apiIds(result)).toEqual(["api:GET:/api/users/:param"]);
  });

  it("大写路径按比较不敏感口径归一，与原写法共用原始回显", () => {
    const result = analyzeSynthetic({
      "src/Cased.tsx": `
        export function Cased() {
          fetch("/API/Users");
          return <div />;
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/users"]);
    expect(result.apiNodes[0]?.attrs.urlRaw).toBe('"/API/Users"');
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

  it("请求体字段包装支持 axios data、fetch body 和直接对象参数", () => {
    const result = analyzeSynthetic({
      "node_modules/axios/index.d.ts": `
        declare const axios: { post(url: string, data?: unknown): Promise<unknown> };
        export default axios;
      `,
      "src/Payloads.tsx": `
        import axios from "axios";
        export function Payloads() {
          const user = { name: "Ada" };
          axios.post("/api/axios-data", { user });
          fetch("/api/fetch-body", { body: { profile: user } });
          fetch("/api/fetch-json", { json: { account: user } });
          return <div />;
        }
      `,
    });

    const bodies = new Map(
      result.apiNodes.map((node) => [node.attrs.url as string, node.attrs.requestBody]),
    );
    expect(bodies.get("/api/axios-data")).toMatchObject({ wrapper: "user", source: "user" });
    expect(bodies.get("/api/fetch-body")).toMatchObject({ wrapper: "profile", source: "user" });
    expect(bodies.get("/api/fetch-json")).toMatchObject({ wrapper: "account", source: "user" });
  });

  it("动态请求体不冒充静态字段包装", () => {
    const result = analyzeSynthetic({
      "src/DynamicBody.tsx": `
        function buildPayload(user: unknown) { return { user }; }
        export function DynamicBody() {
          const payload = buildPayload({ name: "Ada" });
          fetch("/api/dynamic-body", { body: payload });
          return <div />;
        }
      `,
    });

    expect(result.apiNodes[0]?.attrs.requestBody).toBeUndefined();
  });

  it("从解构参数的静态类型提取直接请求体字段", () => {
    const result = analyzeSynthetic({
      "node_modules/axios/index.d.ts": `declare const axios: { post(url: string, data?: unknown): Promise<unknown> }; export default axios;`,
      "src/create.ts": `import axios from "axios"; type Input = { title: string; body: string }; export function create({ data }: { data: Input }) { return axios.post("/api/create", data); }`,
    });

    const body = result.apiNodes[0]?.attrs.requestBody;
    expect(body).toMatchObject({ wrapper: "", source: "data" });
    expect((body as { fields?: Array<{ name: string }> } | undefined)?.fields?.map((field) => field.name)).toEqual(["title", "body"]);
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

/**
 * 请求归属（设计文档 §6.11.3，缺陷 #23）。
 *
 * `findOwner` 只认组件定义位置时，`const handleExport = useCallback(() => fetch(...), [])`
 * 体内的请求会被记到组件名下：图上呈现成「页面加载时请求」，而事实是「点击才请求」。
 * 这不只是少一条边，是把交互链的语义说反了。
 */
describe("请求归属到 hook 回调体", () => {
  it("useCallback 体内的请求归 hook 节点，不归组件", () => {
    const result = analyzeSynthetic({
      "src/Export.tsx": `
        import { useCallback } from "react";

        export function Export() {
          const handleExport = useCallback(async () => {
            await fetch("/api/orders/export");
          }, []);

          return <button onClick={handleExport}>导出</button>;
        }
      `,
    });

    const requests = result.edges.find((e) => e.kind === "requests");
    expect(requests?.from).toBe("src/Export.tsx#Export/handleExport");
    // 不要再出现一条从组件指向同一接口的 requests 边
    expect(result.edges.some((e) => e.kind === "requests" && e.from === "src/Export.tsx#Export")).toBe(
      false,
    );
  });

  it("hook 回调体是图上已有节点，不补 fn 节点", () => {
    const result = analyzeSynthetic({
      "src/Export.tsx": `
        import { useCallback } from "react";

        export function Export() {
          const handleExport = useCallback(async () => {
            await fetch("/api/orders/export");
          }, []);
          return <button onClick={handleExport}>导出</button>;
        }
      `,
    });

    // 补一个同名 fn 会让同一个函数在图上出现两次
    expect(result.fnNodes).toEqual([]);
  });

  it("useEffect 体内的请求仍归组件：挂载即执行，不是具名动作", () => {
    const result = analyzeSynthetic({
      "src/Loading.tsx": `
        import { useEffect } from "react";

        export function Loading() {
          useEffect(() => {
            fetch("/api/users");
          }, []);
          return <div />;
        }
      `,
    });

    const requests = result.edges.find((e) => e.kind === "requests");
    expect(requests?.from).toBe("src/Loading.tsx#Loading");
    expect(result.fnNodes).toEqual([]);
  });

  it("useMemo 回调体与 useCallback 同样归 hook 节点", () => {
    const result = analyzeSynthetic({
      "src/Memo.tsx": `
        import { useMemo } from "react";

        export function Memo({ id }: { id: string }) {
          const load = useMemo(
            () => () => fetch(\`/api/users/\${id}\`),
            [id],
          );
          return <button onClick={load}>加载</button>;
        }
      `,
    });

    const requests = result.edges.find((e) => e.kind === "requests");
    expect(requests?.from).toBe("src/Memo.tsx#Memo/load");
  });

  it("hook 回调体里调用模块级函数时，calls 边从 hook 节点出发", () => {
    const result = analyzeSynthetic({
      "src/services/users.ts": `
        export async function fetchUsers() {
          return fetch("/api/users");
        }
      `,
      "src/Refresh.tsx": `
        import { useCallback } from "react";
        import { fetchUsers } from "./services/users";

        export function Refresh() {
          const handleRefresh = useCallback(() => {
            void fetchUsers();
          }, []);
          return <button onClick={handleRefresh}>刷新</button>;
        }
      `,
    });

    const calls = result.edges.find(
      (e) => e.kind === "calls" && e.to === "src/services/users.ts#fetchUsers",
    );
    expect(calls?.from).toBe("src/Refresh.tsx#Refresh/handleRefresh");
  });

  it("回调体内部定义的局部函数，其请求仍归宿主 hook（局部函数没有独立身份）", () => {
    const result = analyzeSynthetic({
      "src/Nested.tsx": `
        import { useCallback } from "react";

        export function Nested() {
          const handleSave = useCallback(async () => {
            async function buildPayload() {
              return fetch("/api/payload");
            }
            await buildPayload();
          }, []);
          return <button onClick={handleSave}>保存</button>;
        }
      `,
    });

    const requests = result.edges.find((e) => e.kind === "requests");
    // buildPayload 只能在这个回调体里被调用，不构成独立节点；给它补 fn 节点会
    // 造出一个谁也无法从外部引用到的孤立节点
    expect(requests?.from).toBe("src/Nested.tsx#Nested/handleSave");
    expect(result.fnNodes).toEqual([]);
  });

  it("同一组件的多个回调各自归属，不被合并到同一个 hook", () => {
    const result = analyzeSynthetic({
      "src/Actions.tsx": `
        import { useCallback } from "react";

        export function Actions({ id }: { id: string }) {
          const handleExport = useCallback(async () => {
            await fetch("/api/orders/export");
          }, []);
          const handleDelete = useCallback(async () => {
            await fetch(\`/api/orders/\${id}\`, { method: "DELETE" });
          }, [id]);
          return <div />;
        }
      `,
    });

    const fromHooks = result.edges
      .filter((e) => e.kind === "requests")
      .map((e) => e.from)
      .sort();
    expect(fromHooks).toEqual([
      "src/Actions.tsx#Actions/handleDelete",
      "src/Actions.tsx#Actions/handleExport",
    ]);
  });
});

/**
 * URL 前缀求值（设计文档 §6.11.1 L3）。
 *
 * 企业项目的 `fetch` 几乎不写完整路径，而是 `` `${API_PREFIX}/orders` ``，
 * 常量还常常放在另一个模块、并带 `import.meta.env` 兜底。前缀决定的是
 * 「这是哪个接口」，求不出来时整条边只能缺席——但必须告警，不能静默。
 */
describe("URL 前缀求值（跨模块常量）", () => {
  it("同文件常量前缀：求值后进接口清单", () => {
    const result = analyzeSynthetic({
      "src/Orders.ts": `
        const BASE = "/api";
        export async function listOrders() {
          return fetch(\`\${BASE}/orders\`);
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/orders"]);
    expect(result.apiNodes[0]?.attrs.urlRaw).toBe("`${BASE}/orders`");
  });

  it("跨模块常量 + env 兜底：穿透 import 求值", () => {
    const result = analyzeSynthetic({
      "src/env.ts": `
        export const API_PREFIX = import.meta.env.VITE_API_BASE ?? "/api";
      `,
      "src/Orders.ts": `
        import { API_PREFIX } from "./env";
        export async function listOrders() {
          return fetch(\`\${API_PREFIX}/orders\`);
        }
      `,
    });

    // env 的值运行时才知道，取显式 fallback；不求值时宁可不算，也不能把
    // `${API_PREFIX}/orders` 当成接口 url
    expect(apiIds(result)).toEqual(["api:GET:/api/orders"]);
    expect(result.warnings).toEqual([]);
  });

  it("import 别名不影响前缀求值", () => {
    const result = analyzeSynthetic({
      "src/env.ts": `export const BASE = "/v1";`,
      "src/Orders.ts": `
        import { BASE as ROOT } from "./env";
        export async function listOrders() {
          return fetch(\`\${ROOT}/orders\`);
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/v1/orders"]);
  });

  it("字面量拼接的前缀也能求值", () => {
    const result = analyzeSynthetic({
      "src/Orders.ts": `
        const BASE = "/api" + "/v2";
        export async function listOrders() {
          return fetch(\`\${BASE}/orders\`);
        }
      `,
    });

    expect(apiIds(result)).toEqual(["api:GET:/api/v2/orders"]);
  });

  it("前缀求不出来时不产节点，但记 api_url_prefix_unresolved 告警", () => {
    const result = analyzeSynthetic({
      "src/Orders.ts": `
        declare function buildBase(): string;
        export async function listOrders() {
          return fetch(\`\${buildBase()}/orders\`);
        }
      `,
    });

    // 函数返回值属于数据流分析，一期不求——求不出来就如实缺一条，不猜
    expect(result.apiNodes).toEqual([]);
    const warning = result.warnings.find((w) => w.code === "api_url_prefix_unresolved");
    expect(warning).toBeDefined();
    expect(warning?.severity).toBe("warning");
    expect(warning?.file).toBe("src/Orders.ts");
    // 告警里要能看到原始写法，否则用户不知道是哪个调用点没解析出来
    expect(warning?.message).toContain("buildBase()");
  });

  it("前缀未求值且后面还有路径参数时，同样告警而不是产残片", () => {
    const result = analyzeSynthetic({
      "src/Orders.ts": `
        declare const tenant: { base: string };
        export async function getOrder(id: string) {
          return fetch(\`\${tenant.base}/orders/\${id}\`);
        }
      `,
    });

    // `${tenant.base}` 是 URL 主体（前面没有 `/`，也不来自本函数参数），
    // 主体定不下来就整条不进清单——不能把 `/orders/${…}` 当成一个真实接口
    expect(result.apiNodes).toEqual([]);
    expect(result.warnings.some((w) => w.code === "api_url_prefix_unresolved")).toBe(true);
  });

  it("路径参数不被误判成 URL 封装器", () => {
    const result = analyzeSynthetic({
      "src/Users.ts": `
        export async function getUser(id: string) {
          return fetch(\`/api/users/\${id}\`);
        }
      `,
    });

    // 这是请求点，不是封装器：`id` 之于这个 URL 就像 `/users/1` 里的 `1`。
    // 若把它当封装器，真正的请求会被整条跳过且不告警。
    expect(apiIds(result)).toEqual(["api:GET:/api/users/:param"]);
    expect(result.warnings).toEqual([]);
  });

  it("查询参数插值不拆散 URL：query 整体归一掉，接口身份只看路径", () => {
    const result = analyzeSynthetic({
      "src/Users.ts": `
        export async function searchUsers(status: string) {
          return fetch(\`/api/users?status=\${status}\`);
        }
      `,
    });

    // `?status=${…}` 与 `?page=2` 是同一接口的不同查询，L1 把 query 整段去掉
    expect(apiIds(result)).toEqual(["api:GET:/api/users"]);
  });

  it("封装器的 URL 仍来自参数：路径参数修复不影响封装器识别", () => {
    const result = analyzeSynthetic({
      "src/http.ts": `
        export function request(url: string) {
          return fetch(\`\${url}?ts=\${Date.now()}\`);
        }
      `,
      "src/Caller.tsx": `
        import { request } from "./http";
        export function Caller() {
          request("/api/from-wrapper");
          return <div />;
        }
      `,
    });

    // URL 主体是裸参数：调用点决定真实地址，接口产在调用点
    expect(apiIds(result)).toEqual(["api:GET:/api/from-wrapper"]);
  });
});

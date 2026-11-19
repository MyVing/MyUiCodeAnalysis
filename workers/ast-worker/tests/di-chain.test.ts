/**
 * DI 架构下的链路解析测试。
 *
 * 企业项目里请求与事件常隔多层间接（本组用例对应 react-typescript-realworld
 * 的真实形态）：
 *
 *   Login.tsx:  const { onSubmit } = useLogin();  <form onSubmit={onSubmit}>
 *   useLogin.ts: const sessionService = inject<SessionServiceInterface>(...);
 *                const onSubmit = handleSubmit(async data => {
 *                  await sessionService.login(data);     // 方法调用走接口
 *                });
 *   session.service.ts:
 *                private readonly http = inject("httpclient", () => ky);
 *                async login(user) { await this.http.post("/users/login", ...) }
 *
 * 三层缺口都要补上，链条才能从元素走到接口：
 *  1. `this.http.post` —— receiver 的名字（this.http）认不出客户端，
 *     靠类型符号定位到项目外的依赖包（ky）
 *  2. `sessionService.login` —— 方法调用穿透接口声明找到实现类方法
 *  3. `onSubmit` —— 解构 hook 返回值的绑定名落到 hook 调用点节点
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import type { IREdge, IRNode } from "@uicodeanalysis/contracts";

import { analyzeApi } from "../src/api.js";
import { discoverComponents } from "../src/components.js";
import { analyzeEvents, type EventBinding } from "../src/events.js";
import { analyzeHooks } from "../src/hooks.js";
import { buildJsxInventory } from "../src/jsx-inventory.js";
import { analyzeProps } from "../src/props.js";
import { createFullProject } from "../src/project.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

/** 与 index.ts 相同的分析顺序：hooks 先于 api，事件最后。 */
function analyze(rootPath: string) {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  const props = analyzeProps({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const hooks = analyzeHooks({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  const api = analyzeApi({
    rootPath,
    project,
    definitionIndex: discovery.definitionIndex,
    hookCallbackOwners: hooks.callbackOwners,
  });
  const events = analyzeEvents({
    inventory: buildJsxInventory(
      rootPath,
      project,
      discovery.components,
      discovery.declarations,
    ),
    components: discovery.components,
    knownNodes: [...hooks.hookNodes, ...hooks.stateNodes, ...api.apiNodes, ...api.fnNodes],
    propsFields: new Map(
      [...props.byComponent].map(([id, info]) => [id, info.fields.map((f) => f.name)]),
    ),
    rootPath,
  });
  return { hooks, api, events };
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>) {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-di-"));
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

function edgeOf(edges: readonly IREdge[], kind: IREdge["kind"], from: string, to: string): IREdge {
  const edge = edges.find((e) => e.kind === kind && e.from === from && e.to === to);
  if (!edge) {
    throw new Error(
      `缺少 ${kind} 边 ${from} → ${to}；实际边：\n${edges
        .map((e) => `${e.kind} ${e.from} → ${e.to}`)
        .join("\n")}`,
    );
  }
  return edge;
}

/** DI 登录链路的完整夹具：与真实仓的三层间接一一对应。 */
const LOGIN_FILES = {
  "node_modules/ky/index.d.ts": `
    declare const ky: {
      post(url: string, options?: unknown): Promise<unknown>;
    };
    export default ky;
  `,
  "src/di.ts": `
    export function inject<T>(_token: string, factory?: () => T): T {
      if (!factory) throw new Error("missing factory");
      return factory();
    }
  `,
  "src/application/types.ts": `
    export interface SessionServiceInterface {
      login(user: { email: string; password: string }): Promise<void>;
    }
  `,
  "src/application/session.service.ts": `
    import { inject } from "../di";
    import ky from "ky";
    import { SessionServiceInterface } from "./types";

    export class SessionService implements SessionServiceInterface {
      constructor(private readonly http = inject("httpclient", () => ky)) {}

      async login(user: { email: string; password: string }): Promise<void> {
        await this.http.post("/users/login", { json: { user } });
      }
    }
  `,
  "src/login/useLogin.ts": `
    import { inject } from "../di";
    import { SessionServiceInterface } from "../application/types";

    function handleSubmit(fn: (data: { email: string }) => Promise<void>) {
      return fn;
    }

    export function useLogin() {
      const sessionService = inject<SessionServiceInterface>("SessionService");
      const onSubmit = handleSubmit(async (data) => {
        await sessionService.login(data);
      });
      return { onSubmit };
    }
  `,
  "src/login/Login.tsx": `
    import { useLogin } from "./useLogin";

    export function Login() {
      const { onSubmit } = useLogin();
      return (
        <form onSubmit={onSubmit}>
          <button type="submit">Sign In</button>
        </form>
      );
    }
  `,
};

const HOOK_NODE_ID = "src/login/Login.tsx#Login/useLogin";
const LOGIN_FN_ID = "src/application/session.service.ts#login";
const API_ID = "api:POST:/users/login";

describe("DI 链路：inject 客户端 + 接口实现 + 解构 hook", () => {
  const parts = analyzeSynthetic(LOGIN_FILES);

  it("G1：this.http.post 按类型识别为 ky，产出接口节点", () => {
    expect(parts.api.apiNodes.map((n) => n.id)).toEqual([API_ID]);
    expect(parts.api.apiNodes[0]?.attrs).toMatchObject({ method: "POST", url: "/users/login" });
  });

  it("G1：service 方法处在请求链路上，requests 边从方法出发", () => {
    expect(parts.api.fnNodes.map((n) => n.id)).toEqual([LOGIN_FN_ID]);
    expect(edgeOf(parts.api.edges, "requests", LOGIN_FN_ID, API_ID).from).toBe(LOGIN_FN_ID);
  });

  it("G2：sessionService.login 穿透接口声明连到实现类方法", () => {
    expect(edgeOf(parts.api.edges, "calls", HOOK_NODE_ID, LOGIN_FN_ID)).toBeDefined();
  });

  it("G3：解构绑定名落点为 hook 调用点节点，事件 targets 非空", () => {
    expect(parts.hooks.hookNodes.map((n) => n.id)).toContain(HOOK_NODE_ID);

    const form = parts.events.elementNodes.find((n: IRNode) => n.attrs.tag === "form");
    expect(form).toBeDefined();
    const events = form?.attrs.events as EventBinding[];
    const submit = events.find((e) => e.name === "onSubmit");
    expect(submit?.targets).toEqual([{ id: HOOK_NODE_ID, kind: "hook" }]);
    expect(submit?.injected).toBe(false);
  });

  it("receiver 类型是项目内接口时不被误判为 HTTP 客户端", () => {
    // sessionService 的类型 SessionServiceInterface 声明在项目内：
    // sessionService.login 不能产出第二个 api 节点
    expect(parts.api.apiNodes).toHaveLength(1);
  });
});

describe("DI 链路：implements 子句用别名导入（register 断边回归）", () => {
  // 真实仓形态：user.service.ts 里 `import { UserService as IUserService }`
  // 后写 `implements IUserService`——子句文本是别名，接口真名是 UserService。
  // 索引键必须按真名建，`userService.register(data)` 才能穿透接口找到实现。
  const parts = analyzeSynthetic({
    "node_modules/ky/index.d.ts": `
      declare const ky: {
        post(url: string, options?: unknown): Promise<unknown>;
      };
      export default ky;
    `,
    "src/di.ts": `
      export function inject<T>(_token: string, factory?: () => T): T {
        if (!factory) throw new Error("missing factory");
        return factory();
      }
    `,
    "src/application/user/user-service.interface.ts": `
      export interface UserService {
        register(data: { username: string }): Promise<void>;
      }
    `,
    "src/application/api/user.service.ts": `
      import { inject } from "../../di";
      import ky from "ky";
      import { UserService as IUserService } from "../user/user-service.interface";

      export class UserService implements IUserService {
        constructor(private readonly http = inject("httpclient", () => ky)) {}

        async register(data: { username: string }): Promise<void> {
          await this.http.post("/users", { json: { user: data } });
        }
      }
    `,
    "src/register/useRegister.ts": `
      import { inject } from "../di";
      import { UserService } from "../application/user/user-service.interface";

      function handleSubmit(fn: (data: { username: string }) => Promise<void>) {
        return fn;
      }

      export function useRegister() {
        const userService = inject<UserService>("UserService");
        const onSubmit = handleSubmit(async (data) => {
          await userService.register(data);
        });
        return { onSubmit };
      }
    `,
    "src/register/Register.tsx": `
      import { useRegister } from "./useRegister";

      export function Register() {
        const { onSubmit } = useRegister();
        return (
          <form onSubmit={onSubmit}>
            <button type="submit">Sign up</button>
          </form>
        );
      }
    `,
  });

  it("别名 implements 的类方法仍能被接口名索引命中，calls 边连通", () => {
    const hookId = "src/register/Register.tsx#Register/useRegister";
    const fnId = "src/application/api/user.service.ts#register";
    expect(edgeOf(parts.api.edges, "calls", hookId, fnId)).toBeDefined();
    expect(edgeOf(parts.api.edges, "requests", fnId, "api:POST:/users")).toBeDefined();
  });
});

describe("DI 客户端的更多形态", () => {
  it("axios.create() 的实例方法按类型识别", () => {
    const parts = analyzeSynthetic({
      "node_modules/axios/index.d.ts": `
        declare const axios: {
          create(config?: unknown): { get(url: string): Promise<unknown> };
        };
        export default axios;
      `,
      "src/Page.tsx": `
        import axios from "axios";

        const api = axios.create();

        export function Page() {
          api.get("/api/instance");
          return <div />;
        }
      `,
    });

    expect(parts.api.apiNodes.map((n) => n.id)).toEqual(["api:GET:/api/instance"]);
  });

  it("客户端类型丢失（@types 缺失 / any）时退化为现状，不误报", () => {
    const parts = analyzeSynthetic({
      "src/Page.tsx": `
        declare const http: { post(url: string): Promise<unknown> };

        export function Page() {
          http.post("/api/untyped");
          return <div />;
        }
      `,
    });

    // declare 的对象类型没有依赖包载体：类型符号指向项目内的源码声明，
    // 不满足「声明在项目外的依赖包里」的判定，保持不产节点
    expect(parts.api.apiNodes).toEqual([]);
  });

  it("真实样本仓回归：既有链路不受类型级识别影响", () => {
    const parts = analyze(FIXTURE_ROOT);
    expect(parts.api.apiNodes.map((n) => n.id)).toEqual(["api:GET:/api/users"]);
    expect(parts.api.fnNodes.map((n) => n.id)).toEqual(["src/pages/UserList.tsx#fetchUsers"]);
  });
});

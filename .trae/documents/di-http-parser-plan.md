# 解析器增强：DI 注入的 HTTP 客户端与 service 链路解析

> **实施状态（2026-11-19）**：G1/G2/G3 已落地，worker 273 测试全过、backend 347 全过。
> 实施中发现计划外的两处必要修正（详见文末「实施记录」）。

## Context

真实项目（react-typescript-realworld-example-app）里所有请求都经 DI 间接发起：

```ts
// Login.tsx / useLogin.ts —— 事件与请求隔了两层间接
const { onSubmit } = useLogin();                       // 解构 hook 返回值
<form onSubmit={onSubmit}>                             // 事件绑定 targets=[]（解析不出）

// useLogin.ts 内部
const sessionService = inject<SessionServiceInterface>(Tokens.SessionService);  // DI
const onSubmit = handleSubmit(async data => { await sessionService.login(data); });

// session.service.ts
private readonly http = inject('httpclient', () => ky);  // ky 也是 DI 注入
async login(user) { await this.http.post('users/login', ...); }  // this.http.post → 识别不了
```

当前解析结果：全图 **0 个 api 节点**、事件 targets 全空 → 聊天助手生成的规格文档「0 事件、无接口映射」。用户期望解析器应该解析出来。

现有机制已覆盖一半：
- **请求封装器**机制（不动点循环，URL 来自参数的函数）已存在，但 `login()` 的 URL 是写死的字面量、走的是「直接请求点」路径——缺口在 `this.http` 识别不了是 ky。
- 全量分析走 ts-morph 完整 **type checker**（project.ts:174 createFullProject），类型级解析的基础设施现成。

三个缺口（对应本例的三个间接层）：

| 缺口 | 现状代码位置 | 现象 |
| --- | --- | --- |
| G1 receiver 类型识别 | `api.ts:490-493` isHttpClientCall——根名 `this` 不在 HTTP_CLIENTS | `this.http.post(...)` 不产出 api 节点 |
| G2 方法调用连边 | `api.ts:1235-1260` resolveCalledFunctionId——「方法调用（obj.fn()）不在本期范围」注释 | `sessionService.login(...)` 不产 calls 边，service fn 成孤岛 |
| G3 解构 hook 绑定 | `events.ts:189-225` buildNameTable——名字表只有 state/hook(fn binding)/fn；`onSubmit` 的符号声明是 BindingElement | `onSubmit={onSubmit}` targets=[] |

## 方案

### G1：类型级 HTTP 客户端识别（api.ts）

`isHttpClientCall` 中 PropertyAccessExpression 分支（api.ts:490-493），根名不在 HTTP_CLIENTS 时追加类型判定：

- 新 helper `resolveReceiverClient(receiver: Node, rootPath: string): boolean`
- `receiver.getType()` → `getAliasSymbol() ?? getSymbol()` → declarations
- 声明全部在项目外（复用 `isProjectSource`），且声明文件路径匹配 `node_modules/(@scope/)?pkg`，包名去 `@types/` 前缀后 ∈ HTTP_CLIENTS
- 命中即视为直接请求点

覆盖面：`this.http.post`（ky 类/函数类型）、`const api = axios.create()` 后 `api.get`（类型来自 @types/axios）、got 实例——统一走类型，不需要逐个追 `inject()` 初始化器。类型解析失败（any/丢 types）时返回 false，退化为现状，无回归。

method 推断沿用 `inferMethodFromCallee`（api.ts:1141 已支持 PropertyAccessExpression 的 `getName()`，`this.http.post` → "post"）。后续 `login()` 会被 registerFn 登记为 fn 节点 + requests 边（api.ts:996-1022、1057-1059 的既有逻辑，MethodDeclaration 已被 getFunctionName 支持）。

### G2：方法调用解析连边（api.ts）

`resolveCalledFunctionId`（api.ts:1235-1260）扩展 PropertyAccessExpression 分支：

1. `call.getExpression()` 是 PropertyAccessExpression 时，取 `expr.getSymbol()`（`.login` 属性符号）→ declarations
2. 声明是**类 MethodDeclaration**（项目内）→ 直接走既有 positionKey 命中 callTargets
3. 声明是**接口 MethodDeclaration**（项目内、无方法体）→ 查「接口实现索引」：预扫全项目 `implements X` 的类（HeritageClause），`接口名#方法名 → [类方法 MethodDeclaration]`；**全部**实现类都尝试 positionKey 命中（多实现全连，保守可解释）；命中不了返回 null（保持现状语义，不虚构边）

依赖关系：callTargets 只登记「请求链路上的函数」（api.ts:1070-1078）。G1 先把 `login` 方法带上链，G2 才有命中目标——两者必须同批落地。

连边位置：api.ts:1066-1078「第三步」的调用点扫描天然覆盖 useLogin 内的 `sessionService.login` 调用点 → calls 边 `useLogin → login`。

### G3：解构 hook 绑定的事件落点（events.ts）

`buildNameTable`（events.ts:189-225）追加：扫描组件同文件的 `const { a, b } = useXxx()` 解构声明——callee 名能命中已知 hook 节点（knownNodes 中 kind=hook、同文件）时，把解构出的绑定名注册到该 hook 节点 id。

- `onSubmit={onSubmit}` → byName 命中 → target = useLogin hook 节点 → element --calls--> useLogin
- 静态近似：不区分 hook 返回对象的具体键（拿到的是 hook 而不是内部具体函数）；在代码注释说明该边界
- 链路完整性：element → useLogin（G3）→ login fn（G2）→ api（G1）——derive_chains 与召回反向 BFS 全程可走

## 契约与后端影响

**零改动**：api 节点（attrs.method/url）、requests/calls 边、事件 targets 均为既有 IR 契约形状（shared/schemas/ir.schema.json 的 attrs 自由扩展）。backend 的 derive_chains（chains.py:431-493）、ApiUsageIndex（api_usage.py:167-190）、召回种子、specdoc 事件/接口章节自动受益。

## 文件清单

| 文件 | 改动 |
| --- | --- |
| workers/ast-worker/src/api.ts | G1 resolveReceiverClient + isHttpClientCall 分支；G2 接口实现索引 + resolveCalledFunctionId 扩展 |
| workers/ast-worker/src/events.ts | G3 buildNameTable 解构 hook 绑定 |
| workers/ast-worker/tests/api.test.ts | DI 夹具用例：service 类 + inject 客户端 + interface/implements + hook 解构；断言 api 节点、fn 节点、requests/calls 边、事件 targets 非空 |
| workers/ast-worker/tests/events 相关测试 | 解构绑定用例 |
| （回归）axios.create 实例用例 | `const api = axios.create()` → `api.get('/x')` 产 api 节点 |

## 验证

1. `workers/ast-worker` 内 `npm test`（vitest）全过
2. backend `pytest tests -q` 全量回归（无改动，应保持基线）
3. 真实项目端到端：对 prj_c8a25865a754 重新发起分析 → 图中 api 节点数 > 0（应出现 `POST users/login`）、`useLogin → login` calls 边、form 元素事件 targets 非空
4. 聊天助手重问「帮我分析/login的前端交互视图」→ 文档含事件清单与接口映射（不再「本页面无后端接口调用」）

## 边界与风险

- 增量分析路径（createIncrementalProject）同样走 ts-morph，类型查询可用；但丢类型声明（无 @types）时退化为现状
- 接口多实现全连：calls 边可能偏多，但都有 evidence 支撑、可解释
- G3 的静态近似在「hook 返回多个函数」时会把不同事件连到同一 hook 节点——规格文档里 handler 名显示为 hook 名，属可接受的近似

## 实施记录（2026-11-19）

按计划落地 G1/G2/G3 之外，发现并修正了三处计划假设与实际的偏差：

1. **`getFunctionName` 不支持 `MethodDeclaration`**（计划假设已支持）。
   service 类方法 `login` 因此拿不到名字、进不了请求链。已在 api.ts 补上
   `MorphNode.isMethodDeclaration(fn)` 分支。
2. **链路缺口：事件落点（hook 调用点节点）与请求链之间没有边**。计划只断言
   「calls 边 useLogin → login」天然成立，实际 `sessionService.login` 的调用方
   会经 `moduleFunctionIdOf` 解析成 `useLogin.ts#onSubmit` 模块函数——它不是图上
   节点，且与 hook 节点无关联，链条在 hook 节点断开。修正：hooks.ts 为自定义
   hook（`useXxx`，项目内定义）登记其定义体内「绑定到变量名的回调体」位置到
   `callbackOwners`（归到 hook 调用点节点 id，语义与 useCallback 回调体归属一致）。
   api 分析第三步的调用方解析自动命中，产出 `hook 节点 → login` 边。
3. **接口成员在 ts-morph 里是 `MethodSignature` 而非 `MethodDeclaration`**。
   G2 的接口分支必须两者都认，否则 DI 场景的接口实现索引永远查不到。

另两处实现要点：

- `registerCustomHookCallbackBodies` 必须穿透 import 别名（调用点标识符的符号
  声明是 ImportSpecifier），才能拿到 hook 定义处的 FunctionDeclaration。
- 后端拉起 worker 用的是构建产物（`npm run build -w @uicodeanalysis/ast-worker`
  → dist），改完 src 必须重建再重析，否则验证永远看到旧图。


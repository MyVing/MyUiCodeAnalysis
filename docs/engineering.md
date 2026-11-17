# 工程说明

## 契约是唯一真源

`shared/schemas/` 下的 JSON Schema 是静态分析、运行时采集、AI 层、前后端之间唯一的跨层契约。所有语言侧的类型都由它生成，**禁止手写同名定义**。

| Schema | 内容 | 消费方 |
| --- | --- | --- |
| `ir.schema.json` | 统一 IR：节点、边、证据、告警 | ast-worker、backend、frontend |
| `plugin.schema.json` | 插件清单：能力、依赖、版本 | plugin_manager、插件实现 |
| `runtime.schema.json` | 浏览器采集：网络、Console、交互、关联 | runtime-worker、backend |
| `mapping.schema.json` | 二次封装映射：项目内封装组件 ↔ UI 库组件 | ast-worker、插件配置 |

### 生成链路

```
shared/schemas/*.schema.json
        │
        ├── datamodel-codegen ──> backend/app/contracts/*.py
        └── json2ts ───────────> shared/contracts/src/*.ts
                                        │
                                        └──> frontend / workers/ast-worker 通过 @uicodeanalysis/contracts 引用
```

```bash
npm run gen:contracts     # 重新生成两侧类型
npm run check:contracts   # 校验是否漂移（退出码非 0 表示需要重新生成）
```

生成参数固化在 `shared/scripts/generate_contracts.py`，已用现有产物逐字节验证还原。改参数会产生大面积无关 diff，不要随意调整。

`shared/contracts/src/index.ts` 是手写的稳定出口，生成文件名变化时只需改这一处。

## 目录职责

```
backend/app/contracts/     生成的 Python 契约类型（勿手改）
backend/app/ir/sample.py   示例图：标记 meta.sample=true，不冒充真实结果
shared/contracts/          生成的 TS 契约类型 + 手写 index.ts
workers/ast-worker/        静态分析 Worker（ts-morph），stdin/stdout JSON 协议
tests/fixtures/            验收样本，M1 的分析器以它们为准
```

## ast-worker 的约定

- 通过 stdin 读 `WorkerRequest`，stdout 写 `WorkerResponse`，**日志一律走 stderr**（stdout 只有一个 JSON）。
- 失败返回 `{ ok: false, error }` 并**以退出码 0 结束**，让编排层能区分"Worker 报错"与"Worker 被超时 kill"。
- ts-morph 的 `Project` 实例在单次分析内复用。`getExportedDeclarations()` 会强制构建 `ts.Program` 并加载 `lib.d.ts`，单文件重建约 90ms/次，复用后约 1ms/次。
- 全量路径读项目自带 `tsconfig.json`（复用 `paths`），增量路径只加变更文件。两条路径的编译器选项必须分开配置。
- 类型检查分两步：`tsconfig.json` 只管 `src`（产物用，`NodeNext`），`tsconfig.test.json` 另外覆盖 `tests` 与 `vitest.config.ts`。测试 tsconfig 必须用 `moduleResolution: Bundler`，否则 `ajv` 这类 CJS 包的默认导入会被按原生 Node 语义建模，`new Ajv2020()` 在 tsc 里报 not constructable——而 vitest 里跑得好好的。只让检查与运行时对齐，不要为迁就 tsc 改运行时写法。

## IR 的两条硬约束

1. **节点 id 必须是「文件路径 + 组件名」**，同一文件多组件不得冲突（`src/Table.tsx#Table`、`src/Table.tsx#Row`）。
2. **`attrs` 只放插件私有扩展**。`file` / `loc` 是一等字段，不要再塞进 `attrs`。

## 静态分析的解析规则

`workers/ast-worker/src/resolver.ts` 负责模块解析，`components.ts` 负责组件发现。几条不能改的规则：

- **不自己实现 `paths` 匹配**。读入 tsconfig 后 ts-morph 已按项目真实配置解析别名，自己再写一遍 glob 只会在 monorepo / `extends` / 多 `baseUrl` 场景与 TypeScript 语义分叉。只消费 `getModuleSpecifierSourceFile()`。
- **「解析不到」必须分三态**：解析到项目内文件（internal）、裸包名（external）、解析失败（unresolved）。其中 unresolved 还要再分本地说明符（相对路径/别名 → `error`）与裸包名（依赖未安装 → `info`）。混为一谈会让「依赖没装」淹没「代码写错」。
- **barrel 必须穿透且不重复计数**。`getExportedDeclarations()` 会沿 `export *` 展开到最终定义位置，因此只保留 `decl.getSourceFile() === 当前文件` 的条目，组件便只在定义处产出一次。
- **同一 AST 节点的多个导出名合并为一个组件**。`export function X() {}` + `export default X` 会让导出表里同时出现 `X` 与 `default`，两者指向同一节点与同一位置；按位置合并，导出名记在 `attrs.exportNames`。
- **组件判定看结构不看名字**。体内出现 JSX 节点是主依据（不依赖 `@types/react` 是否安装），返回类型为 `JSX.Element` 为补充依据。禁止 `startsWith("use")` 这类名字推断。
- **renders 边只连组件，不连宿主元素**。首字母小写的标签按 JSX 编译语义必然是 DOM 元素，直接跳过；不产 `<div>` 节点，也不产指向它的边。点号标签（`Antd.Button`）以最后一段的首字母判定。
- **边的归属靠「定义位置键」反查**，键为 `file@charOffset`。不要用行号：一行里可以有多个声明；也不要用名字：同名标签在不同文件指向不同组件。
- **同一 (from → to) 只产一条边**。列表里 10 个 `<RowItem />` 是同一个结构事实。自渲染（递归组件）不产自环。
- **外部组件进图，但明确标注不可见**。渲染未安装依赖的组件（如 `antd.Table`）时产出薄节点 `antd#Table`，`attrs.external = true`，置信度 0.6，且不带 `file`/`loc`。它的内部结构不在静态分析范围内——不要为了"图更完整"而编造它的子节点。
- **未导出局部组件无法连边时记 `unresolved_jsx_target` 告警**（warning 级）。不编造边，也不静默丢弃。

## props 展开的三条规则

`workers/ast-worker/src/props.ts`，产出写进 `attrs.props`，前端 `PropsPanel` 消费。

- **可选性只认问号**。`a?: T` 在类型系统里是 `T | undefined`，那是 `?` 的产物；而手写 `a: T | undefined` 是"必须传、但可以传 undefined"。前者 `optional = true`，后者 `optional = false`。永远不要靠类型文本里有没有 `undefined` 来判断可选性。
- **类型文本取源码里的类型节点，不取类型系统**。`rows: UserRowProps[]` 必须原样显示，展开成 `{ userId: string; ... }[]` 会丢掉用户起的类型名。副作用是也可避免可选属性显示成 `boolean | undefined`。只有属性没有类型标注时才退回类型系统——此时不能做 union 成员过滤，否则 `boolean` 会退化成 `false | true`。
- **默认值只能从解构读**。类型系统不记录默认值。`{ request: loadData = fn }` 对外属性名是 `request` 而不是局部名 `loadData`。

## hooks / state 的四条规则

`workers/ast-worker/src/hooks.ts`。归属判定与 renders 共用 `ownership.ts`——两处各写一份迟早会分叉，同一段代码被算到不同组件上。

- **`useState` 产出 state 节点，其余产出 hook 节点**。把 `useState` 也算成 hook，同一个声明在图上会出现两次，前端按「状态」筛选时又看不到它。非解构形态（`const tuple = useState(0)`）没有 setter，谈不上读写语义，因此按 hook 记录而不冒充 state。
- **hook 归属只看组件函数体的顶层语句，穿过函数就停**。React 的规则就是 hook 只能在顶层调用，所以 `useEffect(() => { useFoo() })` 里的 `useFoo` 不属于本组件——那是违规写法，不该被当成结构事实。条件表达式里的调用（`cond ? useX() : []`）在顶层语句内，仍算本组件的。
- **reads / writes 靠符号引用，不靠位置推断**。`setLoading(true)` 写在 effect 回调里，仍然是本组件在写自己的状态，所以引用查找要贯穿全部嵌套层级，与 hook 归属的"不跨函数"是两条独立规则，不要合并。
- **hook 节点 id 不用行列号**。在组件上方插一行注释就会让所有 hook 的 id 漂移。优先用绑定变量名（`handleSelect`），没有绑定时用「hook 名 + 组件内序号」。只有初始化表达式**就是**这个调用时才取绑定名：`const rows = cond ? useData() : []` 里 `rows` 是整个三元表达式的结果，拿它命名会误导。

`deps` 字段的 `null` 与 `[]` 必须区分：前者是"每次渲染都执行"，后者是"仅挂载/卸载"。前端 `HookPanel` 分别显示，合成一个会让用户误判副作用频率。

## API 链路的四条规则

`workers/ast-worker/src/api.ts`。组件里的 `fetch()` 是最少见的情形，``UserList → fetchUsers → request({ url }) → fetch(`${url}${query}`)`` 才是常态。URL 的**字面量出现在封装器的调用点**，不在 `fetch` 处，所以扫 `fetch` 只能拿到一堆无信息量的模板。

- **谁是 HTTP 客户端靠「名字 + 符号来源」双重判定**。`fetch` / `axios` / `ky` / `got` 是一份受控名单，但仅名字匹配不够：项目里自己写一个叫 `fetch` 的函数必须不被当成浏览器 fetch。因此额外要求符号声明在项目外（`isProjectSource` 为 false）。这里**不能用 `toProjectRelative` 判空**——`node_modules` 就在 `rootPath` 之下，相对路径非空但它显然不是项目源码。
- **封装器不产 api 节点**。封装器体内只有参数（``fetch(`${url}${query}`)``），URL 此刻定不下来。它只登记「第几个参数、哪个属性是 URL」，真正的接口节点由调用点产出。若封装器自己成点，图上会出现一个 URL 为 `${url}${query}` 的假接口。
- **封装器识别要做不动点循环**。企业项目常见两层封装（`getUser → send → fetch`）。内层必须先被识别外层才认得出来，而源文件顺序与依赖方向无关（字母序里 `api.ts` 排在 `http.ts` 前）。每轮最多发现一层，循环到某轮零新增为止，另设 `MAX_WRAPPER_ROUNDS` 上界防止互相转发空转。
- **`method` 不存在「不确定」态**。不写 `method` 的请求就是 GET，这是 HTTP 语义而不是猜测，所以直接落 `GET`。取值优先级：调用点显式传入 → 封装器的解构默认值 → 内层封装器继承的默认值 → `GET`。

置信度分三档，反映的是「结论有多确定」而不是「调用点写没写」：URL 是直接字面量 1.0；经封装器参数转一手 0.9；经局部变量转一手 0.8。证据位置也随之一并指向**真正决定 URL 的那一行**——点开证据看到的就该是结论的来源。

`fn` 节点只留给「组件调用、自己又不在封装器名单里」的函数（如 `fetchUsers`）。封装器是请求管道的实现细节，把它也画成业务节点会让星图变噪音，链路信息已经由 `requests` 边承载。

## 事件的表达方式

`workers/ast-worker/src/events.ts`。`NodeKind` 是受控枚举且没有 `event`，但事件从来不是独立实体——它总挂在某个 JSX 元素上。所以**一个带事件处理的元素就是一个 `element` 节点**，事件清单进 `attrs.events`。这样既不升 schema 版本，也不会在图上出现一堆脱离宿主元素的浮空事件点。

- **只认 `on[A-Z]`，但这不是业务名硬编码**。`on` + 大写是 React 的语言级约定，对所有项目一致；`if (tag === "ProTable")` 只对一个项目成立。两件事不能混为一谈。
- **宿主元素与组件回调 prop 同等对待**。`<div onClick>` 是浏览器事件，`<RowItem onClick={...}>` 是往子组件注入回调，静态形态一模一样，分析器没有理由区别对待。`attrs.target` 记 `host` / `component` 供前端区分。
- **`targets` 为空是结论，不是缺口**。三种情形必须分清：落到本组件节点（`targets` 非空）、由 props 注入（`injected: true`，逻辑在父组件，子组件里本来就不该连出去）、无法静态判定（两者皆无）。把第二种标成"未解析"会让人以为链路断了并去改分析器。
- **内联处理函数要下探全部嵌套层级**。`onClick={() => ids.map((id) => setIds([id]))}` 里的 setter 在点击时照样执行。这与 hook 归属的"不跨函数边界"是相反的规则：React 的顶层调用约束只管 hook，不管事件回调。
- **元素 id 不用行列号**。优先用唯一的具名处理函数作后缀（`jsx:div/handleSelect`），否则用「标签 + 组件内序号」。上方插一行注释不能让 id 漂移。

## 可见文案的提取边界

`workers/ast-worker/src/ui-text.ts`，产出写进组件节点 `attrs.uiText`，前端 `UiTextPanel` 消费。

需求的描述对象是"用户看到的东西"（搜索框、姓名列、查询按钮），代码符号描述的是程序结构（`input`、`columns`、`onClick`）。两者唯一的交集是文案，所以它必须由 AST 提取而不是让 LLM 读代码：LLM 采样式读取不完备、给不出 `loc`、结果不可重复。语义判断（这段文案对不对得上需求）属于需求映射层，输入应是本模块产出的候选集。

- **只认可枚举的来源，不做名字猜测**。JSX 文本节点、白名单属性（`placeholder` / `title` / `label` / `aria-*`）、对象字面量的文本类键（`title` / `label` / `message`…）、纯字符串数组、模板字符串静态片段、i18n 取词键。`className` / `id` / `data-testid` 同样是字符串属性，但用户看不到，一个都不收。
- **`array` 来源额外收紧**。组件内部的配置表极多（`["px", "rem", "em"]`），因此要求含中文、含空格或首字母大写。宁可漏几个英文单词，也不要把配置表灌进 attrs。
- **定不下来的表达式记 `dynamic`，不当成没有文案**。`<h1>{title}</h1>` 的字面量在父组件，这个组件确实"有标题但正文不在此处"。把它显示成「动态来自变量」比留空更接近事实。本期不做跨文件求值。
- **组件外的常量只计数、不猜归属**。`const columns = [{ title: "姓名" }]` 写在模块顶层是常态，它不归任何组件。记一条 `ui_text_unassigned` info 告警说明覆盖面缺口，而不是按"最近组件"强行认领——那会把文案挂到错误的节点上，检索时比缺失更糟。
- **截断必须说出来**。单组件超过 80 条时记 `ui_text_truncated`。静默截断会让检索以为"这条文案项目里没有"。
- **文案是语法事实，confidence 恒为 1.0**。它进 `attrs` 而不是 `evidence`，因为它不是对关系的推断，而是对源码字面量的记录。

## 二次封装映射只在配置里

`workers/ast-worker/src/wrappers.ts` + 项目根目录的 `mapping.json`（或 `mappings/*.json`）。

- **核心分析器没有任何业务名分支**。`ProTable` / `SmartTable` / `BizTable` 都是每个企业自造的名字，写进代码就只对一个项目有效。分析器只做两件事：读配置、按组件名查表。
- **映射不新增节点与边**。`ProTable ──renders──> antd#Table` 这条边本来就由 renders 分析器产出（封装体里就写着 `<Table />`）。映射的增量是**语义注解**：写进组件 `attrs.wrapper`，说明这是二次封装以及 props 怎么改名。
- **`resolved` 如实反映渲染边**。封装里用动态组件时 renders 连不上目标，此时 `resolved: false`，前端显示告警。不能因为配置声明了映射就假装成功，也不能替 renders 补一条边。
- **配置错了必须说出来**。文件缺失不告警（没配映射是常态）；文件存在但 JSON 坏了、或字段不符合 `mapping.schema.json`，记 `mapping_invalid`；规则声明的组件在项目里找不到，记 `wrapper_not_found`。后两者都是"用户以为生效了其实没有"，静默会让人长期排查错方向。

## 告警而非静默降级

分析过程中任何失败都要进 `IRGraph.warnings`：

```json
{ "code": "parse_error", "severity": "error", "stage": "static", "file": "src/a.tsx", "loc": { "line": 12, "col": 3 }, "message": "..." }
```

前后端的 `WarningsBanner` 会按 `severity` 排序展示。一次运行没观察到的东西不等于不存在——这条同样适用于运行时。

## 与项目文档的对应

设计依据见仓库外层的 `React代码智能分析平台-项目文档-动态方案补充版.md`。当前处于 **P1（MySQL 持久化）完成、P2（运行时采集）待开始** 的状态。

**M1 已完成**：module resolver、组件发现、renders 边、props 展开、hooks / state、API 链路、事件分析、二次封装映射（`ProTable → antd.Table`）、可见文案提取（`attrs.uiText`）；编排层（`AnalysisService` + `SubprocessWorkerRunner`，queued→running→succeeded/failed，错误码 `worker_timeout` / `worker_spawn_failed` / `worker_protocol_error` / `invalid_ir` 全部可见）；前端工作台（触发分析 + 轮询 + 图谱浏览 + 高级选项）。后端 36 个测试、前端 44 个测试、ruff/mypy 全绿，真实 worker E2E 已验证。

**P1 持久化已完成**：仓储 Protocol 异步化（提交 `686eedc`）→ MySQL 实现（`1376f6a`）→ 集成测试（`b15e606`）。

- 选型：SQLAlchemy 2.0 仅 Core 层 + **aiomysql**（纯 Python，Windows 免编译；不用 asyncmy，因其 Windows wheel 覆盖不全）。
- 5 张表：`projects` / `analysis_tasks` / `graphs`（IR 整包 JSON 列）/ `requirement_tasks` / `requirement_maps`；`DATETIME(6)` 存 naive UTC。
- 「每项目至多一个活动任务」用 `active_flag` 普通列 + 唯一索引表达（登记时写 project_id，终态置 NULL），单条 INSERT 撞 `1062` 判定冲突。**不要用 STORED 生成列**：表达式引用 ENUM 时 InnoDB 8.0 的外键约束会以 1215 失败（实测 8.0.42）。
- lifespan（`app/main.py`）：`APP_DB_URL` 配置后建表（幂等 `CREATE TABLE IF NOT EXISTS`，无 alembic）+ boot recovery（queued/running → `failed/server_restarted`）；缺省走内存实现。
- 测试隔离：conftest 显式传 `Settings(_env_file=None)` 并覆盖 `get_settings`，测试不受本地 `.env` 影响；MySQL 集成测试由 `APP_MYSQL_INTEGRATION=1` + `APP_TEST_DB_URL` 门控。
- 本地开发库：MySQL 8.0.42 免安装版 `D:\mysql8`（端口 3307，root/123456，binlog 关闭），与既有 MySQL57(3306) 并存；凭据在 `backend/.env`（不入 git）。
- 后端 64 测试全绿 + 9 个真实 MySQL 集成测试（env 门控）；`/health` 的 `storage` 字段如实反映 `memory | mysql`。

下一阶段 **P2 运行时采集**：新建 `workers/runtime-worker`（Node + Playwright），被动采集起步——vite dev server 生命周期托管、React DevTools hook 抓组件树（`_debugSource` 直连 IR）、网络请求记录；产物以 `evidence.source=runtime` 与静态图合并（`kind='merged'`）。P3 语义层（LangGraph + AIOMySQLSaver）要求 MySQL ≥ 8.0.19，本地 3307 实例已满足。

**待完成**：
- 路由 / 页面节点（`NodeKind.route` 已预留但无分析器产出）
- 需求映射层（`AnalysisStart.requirement` 已预留但编排层未消费）
- **持久化**：用户已确认放弃内存存储，采用 MySQL（拒绝 SQLite）；tasks/graphs 落库、重启恢复（boot 时 running→failed）；M3 LangGraph checkpointer 复用同一数据库
- **M2 运行时采集**：新建 `workers/runtime-worker`（Node + Playwright），被动采集起步——启动 vite dev server、加载页面、经 React DevTools 协议抓组件树、记录网络请求（URL/方法/状态码），不自动点击遍历；运行时证据以 `evidence.source=runtime` 并入 IR
- **M3 AI 语义层**：需求→组件映射、影响面解释、回归建议；LangGraph 编排，checkpointer 使用同一 MySQL/数据库；LLM provider 进 Settings

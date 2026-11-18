# 链路语义层设计：从「定位到页面」到「定位到交互链路」

> 本文展开 [technical-design.md](./technical-design.md) 的 6.14（从页面到交互链路）与 6.15（定位结果契约扩展），并给出**候选单位从「页面」换成「交互链路」**之后的完整设计。两者冲突时以本文为准；契约字段以 `shared/schemas/*.schema.json` 为准。
>
> 三条既有立场在本文中一条都不放松：**静态分析管事实、AI 管语义**；**不编造节点与边**；**每条结论必须携带来源、置信度、代码位置**。

## 一、问题：候选单位选错了

当前定位链路的产出是 `Match { featureId, pageNodeId, confidence, quote, subgraph }`。它能回答的问题只有一个：**这条需求由哪个页面实现**。

但需求原文几乎从来不是以「页面」为单位的：

> 「订单列表页按状态筛选，并支持导出。」

这句话里有三个可以独立验证的落点：列表页本身、状态筛选下拉框及其链路、导出按钮及其链路。而当前系统只能回答第一个——另外两个落点在产物里无处安放。

这不是「字段不够」的问题，而是**候选单位选错了**。把页面当作候选单位，等于强迫用户从一个粗粒度对象上读出细粒度答案。

### 1.1 为什么不能「把元素也加进候选集」

最直接的修法是把候选从「页面」扩到「页面 + 元素 + 处理函数 + 接口」的扁平列表。它有几个致命问题：

| 问题 | 后果 |
| --- | --- |
| 候选数量爆炸 | 一个中型页面几十个元素 + 十几个 hook/state + 若干接口 → 单页候选上百，30 条候选的上下文装不下 |
| 元素脱离上下文没有意义 | `<Button>` 这个候选说明不了任何事；`<Button> 导出` + `onClick → handleExport` + `GET /api/orders/export` 才是一条可验证的事实 |
| 候选之间没有关系 | 元素、handler、api 是**同一条链上的三段**，扁平列表丢掉了「它们属于同一次点击」这个最关键的信息 |
| AI 的选择空间变成组合 | 从 N 个独立对象里各挑几个再配对，模型可以直接拼出一条代码里不存在的链路 |

正确的候选单位是**交互链路**：一次「用户动作 → 处理函数 → 状态写入 / 接口调用 / 页面跳转」的完整序列。它天然是一个整体，元素、函数、状态、接口是它的组成部分而不是并列候选。

### 1.2 关键前提：链路必须由静态分析确定性构造

这是本文最重要的一条约束，不能用别的方式替代：

> **ActionChain 由确定性图遍历产出，AI 不参与构造。**

理由与 3.4 的置信度分层是同一条：

- 链路的每一段（`element --calls--> fn`、`fn --requests--> api`）都是已经带证据的 IR 边。链路只是把这些边串起来，**不引入任何新事实**。
- 如果让 AI 生成链路，它就有机会拼出一条由真实节点组成的**不真实路径**——每个节点都真的存在，但「A 调用 B」这件事是编的。四道闸拦得住「节点不存在」，拦不住「边不存在」。
- 确定性构造的链路还带来一个免费的好处：**任何一条链路都可以逐段回跳到源码**，因为每一段都对应一条有 `file` / `loc` 的边。

因此本文的分工是：

```
静态分析（ast-worker）  →  事实层：节点、边、视图树、导航关系
确定性推导（backend）    →  ActionChain：纯图遍历，零模型
AI                       →  在链路候选里选「哪条对应这句需求」，逐字引文
```

## 二、IR 的三层图

链路能构造出来，前提是图上有三层结构。前两层已经完整落地，第三层已实现并接入编排。

### 2.1 结构层（已实现）

节点：`component` / `hook` / `state` / `fn` / `api` / `route`
边：`renders` / `calls` / `reads` / `writes` / `requests` / `uses` / `boundary`

这一层回答「程序里有什么、谁引用了谁」。它由 `components.ts`、`props.ts`、`hooks.ts`、`api.ts`、`renders.ts`、`routes.ts` 产出。

### 2.2 视图层（已实现）

节点：`element`（`attrs.viewRole` = `control` / `container` / `text` / `link`）
边：`contains`（组件 / 元素 → 子元素）

这一层回答「页面上看得见什么、属于哪一块」。它由 `jsx-inventory.ts` + `view.ts` 产出。

**关键设计：元素 id 只有一个来源。** `jsx-inventory.ts` 为**每一个**归组件所有的 JSX 开标签分配 id，`view.ts` 与 `events.ts` 都查这一份清单。id 形状是 `所属组件 id + jsx:标签 + 序号`，序号按「该组件内同标签的全部出现顺序」计数，与元素最终是否被收录无关。理由是稳定性：若按「被收录的顺序」编号，给某个按钮加一行 `onClick` 就会让后面所有按钮的 id 漂移，前端的选中态、历史记录、P4 的 diff 全部跟着抖。

两层收录集合不同，交集由 `ir-builder.mergeNodes` 按 id 合并 `attrs`：

| 层 | 收什么 | 例 |
| --- | --- | --- |
| 视图层 | 有语义的元素：控件 / 链接 / 语义容器 / 文本标签 / 带 `columns` 的表格 / 带可见文案的元素 | `<button>导出`、`<input placeholder="搜索用户">` |
| 事件层 | 带 `on[A-Z]` 事件的元素（任意标签，含无文案的 `<div onClick>`） | `<div onClick>`、`<RowItem onSelect={...}>` |

事件层独有的元素（`<div onClick>` 这类无文案布局元素）通过 `ViewAnalysisInput.extraElementIds` 交给视图层**补 `contains` 父边而不产节点**。不补的话，它们在视图树里是孤儿——用户点开一个按钮，看不出它在页面的哪一块。

### 2.3 动作层（已实现）

边：`navigates`（元素 / 组件 → `route` 节点）

这一层回答「从这里能把用户带到哪」。它由 `navigate.ts` 产出，识别口径建立在**包来源 + 符号回溯**上，不看名字猜：

| 形态 | 边从哪出发 | 置信度 |
| --- | --- | --- |
| `<Link to>` / `<NavLink to>` / `<Link href>` | 该链接元素 | 字面量 0.9 / 含插值 0.8 |
| `<Navigate to>` / `<Redirect to>` | 所属组件 | 同上 |
| `navigate("/x")` | 所属组件 | 同上 |
| `router.push("/x")` / `.replace` | 所属组件 | 同上 |
| `redirect("/x")` | 所在函数所属组件 | 同上 |

`useNavigate` 必须来自 `react-router*` / `next/*` / `@remix-run/react` / `@tanstack/react-router`，项目自造的同名函数不算；`navigate` 变量必须回溯到该 hook 的返回值。路径是模板插值时按段匹配动态路由（`/users/${id}` ↔ `/users/:id`）——段数确定、段值不定，因此置信度降一档。路径定不下来或路由表里没有对应节点时**只告警，不编造边**。

### 2.4 三层合起来能回答什么

```
route（/orders）
  └─ renders ─► OrderList（component）
        ├─ contains ─► <Select>（control, viewText="全部状态"）
        │     ├─ calls ─► handleStatusChange（fn）
        │     │     └─ requests ─► GET /api/orders?status=
        │     └─ writes ─► status（state）
        ├─ contains ─► <Button>（control, viewText="导出"）
        │     └─ calls ─► handleExport（fn）
        │           └─ requests ─► GET /api/orders/export
        └─ renders ─► RowItem ── navigates ─► route（/orders/:id）
```

这张图已经包含了链路需要的全部信息。ActionChain 只是把它**按「一次用户动作」重新切分**。

## 三、ActionChain 契约

### 3.1 为什么不做成 NodeKind

IR 的节点契约隐含一条要求：**一个节点对应唯一一个源码位置**。`IRNode.file` / `IRNode.loc` 是一等字段，「点开节点跳回源码」是它的核心用途。

ActionChain 对应多个位置：起点的元素、处理函数、状态、接口，各自在不同的文件、不同的行。硬塞进 `NodeKind` 会迫使 `loc` 变成「随便挑一个」，直接破坏这个能力。

另有一条生命周期上的理由：`IRGraph` 会被运行时合并图覆盖（merged 版本重写），而链路只依赖静态事实。如果链路进了 `IRGraph`，每次运行时合并都得重算链路——用静态事实推导的东西，不该被运行时生命周期绑住。

所以 ActionChain 放在**新的契约**里，`nodeIds` 只是对 `IRGraph` 的引用。它和 `RequirementMap` 同层（都是派生结论），但不同包：`RequirementMap` 是**某一次需求定位**的产物，ActionChain 是**某一版图**的产物（与需求无关，同一份图可以反复被不同需求定位使用）。

### 3.2 结构

```jsonc
{
  "id": "src/pages/OrderList.tsx#OrderList/jsx:button/handleExport::onClick",
  "analysisId": "ana_abc123",
  "pageNodeId": "src/pages/OrderList.tsx#OrderList",
  "routeNodeId": "src/router.tsx#/orders",
  "elementId": "src/pages/OrderList.tsx#OrderList/jsx:button/handleExport",
  "elementLabel": "<button> 导出",        // viewText，便于人读；缺失时退化为 <button>
  "event": "onClick",
  "handler": {
    "nodeId": "src/pages/OrderList.tsx#OrderList/handleExport",
    "kind": "hook"                        // hook（useCallback 绑定的处理函数）| fn | state | inline | prop_callback
  },
  "writes": ["src/pages/OrderList.tsx#OrderList/status"],        // 改写了哪些状态
  "requests": ["api:GET:/api/orders/export"],                    // 调了哪些接口
  "navigates": ["src/router.tsx#/orders/:id"],                   // 跳到哪些路由
  "path": {
    "nodeIds": ["elementId", "handler.nodeId", "src/api/orders.ts#fetchOrders", "api:GET:/api/orders/export"],
    "edgeIds": ["e_calls_...", "e_calls_...", "e_requests_..."]   // 与 nodeIds 同序，前端逐段高亮
  },
  "terminatedAt": null,                    // 见 3.4：链路在哪里断了
  "confidence": 0.9,                       // 取路径上最弱一环的证据置信度
  "evidence": [{ "source": "static", "confidence": 0.9, "file": "...", "loc": {...} }]
}
```

**引用 `nodeIds` 时必须知道各 kind 的 id 形状**（链路层不做任何 id 拼装，只做查表与拼接已有 id）：

| kind | id 形状 | 例 |
| --- | --- | --- |
| `element` | `组件id/jsx:标签[#序号 或 /处理函数名]` | `src/pages/OrderList.tsx#OrderList/jsx:button/handleExport` |
| `state` | `组件id/状态名` | `...#OrderList/status` |
| `hook` | `组件id/绑定名`，无绑定时 `组件id/useXxx[#序号]` | `...#OrderList/handleExport`、`...#OrderList/useEffect` |
| `fn` | `文件#函数名`（**模块级**，不带组件前缀） | `src/api/orders.ts#fetchOrders` |
| `api` | `api:METHOD:URL` | `api:GET:/api/orders/export` |
| `route` | `文件#路由路径` | `src/router.tsx#/orders` |

注意 `handler.kind` 很少是 `fn`：`fn` 节点只收**模块级**的请求链函数，组件内的具名处理函数多半是 `useCallback` 产生的 `hook` 节点（`const handleExport = useCallback(...)`），或者干脆是内联箭头函数（`inline`，此时 `handler.nodeId` 为 `null`，链路的后续段从内联体的 `targets` 直接出发）。

**几条不能省的字段**：

- `path.nodeIds` / `path.edgeIds` **有序且等长**。前端要按段高亮「点击 → 函数 → 接口」，无序集合做不到。`nodeIds[0]` 恒等于 `elementId`。
- `terminatedAt` 显式表达断点。链路的完整性是用户最关心的事之一，用 `null` / `"prop_callback"` / `"dynamic_handler"` 三值表达，比让用户自己从空数组里猜要好。
- `confidence` 取路径上最弱一环，**不是平均值**。一条链的可信度不会高于它最不可信的那一段；取平均会让「两段 1.0 + 一段 0.6」看起来像 0.87。
- `id` 由 `elementId` + 事件名构成，不含序号。元素 id 本身已经稳定（见 2.2），事件名是源码里的字面量。两者确定，链路 id 就确定——P4 的 diff 与前端历史记录都依赖这一点。

### 3.3 构造算法（`derive_chains`）

纯图遍历，**零源码解析、零模型调用**。输入是 `IRGraph`，输出是 `ActionChain[]`。

```
候选起点：所有 attrs.events 非空的 element 节点
         （是否带 viewRole 不影响能否当起点：<div onClick> 只有事件、
          没有视图角色，它照样是一次可定位的交互）

对每个 (element, event)：
  targets ← 该元素出边里 kind ∈ {calls, writes} 的 to
           （events.ts 已把每个事件的落点拆成这两类边，链路层不重新解析源码）
  ├─ target.kind = state     → writes += target.id，终止
  ├─ target.kind = hook      → 沿 calls 向下（≤3 跳）
  ├─ target.kind ∈ {fn, …}   → 沿 calls 向下（≤3 跳）、沿 requests 收 api、
  │                            沿 navigates 收 route，终止于叶子
  └─ targets 为空            → terminatedAt = "prop_callback"
                               （attrs.events[i].injected 为 true 且无 target：
                                父组件注入的回调，链路在本组件终止）

额外：element 自身有 navigates 边时，navigates += target.id
      （<Link> 的情况——它不经过 handler，点击即跳转）

页面归属（决定 pageNodeId / routeNodeId）：
  owner ← elementId 的组件前缀（元素 id 的第一段，见 3.2 的 id 形状表）
  routeNodeId ← 「renders 到 owner 的 route 节点」；多条时按 routes 分析器的
                先出现者为准（与 recall._page_nodes 同一规则，保证两边一致）
  pageNodeId  ← owner
  没有 route 指向 owner 时：pageNodeId 仍为 owner，routeNodeId 缺省，
  并记一条 action_chain_page_unanchored（info）——该组件不是任何路由的页面，
  通常是布局或共享组件，链路本身依然有效

收敛：同一 (elementId, event) 只产一条链；
      path 取「最长且全段有证据」的那条，其余并入该链的证据列表
```

各段的置信度直接沿用边上的值，不重算：`calls` / `writes` 来自事件层（0.9，符号解析依赖完整度），`requests` 来自 api 层（字面量 1.0 / 封装器 0.9 / 局部变量 0.8），`navigates` 来自导航层（0.9 / 0.8）。

**为什么放在 backend 而不是 ast-worker**：`derive_chains` 是 `IRGraph` 的纯函数。放进 ast-worker 会让它成为「第二个产图的地方」，`index.ts` 的编排顺序、契约校验、测试 fixture 都要跟着改；放在 backend 则可以直接对**已落库的历史图**重算——改一次链路切分规则，不需要重跑静态分析。这与 5.2 里「留原始 trace 以便离线重放合并」是同一条思路。

**为什么不是每个元素都产链**：一个中型页面有几十个带事件的元素，全量展开成链会让 `ActionChain[]` 大到无法进 prompt，也会让前端链路列表变成噪音。因此产链后按 `(path 长度, confidence)` 排序，**每页保留 ≤ 40 条**，截断时记 `action_chains_truncated` 告警（info 级）。被截掉的链路不是不存在，只是"这次没展示"——告警文案必须说清这一点。

### 3.4 链路在哪里断：`terminatedAt` 的三档

链路的中断是事实，不是缺陷。当前静态分析有两个已知断点（见 technical-design 4.1.5），链路层必须如实标注：

| 值 | 含义 | 典型场景 |
| --- | --- | --- |
| `null` | 链路走到叶子（接口 / 状态 / 路由），完整 | `<button onClick={handleExport}>` → `GET /api/orders/export` |
| `"prop_callback"` | 处理逻辑由父组件注入，在本组件内不可见 | `<RowItem onSelect={onSelect}>` |
| `"dynamic_handler"` | 处理函数形态无法静态判定（调用结果、条件表达式、动态属性） | `onClick={handlers[key]}` |

`terminatedAt !== null` 的链路**仍然要产出并展示**，只是 `confidence` 相应下调（`prop_callback` 与 `dynamic_handler` 都乘 0.85）并在前端标「链路在此中断」。把它们藏起来会让用户以为「这个按钮没有行为」——那是错的，行为存在，只是不在本组件里。

## 四、AI 在这一层的角色

候选单位从「页面」换成「链路」之后，AI 之前的所有约束原样保留，一条不放松。

### 4.1 输入：链路候选，不是元素列表

模型看到的是每条**已构造好的**链路摘要，而不是散装节点：

```
需求：订单列表页按状态筛选，并支持导出。
功能点：
f1: 按状态筛选订单
f2: 导出订单

交互链路候选（按证据强度降序）：
[
  { "chainId": "...#OrderList/jsx:select/handleStatusChange::onChange",
    "page": "OrderList", "route": "/orders",
    "element": "<select>", "event": "onChange",
    "handler": "handleStatusChange",
    "writes": ["status"], "requests": ["GET /api/orders?status="],
    "navigates": [], "terminatedAt": null,
    "quotes": ["全部状态", "待审核", "GET /api/orders?status="] },
  ...
]
```

`quotes` 是该链路全部可命中原文的并集（元素文案 + 列名 + 处理函数名 + 接口 URL），与现有 `quote_corpus` 同一套机制。模型的选择空间因此被压缩到「在 ≤30 条真实链路里挑」，而「挑」出来的东西必然是一整条有证据的路径。

### 4.2 四道闸不变

| # | 校验 | 失败去向 |
| --- | --- | --- |
| 1 | `chainId` ∈ 本次候选集 | `dropped/node_not_in_candidates` |
| 2 | `quote` 在**该链路的** `quotes` 里逐字存在 | `dropped/quote_not_found` |
| 3 | `featureId` ∈ 本次拆解出的功能点 | `dropped/unknown_feature` |
| 4 | 字段形状合法 | `dropped/invalid_shape` |

第 2 道闸的语料范围从「页面语料」收窄到「链路语料」，这是加强而非放松：想用「这个页面里有『导出』两个字」蒙混过关变得不可能——引文必须来自这条链自身。

### 4.3 置信度：两档分开记

这里有一个容易搞混的地方，必须写清楚：

| 对象 | 置信度来源 | 上限 |
| --- | --- | --- |
| 链路本身（`ActionChain.confidence`） | 路径上最弱一环的静态证据 | 1.0（语法事实）/ 0.9 / 0.8 |
| 「这条链对应这条需求」这个判断（`Match.confidence`） | AI 语义判定 | **0.6** |

也就是说：**链路的每一段都是事实，但「这条需求落在哪条链上」是语义判断**。两者不混。用户能看到一条 `confidence=0.9` 的「导出按钮 → handleExport → GET /api/orders/export」，同时看到「AI 认为 f2 对应这条链，置信度 0.5」。前者可以直接信，后者需要人确认。

这与 3.4 的分层完全一致，不需要新增一档。

## 五、召回：词法 + 语义两路融合

### 5.1 为什么现有召回需要第二路

`recall.py` 是纯词法召回：检索词来自 `lexicon.json` 的概念映射 + 中文 bigram 兜底 + 英文分词，命中后按 `SignalKind` 加权（routePath 1.0 / uiText 0.9 / apiUrl 0.8 / propName 0.6 …）。

它有三个结构性盲点：

1. **同义表达无解**。需求写「未审核的订单」，代码里是 `status === "pending"`。词表里没有 `未审核 → pending` 这条映射，两路都不命中。
2. **跨语言变体无解**。同一个页面上的文案可能是「状态筛选」「按状态过滤」「状态过滤」三种写法，词表覆盖不到的那个就等于不存在。
3. **语义相关但字面无关**。需求写「批量处理」，代码里是 `selectedRowKeys` + `batchAction`——`批量` 这个词根本不出现。

词法的优点是每条命中都能说出「哪个词命中了哪段原文」，这个性质必须保留。所以不是替换，是**加一路**。

### 5.2 融合方式：RRF，不融合分数

两路的分数不可比：词法分是「权重 × 命中词权重之和」，语义分是相似度或模型打分，两者量纲不同、分布不同。**直接加权求和是最常见的错误**——调参调到能用的权重，换一个项目就失效。

RRF（Reciprocal Rank Fusion）只用名次，不用分数：

```
score(d) = Σ_r  weight_r / (k + rank_r(d))
```

- `k = 60`（业界常用默认值）：把头部差距压平，避免某一路的第 1 名垄断；
- `weight_r`：词法路 1.0，语义路 0.8（语义路在首版里精度不确定，给它更低的权重；跑通评估后再调）；
- 只在 `rank ≤ 30` 的范围内计分（与 `llm_max_candidates` 一致）。

RRF 的两个关键好处：**两路不需要分数校准**；**任一路可以独立失败**——语义路超时或未配置时，词法路的名次原样保留，融合退化为单路排序，只记一条告警。

### 5.3 语义路怎么实现

首版用 **LLM 重排**，不用 embedding：

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| LLM 重排（推荐首版） | 复用已有 `LLMClient` 与预算；能给出理由，进 evidence 可解释 | 有延迟与成本；一次只能处理批量候选 |
| 本地 embedding | 无外部调用、可离线 | 需另建向量索引与模型部署；跨语言（中→英标识符）效果取决于模型；命中理由不可解释 |

做法：拿词法路 Top-30 作为输入，一次调用让模型输出「按与当前功能点的相关度排序的 chainId 列表」。模型**不产出新候选，只重排**——这与 4.1 的「只在候选集里挑」是同一条原则。

**dry-run 下语义路关闭**。`dry-run` 的定义是「不调模型」，不能因为融合需要就偷偷调一次。此时 `recall` 退化为单路，`RequirementMap.warnings` 记 `semantic_recall_skipped`（info）。

### 5.4 两路都要落 evidence

融合结果必须能回答「这个候选为什么排第一」。因此每条候选的 `signals` 增加两路的名次：

```jsonc
{ "kind": "uiText", "value": "导出", "score": 0.9 },          // 词法路的命中信号（原有）
{ "kind": "semanticRank", "value": "rank=2", "score": 0.8 }   // 语义路的名次（新增）
```

新增 `SignalKind` 取值需要改 `requirement.schema.json` 并重新生成两侧类型（`npm run gen:contracts`）。这是受控枚举的正常演进，不是例外。

**只落名次，不落分数**：语义分的绝对值没有解释力（「0.83」说明不了什么），而「语义路排第 2」是可以被用户理解和质疑的。

### 5.5 什么时候算融合成功

上线前必须回答一个问题：**融合是否真的比单路好**。因此融合的验收标准是相对的：

- 在同一评估集上，融合后的页面级 P@1 ≥ 单路词法的 P@1 + 5 个百分点；
- 若未达到，**不上线语义路**，保留代码但默认关闭（配置开关）。为了技术而技术是这类系统的常见死法。

## 六、前端：链路视图

链路是定位结果的落点，前端必须能展示、能下钻、能聚焦。

### 6.1 需求面板的结果区（从四段变五段）

现有四段：功能点 → 候选 → 匹配结果 → 未命中/被拒。新增第五段：

**交互链路**：每条命中的链路渲染为一行可展开项，折叠时显示

```
<button> 导出 · onClick · handleExport → GET /api/orders/export   [0.9]
```

展开后显示逐段路径（元素 → 处理函数 → 状态 / 接口），每段带 `file:line` 可点击跳转；`terminatedAt !== null` 时在末尾显示 `[链路在此中断：处理逻辑由父组件注入]`。

### 6.2 聚焦时喂的是链路，不是子图

现有「聚焦子图」按钮把 `match.subgraph.nodeIds` 喂给 `GraphCanvas`。链路命中后改为喂 `chain.path.nodeIds` + `chain.path.edgeIds`：

- 节点集合更小、更准（一条链最多 4~5 个节点，子图最多 80 个）；
- 边集合是**有序**的，可以逐段高亮，这是「可视化功能链路」真正想要的效果；
- `focus.ts` 不需要改：它接受 `Set<string>`，链路的 nodeIds 直接可用。

### 6.3 分页：链路列表必须可折叠

一个页面有几十条链路是常态。默认只展示**对齐了需求锚点的链路**（`aligned = true`），其余折叠在「该页面的其他 N 条交互」里。不折叠的话，用户看到的是一份无法阅读的清单，而不是一份答案。

## 七、验收指标

链路层的评估必须独立于页面层——页面级 P@1 很高但链路级全错，是这类系统最危险的失败模式（用户看到「定位成功」，点进去发现链路是别的东西）。

| 指标 | 定义 | 首版目标 |
| --- | --- | --- |
| 链路级 P@1 | 正确链路排在第一条的比例 | ≥ 50% |
| 链路级 P@5 | 正确链路落在前五条的比例 | ≥ 80% |
| 三元组完整率 | `element + handler + api/state` 三段都被正确找回的比例 | ≥ 70% |
| 断点标注准确率 | `terminatedAt` 判定与人工核对一致的比例 | ≥ 95% |
| 误匹配率 | `confidence ≥ 0.5` 但人工判定为错的比例 | < 5% |
| 超纲识别率 | 代码中不存在的功能被放入 `unmatched` 的比例 | ≥ 90% |
| 单次定位模型调用数 | 每条需求平均调用次数 | ≤ 4 |
| 链路构造耗时 | `derive_chains` 在千节点图上的耗时 | < 200ms |

**评估集**：30~50 条需求片段，覆盖三类仓库（小 demo / 中型后台 / Next.js 项目），每条标注期望的**链路**（元素 loc 邻域 + handler 名 + 接口 URL），而不只是期望页面。数据来自真实需求文档 + 已确认的正确结果。

**回放方式**：直接读 `graphs` 表的历史产物，`derive_chains` 可离线重跑，不重新跑静态分析。改一次链路切分规则，同一份图应得到可复现的链路集合。

**错误按桶归因**：链路未构造出来 / 构造出来但 AI 未选中 / AI 选中了错的链 / 引文校验误杀 / 断点误判。前两类是不同方向的问题，必须分开统计，否则「召回不足」与「判定不准」会互相掩盖。

## 八、与既有文档和契约的关系

### 8.1 契约演进

| 契约 | 变更 | 是否需要重新生成 |
| --- | --- | --- |
| `ir.schema.json` | 已加 `contains` / `navigates`（version 1.1） | 已完成 |
| `requirement.schema.json` | 新 `SignalKind.semanticRank`、`Match.chainId`、`RequirementMap.interactionChains`、`Match.targets`、`Match.anchors` | 待做（v1.1） |
| 新 `chain.schema.json` | `ActionChain` + `ActionChainList` | 待做 |

新契约的理由：`ActionChain` 是「图」的派生，不是「某次需求定位」的产物，放进 `requirement.schema.json` 会让同一份图被不同需求重复序列化一遍。

**版本号策略**：`ir.schema.json` 升 1.1 已做且有测试锁定（`contract.test.ts` 断言 `version === "1.1"`）。`requirement.schema.json` 把 `Match.confidence` 的上限从 0.6 放宽到 0.95，看起来是放宽数值、实际是收紧语义（0.6 变成「AI 判定」这一档的专属上限，见 3.4）。因此必须先改 schema 再改 `judge.py` 的 `_MAX_CONFIDENCE`，不能反过来——否则代码会产出 schema 校验不过的产物。

### 8.2 与 technical-design 6.14 / 6.15 的差异

| 6.14 / 6.15 的表述 | 本文的修正 |
| --- | --- |
| 「两跳子图」，`component → fn → api` 固定追加一跳 | 子图**保持一跳**（它是证据范围，不是依赖闭包）；接口链路由 ActionChain 承载。两者职责不重叠，见 6.8 的提醒 |
| `InteractionChain.handler` 只有 `nodeId / kind` | 增加 `terminatedAt`，把断点变成一等字段 |
| `Match.confidence` 上限提到 0.95 | 只在 `source ∈ {anchor, runtime}` 时可达；ActionChain 的**判定**仍封顶 0.6。链路的**证据**置信度本来就可达 0.9，不需要经过 `Match.confidence` |

## 九、已知限制与不做的

1. **跨组件链路不做**。父组件注入的回调（`prop_callback`）在子组件内终止，不反向展开到父组件。反向展开会把整棵组件树的反向调用都带进来（一个共享按钮可能有十几个父），链路失去边界。这一段的补全依赖人工确认或 P4 的 diff 分析。
2. **条件渲染 / Tab / 弹窗状态不建模**。只记录「这个按钮有事件」，不表达「点击后弹窗出现」。这是运行时的事，由 P2 的 merged 图补。
3. **循环与递归不做环检测**。`calls` 的最大深度是 3，遇环即停。不做强连通分量分析——请求链路上出现环本身就说明分析规则有问题，应该在告警里暴露，而不是静默消解。
4. **不做链路的「重要性」排序**。排序只按证据强度与路径长度，不猜「哪个按钮业务上更重要」。重要性是需求侧的判断，由 AI 与用户决定。
5. **AI 不构造链路、不修改链路、不新增段**。它只能选、只能引用、只能说「没找到」。这条边界一旦松动，本文所有置信度设计都失效。

## 十、落地顺序

按「能独立验证」的原则排：

| 顺序 | 内容 | 前置 | 独立验收方式 |
| --- | --- | --- | --- |
| 1 | `navigate.ts` + `contains` 边 | 无 | ✅ 已完成：211 个 ast-worker 测试，真实样本仓跑通 |
| 2 | `derive_chains` + `chain.schema.json` | 1 | 纯图算法单测；同一份 fixture 图产出可复现的链路集合 |
| 3 | 前端链路视图（结果区第五段 + 聚焦喂链路） | 2 | vitest；手工 E2E：链路列表 → 展开 → 聚焦高亮 |
| 4 | 召回语义路 + RRF 融合 | 2（链路层融合需要） | 评估集上 P@1 提升 ≥ 5 个百分点，否则默认关闭 |

第 2 步不依赖模型、不依赖前端，可以在任何时刻独立完成与验证——这是把它排在第 3、4 步之前的全部理由。

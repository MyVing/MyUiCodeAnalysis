/**
 * 静态分析 Worker 入口。
 *
 * 使用方式（由编排层调用）：
 *   echo '{"projectId":"prj_1","rootPath":"/abs/path","analysisId":"an_1"}' \
 *     | node dist/index.js
 *
 * 设计约束：
 *  - 只通过 stdin/stdout 通信，不写库、不读业务数据。
 *  - 任何失败都以 { ok: false, error } 形式回传，并退出码 0，
 *    让编排层能区分"Worker 报错"与"Worker 被超时 kill"。
 *  - stdout 只输出一行业务 JSON；日志一律走 stderr。
 */

import { existsSync, statSync } from "node:fs";
import path from "node:path";

import type { Warning } from "@uicodeanalysis/contracts";

import { analyzeApi } from "./api.js";
import { discoverComponents } from "./components.js";
import { analyzeEvents } from "./events.js";
import { analyzeHooks } from "./hooks.js";
import { buildGraph } from "./ir-builder.js";
import { buildJsxInventory } from "./jsx-inventory.js";
import { analyzeNavigation } from "./navigate.js";
import { createFullProject, createIncrementalProject } from "./project.js";
import { analyzeProps } from "./props.js";
import type { WorkerRequest, WorkerResponse } from "./protocol.js";
import { analyzeRenders } from "./renders.js";
import { createResolutionIndex } from "./resolver.js";
import { analyzeRoutes } from "./routes.js";
import { analyzeUiText } from "./ui-text.js";
import { analyzeView } from "./view.js";
import { buildWrapperAttrs, loadWrapperMappings } from "./wrappers.js";

function log(message: string): void {
  process.stderr.write(`[ast-worker] ${message}\n`);
}

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    process.stdin.on("data", (chunk: Buffer) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

function parseRequest(raw: string): WorkerRequest {
  const parsed = JSON.parse(raw) as Partial<WorkerRequest>;
  if (!parsed.projectId || !parsed.rootPath || !parsed.analysisId) {
    throw new Error("请求缺少必填字段：projectId / rootPath / analysisId");
  }
  if (!existsSync(parsed.rootPath) || !statSync(parsed.rootPath).isDirectory()) {
    throw new Error(`rootPath 不存在或不是目录：${parsed.rootPath}`);
  }
  return parsed as WorkerRequest;
}

async function analyze(request: WorkerRequest): Promise<WorkerResponse> {
  const rootPath = path.resolve(request.rootPath);
  const changedFiles = request.changedFiles ?? [];
  const isIncremental = changedFiles.length > 0;

  const warnings: Warning[] = [];

  let project;
  if (isIncremental) {
    project = createIncrementalProject(rootPath, changedFiles);
    log(`增量模式：${project.getSourceFiles().length} 个源文件（${changedFiles.length} 个变更）`);
  } else {
    const full = createFullProject(rootPath);
    project = full.project;
    // 配置层的缺口（无 tsconfig / solution-style 换配置 / 扫描不到源文件）
    // 由 project.ts 判定并随结果返回，入口只负责并入告警
    warnings.push(...full.warnings);
    log(
      full.fromTsConfig
        ? `全量模式：读取 ${path.basename(full.tsConfigPath ?? "tsconfig.json")}，${project.getSourceFiles().length} 个源文件`
        : `全量模式：未使用 tsconfig，glob 扫描 ${project.getSourceFiles().length} 个源文件`,
    );
  }

  // 解析索引先于组件发现构造：它负责收齐 unresolved_import / dependency_not_installed 告警
  const resolution = createResolutionIndex(rootPath, project);
  warnings.push(...resolution.warnings);

  const discovery = discoverComponents(rootPath, project);
  warnings.push(...discovery.warnings);

  const propsResult = analyzeProps({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  warnings.push(...propsResult.warnings);

  const renders = analyzeRenders({
    rootPath,
    project,
    components: discovery.components,
    declarations: discovery.declarations,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
  });
  warnings.push(...renders.warnings);

  const hooksResult = analyzeHooks({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  warnings.push(...hooksResult.warnings);

  const apiResult = analyzeApi({
    rootPath,
    project,
    definitionIndex: discovery.definitionIndex,
    // hook 回调体归属：`const handleExport = useCallback(() => request(...), [])`
    // 里的请求归 hook 节点，不归组件——否则图上显示成「加载时请求」
    hookCallbackOwners: hooksResult.callbackOwners,
  });
  warnings.push(...apiResult.warnings);

  // 事件分析要在 hook / state / fn 之后：它回答「处理函数落到谁」，
  // 而"谁"必须是图上已经存在的节点，不能凭空发明。
  const knownNodes = [
    ...hooksResult.hookNodes,
    ...hooksResult.stateNodes,
    ...apiResult.apiNodes,
    ...apiResult.fnNodes,
  ];

  // props 字段名用于判定「事件处理由父组件注入」：`<div onClick={onSelect}>`
  // 里 onSelect 不是本组件的函数，而是 props 上的回调
  const propsFields = new Map<string, readonly string[]>(
    [...propsResult.byComponent].map(([id, info]) => [id, info.fields.map((f) => f.name)]),
  );

  // JSX 元素清单：视图层与事件层共用同一份 id 来源。必须在组件发现之后，
  // 因为归属索引依赖组件声明位置；一旦建立，两个分析器都只查表不自算。
  const inventory = buildJsxInventory(
    rootPath,
    project,
    discovery.components,
    discovery.declarations,
  );

  const eventsResult = analyzeEvents({
    inventory,
    components: discovery.components,
    knownNodes,
    propsFields,
    rootPath,
  });
  warnings.push(...eventsResult.warnings);

  // 视图层：把「页面上有什么」补进图。与事件层产同 id 元素节点，
  // buildGraph 按 id 合并 attrs —— 一个是 events，一个是 viewRole。
  //
  // 事件层收录了视图层不收的元素（`<div onClick>` 这类布局元素），它们已经是
  // 图上的节点，必须同样挂上 contains 父边，否则在视图树里是孤儿、下钻就断了。
  // 因此这里把事件层的 id 传进去：视图层只补父子关系，不重复产节点。
  const viewResult = analyzeView({
    inventory,
    extraElementIds: new Set(eventsResult.elementNodes.map((node) => node.id)),
  });
  warnings.push(...viewResult.warnings);

  // 二次封装映射：规则来自项目根的 mapping.json，核心分析器里没有任何
  // `if (tag === "ProTable")`。文件不存在是常态（不是所有项目都配了映射），
  // 因此不告警；存在但格式不对才告警——那是配置写错了，必须让人看见。
  const wrapperResult = loadWrapperMappings(rootPath, discovery.components);
  warnings.push(...wrapperResult.warnings);

  // 可见文案：需求检索的召回入口。纯语法事实，不依赖其他分析器的结论，
  // 但它需要组件归属索引，因此放在组件发现之后。
  const uiTextResult = analyzeUiText({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
    project,
  });
  warnings.push(...uiTextResult.warnings);

  const textCount = [...uiTextResult.byComponent.values()].reduce(
    (sum, entry) => sum + entry.texts.length,
    0,
  );

  // 路由 / 页面节点：需求说的是「用户列表页」，代码里对应 `/users`。
  // 没有它，页面只能靠目录名暗示，需求定位无从确认目标。放在最后是因为它
  // 需要 definitionIndex 与 resolution，且与前面的分析器没有依赖方向。
  const routesResult = analyzeRoutes({
    rootPath,
    project,
    components: discovery.components,
    definitionIndex: discovery.definitionIndex,
    resolution: resolution.index,
    ...(request.framework ? { framework: request.framework } : {}),
  });
  warnings.push(...routesResult.warnings);

  // 导航边：依赖路由节点（目标是 route）与视图层（<Link> 的起点是元素），
  // 所以放在两者之后。它把「路由表」变成「页面之间的可达关系」。
  const navigationResult = analyzeNavigation({
    rootPath,
    inventory,
    elementNodeIds: new Set([
      ...viewResult.elementNodes.map((node) => node.id),
      ...eventsResult.elementNodes.map((node) => node.id),
    ]),
    routeNodes: routesResult.routeNodes,
    components: discovery.components,
    declarations: discovery.declarations,
  });
  warnings.push(...navigationResult.warnings);

  log(
    `发现 ${discovery.components.length} 个组件、${renders.edges.length} 条 renders 边` +
      `、${renders.externalNodes.length} 个外部组件，` +
      `${hooksResult.hookNodes.length} 个 hook、${hooksResult.stateNodes.length} 个状态，` +
      `${apiResult.apiNodes.length} 个接口、${apiResult.fnNodes.length} 个请求链函数，` +
      `${eventsResult.elementNodes.length} 个事件元素、${viewResult.elementNodes.length} 个视图节点，` +
      `${wrapperResult.byComponent.size} 个封装组件，` +
      `${textCount} 条可见文案、${routesResult.routeNodes.length} 个路由、` +
      `${navigationResult.edges.length} 条导航边，` +
      `${warnings.length} 条告警`,
  );

  // 封装注解要带「渲染边是否真的连到了目标」：封装里用动态组件时可能是 false，
  // 这种"声明了但没连上"必须如实反映，不能假装成功。
  const wrapperAttrs = buildWrapperAttrs(
    wrapperResult.byComponent,
    new Set(renders.edges.map((edge) => `${edge.from} ${edge.to}`)),
  );

  return {
    ok: true,
    graph: buildGraph({
      request,
      components: discovery.components,
      props: propsResult.byComponent,
      wrappers: wrapperAttrs,
      texts: uiTextResult.byComponent,
      edges: [
        ...renders.edges,
        ...hooksResult.edges,
        ...apiResult.edges,
        ...eventsResult.edges,
        ...viewResult.edges,
        ...routesResult.edges,
        ...navigationResult.edges,
      ],
      externalNodes: renders.externalNodes,
      extraNodes: [
        ...knownNodes,
        // 事件引用到、但图里原本不存在的跨文件函数：边必须连到真实节点，
        // 否则前端点开目标会发现它不在图上
        ...eventsResult.fnNodes,
        // 视图层与事件层对同一元素各产一份节点，buildGraph 按 id 合并 attrs：
        // 合并后既带 events 又带 viewRole，才是完整的「这个元素是什么、点了会怎样」。
        // 视图层先放：它的 name 带可见文案（`<button> 导出`），事件层只有 `<button>`；
        // 两层的 file/loc 取自同一份元素清单，不存在覆盖问题。
        ...viewResult.elementNodes,
        ...eventsResult.elementNodes,
        ...routesResult.routeNodes,
      ],
      warnings,
    }),
  };
}

async function main(): Promise<void> {
  let request: WorkerRequest;
  try {
    request = parseRequest(await readStdin());
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`请求解析失败：${message}`);
    process.stdout.write(
      JSON.stringify({
        ok: false,
        error: { code: "invalid_request", message },
      } satisfies WorkerResponse),
    );
    return;
  }

  try {
    const response = await analyze(request);
    process.stdout.write(JSON.stringify(response));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`分析失败：${message}`);
    process.stdout.write(
      JSON.stringify({
        ok: false,
        error: { code: "analysis_failed", message },
      } satisfies WorkerResponse),
    );
  }
}

void main();

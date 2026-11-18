"""LangGraph 编排：decompose → anchor → recall → locate → assemble。

两种模式共用同一张图：

| 模式 | anchor | recall | locate |
| --- | --- | --- | --- |
| dry-run | 锚点直连（零模型） | 词法召回，产出 candidates | 跳过（模型不参与） |
| 非 dry-run | 锚点直连（零模型） | 词法召回 + 预筛依据 | 模型从「页面 + 链路」全集里挑 |

锚点直连（6.13）是零模型的确定性步骤，dry-run 照常产出：锚点覆盖的功能点直接
落 `matches`（source=static），不再进模型——第四步语义残差只针对未覆盖的功能点。
dry-run 的「不调模型」语义不变，但结论不再只看 candidates：确定性锚点本来就是
零模型的，把它排除反而让 dry-run 低估可用结论。

locate 失败（模型不可用 / 输出无效）时 `LocateOutcome.degraded` 为真，未覆盖功能点
退化成「词法候选 + 显式告警」，任务仍然成功——一次模型抖动不该把用户的分析结果打没。

图本身不引入新逻辑：decompose / anchor / recall / derive_chains 都是纯确定性函数；
locate 的每次模型调用都有预算上限（`llm_max_calls_per_request`）。任务状态机
仍由 RequirementLocateService 负责——图管「怎么算」，服务管「算之前和之后」。

checkpointer 落 MySQL（与业务同库）：判定类产物的价值在可审计，中间状态就是
审计材料。thread_id 用 task.id，同一次定位的全部 superstep 可回放。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.contracts import Feature, IRGraph, RequirementMap, RequirementWarning
from app.contracts.requirement import Dropped, Match, Severity, Unmatched
from app.core.config import Settings
from app.requirement.anchors import AnchorOutcome, anchor_direct_connect
from app.requirement.chains import derive_chains
from app.requirement.features import DecomposedFeature, decompose_with_report
from app.requirement.judge import JudgeOutcome, judge
from app.requirement.llm import LLMClient
from app.requirement.locate import (
    LocateOutcome,
    build_page_index,
    chain_contracts,
    locate,
    merge_lexical_candidates,
    semantic_signals,
)
from app.requirement.page_profile import ProfilePayload
from app.requirement.recall import CandidateDetail, RecallResult, recall
from app.schemas import Project, RequirementTask

# 产物格式版本。1.1 起新增 interactionChains 与 Match.chainId/targets（均可选），
# 1.2 起 matches 可含锚点直连产物（source=static，置信度 0.95/0.85，不受 AI 0.6
# 封顶约束）。旧消费方按 1.0/1.1 解析不会崩；锚点结论只在 source 字段上与 AI 区分。
SCHEMA_VERSION = "1.2"


class LocateState(TypedDict, total=False):
    """图状态。LLM 与图等不序列化的对象通过节点闭包传入，不进状态。

    PageIndex / ChainIndex 不进状态：它们由 graph 确定性推导，缓存进 checkpointer
    只是把「可重算的中间态」写了一份副本。状态里只放结论。
    """

    requirement: str
    features: list[DecomposedFeature]
    anchor: AnchorOutcome
    decompose_warnings: list[RequirementWarning]
    candidates: list[CandidateDetail]
    recall_warnings: list[RequirementWarning]
    judge: JudgeOutcome
    locate: LocateOutcome
    map: RequirementMap


class RequirementOrchestrator:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient | None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        # 无持久化后端时退到 MemorySaver：保持「有 checkpointer」这条代码路径
        # 被执行到，行为差异对编排层不可见。
        self._checkpointer = checkpointer if checkpointer is not None else MemorySaver()

    async def run(
        self,
        project: Project,
        task: RequirementTask,
        graph: IRGraph,
        profile: ProfilePayload | None = None,
    ) -> RequirementMap:
        """跑一次定位。`profile` 是离线页面画像（阶段 C 产物，可为 None）。

        画像按图版本缓存，由调用方（`RequirementLocateService`）读出来传进来：
        编排层不该自己碰仓储，它只负责「怎么算」。
        """
        compiled = self._build_graph(project, task, graph, profile)
        config: RunnableConfig = {"configurable": {"thread_id": task.id}}
        final = await compiled.ainvoke({"requirement": task.requirement}, config)
        return final["map"]

    def _build_graph(
        self,
        project: Project,
        task: RequirementTask,
        graph: IRGraph,
        profile: ProfilePayload | None = None,
    ) -> CompiledStateGraph:
        settings = self._settings
        llm = self._llm if not task.dry_run else None

        context = _AssembleContext(
            project_id=project.id,
            requirement_id=task.requirement_id,
            analysis_id=graph.analysisId,
            dry_run=task.dry_run,
        )

        async def decompose_node(state: LocateState) -> dict:
            # 截断必须带回状态：被挤掉的功能点既不进产物也不会自己吭声
            result = decompose_with_report(state["requirement"])
            return {"features": result.features, "decompose_warnings": result.warnings}

        async def anchor_node(state: LocateState) -> dict:
            # 锚点直连（6.13 第三步）：零模型、纯确定性。dry-run 与模型不可用
            # 时照常工作——这是它区别于 locate 的根本属性。
            return {"anchor": anchor_direct_connect(graph, state["features"])}

        async def recall_node(state: LocateState) -> dict:
            top_k = settings.llm_max_candidates
            results = [recall(graph, feature.terms, top_k=top_k) for feature in state["features"]]
            warnings = [warning for result in results for warning in result.warnings]
            # 合并后统一截断：多个功能点各自取 top_k 会重复计数，
            # 让「候选上限」名不副实。
            candidates = merge_lexical_candidates(*results)[:top_k]
            return {"candidates": candidates, "recall_warnings": warnings}

        async def locate_node(state: LocateState) -> dict:
            if llm is None:
                return {}
            # 语义残差（6.13 第四步）：锚点已覆盖的功能点不再进模型——锚点结论
            # 是图上事实（0.95/0.85），模型只能补充锚点未覆盖的功能点。
            anchor = state.get("anchor")
            covered = anchor.covered if anchor is not None else frozenset()
            features = [item for item in state["features"] if item.id not in covered]
            if not features:
                return {}
            # 链路必须由确定性遍历产出（见 chains.py）：模型只负责「选」。
            # 这里重新推导一次而不缓存，因为推导是纯函数且成本远低于一次模型调用。
            chains = derive_chains(graph)
            if not chains.by_id:
                # 图里没有任何可推导的交互（纯静态站点、或元素事实缺失）。
                # 此时让模型在「链路」里挑是无意义的：退回页面级判定，prompt 里
                # 带 props / 接口等更细的证据，候选也收窄到词法 Top-N。
                # 这不是降级——页面本来就是合法落点。
                outcome = await judge(
                    features,
                    state["candidates"],
                    graph,
                    llm,
                    requirement=state["requirement"],
                    max_calls=settings.llm_max_calls_per_request,
                    summaries=_summaries(profile),
                )
                return {"judge": outcome}

            index = build_page_index(
                graph,
                chains,
                # 画像只在这一支被读到（llm 为 None 的 dry-run 在函数开头就返回了），
                # 因此不需要再判一次 dry_run 来「保护」它。
                profile=profile,
                recall_result=_recall_view(state),
                # 0 表示不预筛（见 Settings.llm_max_pages）
                max_pages=settings.llm_max_pages or None,
            )
            locate_outcome = await locate(
                features,
                index,
                chains,
                graph,
                llm,
                requirement=state["requirement"],
                max_calls=settings.llm_max_calls_per_request,
            )
            return {"locate": locate_outcome}

        async def assemble_node(state: LocateState) -> dict:
            warnings = [
                *(state.get("decompose_warnings") or []),
                *(state.get("recall_warnings") or []),
            ]
            candidates = list(state.get("candidates") or [])
            anchor_outcome = state.get("anchor")
            # 锚点直连的结论先落 matches（6.13 第五步：锚点结果先落 matches）；
            # 模型结论只针对未覆盖功能点，两者天然不重叠，不存在抢一个功能点的问题。
            matches: list[Match] = list(anchor_outcome.matches) if anchor_outcome else []
            dropped: list[Dropped] = []
            unmatched: list[Unmatched] = []
            interaction_chains = None

            locate_outcome = state.get("locate")
            judge_outcome = state.get("judge")
            if anchor_outcome:
                warnings.extend(anchor_outcome.warnings)
            if locate_outcome is not None:
                warnings.extend(locate_outcome.warnings)
                matches.extend(locate_outcome.matches)
                dropped = locate_outcome.dropped
                unmatched = locate_outcome.unmatched
                if locate_outcome.chain_ids:
                    interaction_chains = chain_contracts(
                        derive_chains(graph), locate_outcome.chain_ids
                    )
                # 「AI 也选了哪些页面」只作为标注写回候选，不改动词法得分
                semantic_signals(candidates, matches)
            elif judge_outcome is not None:
                warnings.extend(judge_outcome.warnings)
                matches.extend(judge_outcome.matches)
                dropped = judge_outcome.dropped
                unmatched = judge_outcome.unmatched
                semantic_signals(candidates, matches)

            requirement_map = _assemble_map(
                context,
                state["requirement"],
                state["features"],
                matches,
                unmatched,
                candidates,
                dropped,
                warnings,
                interaction_chains,
            )
            return {"map": requirement_map}

        builder = StateGraph(LocateState)
        builder.add_node("decompose", decompose_node)
        builder.add_node("anchor", anchor_node)
        builder.add_node("recall", recall_node)
        builder.add_node("locate", locate_node)
        builder.add_node("assemble", assemble_node)
        builder.add_edge(START, "decompose")
        builder.add_edge("decompose", "anchor")
        builder.add_edge("anchor", "recall")
        builder.add_edge("recall", "locate")
        builder.add_edge("locate", "assemble")
        builder.add_edge("assemble", END)
        return builder.compile(checkpointer=self._checkpointer)


def _summaries(profile: ProfilePayload | None) -> dict[str, str] | None:
    """页面画像压成 nodeId → 一句话。judge 路径用它补「这页是做什么的」。"""
    if profile is None or not profile.profiles:
        return None
    return {item.page_id: item.summary for item in profile.profiles}


def _recall_view(state: LocateState) -> RecallResult:
    """把合并后的候选包成 RecallResult，供页面索引按词法得分排序与预筛。"""
    return RecallResult(
        candidates=state.get("candidates") or [],
        warnings=[],
        route_by_page={},
    )


class _AssembleContext:
    def __init__(
        self,
        *,
        project_id: str,
        requirement_id: str,
        analysis_id: str | None,
        dry_run: bool,
    ) -> None:
        self.project_id = project_id
        self.requirement_id = requirement_id
        self.analysis_id = analysis_id
        self.dry_run = dry_run


def _assemble_map(
    context: _AssembleContext,
    requirement: str,
    features: list[DecomposedFeature],
    matches: list[Match],
    unmatched: list[Unmatched],
    candidates: list[CandidateDetail],
    dropped: list[Dropped],
    warnings: list[RequirementWarning],
    interaction_chains: list | None = None,
) -> RequirementMap:
    return RequirementMap(
        schemaVersion=SCHEMA_VERSION,
        requirementId=context.requirement_id,
        projectId=context.project_id,
        analysisId=context.analysis_id,
        requirement=requirement,
        createdAt=datetime.now(UTC),
        dryRun=context.dry_run,
        features=[
            Feature(id=feature.id, text=feature.text, keywords=feature.keywords())
            for feature in features
        ],
        matches=matches,
        unmatched=unmatched or None,
        candidates=[detail.candidate for detail in candidates] or None,
        dropped=dropped or None,
        warnings=_sorted_warnings(warnings) or None,
        interactionChains=interaction_chains or None,
    )


def _dedupe_warnings(warnings: list[RequirementWarning]) -> list[RequirementWarning]:
    """同一告警在同一份产物里只保留一条：按功能点重复召回会产生完全相同的告警。"""
    seen: set[tuple[str, str]] = set()
    result: list[RequirementWarning] = []
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result


def _sorted_warnings(warnings: list[RequirementWarning]) -> list[RequirementWarning]:
    order = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}
    return sorted(
        _dedupe_warnings(warnings),
        key=lambda warning: (order[warning.severity], warning.code, warning.message),
    )

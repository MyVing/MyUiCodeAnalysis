"""LangGraph 编排：decompose → recall → judge → assemble。

图本身不引入新逻辑：decompose / recall 是纯确定性函数；judge 只在配置了模型
且非 dry-run 时执行（否则图在 recall 后直接收尾，产物 dryRun=true）。任务状态机
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
from app.requirement.features import DecomposedFeature, decompose
from app.requirement.judge import JudgeOutcome, judge
from app.requirement.llm import LLMClient
from app.requirement.recall import CandidateDetail, recall
from app.schemas import Project, RequirementTask

SCHEMA_VERSION = "1.0"


class LocateState(TypedDict, total=False):
    """图状态。LLM 与图等不序列化的对象通过节点闭包传入，不进状态。"""

    requirement: str
    features: list[DecomposedFeature]
    candidates: list[CandidateDetail]
    recall_warnings: list[RequirementWarning]
    judge: JudgeOutcome
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

    async def run(self, project: Project, task: RequirementTask, graph: IRGraph) -> RequirementMap:
        compiled = self._build_graph(project, task, graph)
        config: RunnableConfig = {"configurable": {"thread_id": task.id}}
        final = await compiled.ainvoke({"requirement": task.requirement}, config)
        return final["map"]

    def _build_graph(
        self, project: Project, task: RequirementTask, graph: IRGraph
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
            return {"features": decompose(state["requirement"])}

        async def recall_node(state: LocateState) -> dict:
            top_k = settings.llm_max_candidates
            merged: dict[str, CandidateDetail] = {}
            warnings: list[RequirementWarning] = []
            for feature in state["features"]:
                result = recall(graph, feature.terms, top_k=top_k)
                warnings.extend(result.warnings)
                for detail in result.candidates:
                    existing = merged.get(detail.candidate.nodeId)
                    if existing is None or detail.candidate.score > existing.candidate.score:
                        merged[detail.candidate.nodeId] = detail
            candidates = sorted(
                merged.values(), key=lambda item: (-item.candidate.score, item.candidate.nodeId)
            )[:top_k]
            return {"candidates": candidates, "recall_warnings": warnings}

        async def judge_node(state: LocateState) -> dict:
            if llm is None:
                return {}
            outcome = await judge(
                state["features"],
                state["candidates"],
                graph,
                llm,
                requirement=state["requirement"],
                max_calls=settings.llm_max_calls_per_request,
            )
            return {"judge": outcome}

        async def assemble_node(state: LocateState) -> dict:
            judge_outcome = state.get("judge")
            warnings = list(state.get("recall_warnings") or [])
            matches: list[Match] = []
            dropped: list[Dropped] = []
            unmatched: list[Unmatched] = []
            if judge_outcome is not None:
                warnings.extend(judge_outcome.warnings)
                matches = judge_outcome.matches
                dropped = judge_outcome.dropped
                unmatched = judge_outcome.unmatched
            requirement_map = _assemble_map(
                context,
                state["requirement"],
                state["features"],
                matches,
                unmatched,
                state.get("candidates") or [],
                dropped,
                warnings,
            )
            return {"map": requirement_map}

        builder = StateGraph(LocateState)
        builder.add_node("decompose", decompose_node)
        builder.add_node("recall", recall_node)
        builder.add_node("judge", judge_node)
        builder.add_node("assemble", assemble_node)
        builder.add_edge(START, "decompose")
        builder.add_edge("decompose", "recall")
        builder.add_edge("recall", "judge")
        builder.add_edge("judge", "assemble")
        builder.add_edge("assemble", END)
        return builder.compile(checkpointer=self._checkpointer)


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
        warnings=_sorted_warnings(warnings),
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

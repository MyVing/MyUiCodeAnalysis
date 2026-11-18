"""探索服务：任务生命周期 + 三段式编排（discovery → 计划 → 执行）与 runtime 同构。

任务流转 queued → running → succeeded / failed；前置校验同步拒绝。
编排逻辑：
  1. discover：Worker 枚举页面真实可交互元素；
  2. plan：模型基于元素清单预生成确定性计划（不可用则退兜底计划）；
  3. execute（非 dry-run）：Worker 确定性执行计划，逐步截图；
  4. 产物：探索轨迹 + 场景/用例清单 + Playwright .spec.ts，整体落库。
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.contracts import (
    Action,
    DiscoveredElement,
    ExploreArtifact,
    ExplorePlan,
    ExploreStepResult,
    ExploreWarning,
    Screenshot,
    Selector,
    Severity,
)
from app.core.config import Settings
from app.explore.planning import PlanSource, generate_plan, resolve_step_selector
from app.explore.runner import (
    DiscoverWorkerRequest,
    ExploreStepPayload,
    ExploreWorkerRequest,
    ExploreWorkerRunner,
)
from app.explore.script import build_script
from app.requirement.llm import LLMClient
from app.schemas import ExploreStart, ExploreTask, Project
from app.storage.explore import ExploreRepository

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 12
# 值摘要里的敏感字段口径：字段名或占位值含这些关键字的输入原样脱敏
_SENSITIVE_HINTS = ("password", "passwd", "secret", "token", "验证码")


class ExplorePreconditionError(Exception):
    """前置条件不满足：缺启动来源 / 根路径缺失。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _mask_value(step_action: Action, value: str | None) -> str | None:
    """敏感值脱敏：fill 的值疑似口令类字段时只保留长度信息。"""
    if value is None or step_action is not Action.fill:
        return value
    lowered = value.lower()
    if any(hint in lowered for hint in _SENSITIVE_HINTS):
        return f"***（{len(value)} 字符，疑似敏感字段，已脱敏）"
    return value


class ExploreService:
    def __init__(
        self,
        repository: ExploreRepository,
        runner: ExploreWorkerRunner,
        settings: Settings,
        llm: LLMClient | None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._settings = settings
        self._llm = llm
        self._background: set[asyncio.Task[None]] = set()

    async def list_tasks(self, project_id: str) -> list[ExploreTask]:
        return await self._repository.list_by_project(project_id)

    async def get_task(self, task_id: str) -> ExploreTask | None:
        return await self._repository.get_task(task_id)

    async def get_artifact(self, task_id: str) -> ExploreArtifact | None:
        stored = await self._repository.get_artifact(task_id)
        if stored is None:
            return None
        return ExploreArtifact.model_validate(stored)

    async def get_artifacts_dir(self, task_id: str) -> str | None:
        return await self._repository.get_artifacts_dir(task_id)

    async def start(self, project: Project, payload: ExploreStart) -> ExploreTask:
        if payload.url is None and payload.command is None:
            raise ExplorePreconditionError(
                "missing_launch_source",
                "必须提供 url（访问已在运行的页面）或 command（托管启动 dev server）之一。",
            )
        if payload.command is not None and not Path(project.root_path).is_dir():
            raise ExplorePreconditionError(
                "root_path_missing",
                f"项目根路径不存在：{project.root_path}",
            )

        task = ExploreTask(
            id=f"exp_{uuid4().hex[:12]}",
            project_id=project.id,
            goal=payload.goal,
            status="queued",
            command=payload.command,
            url=payload.url,
            analysis_id=payload.analysis_id,
            dry_run=payload.dry_run,
            max_steps=payload.max_steps,
            created_at=datetime.now(UTC),
        )
        await self._repository.add_task(task)
        background = asyncio.create_task(self._run(task, project))
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return task

    def _artifacts_dir(self, task_id: str) -> Path:
        return self._settings.explore_artifacts_dir / task_id

    async def _run(self, task: ExploreTask, project: Project) -> None:
        task.status = "running"
        task.started_at = datetime.now(UTC)
        await self._repository.update_task(task)
        artifacts_dir = self._artifacts_dir(task.id)
        try:
            # 第 1 段：discovery —— 枚举页面真实元素
            discovered = await self._runner.discover(
                DiscoverWorkerRequest(
                    project_id=task.project_id,
                    root_path=project.root_path,
                    url=task.url,
                    command=task.command,
                    analysis_id=task.analysis_id,
                    settle_ms=self._settings.runtime_settle_ms,
                    max_elements=self._settings.explore_max_elements,
                ),
                timeout_seconds=self._settings.explore_worker_timeout_seconds,
            )
            if not discovered.ok:
                task.status = "failed"
                task.finished_at = datetime.now(UTC)
                task.error_code = discovered.error_code
                task.error_message = discovered.error_message
                await self._repository.update_task(task)
                return

            elements = discovered.elements
            task.element_count = len(elements)
            await self._repository.update_task(task)

            # 第 2 段：plan —— 模型预生成计划（校验目标真实性），不可用退兜底
            max_steps = task.max_steps or DEFAULT_MAX_STEPS
            source: PlanSource = await generate_plan(
                self._llm, task.goal, elements, max_steps
            )
            plan = source.plan
            warnings = list(source.warnings)
            if plan.steps and not task.dry_run:
                # 执行前防线：targetIndex 必须命中 discovery
                for step in plan.steps:
                    resolve_step_selector(step, elements)

            # 第 3 段：execute —— dry-run 只交计划；否则确定性执行
            results: list[ExploreStepResult] = []
            screenshots: list[Screenshot] = []
            page_url = discovered.page_url or ""
            title = discovered.title
            if task.dry_run:
                warnings.append(
                    ExploreWarning(
                        code="dry_run",
                        severity=Severity.info,
                        message="dry-run：仅生成计划与脚本草稿，未在浏览器中执行交互",
                    )
                )
            else:
                step_payloads: list[ExploreStepPayload] = []
                for step in plan.steps:
                    selector = resolve_step_selector(step, elements)
                    step_payloads.append(
                        ExploreStepPayload(
                            index=step.index,
                            action=step.action,
                            selector=selector,
                            value=step.value,
                            assertion=step.assertion,
                            optional=bool(step.optional),
                        )
                    )
                executed = await self._runner.explore(
                    ExploreWorkerRequest(
                        project_id=task.project_id,
                        root_path=project.root_path,
                        task_id=task.id,
                        steps=step_payloads,
                        artifacts_dir=str(artifacts_dir),
                        url=task.url,
                        command=task.command,
                        analysis_id=task.analysis_id,
                    ),
                    timeout_seconds=self._settings.explore_worker_timeout_seconds,
                )
                if not executed.ok:
                    task.status = "failed"
                    task.finished_at = datetime.now(UTC)
                    task.error_code = executed.error_code
                    task.error_message = executed.error_message
                    await self._repository.update_task(task)
                    return
                results = executed.results
                screenshots = executed.screenshots
                page_url = executed.page_url or page_url
                title = executed.title or title
                task.success_count = sum(1 for r in results if r.result == "success")
                task.failed_count = sum(1 for r in results if r.result == "failed")

            script = build_script(plan, page_url, _selector_map(plan, elements), plan.testCases)
            task.step_count = len(plan.steps)
            task.screenshot_count = len(screenshots)

            artifact = ExploreArtifact(
                schemaVersion="1.0",
                taskId=task.id,
                projectId=task.project_id,
                analysisId=task.analysis_id,
                goal=task.goal,
                pageUrl=page_url,
                title=title,
                dryRun=task.dry_run,
                discovery=elements,
                plan=plan,
                results=results,
                scenario=plan.scenario,
                testCases=plan.testCases,
                script=script,
                screenshots=screenshots,
                warnings=warnings,
            )
            await self._repository.save_artifact(
                artifact.model_dump(mode="json", exclude_none=True), str(artifacts_dir)
            )
            task.status = "succeeded"
            task.finished_at = datetime.now(UTC)
            await self._repository.update_task(task)
        except Exception as error:  # noqa: BLE001  # 后台任务兜底：任何异常都要落成可见的 failed
            logger.exception("探索任务 %s 执行异常", task.id)
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
            task.error_code = "explore_internal_error"
            task.error_message = str(error)
            await self._repository.update_task(task)


def _selector_map(
    plan: ExplorePlan, elements: list[DiscoveredElement]
) -> dict[int, Selector]:
    mapping: dict[int, Selector] = {}
    for step in plan.steps:
        if step.targetIndex is None:
            continue
        element = next((el for el in elements if el.index == step.targetIndex), None)
        if element is not None:
            mapping[step.index] = element.selector
    return mapping

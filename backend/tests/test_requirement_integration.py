"""真实模型集成测试：APP_LLM_INTEGRATION=1 门控，默认跳过。

运行方式：
  APP_LLM_INTEGRATION=1
  APP_LLM_ENABLED=true
  APP_LLM_API_KEY=sk-...
  APP_LLM_BASE_URL=https://api.openai.com/v1   # 或任意 OpenAI 兼容端点
  APP_LLM_MODEL=gpt-4o-mini
  uv run pytest tests/test_requirement_integration.py

断言只锁契约不变量（候选内引用、置信度封顶、dropped 可见），不锁模型的
具体结论——真实输出的分布不该成为测试的依赖。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.requirement.llm import build_llm_client
from app.requirement.service import RequirementLocateService
from app.schemas import Project, RequirementLocateStart
from app.storage.memory import InMemoryAnalysisRepository, InMemoryRequirementRepository
from tests.graph_fixture import user_graph

pytestmark = pytest.mark.skipif(
    not os.environ.get("APP_LLM_INTEGRATION"),
    reason="真实模型集成测试需设置 APP_LLM_INTEGRATION=1 与 APP_LLM_API_KEY",
)


def _llm_settings() -> Settings:
    settings = Settings(_env_file=None)
    assert settings.llm_api_key, "APP_LLM_INTEGRATION=1 时必须提供 APP_LLM_API_KEY"
    assert settings.llm_enabled, "APP_LLM_INTEGRATION=1 时必须设置 APP_LLM_ENABLED=true"
    return settings


async def test_real_model_judge_produces_valid_map() -> None:
    settings = _llm_settings()
    analyses = InMemoryAnalysisRepository()
    requirements = InMemoryRequirementRepository()
    project = Project(
        id="prj_llm_it00001",
        name="集成测试",
        root_path="D:/nonexistent",
        framework="react",
        created_at=datetime.now(UTC),
    )
    await analyses.save_graph(project.id, user_graph("an_llm_it0001"))
    service = RequirementLocateService(
        requirements, analyses, settings, llm=build_llm_client(settings)
    )

    task = await service.start(
        project,
        RequirementLocateStart(requirement="用户列表增加状态筛选", dry_run=False),
    )
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        task = await service.get_task(task.id)
        assert task is not None
        if task.status in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.5)
    assert task.status == "succeeded", f"真实模型任务不应失败：{task.error_message}"

    requirement_map = await service.get_map_by_task(task.id)
    assert requirement_map is not None
    assert requirement_map.dryRun is False
    candidate_ids = {candidate.nodeId for candidate in requirement_map.candidates or []}

    for match in requirement_map.matches:
        # 契约不变量：候选内引用 + 置信度封顶 + AI 来源
        assert match.pageNodeId in candidate_ids
        assert match.confidence <= 0.6
        assert str(match.source) == "ai"
        if match.quote:
            detail = next(
                (
                    item
                    for item in (requirement_map.candidates or [])
                    if item.nodeId == match.pageNodeId
                ),
                None,
            )
            assert detail is not None
    # 引文逐字校验已在 judge 层做掉，这里只复核 dropped 不吞结论
    for dropped in requirement_map.dropped or []:
        assert dropped.reason in (
            "node_not_in_candidates",
            "quote_not_found",
            "unknown_feature",
            "invalid_shape",
        )

"""规格文档装配器（specdoc）。

输入一段已定位的需求（RequirementMap）+ IR 图，输出「业务操作步骤 +
前端交互视图」的结构化规格（SpecDocMap）与 Markdown 文档：
  - 确定性装配（零模型）：页面要素清单 / 事件清单 / 接口映射
  - LLM 推断（引文逐字校验、置信度封顶 0.6）：操作步骤 / 业务规则 / 字段映射
  - dry-run：只跑确定性部分
"""

from app.specdoc.orchestrator import SCHEMA_VERSION, SpecDocOrchestrator
from app.specdoc.service import SpecDocPreconditionError, SpecDocService

__all__ = [
    "SCHEMA_VERSION",
    "SpecDocOrchestrator",
    "SpecDocPreconditionError",
    "SpecDocService",
]

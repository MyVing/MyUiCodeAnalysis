"""跨语言契约的 Python 侧生成产物。

本包内所有模块由 shared/scripts/generate_contracts.py 从 shared/schemas/*.schema.json
生成，禁止手改。需要改动时修改 schema 后重新运行：

    python shared/scripts/generate_contracts.py

CI 或提交前用 --check 校验是否漂移。
"""

from app.contracts.chain import (
    ActionChain,
    ActionChainList,
    ChainPath,
    ChainRole,
    ChainStep,
)
from app.contracts.ir import (
    EdgeKind,
    Evidence,
    Framework,
    GraphMeta,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    NodeKind,
    Severity,
    Source,
    Stage,
    Warning,
)
from app.contracts.mapping import WrapperMapping, WrapperRule
from app.contracts.requirement import (
    Candidate,
    Dropped,
    Feature,
    Match,
    Reason,
    RequirementMap,
    RequirementWarning,
    Signal,
    SignalKind,
    Subgraph,
    SubgraphCounts,
    Unmatched,
    VerifiedEvidence,
)
from app.contracts.runtime import (
    ComponentNode,
    DevServer,
    PageInfo,
    RuntimeTrace,
)

__all__ = [
    "Candidate",
    "ComponentNode",
    "DevServer",
    "Dropped",
    "EdgeKind",
    "Evidence",
    "Feature",
    "Framework",
    "GraphMeta",
    "IREdge",
    "IRGraph",
    "IRNode",
    "Location",
    "Match",
    "NodeKind",
    "PageInfo",
    "Reason",
    "RequirementMap",
    "RequirementWarning",
    "RuntimeTrace",
    "Severity",
    "Signal",
    "SignalKind",
    "Source",
    "Stage",
    "Subgraph",
    "SubgraphCounts",
    "Unmatched",
    "VerifiedEvidence",
    "Warning",
    "WrapperMapping",
    "WrapperRule",
]

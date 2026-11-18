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
from app.contracts.explore import (
    Action,
    DiscoveredElement,
    ExploreArtifact,
    ExplorePlan,
    ExploreStepResult,
    ExploreWarning,
    GeneratedScript,
    PlanStep,
    Priority,
    Result,
    Scenario,
    Screenshot,
    Selector,
    Strategy,
    TestCase,
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
    "Action",
    "Candidate",
    "ComponentNode",
    "DevServer",
    "DiscoveredElement",
    "Dropped",
    "EdgeKind",
    "Evidence",
    "ExploreArtifact",
    "ExplorePlan",
    "ExploreStepResult",
    "ExploreWarning",
    "Feature",
    "Framework",
    "GeneratedScript",
    "GraphMeta",
    "IREdge",
    "IRGraph",
    "IRNode",
    "Location",
    "Match",
    "NodeKind",
    "PageInfo",
    "PlanStep",
    "Priority",
    "Reason",
    "RequirementMap",
    "RequirementWarning",
    "Result",
    "RuntimeTrace",
    "Scenario",
    "Screenshot",
    "Selector",
    "Severity",
    "Signal",
    "SignalKind",
    "Source",
    "Stage",
    "Strategy",
    "Subgraph",
    "SubgraphCounts",
    "TestCase",
    "Unmatched",
    "VerifiedEvidence",
    "Warning",
    "WrapperMapping",
    "WrapperRule",
]

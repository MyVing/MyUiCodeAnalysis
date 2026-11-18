"""按需求采集：路由反查的确定性口径 + 编排服务的前置校验与任务创建。"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.contracts import (
    EdgeKind,
    Evidence,
    IREdge,
    IRGraph,
    IRNode,
    Location,
    Match,
    NodeKind,
    RequirementMap,
    Source,
    Subgraph,
    SubgraphCounts,
)
from app.runtime.requirement_collect import resolve_route_urls
from app.schemas import RequirementTask


def _evidence() -> list[Evidence]:
    return [
        Evidence(
            source=Source.static,
            confidence=1.0,
            file="src/x.tsx",
            loc=Location(line=1, col=1),
        )
    ]


def _node(node_id: str, kind: NodeKind, attrs: dict | None = None) -> IRNode:
    return IRNode(
        id=node_id,
        kind=kind,
        name=node_id.split("#")[-1].split(":")[-1],
        file="src/x.tsx",
        loc=Location(line=1, col=1),
        attrs=attrs or {},
        evidence=_evidence(),
    )


def make_graph() -> IRGraph:
    """route /users → UserList；route /users/:id → UserDetail；Orphan 无路由。"""
    return IRGraph(
        version="1.0",
        analysisId="ana_1",
        nodes=[
            _node("route:/users", NodeKind.route, {"path": "/users"}),
            _node("route:/users/:id", NodeKind.route, {"path": "/users/:id"}),
            _node("src/pages/UserList.tsx#UserList", NodeKind.component),
            _node("src/pages/UserDetail.tsx#UserDetail", NodeKind.component),
            _node("src/pages/Orphan.tsx#Orphan", NodeKind.component),
            _node("src/pages/Alias.tsx#Alias", NodeKind.component),
        ],
        edges=[
            IREdge(
                id="e1",
                **{"from": "route:/users"},
                to="src/pages/UserList.tsx#UserList",
                kind=EdgeKind.renders,
                evidence=_evidence(),
            ),
            IREdge(
                id="e2",
                **{"from": "route:/users/:id"},
                to="src/pages/UserDetail.tsx#UserDetail",
                kind=EdgeKind.renders,
                evidence=_evidence(),
            ),
            IREdge(
                id="e3",
                **{"from": "route:/users"},
                to="src/pages/Alias.tsx#Alias",
                kind=EdgeKind.renders,
                evidence=_evidence(),
            ),
        ],
        warnings=[],
    )


class TestResolveRouteUrls:
    def test_success_and_join(self) -> None:
        plans, skipped = resolve_route_urls(
            ["src/pages/UserList.tsx#UserList"], make_graph(), "http://localhost:5199/"
        )
        assert skipped == []
        assert len(plans) == 1
        assert plans[0].url == "http://localhost:5199/users"
        assert plans[0].route_path == "/users"

    def test_dynamic_route_skipped(self) -> None:
        plans, skipped = resolve_route_urls(
            ["src/pages/UserDetail.tsx#UserDetail"], make_graph(), "http://localhost:5199"
        )
        assert plans == []
        assert skipped[0].reason == "dynamic_route_params"
        assert skipped[0].route_path == "/users/:id"

    def test_no_route_skipped(self) -> None:
        plans, skipped = resolve_route_urls(
            ["src/pages/Orphan.tsx#Orphan"], make_graph(), "http://localhost:5199"
        )
        assert plans == []
        assert skipped[0].reason == "no_route_found"

    def test_ambiguous_routes_skipped(self) -> None:
        # Alias 与 UserList 同被 route:/users 渲染——单候选正常，多候选报歧义
        graph = make_graph()
        graph.edges.append(
            IREdge(
                id="e4",
                **{"from": "route:/users/:id"},
                to="src/pages/Alias.tsx#Alias",
                kind=EdgeKind.renders,
                evidence=_evidence(),
            )
        )
        plans, skipped = resolve_route_urls(["src/pages/Alias.tsx#Alias"], graph, "http://x")
        assert plans == []
        assert skipped[0].reason == "ambiguous_routes"

    def test_duplicate_urls_deduped(self) -> None:
        # 两个页面节点解析出同一个 URL 时只采一次
        graph = make_graph()
        graph.edges.append(
            IREdge(
                id="e5",
                **{"from": "route:/users"},
                to="src/pages/Orphan.tsx#Orphan",
                kind=EdgeKind.renders,
                evidence=_evidence(),
            )
        )
        plans, skipped = resolve_route_urls(
            ["src/pages/UserList.tsx#UserList", "src/pages/Orphan.tsx#Orphan"],
            graph,
            "http://localhost:5199",
        )
        assert skipped == []
        assert [p.url for p in plans] == ["http://localhost:5199/users"]


@pytest.fixture
def explore_project(client: TestClient, tmp_path) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": "按需采集项目", "rootPath": str(tmp_path), "framework": "react"},
    )
    assert response.status_code == 201
    return response.json()


def _seed_locate_result(
    analysis_repository, requirement_repository, project_id: str, matches: list[Match]
) -> RequirementTask:
    task = RequirementTask(
        id="req_task_1",
        project_id=project_id,
        requirement_id="req_1",
        status="succeeded",
        dry_run=False,
        requirement="验证用户列表的搜索交互",
        analysis_id="ana_1",
        created_at=datetime.now(UTC),
    )

    async def seed() -> None:
        await analysis_repository.save_graph(project_id, make_graph())
        await requirement_repository.add_task(task)
        await requirement_repository.save_map(
            RequirementMap(
                schemaVersion="1.0",
                requirementId="req_1",
                projectId=project_id,
                analysisId="ana_1",
                requirement=task.requirement,
                createdAt=datetime.now(UTC),
                dryRun=False,
                features=[],
                matches=matches,
            )
        )
        await requirement_repository.link_task(task.id, "req_1")

    asyncio.run(seed())
    return task


def _subgraph() -> Subgraph:
    return Subgraph(
        nodeIds=[],
        edgeIds=[],
        counts=SubgraphCounts(
            component=0, hook=0, state=0, api=0, event=0, fn=0, route=0, element=0
        ),
    )


def _match(page_node_id: str) -> Match:
    return Match(
        featureId=f"f_{page_node_id}",
        pageNodeId=page_node_id,
        confidence=0.5,
        subgraph=_subgraph(),
        source=Source.ai,
    )


def _matches() -> list[Match]:
    return [
        _match("src/pages/UserList.tsx#UserList"),
        _match("src/pages/UserDetail.tsx#UserDetail"),
        _match("src/pages/Orphan.tsx#Orphan"),
    ]


def test_requirement_collect_by_task_id(
    client: TestClient,
    explore_project: dict,
    analysis_repository,
    requirement_repository,
) -> None:
    _seed_locate_result(
        analysis_repository, requirement_repository, explore_project["id"], _matches()
    )
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"requirementTaskId": "req_task_1", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requirementTaskId"] == "req_task_1"
    # 只有 UserList 解析出可访问 URL；Detail 是动态路由，Orphan 无路由
    assert len(body["items"]) == 1
    assert body["items"][0]["url"] == "http://localhost:5199/users"
    assert body["items"][0]["analysisId"] == "ana_1"
    reasons = {item["reason"] for item in body["skipped"]}
    assert reasons == {"dynamic_route_params", "no_route_found"}


def test_requirement_task_not_found_returns_404(client: TestClient, explore_project: dict) -> None:
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"requirementTaskId": "req_missing", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 404


def test_task_without_map_returns_409(
    client: TestClient,
    explore_project: dict,
    analysis_repository,
    requirement_repository,
) -> None:
    task = RequirementTask(
        id="req_task_2",
        project_id=explore_project["id"],
        requirement_id="req_2",
        status="succeeded",
        dry_run=False,
        requirement="r",
        analysis_id="ana_1",
        created_at=datetime.now(UTC),
    )

    async def seed() -> None:
        await analysis_repository.save_graph(explore_project["id"], make_graph())
        await requirement_repository.add_task(task)

    asyncio.run(seed())
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"requirementTaskId": "req_task_2", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 409


def test_no_matched_pages_returns_409(
    client: TestClient,
    explore_project: dict,
    analysis_repository,
    requirement_repository,
) -> None:
    _seed_locate_result(analysis_repository, requirement_repository, explore_project["id"], [])
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"requirementTaskId": "req_task_1", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 409
    assert "没有定位到任何页面" in response.json()["detail"]


def test_all_routes_unresolvable_returns_409(
    client: TestClient,
    explore_project: dict,
    analysis_repository,
    requirement_repository,
) -> None:
    _seed_locate_result(
        analysis_repository,
        requirement_repository,
        explore_project["id"],
        [_match("src/pages/UserDetail.tsx#UserDetail")],
    )
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"requirementTaskId": "req_task_1", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "no_collectable_routes" in detail or "无法解析" in detail


def test_missing_source_returns_409(client: TestClient, explore_project: dict) -> None:
    response = client.post(
        f"/api/v1/projects/{explore_project['id']}/requirement-collects",
        json={"baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 409
    assert "requirement" in response.json()["detail"]


def test_requirement_text_flow_starts_locate_and_collects(
    client: TestClient,
    explore_project: dict,
    analysis_repository,
    requirement_repository,
) -> None:
    """文本入口：先跑需求定位（stub 立即完成），再按 map 采集。"""
    from app.api.deps import get_requirement_locate_service

    _seed_graph_only(analysis_repository, explore_project["id"])
    project_id = explore_project["id"]

    class StubRequirementService:
        async def start(self, project, payload):
            task = RequirementTask(
                id="req_task_text",
                project_id=project.id,
                requirement_id="req_text",
                status="succeeded",
                dry_run=payload.dry_run,
                requirement=payload.requirement,
                analysis_id="ana_1",
                created_at=datetime.now(UTC),
            )
            await requirement_repository.add_task(task)
            await requirement_repository.save_map(
                RequirementMap(
                    schemaVersion="1.0",
                    requirementId="req_text",
                    projectId=project.id,
                    analysisId="ana_1",
                    requirement=payload.requirement,
                    createdAt=datetime.now(UTC),
                    dryRun=False,
                    features=[],
                    matches=_matches()[:1],  # 文本流程只命中一个页面
                )
            )
            await requirement_repository.link_task(task.id, "req_text")
            return task

    app = client.app
    app.dependency_overrides[get_requirement_locate_service] = lambda: StubRequirementService()

    response = client.post(
        f"/api/v1/projects/{project_id}/requirement-collects",
        json={"requirement": "验证用户列表的搜索交互", "baseUrl": "http://localhost:5199"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requirementTaskId"] == "req_task_text"
    assert len(body["items"]) == 1
    assert body["items"][0]["url"] == "http://localhost:5199/users"


def _seed_graph_only(analysis_repository, project_id: str) -> None:
    asyncio.run(analysis_repository.save_graph(project_id, make_graph()))

"""AI 需求助手会话 API 测试：保存/回放/删除与每项目 10 条上限淘汰。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _save(
    client: TestClient,
    project_id: str,
    messages: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    title: str | None = None,
    draft: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": messages}
    if session_id is not None:
        payload["id"] = session_id
    if title is not None:
        payload["title"] = title
    if draft is not None:
        payload["draft"] = draft
    response = client.post(f"/api/v1/projects/{project_id}/chat-sessions", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _messages(text: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "收到"},
    ]


def test_save_returns_session_with_derived_title(
    client: TestClient, project: dict[str, object]
) -> None:
    body = _save(client, str(project["id"]), _messages("用户列表页加一个状态筛选"))
    assert body["id"].startswith("chat_")
    # 标题缺省取首条用户消息截断
    assert body["title"] == "用户列表页加一个状态筛选"
    assert body["projectId"] == project["id"]
    assert body["draft"] is None
    assert [item["role"] for item in body["messages"]] == ["user", "assistant"]
    assert body["createdAt"] and body["updatedAt"]


def test_save_with_explicit_title_and_draft_round_trips(
    client: TestClient, project: dict[str, object]
) -> None:
    messages = [
        {"role": "user", "content": "转账功能怎么做的"},
        {
            "role": "assistant",
            "content": "已定位到页面《转账》",
            "docMarkdown": "## 业务操作步骤\n1. 选择付款账户",
            "docPage": "转账",
        },
    ]
    body = _save(
        client,
        str(project["id"]),
        messages,
        title="转账交互视图",
        draft="在转账页增加收款人校验",
    )
    assert body["title"] == "转账交互视图"
    assert body["draft"] == "在转账页增加收款人校验"
    assert body["messages"][1]["docMarkdown"] == "## 业务操作步骤\n1. 选择付款账户"
    assert body["messages"][1]["docPage"] == "转账"

    detail = client.get(f"/api/v1/chat-sessions/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == body["messages"]


def test_upsert_with_id_replaces_messages_and_keeps_created_at(
    client: TestClient, project: dict[str, object]
) -> None:
    first = _save(client, str(project["id"]), _messages("第一轮"))
    session_id = first["id"]
    created_at = first["createdAt"]

    grown = _messages("第一轮") + _messages("第二轮补充")
    second = _save(client, str(project["id"]), grown, session_id=session_id)
    assert second["id"] == session_id
    assert second["createdAt"] == created_at
    assert len(second["messages"]) == 4

    # 未指定 title 时标题缺省仍来自首条用户消息
    assert second["title"] == "第一轮"


def test_list_returns_summaries_without_messages(
    client: TestClient, project: dict[str, object]
) -> None:
    _save(client, str(project["id"]), _messages("查询余额的前端交互"))
    _save(client, str(project["id"]), _messages("登录页加记住我"))
    response = client.get(f"/api/v1/projects/{project['id']}/chat-sessions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert all("messages" not in item for item in items)
    assert items[0]["messageCount"] == 2
    # 最近写入在前
    assert items[0]["title"] == "登录页加记住我"


def test_delete_session_then_get_returns_404(
    client: TestClient, project: dict[str, object]
) -> None:
    body = _save(client, str(project["id"]), _messages("会删除的会话"))
    response = client.delete(f"/api/v1/chat-sessions/{body['id']}")
    assert response.status_code == 204

    assert client.get(f"/api/v1/chat-sessions/{body['id']}").status_code == 404
    assert client.delete(f"/api/v1/chat-sessions/{body['id']}").status_code == 404


def test_eviction_keeps_ten_most_recent_sessions(
    client: TestClient, project: dict[str, object]
) -> None:
    project_id = str(project["id"])
    saved_ids = []
    for index in range(11):
        body = _save(client, project_id, _messages(f"需求 {index}"))
        saved_ids.append(body["id"])

    items = client.get(f"/api/v1/projects/{project_id}/chat-sessions").json()["items"]
    assert len(items) == 10
    # 最旧的（第 0 个）被淘汰，最新的仍在
    assert saved_ids[0] not in {item["id"] for item in items}
    assert saved_ids[10] == items[0]["id"]
    assert client.get(f"/api/v1/chat-sessions/{saved_ids[0]}").status_code == 404


def test_sessions_are_isolated_by_project(
    client: TestClient, project: dict[str, object]
) -> None:
    other = client.post(
        "/api/v1/projects",
        json={"name": "站点", "rootPath": "D:/work/site", "framework": "react"},
    )
    assert other.status_code == 201
    other_id = other.json()["id"]

    _save(client, str(project["id"]), _messages("本项目会话"))
    response = client.get(f"/api/v1/projects/{other_id}/chat-sessions")
    assert response.json()["items"] == []


def test_save_rejects_unknown_project(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects/p_missing/chat-sessions",
        json={"messages": _messages("hi")},
    )
    assert response.status_code == 404


def test_save_rejects_empty_messages(
    client: TestClient, project: dict[str, object]
) -> None:
    response = client.post(
        f"/api/v1/projects/{project['id']}/chat-sessions",
        json={"messages": []},
    )
    assert response.status_code == 422


def test_save_with_foreign_id_creates_new_session(
    client: TestClient, project: dict[str, object]
) -> None:
    """被淘汰后前端仍持有的旧 id：不跨项目覆盖，按新建处理。"""
    project_id = str(project["id"])
    first = _save(client, project_id, _messages("旧会话"))
    # 直接删掉模拟淘汰，再以同 id 回传
    client.delete(f"/api/v1/chat-sessions/{first['id']}")

    body = _save(client, project_id, _messages("新会话"), session_id=first["id"])
    assert body["messages"][0]["content"] == "新会话"
    assert body["title"] == "新会话"

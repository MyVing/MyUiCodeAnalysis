"""占位端点测试：M1 后仅剩 plugin 更新仍是显式 501。"""


def test_plugin_list_declares_subprocess_isolation(client, project) -> None:
    response = client.get(f"/api/v1/projects/{project['id']}/plugins")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["isolation"] == "subprocess"
    assert "子进程" in body["note"]


def test_plugin_update_is_explicitly_not_implemented(client, project) -> None:
    response = client.patch(f"/api/v1/projects/{project['id']}/plugins")

    assert response.status_code == 501

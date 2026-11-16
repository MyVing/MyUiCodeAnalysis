def test_project_crud_uses_camel_case_contract(client) -> None:
    assert client.get("/api/v1/projects").json() == []

    created = client.post(
        "/api/v1/projects",
        json={"name": "  订单平台  ", "rootPath": "D:/work/orders", "framework": "next"},
    )
    assert created.status_code == 201

    project = created.json()
    assert project["name"] == "订单平台"
    assert project["rootPath"] == "D:/work/orders"
    assert project["framework"] == "next"
    assert project["storage"] == "memory"
    assert project["id"].startswith("prj_")
    assert project["createdAt"]

    listed = client.get("/api/v1/projects").json()
    assert [item["id"] for item in listed] == [project["id"]]

    fetched = client.get(f"/api/v1/projects/{project['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == project


def test_unknown_project_returns_404(client) -> None:
    response = client.get("/api/v1/projects/prj_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "项目不存在"


def test_blank_project_name_is_rejected(client) -> None:
    response = client.post(
        "/api/v1/projects",
        json={"name": "   ", "rootPath": "D:/work/orders", "framework": "react"},
    )

    assert response.status_code == 422

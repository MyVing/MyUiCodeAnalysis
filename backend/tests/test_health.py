def test_health_reports_memory_storage(client) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["storage"] == "memory"
    assert body["serverTime"]

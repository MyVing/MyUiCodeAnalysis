"""Git 接入：地址归一化去重、克隆失败可见、更新对齐远端。"""

import pytest
from fastapi.testclient import TestClient

from app.git_client import inject_credentials, normalize_remote_url, repo_directory_name
from tests.fakes import FakeGitClient


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/Acme/App.git",
        "https://github.com/acme/app/",
        "git@github.com:acme/app.git",
        "ssh://git@github.com/acme/app.git",
        "https://oauth2:token@github.com/acme/app",
    ],
)
def test_equivalent_remote_urls_normalize_to_one_key(raw: str) -> None:
    assert normalize_remote_url(raw) == "github.com/acme/app"


def test_repo_directory_is_stable_and_readable() -> None:
    first = repo_directory_name("github.com/acme/app")
    second = repo_directory_name("github.com/acme/app")
    assert first == second
    assert first.startswith("app-")


def test_unrecognized_git_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_remote_url("not a url")
    with pytest.raises(ValueError):
        normalize_remote_url("https://github.com")


def test_create_git_project_clones_and_detects_metadata(
    client: TestClient, fake_git: FakeGitClient, tmp_path
) -> None:
    response = client.post(
        "/api/v1/projects/git",
        json={"gitUrl": " https://github.com/Acme/App.git ", "token": "secret"},
    )

    assert response.status_code == 201
    project = response.json()
    assert project["source"] == "git"
    assert project["remoteUrl"] == "github.com/acme/app"
    assert project["name"] == "app"
    assert project["gitBranch"] == "main"
    assert project["commitSha"] == "a" * 40
    assert project["framework"] == "react"
    assert project["lastSyncedAt"]
    assert "secret" not in response.text

    assert len(fake_git.clone_calls) == 1
    raw_url, destination, _, username, password, token = fake_git.clone_calls[0]
    assert raw_url == "https://github.com/Acme/App.git"
    assert username is None
    assert password is None
    assert token == "secret"
    assert destination.parent == tmp_path / "repos"
    assert project["rootPath"] == str(destination)


def test_create_git_project_passes_basic_auth(
    client: TestClient, fake_git: FakeGitClient
) -> None:
    response = client.post(
        "/api/v1/projects/git",
        json={
            "gitUrl": "https://github.com/acme/private",
            "username": "  me  ",
            "password": "p@ss word",
        },
    )

    assert response.status_code == 201
    assert "p@ss word" not in response.text

    _, _, _, username, password, token = fake_git.clone_calls[0]
    assert username == "me"
    # 密码不做 strip：首尾空格可能属于凭据本身
    assert password == "p@ss word"
    assert token is None


def test_inject_credentials_encodes_special_characters() -> None:
    url = inject_credentials(
        "https://github.com/acme/app.git", username="me@corp.com", password="p@ss:/word"
    )

    assert url == "https://me%40corp.com:p%40ss%3A%2Fword@github.com/acme/app.git"


def test_token_is_the_fallback_when_password_is_absent() -> None:
    url = inject_credentials("https://ghe.example.com/acme/app", token="abc/def")

    assert url == "https://oauth2:abc%2Fdef@ghe.example.com/acme/app"


def test_ssh_urls_keep_their_native_auth() -> None:
    url = "git@github.com:acme/app.git"

    assert inject_credentials(url, username="me", password="secret") == url
    assert inject_credentials(url, token="secret") == url
    assert inject_credentials("https://github.com/acme/app") == "https://github.com/acme/app"


def test_same_remote_can_only_be_onboarded_once(
    client: TestClient, fake_git: FakeGitClient
) -> None:
    first = client.post(
        "/api/v1/projects/git", json={"gitUrl": "https://github.com/acme/app.git"}
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/projects/git", json={"gitUrl": "git@github.com:acme/app.git"}
    )

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "git_project_exists"
    assert detail["projectId"] == first.json()["id"]
    assert len(fake_git.clone_calls) == 1
    assert len(client.get("/api/v1/projects").json()) == 1


def test_clone_failure_is_visible_and_leaves_no_project(
    client: TestClient, fake_git: FakeGitClient, tmp_path
) -> None:
    fake_git.error = "repository not found"

    response = client.post(
        "/api/v1/projects/git", json={"gitUrl": "https://github.com/acme/missing"}
    )

    assert response.status_code == 422
    assert "repository not found" in response.json()["detail"]
    assert client.get("/api/v1/projects").json() == []
    assert list((tmp_path / "repos").glob("*")) == []


def test_delete_git_project_reclaims_managed_directory(
    client: TestClient, fake_git: FakeGitClient
) -> None:
    created = client.post(
        "/api/v1/projects/git", json={"gitUrl": "https://github.com/acme/app"}
    ).json()
    destination = fake_git.clone_calls[0][1]
    assert destination.is_dir()

    deleted = client.delete(f"/api/v1/projects/{created['id']}")
    assert deleted.status_code == 204
    # 受管克隆目录由平台独占，删除项目时一并回收
    assert not destination.exists()

    # 去重键随项目删除而释放：同一仓库可以重新接入
    re_created = client.post(
        "/api/v1/projects/git", json={"gitUrl": "https://github.com/acme/app.git"}
    )
    assert re_created.status_code == 201
    assert len(fake_git.clone_calls) == 2


def test_sync_pulls_latest_commit(client: TestClient, fake_git: FakeGitClient) -> None:
    created = client.post(
        "/api/v1/projects/git", json={"gitUrl": "https://github.com/acme/app"}
    ).json()

    response = client.post(f"/api/v1/projects/{created['id']}/sync")

    assert response.status_code == 200
    synced = response.json()
    assert synced["commitSha"] == "b" * 40
    assert synced["gitBranch"] == "main"
    assert len(fake_git.sync_calls) == 1
    assert str(fake_git.sync_calls[0]) == created["rootPath"]


def test_local_project_cannot_be_synced(client: TestClient, project: dict[str, object]) -> None:
    response = client.post(f"/api/v1/projects/{project['id']}/sync")

    assert response.status_code == 409
    assert response.json()["detail"] == "只有 Git 接入的项目支持从远端更新"

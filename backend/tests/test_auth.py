"""W1 认证模块测试：健康探针 / 注册 / 登录 / me / 鉴权错误路径。"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "Insight AI"


def test_register_success(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/register", json={"username": "alice", "password": "password123"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert body["id"] > 0
    assert "created_at" in body
    # 绝不泄露密码哈希
    assert "hashed_password" not in body
    assert "password" not in body


def test_register_duplicate_conflict(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json={"username": "bob", "password": "password123"})
    resp = client.post("/api/v1/auth/register", json={"username": "bob", "password": "password123"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "用户名已存在"


def test_register_validation_error(client: TestClient) -> None:
    # 密码过短
    resp = client.post("/api/v1/auth/register", json={"username": "carol", "password": "short"})
    assert resp.status_code == 422
    # 非法用户名
    resp = client.post("/api/v1/auth/register", json={"username": "bad name!", "password": "password123"})
    assert resp.status_code == 422


def test_login_success_returns_token_and_user(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json={"username": "dave", "password": "password123"})
    resp = client.post("/api/v1/auth/login", data={"username": "dave", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["username"] == "dave"


def test_login_wrong_password_401(client: TestClient) -> None:
    client.post("/api/v1/auth/register", json={"username": "erin", "password": "password123"})
    resp = client.post("/api/v1/auth/login", data={"username": "erin", "password": "wrong-password"})
    assert resp.status_code == 401


def test_login_unknown_user_401(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", data={"username": "ghost", "password": "password123"})
    assert resp.status_code == 401


def test_me_with_token(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == "tester"


def test_me_without_token_401(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_invalid_token_401(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.valid.token"})
    assert resp.status_code == 401


def test_full_flow_register_login_me(client: TestClient) -> None:
    """端到端：注册 → 登录 → 携带 token 访问受保护接口。"""
    client.post("/api/v1/auth/register", json={"username": "frank", "password": "password123"})
    login = client.post("/api/v1/auth/login", data={"username": "frank", "password": "password123"})
    token = login.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "frank"
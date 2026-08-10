"""pytest 全局夹具：独立 SQLite 测试库 + FastAPI TestClient。

注意：必须在导入 app 之前设置 DATABASE_URL（模块导入顺序敏感），
因此这里在模块顶层先设置环境变量再构造 client。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# ---- 在导入 app 前注入测试库路径（临时文件，避免污染开发库）----
_tmp_db = Path(tempfile.mkdtemp(prefix="insightai-test-")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.as_posix()}"


from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c
    # 会话结束：先释放 SQLAlchemy 连接池，再清理临时库（Windows 文件锁）
    from app.db.session import engine

    engine.dispose()
    _tmp_db.unlink(missing_ok=True)


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    """注册并登录一个测试用户，返回 Bearer 请求头。"""
    client.post("/api/v1/auth/register", json={"username": "tester", "password": "password123"})
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": "tester", "password": "password123"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
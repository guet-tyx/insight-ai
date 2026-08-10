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


@pytest.fixture()
def sample_pdf_bytes() -> bytes:
    """现场生成含 H1/H2 标题树的中文测试 PDF（china-s 内置中文字体）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        ("Insight AI 用户手册", 22),      # H1
        ("第一章 系统架构", 16),           # H2
        ("本系统基于 LangGraph 多智能体架构，包含 Supervisor 与多个专家子图。", 11),
        ("Supervisor Agent 负责任务拆解与路由分发。", 11),
        ("第二章 知识库 Pipeline", 16),    # H2
        ("文档经过版面感知解析后向量化写入 Milvus 数据库。", 11),
        ("嵌入模型采用 BAAI/bge-m3，维度为 1024。", 11),
        ("第三章 检索问答", 16),           # H2
        ("使用余弦相似度进行 Top-K 召回，由大模型生成引证式回答。", 11),
    ]
    y = 60
    for text, size in lines:
        page.insert_text((60, y), text, fontsize=size, fontname="china-s")
        y += size + 14
    data = doc.tobytes()
    doc.close()
    return data


# ---- 基础设施探测：Milvus / 嵌入 Key / LLM Key 齐备才跑集成测试 ----
def _milvus_up() -> bool:
    try:
        from app.services.ingest_service import get_milvus_client

        get_milvus_client().list_collections()
        return True
    except Exception:  # noqa: BLE001
        return False


from app.core.config import settings  # noqa: E402

MILVUS_UP = _milvus_up()
HAS_EMBED_KEY = bool(settings.siliconflow_api_key)
HAS_LLM_KEY = bool(settings.openai_api_key)
INFRA_READY = MILVUS_UP and HAS_EMBED_KEY and HAS_LLM_KEY

# 供测试模块使用：pytestmark = pytest.mark.skipif(not INFRA_READY, ...)


# ---- 浏览器采集集成测试探测：LLM Key + Chromium 可启动 ----
def _browser_ready() -> bool:
    if not HAS_LLM_KEY:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


BROWSER_READY = _browser_ready()


@pytest.fixture()
def local_test_page() -> str:
    """线程内启动本地测试页（http.server），返回可直接采集的 URL。

    页面包含标题 / h1 / 无序列表 / 表格，供自然语言采集指令验证。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Insight AI 测试采集页</title></head>
<body>
<h1>Insight AI 开源情报分析平台</h1>
<ul>
<li>LangGraph 多智能体编排</li>
<li>Milvus 向量检索</li>
<li>Neo4j 图查询</li>
</ul>
<table><tr><th>模型</th><th>维度</th></tr><tr><td>bge-m3</td><td>1024</td></tr></table>
<p>本项目基于 MCP 架构实现工具服务化。</p>
</body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))

        def log_message(self, *args):  # 静默日志
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    yield url
    server.shutdown()
    server.server_close()
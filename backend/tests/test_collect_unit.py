"""采集模块单元测试：单例 / 代理池轮换 / URL 与内网拦截 / Schema 转换。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.browser_agent import BrowserSessionManager
from app.services.collector_service import schema_to_pydantic, validate_url


# ---------- 单例 ----------

def test_singleton_returns_same_instance() -> None:
    a = BrowserSessionManager()
    b = BrowserSessionManager()
    assert a is b


def test_singleton_shared_across_imports() -> None:
    from app.core.browser_agent import session_manager

    assert BrowserSessionManager() is session_manager


# ---------- 代理池轮换 ----------

def test_proxy_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "browser_proxy_list", "http://p1:8080,http://p2:8080")
    mgr = BrowserSessionManager()
    mgr._proxy_index = 0  # noqa: SLF001 — 单测对齐轮换起点
    assert mgr._next_proxy() == "http://p1:8080"  # noqa: SLF001
    assert mgr._next_proxy() == "http://p2:8080"  # noqa: SLF001
    assert mgr._next_proxy() == "http://p1:8080"  # noqa: SLF001 — 回到池头


def test_proxy_rotation_empty_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "browser_proxy_list", "")
    assert BrowserSessionManager()._next_proxy() is None  # noqa: SLF001 — 直连


# ---------- URL 校验与内网拦截（SSRF 防护） ----------

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/article",
        "http://docs.siliconflow.cn/api",
        "https://sub.domain.io:8443/path?q=1",
    ],
)
def test_validate_url_allows_public(url: str) -> None:
    ok, err = validate_url(url)
    assert ok, f"公共 URL 应放行: {err}"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1/x", "内网"),
        ("http://localhost:8000/", "内网"),
        ("http://10.0.0.5/", "内网"),
        ("http://172.16.3.4/", "内网"),
        ("http://192.168.1.100/", "内网"),
        ("http://169.254.169.254/latest/meta-data", "内网"),  # 云元数据 SSRF 经典路径
        ("ftp://example.com/file", "协议"),
        ("file:///etc/passwd", "协议"),
        ("not a url", "协议"),
    ],
)
def test_validate_url_blocks_internal(url: str, expected: str) -> None:
    ok, err = validate_url(url)
    assert not ok
    assert expected in err


def test_validate_url_allow_internal_override() -> None:
    ok, _ = validate_url("http://127.0.0.1:8080/", allow_internal=True)
    assert ok


# ---------- JSON Schema → Pydantic ----------

def test_schema_to_pydantic_basic() -> None:
    model = schema_to_pydantic(
        "CollectOutput",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "count": {"type": "integer"},
                "price": {"type": "number"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    )
    instance = model(title="情报摘要", count=3, price=9.9, tags=["a", "b"])
    assert instance.model_dump() == {"title": "情报摘要", "count": 3, "price": 9.9, "tags": ["a", "b"]}
    # 非 required 字段缺省为 None
    assert model(title="x").count is None


def test_schema_to_pydantic_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="type"):
        schema_to_pydantic("M", {"properties": {}})
    with pytest.raises(ValueError, match="properties"):
        schema_to_pydantic("M", {"type": "object"})
    with pytest.raises(ValueError, match="properties"):
        schema_to_pydantic("M", {"type": "object", "properties": {}})


def test_schema_type_validation() -> None:
    model = schema_to_pydantic("M", {
        "type": "object",
        "properties": {"age": {"type": "integer"}},
    })
    with pytest.raises(ValidationError):
        model(age="not-a-number")


# ---------- W6：连接类失败自动换代理重试 ----------

def test_connection_error_detection() -> None:
    from app.core.browser_agent import _is_connection_error

    class _Fake:
        pass

    assert _is_connection_error(ConnectionError("refused"))
    assert _is_connection_error(TimeoutError("timeout"))
    assert _is_connection_error(ValueError("Failed to connect to proxy"))
    assert _is_connection_error(RuntimeError("dns resolution failed"))
    assert not _is_connection_error(ValueError("Agent 未产出任何结果"))
    assert not _is_connection_error(None)


def test_proxy_retry_rebuilds_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接类失败 → 换代理并重建会话（断言会话重建与轮换次数）。"""
    import asyncio

    from app.core import browser_agent as mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "browser_proxy_list", "http://p1:8080,http://p2:8080")
    monkeypatch.setattr(settings, "collector_max_proxy_retries", 2)
    mgr = mod.BrowserSessionManager()
    mgr._proxy_index = 0  # noqa: SLF001
    mgr._session = object()  # noqa: SLF001 — 伪造已建会话
    mgr._session_loop_id = None  # noqa: SLF001

    calls = {"attempts": 0}

    class _FakeAgent:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, **_k):
            calls["attempts"] += 1
            raise ConnectionError("connect timeout")

    class _FakeBrowserSession:
        """占位 BrowserSession：任意构造参数均可接受。"""

        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(mod, "_Agent", _FakeAgent)
    monkeypatch.setattr(mod, "_load_browser_use", lambda: (_FakeAgent, _FakeBrowserSession))

    async def _run():
        with pytest.raises(mod.CollectorError):
            await mgr.execute_task("任务", max_attempts=2)

    asyncio.run(_run())
    # 初始 + 各失败重试 → 多次尝试；代理轮换启用后会话至少重建一次
    assert calls["attempts"] >= 2
    assert mgr._proxy_index >= 2  # noqa: SLF001 — 池已轮换
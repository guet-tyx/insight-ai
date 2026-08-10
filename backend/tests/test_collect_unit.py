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
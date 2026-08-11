"""采集模块单元测试：单例 / 代理池轮换 / URL 与内网拦截 / Schema 转换。"""
from __future__ import annotations

import asyncio

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

    # W9 后 _get_session 优先 stealth CDP（真实 Chromium）→ 本单测必须禁用
    async def _no_stealth() -> None:
        return None

    monkeypatch.setattr(
        "app.core.stealth_browser.ensure_stealth_browser", _no_stealth
    )

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


# ---------- W6/W10：collector worker（fetch_static / URL 提取 / 节点路由） ----------

def test_extract_url_variants() -> None:
    from app.agents.workers.collector import _extract_url

    assert _extract_url("请采集 https://example.com/a 的内容") == "https://example.com/a"
    assert _extract_url("采集 example.com 首页") == "https://example.com"  # 裸域补 https
    assert _extract_url("https://a.com/path,。") == "https://a.com/path"  # 去尾标点
    assert _extract_url("没有网址") == ""


class _TlsResult:
    """tls_fetch 返回对象的最小桩（status_code / html / text）。"""

    def __init__(self, status_code: int = 200, html: str = "", text: str = "") -> None:
        self.status_code = status_code
        self.html = html
        self.text = text


def _stub_tls(monkeypatch: pytest.MonkeyPatch, result: _TlsResult) -> None:
    """桩掉 _fetch_static_impl 的依赖：TLS 抓取 + 礼貌节流（避免真实网络/sleep）。

    函数内 `from X import y` 从模块属性取值 → patch 模块属性即可生效；
    tls_fetch 是 async 函数 → stub 必须 async。
    """
    import app.services.anti_bot as anti_mod
    import app.services.tls_fetch as tls_mod

    async def _fake_tls_fetch(url: str) -> _TlsResult:
        return result

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_tls_fetch)
    monkeypatch.setattr(anti_mod, "polite_wait", lambda u: None)


def test_fetch_static_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.workers.collector import fetch_static

    _stub_tls(monkeypatch, _TlsResult(
        status_code=200,
        html="<html><body>",
        text="Insight AI 平台基于 LangGraph 多智能体编排，使用 Milvus 向量检索，"
             "配合 Neo4j 图谱拓扑查询与 RRF 融合，支撑情报分析报告生成。",
    ))
    out = asyncio.run(fetch_static.ainvoke({"url": "https://x.com/a"}))
    assert "Insight AI" in out
    assert "LangGraph" in out


def test_fetch_static_403_raises_clear_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """反爬 4xx/5xx → 明确异常信号（促使 LLM 回退浏览器）。"""
    from app.agents.workers import collector as mod

    _stub_tls(monkeypatch, _TlsResult(status_code=403, text="blocked"))
    with pytest.raises(ValueError, match="反爬拦截"):
        asyncio.run(mod._fetch_static_impl("https://x.com/a"))


def test_fetch_static_captcha_page_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """极短验证页 → 命中验证特征明确异常。"""
    from app.agents.workers import collector as mod

    _stub_tls(monkeypatch, _TlsResult(
        status_code=200, html="<div>正在安全验证…</div>", text="安全验证"
    ))
    with pytest.raises(ValueError, match="安全验证"):
        asyncio.run(mod._fetch_static_impl("https://x.com/a"))


def test_fetch_static_too_short_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """正文过少（<50 字）→ 提示浏览器渲染。"""
    from app.agents.workers import collector as mod

    _stub_tls(monkeypatch, _TlsResult(status_code=200, html="<html>", text="少"))
    with pytest.raises(ValueError, match="内容过少"):
        asyncio.run(mod._fetch_static_impl("https://x.com/a"))


def test_collector_node_no_url() -> None:
    from app.agents.workers.collector import collector_node

    state = asyncio.run(collector_node({"task_requirement": "采集某网页", "retry_count": 1}))
    assert state["raw_artifacts"][0]["error"]  # 缺 URL 明确错误


def test_collector_node_rss_hint_routes_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """RSS 特征 → 提示词引导 fetch_rss 首选；产物归一进 raw_artifacts。"""
    from types import SimpleNamespace

    from app.agents.workers import collector as mod

    captured: dict = {}

    class _FakeAgent:
        def __init__(self, *_a, **_k):
            pass

        async def ainvoke(self, messages):
            # collector_node 传 {"messages": [...]}（dict）→ 取 messages["messages"]
            captured["prompt"] = messages["messages"][0]["content"]
            # 返回消息是 getattr(m, "type")/getattr(m, "tool_calls") 判断 → 需属性对象
            return {
                "messages": [
                    SimpleNamespace(type="ai", content="解析完成", tool_calls=None),
                    SimpleNamespace(type="tool", content="ok"),
                ]
            }

    def _fake_factory(*_a, **_k):
        # RSS 提示词在 create_react_agent(prompt=...) 构造参数里注入
        captured["agent_prompt"] = _k.get("prompt", "")
        return _FakeAgent()

    monkeypatch.setattr(mod, "create_react_agent", _fake_factory)
    state = asyncio.run(mod.collector_node({
        "task_requirement": "解析 https://example.com/feed.xml 的内容",
        "url": "https://example.com/feed.xml",
        "retry_count": 0,
    }))
    assert "fetch_rss_tool" in captured["agent_prompt"]
    assert state["raw_artifacts"][0]["data"] == "解析完成"
    assert state["source_type"] == "rss"


def test_collect_webpage_tool_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect_webpage 工具：强制 web 路径并把结构化结果 JSON 化。"""
    from app.agents.workers.collector import collect_webpage

    async def _fake_collect(url, instruction, source="web", allow_internal=False):
        assert source == "web"  # 工具层强制浏览器路径，不做二次特征路由
        return {"text": "采集到的内容", "source_type": "web"}

    monkeypatch.setattr(
        "app.agents.workers.collector.run_collect", _fake_collect
    )
    out = asyncio.run(collect_webpage.ainvoke(
        {"url": "https://x.com/a", "instruction": "提取内容"}
    ))
    assert '"source_type": "web"' in out

# ---------- W9/W10：collect 策略路由（RSS/TLS/浏览器 三路由 + 验证码接管） ----------

class _TlsResp:
    """tls_fetch 返回桩。"""

    def __init__(self, status_code: int = 200, text: str = "", html: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.html = html


def test_collect_invalid_url_raises() -> None:
    from app.core.browser_agent import CollectorError
    from app.services.collector_service import collect

    with pytest.raises(CollectorError, match="内网"):
        asyncio.run(collect("http://127.0.0.1:8080/", "提取", allow_internal=False))


def test_collect_unknown_source_raises() -> None:
    from app.core.browser_agent import CollectorError
    from app.services.collector_service import collect

    with pytest.raises(CollectorError, match="未知 source"):
        asyncio.run(collect("https://example.com/a", "提取", source="ftp"))


def test_collect_forced_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 rss → RSS 快速路径（不耗浏览器），返回规范化条目。"""
    import app.services.rss_service as rss_mod
    from app.schemas.collect import RssExtract, RssItem
    from app.services.collector_service import collect

    item = RssItem(title="LangGraph 增强", link="https://example.com/1", published="", summary="多智能体")
    async def _fake_fetch_rss(url: str) -> RssExtract:
        return RssExtract(items=[item], feed_title="Insight AI 速递")

    monkeypatch.setattr(rss_mod, "fetch_rss", _fake_fetch_rss)
    out = asyncio.run(collect("https://example.com/feed.xml", "解析", source="rss"))
    assert out["feed_title"] == "Insight AI 速递"
    assert out["items"][0]["title"] == "LangGraph 增强"


def test_collect_auto_rss_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto + RSS 特征 URL → RSS 路径（无需策略判定）。"""
    import app.services.rss_service as rss_mod
    from app.services.collector_service import collect

    async def _fake_fetch_rss(url: str):
        from app.schemas.collect import RssExtract

        return RssExtract(items=[], feed_title="t")

    monkeypatch.setattr(rss_mod, "fetch_rss", _fake_fetch_rss)
    out = asyncio.run(collect("https://example.com/rss.xml", "解析", source="auto"))
    assert out["feed_title"] == "t"


def test_collect_tls_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS 指纹抓取成功（短文本充足）→ 直接返回文本结果。"""
    import app.services.tls_fetch as tls_mod
    from app.services.collector_service import collect

    async def _fake_fetch(url: str) -> _TlsResp:
        return _TlsResp(status_code=200, text="内容" * 60, html="<html>")

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_fetch)
    out = asyncio.run(collect("https://example.com/a", "提取", source="tls"))
    assert out["source_type"] == "tls"
    assert "内容" in out["text"]


def test_collect_tls_captcha_returns_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS 命中验证码页 → 返回 captcha 人工接管产物。"""
    import app.services.tls_fetch as tls_mod
    from app.services.collector_service import collect

    async def _fake_fetch(url: str) -> _TlsResp:
        return _TlsResp(status_code=200, text="请完成安全验证", html="<div id='nc_1_n1z'></div>")

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_fetch)
    out = asyncio.run(collect("https://example.com/a", "提取", source="tls"))
    assert out["status"] == "captcha"
    assert "人工" in out["hint"]


def test_collect_tls_intercepted_raises_when_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 tls 且被反爬拦截 → CollectorError（不静默转浏览器）。"""
    import app.services.tls_fetch as tls_mod
    from app.core.browser_agent import CollectorError
    from app.services.collector_service import collect

    async def _fake_fetch(url: str) -> _TlsResp:
        return _TlsResp(status_code=403)

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_fetch)
    with pytest.raises(CollectorError, match="TLS"):
        asyncio.run(collect("https://example.com/a", "提取", source="tls"))


def test_collect_auto_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto：TLS 不可用 → 浏览器路径（stealth CDP）→ WebExtract 结构化。"""
    import app.services.tls_fetch as tls_mod
    from app.core.browser_agent import session_manager
    from app.schemas.collect import WebExtract
    from app.services.collector_service import collect

    async def _fake_fetch(url: str) -> _TlsResp:
        return _TlsResp(status_code=403, text="blocked", html="<html>")

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_fetch)
    async def _fake_execute(**kwargs):
        return WebExtract(title="示例标题", summary="摘要内容",
                          key_points=["点一", "点二"], extracted_at="2026-08-11")

    monkeypatch.setattr(session_manager, "execute_task", _fake_execute)
    out = asyncio.run(collect("https://example.com/a", "提取", source="auto"))
    assert out["title"] == "示例标题"
    assert out["key_points"] == ["点一", "点二"]


def test_collect_custom_schema_and_captcha_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """output_schema → schema_to_pydantic；浏览器结果层面验证码检测 → 接管。"""
    import app.services.tls_fetch as tls_mod
    from app.core.browser_agent import session_manager
    from app.schemas.collect import WebExtract
    from app.services.collector_service import collect

    async def _fake_fetch(url: str) -> _TlsResp:
        return _TlsResp(status_code=403, text="blocked", html="<html>")

    monkeypatch.setattr(tls_mod, "tls_fetch", _fake_fetch)
    async def _fake_execute(**kwargs):
        return WebExtract(title="请完成安全验证", summary="滑块验证", key_points=[], extracted_at="")

    monkeypatch.setattr(session_manager, "execute_task", _fake_execute)
    out = asyncio.run(collect(
        "https://example.com/a", "提取",
        output_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        source="auto",
    ))
    assert out["status"] == "captcha"


def test_collect_task_store_ready_and_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """后台任务入口：成功置 ready；异常置 failed 并记录错误。"""
    from app.services import collector_service as svc

    async def _ok(*_a, **_k):
        return {"text": "结果"}

    async def _bad(*_a, **_k):
        raise ValueError("连接超时")

    # 成功路径
    monkeypatch.setattr(svc, "collect", _ok)
    tid = svc.task_store.create("https://example.com/a")
    asyncio.run(svc.collect_task(tid, "https://example.com/a", "i", None, 30))
    body = svc.task_store.get(tid)
    assert body.status == "ready"
    assert body.data == {"text": "结果"}

    # 失败路径
    monkeypatch.setattr(svc, "collect", _bad)
    tid2 = svc.task_store.create("https://example.com/b")
    asyncio.run(svc.collect_task(tid2, "https://example.com/b", "i", None, 30))
    body2 = svc.task_store.get(tid2)
    assert body2.status == "failed"
    assert "连接超时" in body2.error


def test_captcha_record_path() -> None:
    """验证码归档路径：data/captcha/ 下哈希+时间戳命名。"""
    from app.services.collector_service import _captcha_record

    p = _captcha_record("https://example.com/verify")
    assert "captcha" in p and p.endswith(".png")

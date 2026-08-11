"""W9 采集增强单元测试：TLS 指纹抓取 / 策略注册表 / 验证码检测 / stealth 注入。"""
from __future__ import annotations

import asyncio

import pytest

from app.core.captcha_guard import captcha_artifact, look_like_captcha_page
from app.services.anti_bot import policy_for


# ---------- 策略注册表 ----------

def test_policy_matches_domestic_sites() -> None:
    assert policy_for("https://baike.baidu.com/item/x").fetch == "tls"
    assert policy_for("https://blog.csdn.net/article/x").fetch == "tls"
    assert policy_for("https://www.zhihu.com/question/1").fetch == "browser"
    assert policy_for("https://space.bilibili.com/1").fetch == "browser"
    assert policy_for("https://m.weibo.cn/x").fetch == "browser"
    assert policy_for("https://news.qq.com/x").fetch == "auto"  # 未注册 → 默认


def test_policy_longest_suffix_wins() -> None:
    p = policy_for("https://baike.baidu.com/item/x")
    assert p.delay == (2.0, 4.0)  # 精确匹配 baike 规则而非泛 baidu


def test_policy_captcha_flag() -> None:
    assert policy_for("https://book.douban.com/subject/1/").captcha is True
    assert policy_for("https://www.ithome.com/").captcha is False


# ---------- 验证码检测 ----------

def test_captcha_keyword_detection() -> None:
    assert look_like_captcha_page(url="https://x.com/verify?r=1")
    assert look_like_captcha_page(title="请完成安全验证")
    assert look_like_captcha_page(html="拖动滑块完成拼图")
    assert not look_like_captcha_page(url="https://x.com/article/1", html="正常文章内容")


def test_captcha_dom_detection() -> None:
    assert look_like_captcha_page(html='<div id="nc_1_n1z"></div>')  # 阿里滑块
    assert look_like_captcha_page(html='<div class="slide-verify"></div>')


def test_captcha_artifact_shape() -> None:
    art = captcha_artifact("https://x.com", __import__("pathlib").Path("a.png"))
    assert art["status"] == "captcha"
    assert "人工处理" in art["hint"]


# ---------- TLS 指纹抓取（真实站点；网络波动时 rerun） ----------

@pytest.mark.flaky(reruns=2, reruns_delay=3)
def test_tls_fetch_baike_200() -> None:
    """百度百科：httpx 曾 403，curl_cffi Chrome 指纹应 200 且正文非空。"""
    from app.services.tls_fetch import tls_fetch

    result = asyncio.run(
        tls_fetch("https://baike.baidu.com/item/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E5%A4%A7%E6%A8%A1%E5%9E%8B/63800799")
    )
    assert result.status_code == 200
    assert len(result.text) > 200


@pytest.mark.flaky(reruns=2, reruns_delay=3)
def test_tls_fetch_csdn_200() -> None:
    from app.services.tls_fetch import tls_fetch

    result = asyncio.run(tls_fetch("https://blog.csdn.net/"))
    assert result.status_code == 200
    assert len(result.text) > 200


def test_collect_auto_prefers_tls(local_test_page: str) -> None:
    """W9: auto 路由本地静态页 → TLS 快路径（source_type=tls 且正文命中）。"""
    from app.services.collector_service import collect

    result = asyncio.run(
        collect(local_test_page, "提取页面内容", allow_internal=True)
    )
    assert isinstance(result, dict)
    assert result.get("source_type") == "tls"
    assert "Insight AI" in result.get("text", "")


# ---------- Stealth 指纹注入（需 Chromium） ----------

@pytest.mark.skipif(not __import__("tests.conftest", fromlist=["BROWSER_READY"]).BROWSER_READY,
                    reason="Chromium 未就绪")
def test_stealth_fingerprint_injected() -> None:
    """stealth JS 生效：webdriver 隐藏、plugins/languages/并发补全、chrome 存在。"""
    from app.core.stealth_browser import stealth_manager

    async def _run() -> dict:
        await stealth_manager.start()
        page = await stealth_manager._context.new_page()  # noqa: SLF001
        try:
            await page.goto("about:blank")
            return await page.evaluate(
                """() => ({
                    webdriver: navigator.webdriver,
                    plugins: navigator.plugins.length,
                    languages: navigator.languages.join(','),
                    cores: navigator.hardwareConcurrency,
                    hasChrome: !!window.chrome,
                })"""
            )
        finally:
            await page.close()

    vals = asyncio.run(_run())
    assert vals["webdriver"] is False
    assert vals["plugins"] >= 1
    assert "zh-CN" in vals["languages"]
    assert vals["cores"] > 1
    assert vals["hasChrome"] is True
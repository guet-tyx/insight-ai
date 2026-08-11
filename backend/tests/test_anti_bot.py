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


# ---------- 礼貌采集：每秒节流 ----------

def test_polite_wait_short_interval_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一域请求间隔小于策略下界 → 补齐等待（if 分支）。"""
    import time as _time

    from app.services import anti_bot

    slept: list[float] = []
    monkeypatch.setattr(anti_bot.random, "uniform", lambda a, b: b)  # 取区间上限
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))
    host = "example.com"
    anti_bot._last_request[host] = _time.monotonic() - 0.5  # 刚刚请求过
    anti_bot.polite_wait(f"https://{host}/x")
    assert slept and slept[-1] > 0          # 补齐等待
    assert slept[-1] <= 10.0                # 钳制上限


def test_polite_wait_elapsed_enough_small_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """间隔已满足 → 短延迟（else 分支）。"""
    import time as _time

    from app.services import anti_bot

    slept: list[float] = []
    monkeypatch.setattr(anti_bot.random, "uniform", lambda a, b: (a + b) / 2)
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))
    host = "example.org"
    anti_bot._last_request[host] = _time.monotonic() - 60.0  # 60 秒前已请求
    anti_bot.polite_wait(f"https://{host}/x")
    assert slept and 0.3 <= slept[-1] <= 1.0


def test_polite_wait_unknown_host_default_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """未注册域 → 默认策略（fetch=auto），不抛异常。"""
    from unittest.mock import patch

    from app.services.anti_bot import polite_wait

    with patch("app.services.anti_bot.random.uniform", return_value=0.0), \
            patch("time.sleep") as sleeper:
        polite_wait("https://unknown-domain-xyz.com/page")
    # sleep_for=0.0 → 不 sleep（节流已满足），仅要求不抛异常
    assert sleeper.call_count == 0


# ---------- Stealth 指纹注入（需 Chromium） ----------

@pytest.mark.skipif(not __import__("tests.conftest", fromlist=["BROWSER_READY"]).BROWSER_READY,
                    reason="Chromium 未就绪")
@pytest.mark.flaky(reruns=2, reruns_delay=3)  # 浏览器单例跨用例偶发连接抖动
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
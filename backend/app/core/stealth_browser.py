"""Stealth 浏览器启动器：真实指纹 Chromium + stealth JS 注入（CDP 接入 browser_use）。

调研结论：headless Chromium 默认暴露 navigator.webdriver、SwiftShader WebGL、
空插件列表等自动化特征，成为国内站风控识别点。本模块：
1. 以 playwright `launch_persistent_context` 启动高仿真 Chromium
   （headless=new、完整 viewport/locale/timezone、真实 UA、remote-debugging-port）
2. 注入 stealth JS（playwright-stealth 同款思路）：隐藏 webdriver、
   补全 window.chrome/plugins/languages/hardwareConcurrency
3. 通过 DevToolsActivePort 文件获取 CDP ws 端点 →
   browser_use BrowserSession(cdp_url=...) 复用（现有 Agent 流程零改动）
4. 登录态复用：collector_browser_profile=default 时复用独立持久 profile
   （Cookie 持久化跨任务；本机账号低频使用）
   —— 注：复用「本机默认 Chrome 登录态」需用户以调试端口启动 Chrome，
   本实现采用独立持久 profile（更安全），后续可再加 channel 连本机。

⚠️ 伦理与合规：仅用于采集公开可访问信息与本人账号已登录内容，
遵守 robots.txt 与目标站条款，低频礼貌采集。
"""
from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser-profile"
_CAPTCHA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "captcha"

# stealth JS：在每次页面加载前注入（playwright-stealth 核心思路的子集）
STEALTH_JS = r"""
// 1) 隐藏 webdriver 标记（最常见的指纹检测点）
try {
  Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
    set: undefined, enumerable: true, configurable: true,
    get: new Proxy(Object.getOwnPropertyDescriptor(Object.getPrototypeOf(navigator), 'webdriver').get,
      { apply: (t, thisArg, a) => { Reflect.apply(t, thisArg, a); return false; } }),
  });
} catch (e) {}
// 2) 补全 window.chrome 自动化检测对象
if (!window.chrome) {
  window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}), app: {} };
}
// 3) 插件列表与语言（真实 Chrome 非空）
try {
  Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin',
      filename: 'internal-pdf-viewer', length: 1 })),
  });
  Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 16 });
} catch (e) {}
"""


def _find_free_port() -> int:
    """找空闲端口（debugging 端口冲突时自动避让）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StealthBrowserManager:
    """全局单例：持久化 context（登录态保留）+ CDP 端点供 browser_use 复用。"""

    _instance: "StealthBrowserManager | None" = None
    _context: Any = None
    _cdp_url: str | None = None
    _debug_port: int | None = None

    def __new__(cls) -> "StealthBrowserManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def cdp_url(self) -> str | None:
        return self._cdp_url

    def captcha_dir(self) -> Path:
        _CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
        return _CAPTCHA_DIR

    async def start(self) -> str:
        """启动（幂等）并返回 CDP ws 端点（供 browser_use BrowserSession(cdp_url=)）。"""
        if self._cdp_url:
            return self._cdp_url

        from playwright.async_api import async_playwright

        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._debug_port = _find_free_port()

        pw = await async_playwright().start()
        context_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-notifications",
                f"--remote-debugging-port={self._debug_port}",
            ],
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        try:
            self._context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_DATA_DIR), **context_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — 任意启动失败重试一次
            logger.warning("Stealth 浏览器启动失败（%s），重试一次", exc)
            self._debug_port = _find_free_port()
            context_kwargs["args"] = [
                a for a in context_kwargs["args"]
                if not a.startswith("--remote-debugging-port")
            ] + [f"--remote-debugging-port={self._debug_port}"]
            self._context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_DATA_DIR), **context_kwargs
            )

        # stealth JS 注入（每次导航前执行）
        await self._context.add_init_script(STEALTH_JS)
        # 预热 tab（保证 CDP 目标可达）
        page = await self._context.new_page()
        await page.goto("about:blank")

        # 从 DevToolsActivePort 读取调试端口 → ws 端点（避免依赖 playwright 内部 API）
        self._cdp_url = await self._read_cdp_url()
        logger.info("Stealth 浏览器就绪（CDP=%s…）", self._cdp_url[:40])
        return self._cdp_url

    async def _read_cdp_url(self) -> str:
        port = self._debug_port
        for _ in range(20):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                    if resp.status_code == 200:
                        return resp.json()["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 — 浏览器未就绪，等待
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError("无法读取 Chrome 调试端点（DevTools 未就绪）")


stealth_manager = StealthBrowserManager()


async def ensure_stealth_browser() -> str:
    """采集前确保 stealth 浏览器可用（幂等），返回 CDP 端点。"""
    return await stealth_manager.start()
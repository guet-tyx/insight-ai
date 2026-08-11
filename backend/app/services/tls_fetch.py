"""TLS/JA3 指纹级 HTTP 抓取（curl_cffi 模拟 Chrome 握手 + HTTP/2）。

背景（联网调研）：requests/httpx 的 TLS 握手指纹（JA3）与真实浏览器差异明显，
百度等站点对非浏览器流量直接 403 —— 这就是 W8 之前 fetch_static 被拦的根因。
curl_cffi 基于 curl-impersonate，复刻 Chromium BoringSSL 握手行为与 HTTP/2
帧结构，让服务端无法区分自动化请求。

最佳实践（sources：php.cn/CSDN 实战）：
- 显式 Session + impersonate（不可用 get(impersonate=...) 临时会话，指纹会失效）
- impersonate 与 UA 严格匹配；HTTP/2 默认开启
- 会话按域复用（连接复用 + 指纹一致性）
- ⚠️ Windows + 非 ASCII 项目路径：CA 文件必须放在纯 ASCII 路径
  （libcurl 对含中文路径的 CAfile 加载失败，实测 curl(77)）
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from app.core.browser_agent import UA_POOL

logger = logging.getLogger(__name__)

# 系统临时目录（纯 ASCII 路径）中的 CA 证书 —— libcurl 对含中文的
# CAfile 路径加载失败（实测 curl(77)，Win 下非 ASCII 路径问题）
_CA_PATH = Path(
    os.environ.get("TEMP", "C:/Windows/Temp"),
    "insightai-cacert.pem",
)

CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.bing.com/",
}

# 按域缓存的 Session（连接复用 + 指纹一致）
_sessions: dict[str, cffi_requests.Session] = {}
_TIMEOUT = 15.0


def _ensure_ca() -> Path:
    if not _CA_PATH.exists():
        import certifi

        _CA_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CA_PATH.write_bytes(Path(certifi.where()).read_bytes())
    return _CA_PATH


def _session_for(domain: str) -> cffi_requests.Session:
    """按域复用 Session（impersonate 指纹一致性 + 连接复用）。"""
    if domain not in _sessions:
        from curl_cffi import requests as cffi  # noqa: F401

        _sessions[domain] = cffi_requests.Session(
            impersonate="chrome",
            verify=str(_ensure_ca()),
            timeout=_TIMEOUT,
        )
        logger.info("tls_fetch 创建 %s 会话（指纹=chrome）", domain)
    return _sessions[domain]


def _extract_text(html: str) -> str:
    """去 script/style/标签，折叠空白，保正文。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class TlsFetchResult:
    """抓取结果：状态码 + 正文（供上层判断反爬/成功）。"""

    def __init__(self, status_code: int, html: str) -> None:
        self.status_code = status_code
        self.html = html
        self.text = _extract_text(html)


async def tls_fetch(url: str) -> TlsFetchResult:
    """模拟 Chrome 指纹抓取页面（同步 curl 调用，异步入口为等待包装）。

    返回 TlsFetchResult；网络异常抛 curl_cffi 请求异常（上游统一转采集失败）。
    """
    domain = urlparse(url).netloc
    session = _session_for(domain)
    # 随机 UA（与 Chrome 指纹匹配的桌面 UA；偏离过大可能触发 UA 校验）
    headers = {**CHROME_HEADERS, "User-Agent": UA_POOL[0]}
    import asyncio

    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: session.get(url, headers=headers, allow_redirects=True),
    )
    return TlsFetchResult(resp.status_code, resp.text)
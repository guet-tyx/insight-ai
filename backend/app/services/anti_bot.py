"""反爬策略注册表 + 礼貌采集（P0）。

- 域特征 → 策略：{fetch: tls|browser, delay 区间, need_login, captcha 常见}
- 礼貌采集：每域请求间隔随机延迟 + 48h 频控窗口复位
- collector 路由将按本注册表选择 fetch 路径（RSS 特征优先，tls 可采走
  curl_cffi，需 JS/登录走浏览器）
"""
from __future__ import annotations

import logging
import random
import time
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 每域请求节流（礼貌采集）
_MIN_DELAY = 2.0
_MAX_DELAY = 5.0
_last_request: dict[str, float] = {}
_lock = threading.Lock()


class SitePolicy:
    """单站点采集策略。"""

    def __init__(self, fetch: str = "auto", delay: tuple[float, float] = (_MIN_DELAY, _MAX_DELAY),
                 need_login: bool = False, captcha: bool = False) -> None:
        self.fetch = fetch          # auto / tls / browser
        self.delay = delay          # 请求间隔区间（秒）
        self.need_login = need_login
        self.captcha = captcha


# 内置国内站点策略表（域后缀 → 策略；2026 实测/调研特征归纳）
POLICY_RULES: list[tuple[str, SitePolicy]] = [
    ("baike.baidu.com", SitePolicy(fetch="tls", delay=(2.0, 4.0))),
    ("baidu.com",       SitePolicy(fetch="tls", delay=(3.0, 6.0))),
    ("csdn.net",        SitePolicy(fetch="tls", delay=(2.0, 5.0))),
    ("cnblogs.com",     SitePolicy(fetch="tls", delay=(2.0, 4.0))),
    ("jianshu.com",     SitePolicy(fetch="tls", delay=(2.0, 4.0))),
    ("ithome.com",      SitePolicy(fetch="tls", delay=(2.0, 4.0))),
    ("douban.com",      SitePolicy(fetch="tls", delay=(3.0, 6.0), captcha=True)),
    ("bilibili.com",    SitePolicy(fetch="browser", delay=(3.0, 6.0))),
    ("zhihu.com",       SitePolicy(fetch="browser", delay=(3.0, 6.0), need_login=True, captcha=True)),
    ("weibo.com",       SitePolicy(fetch="browser", delay=(3.0, 6.0), need_login=True, captcha=True)),
    ("weibo.cn",        SitePolicy(fetch="browser", delay=(3.0, 6.0), need_login=True, captcha=True)),
    ("weixin.qq.com",   SitePolicy(fetch="browser", delay=(3.0, 6.0))),
    ("taobao.com",      SitePolicy(fetch="browser", delay=(3.0, 6.0), captcha=True)),
    ("jd.com",          SitePolicy(fetch="browser", delay=(3.0, 6.0), captcha=True)),
]

# 兜底策略（未命中注册表）
_DEFAULT_POLICY = SitePolicy(fetch="auto", delay=(2.0, 5.0))


def policy_for(url: str) -> SitePolicy:
    """按域匹配策略（最长后缀优先）。"""
    host = (urlparse(url).netloc or "").lower()
    matched = _DEFAULT_POLICY
    best_len = 0
    for suffix, policy in POLICY_RULES:
        if host.endswith(suffix) and len(suffix) > best_len:
            matched = policy
            best_len = len(suffix)
    logger.info("策略匹配 %s → fetch=%s delay=%s login=%s", host, matched.fetch, matched.delay, matched.need_login)
    return matched


def polite_wait(url: str) -> None:
    """礼貌采集：按域节流（最小间隔=策略 delay 下界；超时钳制）。"""
    host = urlparse(url).netloc or "unknown"
    policy = policy_for(url)
    with _lock:
        last = _last_request.get(host, 0.0)
        elapsed = time.monotonic() - last
        need = random.uniform(*policy.delay)
        if elapsed < need:
            sleep_for = need - elapsed
        else:
            sleep_for = random.uniform(0.3, 1.0)
        _last_request[host] = time.monotonic()
    if sleep_for > 0:
        time.sleep(min(sleep_for, 10.0))
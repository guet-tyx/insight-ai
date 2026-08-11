"""验证码检测与人工接管（P0：检测+截图归档，不做自动破解）。

检测特征（调研归纳的国内常见形态）：
- URL/标题关键词：verify/captcha/安全验证/人机验证/slider
- 页面元素：滑块/验证码常见 DOM 特征（nc_1_n1z 阿里滑块、td_?
  通用 #captcha 等）
命中 → 截图归档 data/captcha/ + 产物标记 {status:"captcha"} →
采集/任务事件 captcha_required（前端 HITL 提示人工处理后可重试）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.stealth_browser import stealth_manager

logger = logging.getLogger(__name__)

# 关键词特征（URL/标题）
KEYWORD_PATTERNS = [
    r"verify",
    r"captcha",
    r"安全验证",
    r"人机验证",
    r"请完成验证",
    r"拖动滑块",
    r"点选验证",
    r"slider",
]
# DOM 特征（滑块/验证码容器常见）
DOM_PATTERNS = [
    r"id=[\"']nc_1_n1z[\"']",  # 阿里滑块
    r"class=[\"'][^\"']*slide[^\"']*[\"']",
    r"id=[\"'][^\"']*captcha[^\"']*[\"']",
    r"id=[\"'][^\"']*verify[^\"']*[\"']",
]


def look_like_captcha_page(url: str = "", title: str = "", html: str = "") -> bool:
    """验证码页面特征判定（URL/标题/HTML 三路任一命中）。"""
    haystack = f"{url} {title} {html[:20000]}"
    lower = haystack.lower()
    if any(re.search(p, lower) for p in KEYWORD_PATTERNS):
        return True
    return any(re.search(p, haystack) for p in DOM_PATTERNS)


async def archive_captcha(url: str, page: Any, run_id: str = "collect") -> Path:
    """命中验证码：截图归档 data/captcha/，返回归档路径。"""
    directory = stealth_manager.captcha_dir()
    stamp = datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    path = directory / f"{stamp}_{run_id[:8]}_{digest}.png"
    try:
        await page.screenshot(path=str(path))
        logger.warning("验证码已归档：%s（url=%s）", path, url[:80])
    except Exception as exc:
        logger.warning("验证码截图失败：%s", exc)
    return path


def captcha_artifact(url: str, archive_path: Path) -> dict[str, Any]:
    """标准化的验证码产物（供 raw_artifacts/事件消费）。"""
    return {
        "status": "captcha",
        "url": url,
        "hint": "页面触发验证码，需人工处理（截图已归档）",
        "archive": str(archive_path),
    }

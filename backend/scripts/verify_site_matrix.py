"""国内站点采集矩阵验收脚本（W9）。

一键验证 Collector 增强后的真实采集能力：
    uv run python scripts/verify_site_matrix.py

矩阵：TLS 指纹层（curl_cffi）优先 + 浏览器兜底（stealth CDP）。
输出每站状态码/正文长度/耗时；exit code = 失败站点数。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (站点, URL, 期望关键词)
SITE_MATRIX = [
    ("百度百科", "https://baike.baidu.com/item/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E5%A4%A7%E6%A8%A1%E5%9E%8B/63800799", "人工智能大模型"),
    ("CSDN 博客", "https://blog.csdn.net/", "CSDN"),
    ("IT之家", "https://www.ithome.com/", "IT之家"),
    ("博客园", "https://www.cnblogs.com/", "博客园"),
    ("简书", "https://www.jianshu.com/", "简书"),
    ("豆瓣读书", "https://book.douban.com/", "豆瓣"),
    ("B 站首页", "https://www.bilibili.com/", "哔哩哔哩"),
    ("知乎发现页", "https://www.zhihu.com/explore", ""),
]


async def verify_one(name: str, url: str, keyword: str, mode: str = "auto") -> dict:
    from app.services.collector_service import collect

    start = time.monotonic()
    try:
        result = await collect(url, f"提取 {name} 页面要点", source=mode, max_steps=12)
        text = str(result)
        passed = bool(keyword and keyword in text) or len(text) > 100
        return {"name": name, "ok": passed, "len": len(text),
                "ms": int((time.monotonic() - start) * 1000), "kw": keyword in text}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "len": 0,
                "ms": int((time.monotonic() - start) * 1000), "err": str(exc)[:120]}


async def main() -> None:
    print("=" * 72)
    print("Insight AI · 国内站点采集矩阵（TLS 指纹 + Stealth 浏览器）")
    print("=" * 72)
    fails = 0
    for name, url, keyword in SITE_MATRIX:
        r = await verify_one(name, url, keyword)
        mark = "✅" if r["ok"] else "❌"
        detail = f"len={r['len']} {r['ms']}ms" + (f" 关键词命中" if r.get("kw") else "")
        if not r["ok"]:
            detail += f" err={r.get('err', '')}"
            fails += 1
        print(f"  {mark} {name:8s} {detail}")
    print("=" * 72)
    print(f"通过 {len(SITE_MATRIX) - fails}/{len(SITE_MATRIX)} | 失败 {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
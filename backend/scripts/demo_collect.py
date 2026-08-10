"""CLI 演示：自然语言驱动的三路由采集（W6：rss / web / auto）。

用法（backend/ 目录下）：
    # RSS 快速解析（本地演示用 --source rss + --internal-demo + 本地 feed URL）
    uv run python scripts/demo_collect.py --source rss --internal-demo \
        --url http://127.0.0.1:8099/demo_feed.xml "提取最新条目"

    # 网页结构化提取（浏览器）
    uv run python scripts/demo_collect.py --schema --url https://example.com "提取页面标题与列表内容"

    # 自动路由：RSS 特征 URL 自动走 RSS 路径
    uv run python scripts/demo_collect.py --internal-demo --url http://127.0.0.1:8099/demo_feed.xml "查看订阅源"

    # 自由文本指令（不加 --schema 输出纯文本结果）
    uv run python scripts/demo_collect.py --url https://example.com "页面讲了什么？"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 保证从 scripts/ 目录直接运行时也能找到 backend 根下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel  # noqa: E402


class PageOutline(BaseModel):
    """内置示例输出 Schema：网页标题与要点列表。"""

    title: str
    key_points: list[str]


async def _run(url: str, instruction: str, use_schema: bool, allow_internal: bool, source: str = "auto") -> None:
    from app.services.collector_service import collect

    print(f"▶  目标: {url}")
    print(f"▶  指令: {instruction}")
    print(f"▶  输出: {'结构化 Schema' if use_schema else '纯文本'}　路由: {source}")
    print("-" * 60)
    try:
        result = await collect(
            url, instruction,
            output_schema=None if not use_schema else {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
            allow_internal=allow_internal,
            source=source,
        )
        print("采集结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 采集失败: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="自然语言驱动的采集演示（W6 三路由）")
    parser.add_argument("--url", required=True, help="目标 URL（网页或 RSS feed）")
    parser.add_argument("instruction", nargs="?", default="提取页面标题与主要内容", help="提取指令")
    parser.add_argument("--schema", action="store_true", help="启用结构化 Schema 输出（内置示例）")
    parser.add_argument("--source", default="auto", choices=["auto", "rss", "web"], help="采集路由")
    parser.add_argument("--internal-demo", action="store_true", help="允许采集内网地址（仅演示/测试）")
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.instruction, args.schema, args.internal_demo, args.source))


if __name__ == "__main__":
    main()
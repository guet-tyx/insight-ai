"""CLI 演示：自然语言驱动的网页采集。

用法（backend/ 目录下）：
    # 结构化示例：提取页面标题与列表项（内置示例 Schema）
    uv run python scripts/demo_collect.py --url https://example.com --schema "提取页面标题与列表内容"

    # 自由文本指令（不加 --schema 输出纯文本结果）
    uv run python scripts/demo_collect.py --url https://example.com "页面讲了什么？"

    # 本地测试站点（走 allow_internal_demo 开关，仅演示/测试用）
    uv run python scripts/demo_collect.py --internal-demo --url http://127.0.0.1:8080/ "提取标题"
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


async def _run(url: str, instruction: str, use_schema: bool, allow_internal: bool) -> None:
    from app.services.collector_service import collect

    print(f"▶  目标: {url}")
    print(f"▶  指令: {instruction}")
    print(f"▶  输出: {'结构化 Schema' if use_schema else '纯文本'}")
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
        )
        print("采集结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 采集失败: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="自然语言驱动的网页采集演示")
    parser.add_argument("--url", required=True, help="目标网页 URL")
    parser.add_argument("instruction", nargs="?", default="提取页面标题与主要内容", help="提取指令")
    parser.add_argument("--schema", action="store_true", help="启用结构化 Schema 输出（内置示例）")
    parser.add_argument("--internal-demo", action="store_true", help="允许采集内网地址（仅演示/测试）")
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.instruction, args.schema, args.internal_demo))


if __name__ == "__main__":
    main()
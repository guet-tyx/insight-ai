"""LangGraph 检查点单例：AsyncRedisSaver 持久化（跨重启保留会话），Redis 不可用时降级 MemorySaver。

设计要点（适配 AsyncRedisSaver 的 loop 捕获语义，Issue #179）：
- 仅在「真实事件循环」内构建并 asetup()，把运行 loop 绑定到 saver（sync 包装
  通过 run_coroutine_threadsafe 转发到该 loop）
- 异步入口（SSE 流式、agents 任务）先 `await ensure_checkpointer()` 再取图
- 同步入口（线程池，如列表查询）用 `get_checkpointer_sync()`（内部自建一次 loop）
- 任何构建失败 → MemorySaver 降级（告警日志）
"""
from __future__ import annotations

import asyncio
import logging

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.redis import AsyncRedisSaver

from app.core.config import settings

logger = logging.getLogger(__name__)

_saver = None
_is_redis = False


async def ensure_checkpointer():
    """在调用方的事件循环内构建检查点并建索引（进程内单例；幂等）。

    settings.checkpointer_backend=memory 时（测试环境）直接使用 MemorySaver：
    pytest 每个 TestClient 请求运行在不同事件循环，AsyncRedisSaver 连接
    跨循环复用会报『事件循环已关闭』；持久化正确性由独立的
    test_redis_saver_cross_instance_persistence 覆盖。
    """
    global _saver, _is_redis
    if _saver is not None:
        return _saver
    if settings.checkpointer_backend != "redis":
        _saver = MemorySaver()
        logger.info("检查点使用 MemorySaver（CHECKPOINTER_BACKEND=memory）")
        return _saver
    try:
        saver = AsyncRedisSaver(
            redis_url=settings.redis_url,
            ttl={"checkpoints": 86400 * 7, "checkpoint_writes": 86400 * 7},  # 7 天保留
        )
        await saver.asetup()  # 捕获当前 loop，供 sync 包装 threadsafe 转发
        _saver = saver
        _is_redis = True
        logger.info("检查点已启用 AsyncRedisSaver 持久化 (%s)", settings.redis_url)
    except Exception as exc:  # noqa: BLE001 — 降级路径
        logger.warning("AsyncRedisSaver 初始化失败（%s），降级为 MemorySaver（重启后会话丢失）", exc)
        _saver = MemorySaver()
    return _saver


def get_checkpointer_sync():
    """同步上下文取检查点（线程池/无运行 loop 时自建一次）。"""
    global _saver
    if _saver is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(ensure_checkpointer())
        else:
            raise RuntimeError(
                "检查点未初始化：异步入口必须先 await ensure_checkpointer()"
            )
    return _saver


def reset_checkpointer() -> None:
    """测试专用：重置单例，使检查点在当前事件循环内重新构建。

    背景：AsyncRedisSaver 的连接绑定创建时的事件循环；pytest 每个
    asyncio.run 都会新建/关闭循环，跨循环复用旧连接会报『事件循环已关闭』。
    生产（uvicorn 单循环）无需调用。
    """
    global _saver, _is_redis
    _saver = None
    _is_redis = False


def is_redis_backed() -> bool:
    """当前检查点是否为 Redis 持久化（供测试与监控断言）。"""
    return _saver is not None and _is_redis
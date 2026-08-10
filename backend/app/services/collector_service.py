"""采集任务编排：URL 校验 + 内网拦截（防 SSRF）+ 指令组装 + 结果归一。

采集任务为瞬时状态（无需持久化），使用线程安全的内存任务表；
生产环境（W11）替换为 Celery/Redis 队列。
"""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, create_model
from pydantic_core import PydanticUndefined

from app.core.browser_agent import CollectorError, session_manager
from app.schemas.collect import CollectTaskOut

logger = logging.getLogger(__name__)

# 默认拦截的内网/保留网段（SSRF 防护，API 层强制；演示/测试可传 allow_internal_demo）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

# JSON Schema 类型 → Python 类型映射（bge 支持的最小子集）
_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

_TASK_HINT = (
    "请访问该网页并完成提取任务。直接给出最终结果，不要解释过程。"
    "若页面元素不存在，对应字段返回空字符串或 null。"
)


class _CollectStore:
    """线程安全的内存态采集任务表。"""

    def __init__(self) -> None:
        self._tasks: dict[str, CollectTaskOut] = {}
        self._lock = threading.Lock()

    def create(self, url: str) -> str:
        task_id = uuid.uuid4().hex
        with self._lock:
            self._tasks[task_id] = CollectTaskOut(
                task_id=task_id, url=url, status="running",
                created_at=datetime.now(timezone.utc),
            )
        return task_id

    def get(self, task_id: str) -> CollectTaskOut | None:
        with self._lock:
            return self._tasks.get(task_id)

    def finish(self, task_id: str, status: str, data: Any = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks[task_id]
            self._tasks[task_id] = task.model_copy(
                update={"status": status, "data": data, "error": error}
            )


task_store = _CollectStore()


def validate_url(url: str, allow_internal: bool = False) -> tuple[bool, str]:
    """URL 合法性 + 内网拦截校验；返回 (是否合法, 错误信息)。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https 协议"
    if not parsed.hostname:
        return False, "URL 缺少主机名"
    host = parsed.hostname.lower()
    if allow_internal:
        return True, ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return False, "禁止采集内网地址（演示请用 allow_internal_demo）"
    try:
        ip = ipaddress.ip_address(host)
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return False, "禁止采集内网/保留地址段"
    except ValueError:
        pass  # 域名：不做 DNS 解析拦截（W6 增强）
    return True, ""


def schema_to_pydantic(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """把请求中的 JSON Schema（type=object + properties）转成 Pydantic 模型。

    支持最小子集：string/integer/number/boolean/array/object；
    required 字段缺省时允许 None（采集场景宽容处理）。
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError('output_schema 必须是 {"type": "object", "properties": {...}}')
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("output_schema.properties 不能为空")

    fields: dict[str, tuple[Any, Any]] = {}
    required = set(schema.get("required", []) or [])
    for field_name, prop in properties.items():
        if not isinstance(prop, dict):
            raise ValueError(f"字段 {field_name} 定义必须是对象")
        py_type = _SCHEMA_TYPE_MAP.get(prop.get("type", "string"), str)
        default = None if field_name not in required else PydanticUndefined
        fields[field_name] = (py_type, default)
    return create_model(name, **fields)  # type: ignore[arg-type]


def build_task_instruction(url: str, instruction: str) -> str:
    """组装 Agent 执行指令（限定提取目标，控制 Token 开销）。"""
    return f"访问网页 {url}。任务：{instruction}。{_TASK_HINT}"


async def collect(
    url: str,
    instruction: str,
    output_schema: dict[str, Any] | None = None,
    max_steps: int = 30,
    allow_internal: bool = False,
    source: str = "auto",
) -> Any:
    """执行一次采集（W6 三路由）；返回结构化数据（dict/列表）或文本。

    source：auto（RSS 特征识别）/ rss（强制 RSS 解析，不耗浏览器）/ web（强制浏览器）。
    rss 路径默认输出 RssExtract（自定义 output_schema 仅对 web 路径生效）。
    """
    ok, err = validate_url(url, allow_internal=allow_internal)
    if not ok:
        raise CollectorError(err)
    if source not in ("auto", "rss", "web"):
        raise CollectorError(f"未知 source: {source}")

    # ---- RSS 快速路径（auto 特征命中或强制 rss）----
    from app.services.rss_service import fetch_rss, looks_like_rss_url

    if source == "rss" or (source == "auto" and looks_like_rss_url(url)):
        extract = await fetch_rss(url)
        return extract.model_dump()

    # ---- Web（浏览器）路径 ----
    task = build_task_instruction(url, instruction)
    output_model = None
    if output_schema:
        output_model = schema_to_pydantic("CollectOutput", output_schema)
    if output_model is None:
        # W6：web 路径默认强类型输出（WebExtract），校验失败由引擎回退文本
        from app.schemas.collect import WebExtract

        output_model = WebExtract

    start = time.monotonic()
    result = await session_manager.execute_task(
        task_instruction=task,
        output_model=output_model,
        max_steps=max_steps,
    )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if output_model is not None:
        data: Any = result.model_dump() if hasattr(result, "model_dump") else result
    else:
        data = str(result)
    logger.info("采集完成 url=%s 耗时=%dms 结构=%s", url, elapsed_ms, isinstance(data, dict))
    return data


async def collect_task(task_id: str, url: str, instruction: str,
                       output_schema: dict[str, Any] | None, max_steps: int,
                       source: str = "auto") -> None:
    """后台任务入口（FastAPI BackgroundTasks 调用）。"""
    try:
        data = await collect(url, instruction, output_schema, max_steps, source=source)
        task_store.finish(task_id, "ready", data=data)
    except Exception as exc:  # noqa: BLE001 — 后台任务统一转为 failed
        task_store.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        logger.error("采集任务 %s 失败：%s", task_id, exc, exc_info=True)
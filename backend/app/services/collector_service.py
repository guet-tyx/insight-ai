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
    """执行一次采集（W6 路由 + W9 策略增强）；返回结构化数据（dict/列表）。

    source：auto（RSS/策略特征识别）/ rss（强制 RSS）/ tls（强制 TLS 指纹抓取）/
            web（强制浏览器）。
    策略路由（auto）：RSS 特征 → RSS；反爬策略表 fetch=tls → tls_fetch；
    fetch=browser 或 tls 失败 → 浏览器（stealth CDP）。
    验证码命中：返回 {status:"captcha", ...} 供人工接管。
    """
    ok, err = validate_url(url, allow_internal=allow_internal)
    if not ok:
        raise CollectorError(err)
    if source not in ("auto", "rss", "tls", "web"):
        raise CollectorError(f"未知 source: {source}")

    from app.core.captcha_guard import captcha_artifact, look_like_captcha_page
    from app.services.anti_bot import policy_for
    from app.services.rss_service import fetch_rss, looks_like_rss_url
    from app.services.tls_fetch import tls_fetch

    # ---- RSS 快速路径（auto 特征命中或强制 rss）----
    if source == "rss" or (source == "auto" and looks_like_rss_url(url)):
        extract = await fetch_rss(url)
        return extract.model_dump()

    # ---- 策略判定（auto）：tls 站点优先 TLS 指纹抓取 ----
    policy = policy_for(url)
    if source == "tls" or (source == "auto" and policy.fetch in ("tls", "auto")):
        try:
            result = await tls_fetch(url)
            if result.status_code in (401, 403, 429) or result.status_code >= 500:
                raise CollectorError(f"TLS 抓取被拦截（HTTP {result.status_code}），转浏览器")
            if look_like_captcha_page(url=url, html=result.html):
                return captcha_artifact(url, _captcha_record(url))
            if result.text and len(result.text) >= 80:
                logger.info("TLS 指纹抓取成功 url=%s len=%d", url, len(result.text))
                return {"url": url, "text": result.text[:8000], "source_type": "tls"}
            raise CollectorError("TLS 内容过少，转浏览器")
        except CollectorError:
            if source == "tls":
                raise
            logger.info("TLS 抓取不可用，转浏览器采集：%s", url)
        except Exception as exc:  # noqa: BLE001 — 网络类失败转浏览器
            logger.warning("TLS 抓取异常（%s），转浏览器", exc)
            if source == "tls":
                raise CollectorError(f"TLS 抓取失败：{exc}") from exc

    # ---- Web（浏览器）路径（stealth CDP 指纹对抗）----
    task = build_task_instruction(url, instruction)
    output_model = None
    if output_schema:
        output_model = schema_to_pydantic("CollectOutput", output_schema)
    if output_model is None:
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
    # 验证码特征（结果文本层面检测，人工接管）
    if look_like_captcha_page(url=url, title=str(data)[:500], html=str(data)[:20000]):
        return captcha_artifact(url, _captcha_record(url))
    logger.info("采集完成 url=%s 耗时=%dms 结构=%s", url, elapsed_ms, isinstance(data, dict))
    return data


def _captcha_record(url: str) -> str:
    """验证码命中记录（归档占位路径；截图归档由 stealth 上下文补充）。"""
    import hashlib
    import time as _t
    from pathlib import Path

    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    stamp = _t.strftime("%Y%m%d_%H%M%S")
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "captcha" / f"{stamp}_{digest}.png")


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
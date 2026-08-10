"""自然语言驱动的浏览器采集引擎（browser-use 0.11 封装）。

核心设计（对应计划 Week 3《core/browser_agent.py 浏览器 Agent 封装与会话池管理》）：
- BrowserSessionManager：单例持有 BrowserSession（headless + keep_alive），
  浏览器实例跨任务复用，避免反复启动 Chromium 的开销
- execute_task：以深度优先的自然语言指令驱动 Agent；支持 Pydantic 输出
  Schema 强类型结构化提取；内置指数退避重试与超时熔断
- 反检测基线：随机 User-Agent 注入 + 可选代理池轮换（配置驱动，本机无池时可空）

⚠️ 0.11 版 API 与计划示例（0.2.x，Browser/BrowserConfig/browser_context）不兼容：
   新版为 BrowserSession + Agent(browser_session=...)，本模块已适配。
⚠️ deepseek-v4-flash 为非视觉模型：use_vision=False 走 DOM/Accessibility Tree
   文本路线（正合计划"过滤冗余节点、降低 Token 体积"的目标）。
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, TypeVar



from app.core.config import settings

logger = logging.getLogger(__name__)

# 避免无代理环境构造失败：惰性导入（browser_use 包较大且依赖可运行环境）
_Agent = None
_BrowserSession = None


def _load_browser_use():
    global _Agent, _BrowserSession
    if _Agent is None:
        from browser_use import Agent, BrowserSession  # type: ignore[import-not-found]

        _Agent = Agent
        _BrowserSession = BrowserSession
    return _Agent, _BrowserSession


class CollectorError(Exception):
    """采集失败（重试耗尽 / 模型未配置 / 浏览器异常）。"""


# 桌面浏览器常见 UA 池（随机注入，降低自动化检测特征）
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

_StructuredOut = TypeVar("_StructuredOut")


class BrowserSessionManager:
    """浏览器会话单例管理器（全局唯一，复用 Chromium 实例）。"""

    _instance: "BrowserSessionManager | None" = None
    _session: Any = None  # BrowserSession，惰性初始化
    _proxy_index: int = 0

    def __new__(cls) -> "BrowserSessionManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def proxy_list(self) -> list[str]:
        """配置驱动的代理池（.env BROWSER_PROXY_LIST，逗号分隔；空=直连）。"""
        raw = settings.browser_proxy_list
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _next_proxy(self) -> str | None:
        """轮换出代理池中的下一个代理（无池时返回 None=直连）。"""
        pool = self.proxy_list
        if not pool:
            return None
        proxy = pool[self._proxy_index % len(pool)]
        self._proxy_index += 1
        return proxy

    def _get_session(self) -> Any:
        """获取（必要时创建）全局唯一的 BrowserSession。

        反检测基线：随机 UA + 中文语言头 + 无沙箱参数 + 代理（若配置）。
        """
        if self._session is None:
            _, BrowserSession = _load_browser_use()
            proxy = self._next_proxy()
            args = ["--no-sandbox", "--disable-setuid-sandbox"]
            if proxy:
                args.append(f"--proxy-server={proxy}")
            self._session = BrowserSession(
                headless=True,
                keep_alive=True,
                args=args,
                headers={
                    "User-Agent": random.choice(UA_POOL),
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            logger.info("BrowserSession 已创建 (proxy=%s)", proxy or "直连")
        return self._session

    def build_llm(self) -> Any:
        """Agent 驱动的 LLM：SenseNova 网关（OpenAI 兼容，tool calling 已验证）。

        ⚠️ 必须用 browser_use 自带的 ChatOpenAI（browser_use.llm.openai.chat），
        其内部已从 langchain 迁移到 openai.types 协议；langchain 的
        ChatOpenAI 缺少 provider 属性会导致 Agent 初始化失败。
        """
        if not settings.openai_api_key:
            raise CollectorError("LLM 未配置（OPENAI_API_KEY），无法驱动浏览器 Agent")
        from browser_use.llm.openai.chat import ChatOpenAI

        return ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            base_url=settings.llm_base_url,
            temperature=0,
        )

    async def execute_task(
        self,
        task_instruction: str,
        output_model: type[_StructuredOut] | None = None,
        max_steps: int = 30,
        max_attempts: int = 3,
    ) -> _StructuredOut | str:
        """执行自然语言采集任务；可选 Pydantic 模型做强类型结构化输出。

        外层指数退避重试（1s/2s/4s）兜底浏览器/导航类瞬时故障；
        重试耗尽抛 CollectorError。
        """
        Agent, _ = _load_browser_use()
        session = self._get_session()
        llm = self.build_llm()
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                agent = Agent(
                    task=task_instruction,
                    llm=llm,
                    browser_session=session,
                    output_model_schema=output_model,
                    use_vision=False,          # deepseek-v4-flash 非视觉模型
                    max_failures=3,            # 内置步骤级重试
                    llm_timeout=120,
                    step_timeout=60,
                )
                history = await agent.run(max_steps=max_steps)
                if output_model is not None:
                    structured = history.structured_output
                    if structured is not None:
                        return structured
                    # 兜底：结构化解析失败时尽量返回原始文本
                    final = history.final_result
                    if final:
                        logger.warning("结构化输出解析失败，回退文本结果")
                        return final
                else:
                    final = history.final_result
                    if final:
                        return final
                last_exc = CollectorError("Agent 未产出任何结果（可能是任务无法完成）")
            except CollectorError as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — 浏览器/驱动异常统一重试
                last_exc = exc
                logger.warning("采集任务第 %d 次失败：%s", attempt, exc)
            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

        raise CollectorError(f"采集失败（已重试 {max_attempts} 次）: {last_exc}")


session_manager = BrowserSessionManager()
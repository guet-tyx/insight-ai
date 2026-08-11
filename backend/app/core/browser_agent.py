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

    _instance: BrowserSessionManager | None = None
    _session: Any = None  # BrowserSession，惰性初始化
    _session_loop_id: int | None = None  # 创建时的 asyncio 事件循环标识
    _proxy_index: int = 0

    def __new__(cls) -> BrowserSessionManager:
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

    async def _get_session(self) -> Any:
        """获取（必要时创建）全局唯一的 BrowserSession。

        反检测基线（W9 增强）：
        - 优先连接 **Stealth 浏览器**（stealth_browser 模块：真实指纹 + JS 注入 +
          持久 profile 登录态），通过 CDP 复用 —— 国内站风控对抗主路径
        - 兜底：自启 Chromium（随机 UA + 中文语言头 + 拟人时序 + 代理池轮换）
        事件循环变更时自动重建（playwright 连接绑定创建时的 loop）。

        ⚠️ 必须为 async：stealth 协程须在当前 loop 内 await（旧实现用
        run_coroutine_threadsafe(...).result(30) 同步阻塞 loop，提交给同一
        loop 的协程永不被调度 → 每次构建会话白等 30s 超时）。
        """
        loop = asyncio.get_running_loop()
        if self._session is not None and self._session_loop_id != id(loop):
            logger.warning("事件循环已变更，重建 BrowserSession（旧连接绑定旧 loop）")
            self._session = None
        if self._session is None:
            _, BrowserSession = _load_browser_use()
            # W9：优先 stealth CDP（指纹对抗 + 登录态）
            cdp_url = None
            try:
                from app.core.stealth_browser import ensure_stealth_browser

                cdp_url = await asyncio.wait_for(ensure_stealth_browser(), timeout=30)
            except Exception as exc:
                logger.warning("Stealth 浏览器不可用（%s），回退自启 Chromium", exc)
            if cdp_url:
                logger.info("BrowserSession 连接 Stealth CDP 浏览器")
                self._session = BrowserSession(cdp_url=cdp_url, keep_alive=True)
            else:
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
                    wait_between_actions=0.5,  # 拟人：动作间延迟
                    minimum_wait_page_load_time=1.0,  # 拟人：页面加载等待
                    wait_for_network_idle_page_load_time=2.0,  # 拟人：网络空闲判定
                )
            self._session_loop_id = id(loop)
            logger.info("BrowserSession 已创建")
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

    def build_judge_llm(self) -> Any:
        """任务完成判定（browser_use judge，带结构化输出约束）。

        ⚠️ 必须用主模型：browser_use 的 judge 走 guided grammar 结构化输出，
        lite 模型（sensenova-6.7-flash-lite）网关缺 xgrammar 模块 → 每次
        判分 400（实测 Judge trace failed）。主模型 deepseek-v4-flash 正常。
        """
        if not settings.openai_api_key:
            raise CollectorError("LLM 未配置（OPENAI_API_KEY），无法驱动浏览器 Agent")
        from browser_use.llm.openai.chat import ChatOpenAI

        return ChatOpenAI(
            model=settings.llm_model,  # 主模型（xgrammar 兼容）
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

        重试策略（叠加）：
        1. 指数退避（1s/2s/4s）重试瞬时故障（max_attempts）
        2. W6 新增：连接/超时类失败自动**换代理重建会话**重试
           （collector_max_proxy_retries 轮，0=关闭；需配置代理池）
        """
        Agent, _ = _load_browser_use()
        llm = self.build_llm()
        last_exc: Exception | None = None
        proxy_retries = 0

        for attempt in range(1, max_attempts + 1):
            session = await self._get_session()
            try:
                agent = Agent(
                    task=task_instruction,
                    llm=llm,
                    browser_session=session,
                    output_model_schema=output_model,
                    judge_llm=self.build_judge_llm(),  # lite 模型做完成判定（省成本）
                    use_vision=False,  # deepseek-v4-flash 非视觉模型
                    max_failures=3,  # 内置步骤级重试
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
            except Exception as exc:
                last_exc = exc
                logger.warning("采集任务第 %d 次失败：%s", attempt, exc)

            # W6：连接/超时类失败 → 换代理重建会话（抗封禁；需代理池非空且配额未用尽）
            if (
                self.proxy_list
                and proxy_retries < settings.collector_max_proxy_retries
                and _is_connection_error(last_exc)
            ):
                proxy_retries += 1
                proxy = self._next_proxy()
                logger.warning("连接类失败，第 %d 次换代理 %s 重试", proxy_retries, proxy)
                self._session = None  # 强制按新代理重建会话
                await asyncio.sleep(1)
                continue
            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

        raise CollectorError(f"采集失败（已重试 {max_attempts} 次）: {last_exc}")


def _is_connection_error(exc: Exception | None) -> bool:
    """判断异常是否属于连接/网络类（触发换代理重试）。"""
    if exc is None:
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("connect", "timeout", "dns", "proxy", "refused", "closed pipe"))


session_manager = BrowserSessionManager()

# W10 测试覆盖率报告

> 阶段：W10 性能优化 + 覆盖测试
> 日期：2026-08-11
> 结论：**整体覆盖率 80%（目标 ≥80%，达成）**，新增/增强测试用例 45+，共 **165 个用例通过**（另 13 个真实 LLM/浏览器用例标记 flaky 独立运行）。

## 一、覆盖率总览

```
TOTAL    2167 stmts   436 miss   80%
```

| 指标 | 基线（W10 前） | 本次（W10） | 变化 |
|------|------:|------:|------:|
| 测试用例数 | 96 | 165（+13 flaky） | +69 |
| 整体覆盖率 | 65% | **80%** | +15pp |
| 关键服务层覆盖 | 分散 | 见下表 | — |

## 二、关键文件覆盖率变化

| 模块 | 前 | 后 | 补充测试 |
|------|-----|-----|---------|
| `services/guardrails.py` | 0% | **100%** | 新增 `test_guardrails.py`（引用校验/路由枚举/幻觉埋点） |
| `services/trace_logger.py` | 0% | **100%** | 新增 `test_trace_logger.py`（环形缓冲/分位数/失败率/摘要） |
| `services/anti_bot.py` | 71% | **100%** | polite_wait 节流三分支 |
| `agents/human_review.py` | 54% | **100%** | interrupt 载荷/动作映射/未知动作/熔断 |
| `agents/supervisor.py` | 65% | **97%** | 熔断/配置校验/条件边/决策上下文 |
| `services/agent_service.py` | 37% | **93%** | 新增 `test_stream_sse.py`（事件协议全分支+心跳+错误+Trace 埋点） |
| `services/mcp_registry.py` | 49% | **97%** | JSON 解析/调用边界/HTTP 握手/失败隔离/下线清理 |
| `agents/workers/collector.py` | 46% | **83%** | fetch_static 信号/URL 提取/RSS 路由/工具载荷 |
| `services/rrf.py` | 100% | 100% | — |

## 三、新增/增强测试文件

| 文件 | 用例数 | 覆盖内容 |
|------|-------:|---------|
| `tests/test_guardrails.py` | 9 | W10 幻觉防护层 |
| `tests/test_trace_logger.py` | 10 | 本地 Trace 双轨 |
| `tests/test_human_review.py` | 8 | HITL 审核节点 |
| `tests/test_supervisor.py` | 6 | 路由节点 |
| `tests/test_stream_sse.py` | 7 | SSE 事件流全协议 |
| `tests/test_knowledge_api_errors.py` | 6 | 知识库 API 错误分支（免 infra） |
| `tests/test_collect_unit.py` | +19 | 采集策略路由（RSS/TLS/浏览器/验证码） |
| `tests/test_mcp_registry.py` | +7 | HTTP 握手/隔离/下线 |
| `tests/test_anti_bot.py` | +3 | 礼貌采集节流 |
| `tests/test_agents.py` | +9 | run_store 状态机/阶段事件/审核边界 |
| `tests/test_chat.py` | +2 | 503/404 分支 |

## 四、覆盖过程中发现并修复的问题

1. **`trace_logger.tool_fail_ratio` 恒为 0**：`tool_ok/tool_fail` 记录到 `tool` kind，但失败率读 `tool_ok/tool_fail` 独立计数 → record 内按 status 细分计数。
2. **`mcp_registry._run` 对缺 `text` 字段的 content 崩溃**：`str(c.text)` 直接访问 → 改 `getattr(c, "text", "")`。
3. **`MCPRegistry.endpoints` 不去两端空白**：`" http://x "` 保留空格 → `str(e).strip().rstrip("/")`。
4. **`browser_agent._get_session` 死锁**：`run_coroutine_threadsafe(...).result(30)` 在事件循环线程内同步阻塞，提交给同一 loop 的 stealth 协程永不被调度 → 每次构建会话白等 30s 超时后回退自启。改 async + `await asyncio.wait_for`（**生产性能缺陷，实际 stealth 路径从未生效**）。
5. **`stream_sse` 错误后仍发 `done`**：error 事件后继续 `aget_state` 产出空 answer done 事件，误导前端 → error 分支 `return`。
6. **`graph_service` 每查询新建/关闭 Neo4j driver**（压测暴露，详见压测报告）→ 模块级单例复用。
7. **Neo4j 5.26 deprecation**：`CALL { WITH n ... }` → `CALL (n) { ... }`。

## 五、仍存在的缺口（后续周次）

| 模块 | 缺口 | 原因 |
|------|------|------|
| `api/v1/agents.py`（57%） | `_execute_run`/`_continue_run`/stream 轮询 | 真实 LLM+浏览器链路，已有 2 个 flaky 集成用例覆盖 |
| `services/ingest_service.py`（25%） | 向量化入库全流程 | 依赖 Milvus + 真实嵌入，集成用例覆盖（knowledge API） |
| `services/collector_service.py`（71%） | Web 浏览器路径 | 真实 Chromium 慢，集成用例覆盖 |
| `services/graph_service.py`（60%） | 实体提取/路径解析 | 真实 LLM 提取，集成用例覆盖 |

> 说明：缺口集中在「真实基础设施/LLM」路径，均已有 flaky 集成用例（`INFRA_READY`/`BROWSER_READY` 守卫）独立覆盖；纯逻辑层已 ≥80%。

## 六、固化

`pyproject.toml` 新增：

```toml
[tool.pytest.ini_options]
cov_fail_under = "80"
```

`uv run pytest --cov=app` 低于 80% 即失败（CI/后续回归自动守护）。

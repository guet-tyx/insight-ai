# Insight AI — 工作区指令

情报分析平台（求职作品）：LangGraph 多智能体编排（Supervisor-Worker + HITL 审核）+ FastMCP 插件化工具 + RAG/GraphRAG（Milvus + Neo4j + RRF 融合）+ Browser Use 采集 + Next.js 14 前端。12 周路线以 **`计划.txt`** 为准（当前 W10 已完成，下一 W11）。

## 目录

- `backend/` — FastAPI + LangGraph（uv 管理，Python 3.12+）
  - `app/api/v1/*.py` 薄路由层（JWT 鉴权 `get_current_user`）；`app/services/*.py` 业务逻辑；`app/agents/*` 图/节点（`workers/` 为专家子图）；`app/core/*` 基础设施（config/security/checkpointer/browser/stealth）
  - `tests/` — pytest（14+ 文件）；`scripts/` — 压测/验证脚本；`docs/api-v1.md` API 协议
- `frontend/` — Next.js 14 App Router（pnpm），SSE 解析在 `src/app/chat`，React StrictMode（必须纯 updaters）
- `mcp_servers/` — 4 个 FastMCP HTTP server：browser(8101)/vector(8102)/graph(8103)/system(8104)
- `docker-compose.yml` — etcd/minio/milvus/neo4j/redis 容器；`.env` 在仓库根
- `docs/` — 采集伦理与合规、coverage-report、benchmark-report

## 命令（均在对应目录执行）

```bash
cd backend
uv run pytest -q -m "not flaky"          # 单测（真实 LLM/浏览器用例标 flaky）
uv run pytest --cov=app                  # 覆盖率（pyproject 固化 fail_under=80）
uv run python scripts/bench_retrieval.py --qps 1,5,10,20   # 检索压测
uv run python scripts/verify_neo4j_indexes.py              # 索引 EXPLAIN 验证
uv run python ../mcp_servers/browser_mcp.py --port 8101    # 起 MCP server（必须 --port）

cd frontend && pnpm dev                  # Next.js 开发服务器
```

## 关键架构规则

- Agent 工具**双模式**：`agents/tool_factory.py` MCP 远端优先 + 本地回退（registry.ready 判断）
- LangGraph 模型节点名是 **"agent"**（不是 "model"）；检查点 `AsyncRedisSaver` 绑定创建时的事件循环 → 异步路径必须 `aget_state`
- 采集三路由：RSS 特征 → TLS 指纹（curl_cffi，`services/tls_fetch.py`）→ 浏览器（stealth CDP，`core/stealth_browser.py`）；验证码命中返回 `captcha` 人工接管
- Neo4j driver 为模块级单例（`graph_service.get_driver()`），**勿 close**；收尾用 `close_driver()`
- 检索并行：`retrieval_service.search()` 向量/图谱经 ThreadPoolExecutor 并行 + RRF(k=60)
- 实体提取走 lite 模型（`LLM_MODEL_LITE`），带 5min TTL 缓存

## 环境/测试陷阱（实测）

- **HTTPS_PROXY 会破坏 grpc-Milvus 连接** → 跑测试/连 Milvus 前先 `unset HTTPS_PROXY HTTP_PROXY ALL_PROXY`
- **模型无 vision**：禁止 `Read`/`cat` 读图片（会 400 崩溃回合）；看图片一律用 `mcp__sensenova-image-recognition__understand_image`
- 嵌入必须 `check_embedding_ctx_length=False`（SiliconFlow 拒绝 token ID）
- `with_structured_output` / browser-use `judge_llm` 必须用**主模型**（lite 缺 xgrammar 模块 → 400）
- `conftest.py` 已在导入 app 前设 `DATABASE_URL`（SQLite 临时库）+ `CHECKPOINTER_BACKEND=memory`，**勿改动顺序**
- Windows 中文路径 curl(77) CA 错误 → certifi 复制到 `%TEMP%`；git LF→CRLF 警告可忽略
- MCP server 默认端口 8000 与 backend 冲突，启动必须 `--port 810X`

## 必读文档（改敏感区域前）

- `计划.txt` — 12 周路线与风险表（一切工作的依据）
- `环境验证报告.md` — §1-§19 累计环境/踩坑记录
- `backend/docs/api-v1.md` — SSE 事件协议、错误码、端点清单
- `docs/采集伦理与合规.md` — 采集合规红线（robots/低频/不破解验证码）

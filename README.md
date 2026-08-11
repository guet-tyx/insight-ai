# Insight AI

基于 **LangGraph 多智能体协作** 与 **MCP 插件化架构** 的生产级情报分析平台。

用户输入复合型情报分析指令 → Supervisor Agent 任务拆解 → Collector（Browser Use 动态采集）/ Research（GraphRAG 混合检索）/ Analyst（报告生成）专家子图协作 → HITL 人机审核 → 输出结构化情报分析报告。

## 技术栈

| 层 | 技术 |
|---|---|
| 多智能体编排 | LangGraph（Supervisor-Worker 拓扑、Subgraph 状态隔离、interrupt() HITL） |
| 混合检索 (GraphRAG) | Milvus（HNSW 密集向量）+ Neo4j（Cypher 拓扑图谱）+ RRF 融合 |
| 文档解析 | PyMuPDF 版面感知解析 + 语义分块 |
| 动态网页采集 | browser-use + Playwright（会话池、stealth CDP 指纹对抗、验证码人工接管） |
| 工具服务化 | FastMCP 2.0（Streamable HTTP，4 独立 Server + 热插拔注册中心） |
| 后端 / 前端 | FastAPI（异步）+ Next.js 14 + Tailwind CSS（SSE 流式） |
| 可观测性 | 本地 Trace 面板（/system/trace/ui）+ LangSmith 可选双轨 |
| 云原生 | Docker Compose 全栈编排、GitHub Actions CI（lint/test/镜像） |

## 快速开始

### 方式一：Docker 一键全栈（推荐）

```bash
# 1. 准备环境变量（填入 SILICONFLOW_API_KEY / OPENAI_API_KEY）
cp .env.example .env

# 2. 构建并启动全栈（基础设施 + backend + frontend + 4×MCP 工具服务）
docker compose up -d --build
#    http://localhost:3000   前端
#    http://localhost:8000/docs  API 文档
#    http://localhost:8000/api/v1/system/trace/ui  Trace 诊断面板
#    http://localhost:7474    Neo4j 控制台（neo4j/insightai-neo4j）
```

### 方式二：本地开发

```bash
# 1. 基础设施（Milvus / Neo4j / Redis）
docker compose up -d etcd minio milvus-standalone neo4j-graph redis-state-store

# 2. MCP 工具服务（backend/ 目录下，4 个终端）
uv run python ../mcp_servers/browser_mcp.py --port 8101   # 8102/8103/8104 同理

# 3. 后端
uv run uvicorn app.main:app --reload --port 8000

# 4. 前端
cd frontend && pnpm dev
```

> ⚠️ Windows 本地 `pnpm build` 需要开发者模式（Next.js standalone 输出创建 symlink）；
> Docker/CI（Linux）无此限制。API 地址由 `NEXT_PUBLIC_API_BASE` 控制（默认 `http://localhost:8000/api/v1`）。

## 测试与质量

```bash
cd backend
uv run ruff check app ../mcp_servers   # lint（CI 门禁）
uv run pytest -q -m "not flaky"        # 单测（真实 LLM/浏览器用例标 flaky 独立运行）
uv run pytest --cov=app                # 覆盖率（门槛 ≥80%，pyproject 固化）
uv run python scripts/bench_retrieval.py --qps 1,5,10,20   # 检索压测
uv run python scripts/verify_neo4j_indexes.py              # Neo4j 索引 EXPLAIN 验证
```

## Trace 诊断面板

`GET /system/trace/ui`（自包含 HTML，无前端依赖）：事件统计 / 工具失败率 / 延迟分位（P50/P95/P99）/ Guardrail 幻觉信号 / 最近事件。首帧服务端渲染，登录态下每 3s 自动刷新；配置 `LANGCHAIN_API_KEY` + `LANGCHAIN_TRACING_V2=true` 可启用 LangSmith 全链路双轨。

## CI/CD

`.github/workflows/ci.yml`：push/PR → main 触发
- **backend**：ruff lint → 起基础设施容器 → pytest（非 flaky，cov≥80%）
- **frontend**：pnpm lint + build（standalone）
- **docker**：multi-stage 构建 backend/frontend 镜像

## 部署（腾讯云 CloudBase）

镜像已按多阶段构建（`backend/Dockerfile`、`frontend/Dockerfile`，防镜像过大）。
CloudBase 部署步骤待相应工具链接入后按同一镜像执行（见 `环境验证报告.md` §20）。

## 目录结构

```
insight-ai/
├─ backend/            # FastAPI 主后端 + LangGraph 智能体
│  ├─ app/agents/      #   图/节点（supervisor / workers / human_review）
│  ├─ app/services/    #   检索/采集/图谱/MCP 注册/Trace 业务层
│  ├─ app/api/v1/      #   路由（chat/knowledge/agents/system/collect）
│  ├─ tests/           #   pytest（176 用例 + 13 flaky 集成）
│  └─ scripts/         #   压测 / 索引验证脚本
├─ frontend/           # Next.js 14 聊天界面（SSE 流式）
├─ mcp_servers/        # FastMCP 工具集群（browser/vector/graph/system）
├─ .github/workflows/  # CI（lint / test / 镜像构建）
└─ docker-compose.yml  # 全栈编排（5 基础设施 + 6 应用容器）
```

> 完整文档：`环境验证报告.md`（§1-§20 累计工程记录）；API 协议：`backend/docs/api-v1.md`；
> 十二周实施路线：`计划.txt`；采集合规红线：`docs/采集伦理与合规.md`。

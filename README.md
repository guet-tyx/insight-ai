# Insight AI

基于 **LangGraph 多智能体协作** 与 **MCP 插件化架构** 的生产级情报分析平台。

用户输入复合型情报分析指令 → Supervisor Agent 任务拆解 → Collector（Browser Use 动态采集）/ Research（GraphRAG 混合检索）/ Analyst（报告生成）专家子图协作 → HITL 人机审核 → 输出结构化情报分析报告。

## 技术栈

| 层 | 技术 |
|---|---|
| 多智能体编排 | LangGraph（Supervisor-Worker 拓扑、Subgraph 状态隔离、interrupt() HITL） |
| 混合检索 (GraphRAG) | Milvus（HNSW 密集向量）+ Neo4j（Cypher 拓扑图谱）+ RRF 融合 |
| 文档解析 | PyMuPDF 版面感知解析 + 语义分块 |
| 动态网页采集 | browser-use + Playwright（会话池、防检测） |
| 工具服务化 | FastMCP 2.0（Streamable HTTP / SSE，热插拔注册中心） |
| 后端 / 前端 | FastAPI（异步 Serverless）+ Next.js 14 + Tailwind CSS（SSE 流式） |
| 云原生 | Docker Compose、GitHub Actions CI/CD、LangSmith 可观测 |

## 快速开始

```bash
# 1. 基础设施（Milvus / Neo4j / Redis）
docker compose up -d

# 2. 后端（backend/）
cp ../.env.example ../.env   # 填入 SILICONFLOW_API_KEY 等
uv run uvicorn app.main:app --reload --port 8000   # http://localhost:8000/docs

# 3. 前端（frontend/）
pnpm dev                     # http://localhost:3000
```

## 目录结构

```
insight-ai/
├─ backend/       # FastAPI 主后端 + LangGraph 智能体
├─ frontend/      # Next.js 14 聊天界面
├─ mcp_servers/   # FastMCP 工具服务集群（Browser/Vector/Graph）
└─ docker-compose.yml  # 基础设施编排
```

> 架构图与完整文档见 `环境验证报告.md`；十二周实施路线见 `计划.txt`。
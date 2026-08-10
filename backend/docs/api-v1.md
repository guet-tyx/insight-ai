# Insight AI API 路由设计（v1）

基础路径：`http://localhost:8000/api/v1`；交互文档：`/docs`（Swagger UI），规范文件：`/openapi.json`（FastAPI 自动生成）。

## 认证机制

- 注册/登录后签发 **JWT（HS256，默认 24h 过期）**，请求头 `Authorization: Bearer <token>` 访问受保护接口
- `/docs` 右上角 Authorize 按钮填入 token 即可在文档中直接调试

## 路由总览

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| GET | `/health` | ✅ W1 | 存活探针（容器编排健康检查用） |
| POST | `/auth/register` | ✅ W1 | 注册：`{username, password}` → 201 用户信息；409 重名；422 校验失败 |
| POST | `/auth/login` | ✅ W1 | OAuth2 密码流（表单）→ `{access_token, token_type, user}`；401 凭证错误 |
| GET | `/auth/me` | ✅ W1 | 当前用户信息（Bearer）；401 未认证/过期 |
| GET | `/knowledge/documents` | 🚧 W2 | 文档库列表 |
| POST | `/knowledge/documents/upload` | 🚧 W2 | PDF 上传 → PyMuPDF 解析 → Milvus 向量化 |
| POST | `/knowledge/query` | 🚧 W2 | 向量检索问答（GraphRAG 基础版） |
| POST | `/chat/sessions` | 🚧 W4 | 创建对话会话 |
| POST | `/chat/sessions/{id}/messages` | 🚧 W4 | 发消息，SSE 流式返回 |
| POST | `/agents/runs` | 🚧 W5 | 启动多智能体复合分析任务 |
| POST | `/agents/runs/{id}/review` | 🚧 W8 | HITL 人工审核（interrupt 恢复） |

> 🚧 = 占位路由，当前返回 501；对应周次实现后替换。

## 错误码约定

| 状态码 | 含义 |
|---|---|
| 200 / 201 | 成功 |
| 401 | 未认证 / 凭据错误（响应含 `WWW-Authenticate: Bearer`） |
| 404 | 资源不存在 |
| 409 | 资源冲突（如用户名已存在） |
| 422 | 请求体校验失败（Pydantic 自动生成错误明细） |
| 501 | 路由已规划未实现（占位） |

## 安全与规范

- 密码存储：**Argon2**（pwdlib，自动加盐），任何响应不泄露哈希
- JWT：HS256，`sub`=用户 ID，含 `iat`/`exp`；`JWT_SECRET` 来自 `.env`
- 数据模型：SQLAlchemy 2.0 声明式 + SQLite（MVP）→ W1 后可按需切 Postgres（仅改 `DATABASE_URL`）
- 用户模型字段：`id / username(3-32位字母数字下划线) / hashed_password / created_at`

## 前端对接要点（W4 备忘）

- SenseNova 网关（`LLM_BASE_URL=https://token.sensenova.cn/v1`，`LLM_MODEL=deepseek-v4-flash`）为 OpenAI 兼容协议，tool calling / SSE 均可用（2026-08-10 实测）
- ⚠️ 流式响应包含 `reasoning_content` 字段（思维链增量），且存在空 `choices` 的尾部块 —— 解析时必须跳过空 choices、可按需展示/隐藏思考过程
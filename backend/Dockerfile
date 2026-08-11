# syntax=docker/dockerfile:1
# Insight AI 后端镜像（多阶段，防镜像过大——W11 风险表）
# 同时作为 mcp_servers 4 个 MCP Server 的基础镜像（compose 覆盖 command）
#
# 结构：/app/backend（FastAPI + app/）+ /app/mcp_servers（4 个 FastMCP server）
# mcp_servers 的 sys.path.insert(../backend) 相对路径在容器内保持成立。
#
# 国内网络适配：pip/uv 走清华镜像、Playwright 走 npmmirror 二进制镜像；
# 基础镜像支持 DOCKER_BASE_PY 覆盖（BuildKit 不读 daemon registry-mirrors，
# 本地可指到 docker.1ms.run 等加速镜像；CI 默认官方 python:3.12-slim）

ARG BASE_PY=python:3.12-slim

# ---- 依赖阶段：安装全部 Python 依赖 + 下载 Playwright Chromium ----
FROM ${BASE_PY} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN pip install --no-cache-dir uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
# Playwright Chromium 二进制（供 browser_mcp/collector 浏览器采集；系统库在 runtime 装）
RUN uv run playwright install chromium

# ---- 运行阶段：最小运行时 + 源码 + chromium 系统依赖 ----
FROM ${BASE_PY} AS runtime
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# curl 供 healthcheck；系统库供 Chromium 运行
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=deps /app/backend/.venv /app/backend/.venv
COPY --from=deps /root/.cache/ms-playwright /ms-playwright
RUN /app/backend/.venv/bin/python -m playwright install-deps chromium
COPY backend/ /app/backend/
COPY mcp_servers/ /app/mcp_servers/
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/api/v1/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

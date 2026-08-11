# syntax=docker/dockerfile:1
# Insight AI 前端镜像（Next.js 14 standalone，多阶段——W11 风险表）
# build 阶段注入 NEXT_PUBLIC_API_BASE（浏览器端 API 地址）；standalone 输出减小体积
# 国内网络适配：npm registry 走 npmmirror；基础镜像支持 DOCKER_BASE_NODE 覆盖
#（BuildKit 不读 daemon registry-mirrors，本地可指到 docker.1ms.run 等加速镜像）

ARG BASE_NODE=node:20-alpine

# ---- 依赖阶段 ----
FROM ${BASE_NODE} AS deps
RUN corepack enable && corepack prepare pnpm@9 --activate \
    && npm config set registry https://registry.npmmirror.com
WORKDIR /app
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# ---- 构建阶段 ----
FROM ${BASE_NODE} AS build
RUN corepack enable && corepack prepare pnpm@9 --activate \
    && npm config set registry https://registry.npmmirror.com
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN pnpm build

# ---- 运行阶段（standalone）----
FROM ${BASE_NODE} AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]

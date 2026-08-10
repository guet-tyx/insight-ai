"""应用配置：从环境变量 / 项目根 .env 加载（pydantic-settings）。

.env 位于项目根目录（backend/ 的上一级）；find_dotenv 从本文件位置
（backend/app/core/）向上查找，无论从 backend/ 还是项目根启动都能读到。
已有环境变量优先（load_dotenv 默认不覆盖）。
"""
from __future__ import annotations

from functools import lru_cache

from dotenv import find_dotenv, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# 从当前文件位置向上查找项目根的 .env；不覆盖已存在的环境变量
_ = load_dotenv(find_dotenv())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- JWT 认证 ----
    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24h

    # ---- 数据库 ----
    database_url: str = "sqlite:///./insightai.db"
    # 检查点后端：redis（默认，AsyncRedisSaver 持久化）| memory（测试/降级）
    checkpointer_backend: str = "redis"

    # ---- 基础设施（后续周次使用）----
    milvus_uri: str = "http://127.0.0.1:19530"
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "insightai-neo4j"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---- LLM 推理（SenseNova 网关，OpenAI 兼容协议）----
    openai_api_key: str = ""  # OPENAI_API_KEY：网关 Token，供 ChatOpenAI 使用
    llm_base_url: str = "https://token.sensenova.cn/v1"
    llm_model: str = "deepseek-v4-flash"  # 主模型：复杂推理/分析/浏览器操作
    llm_model_lite: str = "sensenova-6.7-flash-lite"  # 轻量模型：路由决策等低成本任务

    # ---- Embedding（硅基流动，OpenAI 兼容协议）----
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # ---- 浏览器采集（W3）----
    browser_proxy_list: str = ""  # 代理池，逗号分隔；空 = 直连（本机开发默认）
    collector_allow_internal: bool = False  # SSRF 防护；仅本地演示/测试时置 true
    collector_max_proxy_retries: int = 2  # W6：连接类失败自动换代理重试轮数（0=关闭）

    app_name: str = "Insight AI"
    app_version: str = "0.1.0"

    @property
    def database_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """缓存 Settings 单例（uvicorn --reload 每次进程重启重新加载）。"""
    return Settings()


settings = get_settings()
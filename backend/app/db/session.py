"""SQLAlchemy 引擎与会话管理（默认 SQLite 文件库，零外部依赖）。

后续周次可无缝切换 Postgres：仅修改 Settings.database_url。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


# SQLite 需要关闭跨线程检查（FastAPI 线程池访问同一连接）
_connect_args = {"check_same_thread": False} if settings.database_is_sqlite else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

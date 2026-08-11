"""安全原语：Argon2 密码哈希 + HS256 JWT 签发/校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

PASSWORD_HASH = PasswordHash.recommended()


def hash_password(plain: str) -> str:
    """对明文密码进行 Argon2 哈希（自动加盐）。"""
    return PASSWORD_HASH.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希是否匹配。"""
    return PASSWORD_HASH.verify(plain, hashed)


def create_access_token(subject: str | int) -> str:
    """签发 HS256 JWT，subject 为用户唯一标识，默认 24h 过期。"""
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(subject), "exp": expire, "iat": datetime.now(UTC)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """校验 JWT 并返回用户标识（sub）；无效/过期抛出 jwt.PyJWTError。"""
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("token missing 'sub' claim")
    return sub

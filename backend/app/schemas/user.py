"""用户相关的 Pydantic Schema（请求/响应模型）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

USERNAME_PATTERN = r"^[a-zA-Z0-9_]{3,32}$"


class RegisterRequest(BaseModel):
    username: str = Field(description="用户名，3-32 位字母/数字/下划线", pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128, description="密码，至少 8 位")

    @field_validator("password")
    @classmethod
    def password_not_blank(cls, v: str) -> str:
        if v.strip() != v or not v.strip():
            raise ValueError("密码不能包含首尾空白")
        return v


class UserOut(BaseModel):
    """对外暴露的用户信息（绝不包含密码哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
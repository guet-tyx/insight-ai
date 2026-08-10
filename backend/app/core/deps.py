"""公共依赖：OAuth2 方案与当前用户解析（供各路由复用）。"""
from __future__ import annotations

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

# tokenUrl 指向登录接口，/docs 的 Authorize 按钮自动填入 token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """依赖：解析 Bearer Token 并加载当前用户；无效则 401。"""
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或过期的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        subject = decode_access_token(token)
    except pyjwt.PyJWTError:
        raise credentials_exc
    user = db.get(User, int(subject))
    if user is None:
        raise credentials_exc
    return user
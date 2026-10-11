"""鉴权依赖（T1.4）：除 login/bootstrap 外全部 API 要求会话。"""
from __future__ import annotations

from fastapi import Depends, Request, Response

from app.core.errors import AuthError
from app.core.sessions import SESSION_TIMEOUT_SECONDS, AuthService


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth


def get_current_actor(request: Request, response: Response,
                      auth: AuthService = Depends(get_auth_service)) -> dict:
    token = request.cookies.get("dsync_session")
    user = auth.me(token)  # 未登录抛 401
    # P2 修复：服务端滑动续期同步浏览器 Cookie（修复前 max_age 固定 2h，
    # 活跃用户仍会被浏览器按固定有效期删 Cookie 踢下线）
    response.set_cookie("dsync_session", token, httponly=True, samesite="lax",
                        max_age=SESSION_TIMEOUT_SECONDS)
    return {"username": user["username"], "ip": request.client.host if request.client else None,
            "token": token}

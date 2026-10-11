"""认证路由（T1.4 / E14）：bootstrap 一次性 + 登录登出 + 审计。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_auth_service, get_current_actor
from app.core.sessions import AuthService
from app.schemas.api import BootstrapReq, LoginReq

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "dsync_session"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/status")
def status(request: Request, auth: AuthService = Depends(get_auth_service)):
    """前端首屏探测：是否已初始化、当前是否登录。"""
    return {"initialized": auth.is_initialized(),
            "authenticated": auth.sessions.validate(request.cookies.get(COOKIE)) is not None}


@router.post("/bootstrap")
def bootstrap(body: BootstrapReq, request: Request, response: Response,
              auth: AuthService = Depends(get_auth_service)):
    ip = _client_ip(request)
    auth.bootstrap(body.username, body.password)
    request.app.state.audit.record("system", "auth.bootstrap", ip=ip, object_type="app")
    token = auth.login(body.username, body.password, ip)
    request.app.state.audit.record(body.username, "auth.login", ip=ip)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=2 * 60 * 60)
    return {"ok": True, "username": body.username}


@router.post("/login")
def login(body: LoginReq, request: Request, response: Response,
          auth: AuthService = Depends(get_auth_service)):
    ip = _client_ip(request)
    token = auth.login(body.username, body.password, ip)
    request.app.state.audit.record(body.username, "auth.login", ip=ip)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=2 * 60 * 60)
    return {"ok": True, "username": body.username}


@router.post("/logout")
def logout(request: Request, response: Response,
          auth: AuthService = Depends(get_auth_service)):
    token = request.cookies.get(COOKIE)
    user = auth.sessions.validate(token)
    if user:
        request.app.state.audit.record(user["username"], "auth.logout", ip=_client_ip(request))
    auth.sessions.revoke(token)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(actor: dict = Depends(get_current_actor)):
    return {"username": actor["username"]}

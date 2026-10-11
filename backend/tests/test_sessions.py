"""T1.10 单元：会话超时、登录锁定策略、bootstrap 一次性。"""
from __future__ import annotations

import pytest

from app.core.errors import AppError, AuthError, ConflictError
from app.core.sessions import AuthService, LoginGuard, SessionStore


@pytest.fixture()
def auth(repos):
    s = AuthService(repos["settings"], SessionStore(), LoginGuard())
    return s


def test_bootstrap_then_rejected(auth):
    auth.bootstrap("admin", "strong-pass-1")
    with pytest.raises(ConflictError):
        auth.bootstrap("admin", "another-pass")


def test_bootstrap_weak_password(auth):
    with pytest.raises(AppError):
        auth.bootstrap("admin", "short")


def test_login_ok_and_me(auth):
    auth.bootstrap("admin", "strong-pass-1")
    token = auth.login("admin", "strong-pass-1", "127.0.0.1")
    assert auth.me(token)["username"] == "admin"
    assert auth.sessions.validate(token)["username"] == "admin"


def test_login_wrong_password(auth):
    auth.bootstrap("admin", "strong-pass-1")
    with pytest.raises(AuthError):
        auth.login("admin", "wrong-pass", "127.0.0.1")


def test_session_expiry(auth):
    auth.bootstrap("admin", "strong-pass-1")
    auth.sessions.timeout = 0  # 立即过期
    token = auth.login("admin", "strong-pass-1", None)
    assert auth.sessions.validate(token) is None
    with pytest.raises(AuthError):
        auth.me(token)


def test_lockout_after_5_failures(auth):
    auth.bootstrap("admin", "strong-pass-1")
    for _ in range(5):
        with pytest.raises(AuthError):
            auth.login("admin", "bad", "10.0.0.9")
    # 第 6 次：即使密码正确也被锁定
    with pytest.raises(AuthError) as ei:
        auth.login("admin", "strong-pass-1", "10.0.0.9")
    assert ei.value.code == "LOGIN_LOCKED"
    # 其他 IP 不受影响
    token = auth.login("admin", "strong-pass-1", "10.0.0.10")
    assert token

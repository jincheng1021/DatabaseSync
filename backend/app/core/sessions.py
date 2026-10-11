"""会话与认证状态管理（T1.4 / §12）。

- 首启 bootstrap 一次性；argon2 哈希存储
- 会话超时 2 小时（滑动续期）
- 登录失败 5 次锁定 15 分钟
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import (BOOTSTRAP_ADMIN_USERNAME, LOGIN_LOCKOUT_SECONDS,
                        LOGIN_MAX_FAILURES, SESSION_TIMEOUT_SECONDS)
from app.core.errors import AuthError, ConflictError
from app.models.repository import SettingRepository

_ph = PasswordHasher()


class SessionStore:
    """内存会话表（单管理员场景足够；重启即失效，符合内网工具定位）。"""

    def __init__(self, timeout: int = SESSION_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {"username": username, "expires": time.time() + self.timeout}
        return token

    def validate(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if time.time() > s["expires"]:
                del self._sessions[token]
                return None
            s["expires"] = time.time() + self.timeout  # 滑动续期
            return dict(s)

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)


class LoginGuard:
    """失败 5 次锁定 15 分钟（§12）。key = 用户名+IP。"""

    def __init__(self):
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, ip: str | None) -> str:
        return f"{username}@{ip or '-'}"

    def check_locked(self, username: str, ip: str | None) -> None:
        with self._lock:
            fails = [t for t in self._failures.get(self._key(username, ip), []) if time.time() - t < LOGIN_LOCKOUT_SECONDS]
            self._failures[self._key(username, ip)] = fails
            if len(fails) >= LOGIN_MAX_FAILURES:
                remain = int(LOGIN_LOCKOUT_SECONDS - (time.time() - fails[-1]))
                raise AuthError(
                    f"登录失败次数过多，账号已锁定，请 {remain // 60 + 1} 分钟后重试",
                    code="LOGIN_LOCKED",
                    hint=f"连续失败 {LOGIN_MAX_FAILURES} 次将锁定 {LOGIN_LOCKOUT_SECONDS // 60} 分钟")

    def record_failure(self, username: str, ip: str | None) -> None:
        with self._lock:
            # P2 修复：失败表按 key 上限清理（修复前无上限累积，伪造用户名可灌爆内存）
            if len(self._failures) >= 10000:
                cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
                self._failures = {k: v for k, v in self._failures.items()
                                  if v and v[-1] > cutoff}
                if len(self._failures) >= 10000:
                    self._failures.clear()   # 极端兜底：锁定期内的全清，宁可少锁不可 OOM
            self._failures.setdefault(self._key(username, ip), []).append(time.time())

    def clear(self, username: str, ip: str | None) -> None:
        with self._lock:
            self._failures.pop(self._key(username, ip), None)


class AuthService:
    def __init__(self, settings: SettingRepository, sessions: SessionStore, guard: LoginGuard):
        self.settings = settings
        self.sessions = sessions
        self.guard = guard

    # ---- 首启状态 ----
    def is_initialized(self) -> bool:
        return self.settings.get("admin_password_hash") is not None

    def bootstrap(self, username: str, password: str) -> None:
        if self.is_initialized():
            raise ConflictError("管理员已初始化，bootstrap 仅可使用一次",
                                code="BOOTSTRAP_DONE", hint="请直接登录")
        if len(password) < 8:
            raise AuthError("密码长度至少 8 位", code="WEAK_PASSWORD")
        self.settings.set("admin_username", username or BOOTSTRAP_ADMIN_USERNAME)
        self.settings.set("admin_password_hash", _ph.hash(password))

    def login(self, username: str, password: str, ip: str | None) -> str:
        self.guard.check_locked(username, ip)
        stored_name = self.settings.get("admin_username") or BOOTSTRAP_ADMIN_USERNAME
        pwhash = self.settings.get("admin_password_hash")
        if not pwhash or username != stored_name:
            self.guard.record_failure(username, ip)
            raise AuthError("用户名或密码错误", code="BAD_CREDENTIALS")
        try:
            _ph.verify(pwhash, password)
        except VerifyMismatchError:
            self.guard.record_failure(username, ip)
            raise AuthError("用户名或密码错误", code="BAD_CREDENTIALS") from None
        self.guard.clear(username, ip)
        return self.sessions.create(username)

    def me(self, token: str | None) -> dict:
        s = self.sessions.validate(token)
        if not s:
            raise AuthError()
        return {"username": s["username"]}

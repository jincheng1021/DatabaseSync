"""统一错误结构（§5.1 约定）：错误码 + 用户可读消息 + 修复指引。"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务异常基类。HTTP 响应统一为 {error:{code,message,hint}}。"""

    status_code = 400
    code = "APP_ERROR"
    message = "请求处理失败"
    hint: str | None = None

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 hint: str | None = None, status_code: int | None = None):
        if message:
            self.message = message
        if code:
            self.code = code
        if hint is not None:
            self.hint = hint
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)

    def to_payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


class AuthError(AppError):
    status_code = 401
    code = "AUTH_REQUIRED"
    message = "未登录或会话已过期"
    hint = "请重新登录"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "无权执行该操作"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "资源不存在"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "资源状态冲突"


class ValidationError(AppError):
    status_code = 422
    code = "VALIDATION_FAILED"
    message = "参数校验失败"


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 不向外泄漏内部细节；日志中记录完整堆栈
    import logging
    logging.getLogger("dsync").exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR",
                           "message": "服务器内部错误",
                           "hint": "请查看服务端日志 logs/ 中的完整堆栈定位问题"}},
    )

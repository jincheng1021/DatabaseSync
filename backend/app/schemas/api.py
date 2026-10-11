"""请求/响应结构定义（schemas 层，T1.1）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BootstrapReq(BaseModel):
    username: str = Field(default="admin", max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginReq(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class DatasourceReq(BaseModel):
    name: str = Field(max_length=128)
    db_type: str = Field(default="mysql")
    host: str = Field(max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    username: str = Field(max_length=128)
    password: str | None = Field(default=None, max_length=256)
    ssl_mode: str = Field(default="disabled")
    ssl_ca: str | None = None
    ssl_cert: str | None = None
    ssl_key: str | None = None
    default_schema: str | None = Field(default=None, max_length=64)
    charset: str = Field(default="utf8mb4", max_length=32)
    connect_timeout: int = Field(default=10, ge=1, le=60)
    note: str | None = None


class DatasourceUpdateReq(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = None      # None/空 = 不变更
    ssl_mode: str | None = None
    ssl_ca: str | None = None
    ssl_cert: str | None = None
    ssl_key: str | None = None
    default_schema: str | None = None
    charset: str | None = None
    connect_timeout: int | None = Field(default=None, ge=1, le=60)
    note: str | None = None


class PrecheckReq(BaseModel):
    role: str = Field(default="both")  # source | target | both


class DDLDecisionReq(BaseModel):
    decision: str  # execute | skip
    remember: bool = False

"""系统设置（T1.9）+ M4 告警通道配置/测试（T4.3）+ 审计与告警日志查询路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_actor
from app.config import GLOBAL_DEFAULTS, KEYS_DIR
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api", tags=["settings"])

SMTP_KEYS = ("host", "port", "username", "password", "use_tls", "use_ssl", "from", "to")


class AlertChannelsReq(BaseModel):
    webhook_url: str | None = Field(default=None, max_length=1024)
    smtp: dict | None = None        # {host, port, username, password, use_tls/use_ssl, from, to}
    silence_minutes: int | None = Field(default=None, ge=1, le=10080)


@router.get("/settings")
def get_settings(request: Request, actor: dict = Depends(get_current_actor)):
    repo = request.app.state.settings
    values = {k: repo.get(k) for k in GLOBAL_DEFAULTS}
    mk = request.app.state.key_manager
    smtp = repo.get("alert_smtp_json")
    smtp_masked = None
    if smtp:
        try:
            conf = json.loads(smtp) if isinstance(smtp, str) else dict(smtp)
            if conf.get("password"):
                conf["password"] = "******"
            smtp_masked = conf
        except (TypeError, ValueError):
            smtp_masked = None
    return {
        "global_defaults": values,
        "master_key": {"fingerprint": mk.fingerprint(), "path": str(KEYS_DIR),
                       "hint": "主密钥与数据一并备份，丢失将无法解密已存凭据"},
        "channels": {"webhook": bool(repo.get("alert_webhook_url")),
                     "smtp": bool(repo.get("alert_smtp_json"))},
        # P2 修复：webhook URL 通常含机器人 token，GET 一律掩码（提交 ****** = 不变更）
        "alert_channels": {"webhook_url": ("******"
                                           if repo.get("alert_webhook_url") else None),
                           "smtp": smtp_masked,
                           "silence_minutes": repo.get("alert_silence_minutes")},
    }


@router.put("/settings/alert-channels")
def set_alert_channels(body: AlertChannelsReq, request: Request,
                       actor: dict = Depends(get_current_actor)):
    """T4.3：告警通道配置（Webhook URL / SMTP / 静默期）。"""
    s = request.app.state
    # P2：掩码值 = 不变更（对齐 SMTP 密码掩码语义）
    if body.webhook_url is not None and body.webhook_url != "******":
        s.settings.set("alert_webhook_url", body.webhook_url.strip() or None)
    if body.silence_minutes is not None:
        s.settings.set("alert_silence_minutes", body.silence_minutes)
    if body.smtp is not None:
        conf = {k: v for k, v in body.smtp.items() if k in SMTP_KEYS and v not in (None, "")}
        if conf.get("password") == "******":
            conf.pop("password")   # 掩码值 = 不变更
        if conf:
            old = s.settings.get("alert_smtp_json")
            if old and "password" not in conf:
                try:
                    old_conf = json.loads(old) if isinstance(old, str) else old
                    if old_conf.get("password"):
                        conf["password"] = old_conf["password"]
                except (TypeError, ValueError):
                    pass
            # T5.5 安全自查：SMTP 密码不得明文落库（AES-256-GCM，enc:v1: 前缀标识密文）
            if conf.get("password") and not str(conf["password"]).startswith("enc:v1:"):
                conf["password"] = "enc:v1:" + s.cipher.encrypt(str(conf["password"]))
            s.settings.set("alert_smtp_json", json.dumps(conf, ensure_ascii=False))
        else:
            s.settings.set("alert_smtp_json", None)
    s.audit.record(actor["username"], "settings.alert_channels", ip=actor["ip"],
                   after={"webhook": bool(body.webhook_url), "smtp": bool(body.smtp)})
    return {"ok": True}


@router.post("/settings/alert-test")
def alert_test(request: Request, actor: dict = Depends(get_current_actor)):
    """T4.3 验收：测试发送成功。"""
    results = request.app.state.alert_repo.test_channels()
    request.app.state.audit.record(actor["username"], "settings.alert_test",
                                   ip=actor["ip"], after=results)
    return {"results": results}


@router.get("/alerts")
def alert_logs(request: Request, limit: int = 50,
               actor: dict = Depends(get_current_actor)):
    return {"items": request.app.state.alert_repo.list_recent(min(limit, 200))}


class AlertResolveReq(BaseModel):
    alert_key: str = Field(min_length=1, max_length=200)


@router.post("/alerts/resolve")
def alert_resolve(body: AlertResolveReq, request: Request,
                  actor: dict = Depends(get_current_actor)):
    """人工解除：按 alert_key 关闭所有未解除行（同名 key 重复告警一并处理）。"""
    resolved = request.app.state.alert_repo.resolve(body.alert_key)
    request.app.state.audit.record(actor["username"], "alert.resolve", ip=actor["ip"],
                                   before={"alert_key": body.alert_key},
                                   after={"resolved": resolved})
    if not resolved:
        raise NotFoundError("该告警无未解除记录")
    return {"ok": True}


@router.get("/audit-logs")
def audit_logs(request: Request, limit: int = 100,
               actor: dict = Depends(get_current_actor)):
    return {"items": request.app.state.audit.list_recent(min(limit, 500))}

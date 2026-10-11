"""告警服务（T4.3 / §4.6）：通道触达（Webhook + SMTP）+ 降噪 + WS 推送。

- 存储沿用 AlertRepository（alert_log，alert_key 去重 + 静默期降噪）。
- 真正触发（fire 返回 True）时派发通道：Webhook POST JSON（钉钉/企业微信机器人
  兼容 {"msgtype":"text","text":{"content":...}}）；SMTP 纯文本邮件（smtplib 标准库）。
- 通道结果落 channel_results_json；发送失败不影响告警落库（告警先保底存储）。
- WS：alert.fired / alert.resolved（线程侧经 emit_from_thread 投递）。
"""
from __future__ import annotations

import json
import logging
import smtplib
import threading
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx

from app.core.ws_broadcaster import emit_from_thread
from app.models.repository import AlertRepository, SettingRepository

log = logging.getLogger("dsync.alert")

# 告警项全集（§4.6）：各模块按 alert_key 埋点；此处登记展示名供前端/测试对齐
ALERT_ITEMS: dict[str, str] = {
    "task_failed": "任务失败/熔断",
    "lag_threshold": "延迟超阈值",
    "position_invalid": "位点失效",
    "ddl_pending": "DDL 待决策",
    "verify_mismatch": "校验不一致超标",
    "backup_failed": "备份失败",
    "disk_insufficient": "磁盘不足",
    "app_recovered": "应用重启恢复",
    "ftwrl_timeout": "FTWRL 取位点超时",
    "restore_failed": "备份恢复失败",
    "orphan_cleanup_failed": "孤儿行清理失败",
    "schedule_action_failed": "调度动作执行失败",
}


class AlertService:
    """app.state.alert_repo 的 M4 替身：接口与 AlertRepository 兼容（fire/resolve/list_recent）。"""

    def __init__(self, repo: AlertRepository, settings: SettingRepository, broadcaster=None,
                 cipher=None):
        self.repo = repo
        self.settings = settings
        self.broadcaster = broadcaster
        self.cipher = cipher  # T5.5：SMTP 密码密文解密（enc:v1: 前缀）

    # ---- 设置 ----
    def _silence_minutes(self) -> int:
        v = self.settings.get("alert_silence_minutes")
        return int(v) if v is not None else 30

    def _webhook_url(self) -> str | None:
        v = self.settings.get("alert_webhook_url")
        return str(v) if v else None

    def _smtp_conf(self) -> dict | None:
        v = self.settings.get("alert_smtp_json")
        if not v:
            return None
        try:
            conf = json.loads(v) if isinstance(v, str) else dict(v)
            # 密文解密（enc:v1: 前缀）；无前缀视为历史明文，原样使用
            pw = conf.get("password")
            if isinstance(pw, str) and pw.startswith("enc:v1:"):
                conf["password"] = (self.cipher.try_decrypt(pw[len("enc:v1:"):]) or "") \
                    if self.cipher else ""
            return conf or None
        except (TypeError, ValueError):
            return None

    # ---- 触达 ----
    def _send_webhook(self, message: str, level: str) -> dict:
        url = self._webhook_url()
        if not url:
            return {"channel": "webhook", "sent": False, "reason": "未配置"}
        try:
            resp = httpx.post(url, json={"msgtype": "text",
                                         "text": {"content": f"[数据库同步工具·{level}] {message}"}},
                              timeout=10)
            return {"channel": "webhook", "sent": resp.status_code == 200,
                    "status_code": resp.status_code}
        except Exception as e:  # noqa: BLE001 —— 触达失败不阻断告警
            return {"channel": "webhook", "sent": False, "reason": str(e)[:200]}

    def _send_smtp(self, message: str, level: str) -> dict:
        conf = self._smtp_conf()
        if not conf or not conf.get("host"):
            return {"channel": "smtp", "sent": False, "reason": "未配置"}
        try:
            msg = MIMEText(message, "plain", "utf-8")
            sender = conf.get("from") or conf.get("username", "")
            msg["Subject"] = Header(f"数据库同步工具告警（{level}）", "utf-8")
            msg["From"] = formataddr((str(Header("数据库同步工具", "utf-8")), sender))
            to = conf.get("to") or []
            msg["To"] = ", ".join(to) if isinstance(to, list) else str(to)
            if conf.get("use_ssl"):
                client = smtplib.SMTP_SSL(conf["host"], int(conf.get("port", 465)), timeout=15)
            else:
                client = smtplib.SMTP(conf["host"], int(conf.get("port", 25)), timeout=15)
            try:
                if conf.get("use_tls"):
                    client.starttls()
                if conf.get("username"):
                    client.login(conf["username"], conf.get("password") or "")
                client.sendmail(sender, [to] if isinstance(to, str) else to, msg.as_string())
            finally:
                client.quit()
            return {"channel": "smtp", "sent": True}
        except Exception as e:  # noqa: BLE001
            return {"channel": "smtp", "sent": False, "reason": str(e)[:200]}

    def _dispatch(self, alert_key: str, message: str, level: str, alert_id: int) -> list[dict]:
        results = [self._send_webhook(message, level), self._send_smtp(message, level)]
        if any(r.get("sent") for r in results):
            log.info("告警触达成功 key=%s id=%s %s", alert_key, alert_id, results)
        else:
            log.warning("告警触达失败 key=%s id=%s %s", alert_key, alert_id, results)
        self.repo.save_channel_results(alert_id, results)
        return results

    # ---- 兼容 AlertRepository 的对外接口 ----
    def fire(self, alert_key: str, message: str, *, level: str = "warning",
             task_id: int | None = None, silence_minutes: int | None = None) -> bool:
        """落库（静默期降噪）→ 通道触达（后台线程）→ WS alert.fired。"""
        fired = self.repo.fire(alert_key, message, level=level, task_id=task_id,
                               silence_minutes=self._silence_minutes() if silence_minutes is None
                               else silence_minutes)
        if not fired:
            return False
        recent = self.repo.list_recent(1)
        alert_id = int(recent[0]["id"]) if recent else 0
        if self.broadcaster:
            emit_from_thread(self.broadcaster, "alert.fired",
                             {"id": alert_id, "alert_key": alert_key, "level": level,
                              "task_id": task_id, "message": message}, task_id)
        threading.Thread(target=self._dispatch, args=(alert_key, message, level, alert_id),
                         daemon=True, name=f"alert-{alert_id}").start()
        return True

    def resolve(self, alert_key: str) -> bool:
        resolved = self.repo.resolve(alert_key)
        if resolved and self.broadcaster:
            emit_from_thread(self.broadcaster, "alert.resolved",
                             {"alert_key": alert_key, "message": f"告警解除：{alert_key}"})
        return resolved

    def list_recent(self, limit: int = 50) -> list[dict]:
        return self.repo.list_recent(limit)

    def count_unresolved(self) -> int:
        return self.repo.count_unresolved()

    # ---- 测试发送（T4.3 验收：测试发送成功） ----
    def test_channels(self) -> list[dict]:
        message = "这是一条测试告警：如果您收到此消息，说明告警通道配置正确。"
        return [self._send_webhook(message, "info"), self._send_smtp(message, "info")]

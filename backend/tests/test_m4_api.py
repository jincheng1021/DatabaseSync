"""M4 API 测试：调度 CRUD/预览、校验发起与报告、孤儿清理端点、告警通道配置、映射校验。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


@pytest.fixture()
def m4client(tmp_path, monkeypatch):
    import app.config as cfg
    from app.db.database import Database
    from app.models.m2_repository import (CdcRunRepository, DdlPendingRepository,
                                          PositionRepository, RunRepository, TaskRepository)
    from app.models.m4_repository import ScheduleRepository, VerifyResultRepository
    from app.models.repository import (AlertRepository, AuditRepository,
                                       DatasourceRepository, SettingRepository)

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "data" / "meta.db")
    monkeypatch.setattr(cfg, "KEYS_DIR", tmp_path / "data" / "keys")
    monkeypatch.setattr(cfg, "MASTER_KEY_PATH", tmp_path / "data" / "keys" / "master.key")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    for d in (tmp_path / "data", tmp_path / "data" / "keys", tmp_path / "logs",
              tmp_path / "backups"):
        d.mkdir(parents=True, exist_ok=True)

    from app.main import create_app
    app = create_app()
    app.state.db = Database(tmp_path / "meta.db")
    app.state.db.init_schema()
    db = app.state.db
    app.state.settings = SettingRepository(db)
    app.state.audit = AuditRepository(db)
    class FakeAlertService:
        def __init__(self, repo):
            self.repo = repo

        def fire(self, key, message, **kw):
            return True

        def resolve(self, key):
            return self.repo.resolve(key)   # 委托真实语义：关未解除行，无未解除返回 False

        def list_recent(self, limit=50):
            return []

        def test_channels(self):
            return [{"channel": "webhook", "sent": False, "reason": "未配置"},
                    {"channel": "smtp", "sent": False, "reason": "未配置"}]

    app.state.alert_repo = FakeAlertService(AlertRepository(db))
    app.state.task_repo = TaskRepository(db)
    app.state.run_repo = RunRepository(db)
    app.state.position_repo = PositionRepository(db)
    app.state.ddl_repo = DdlPendingRepository(db)
    app.state.lag_repo = CdcRunRepository(db)
    app.state.schedule_repo = ScheduleRepository(db)
    app.state.verify_repo = VerifyResultRepository(db)
    from app.core.orchestrator import Orchestrator
    app.state.orch = Orchestrator(app.state.task_repo, app.state.broadcaster)

    ds_repo = DatasourceRepository(db)
    src = ds_repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"})
    dst = ds_repo.create({"name": "dst", "host": "127.0.0.1", "port": 3307, "username": "u"})
    tid = app.state.task_repo.create({"name": "t", "source_id": src, "target_id": dst,
                                      "mode": "init_plus_cdc", "mapping": {"scope": "all"},
                                      "state": "idle"})

    # ---- 桩：验证/孤儿/调度器 ----
    class FakeVerify:
        def __init__(self):
            self.calls = []

        def start(self, task_id, *, method="checksum", trigger="manual", tables=None):
            self.calls.append((task_id, method, trigger))
            return 900 + len(self.calls)

        def repair(self, vid, tables=None, pks=None):
            return {"verify_id": vid, "replaced": 2, "deleted": 1, "skipped": 0}

    class FakeOrphan:
        def __init__(self):
            self.calls = []

        def start(self, task_id, *, trigger="manual"):
            self.calls.append((task_id, trigger))
            return 555

    class FakeScheduler:
        def __init__(self):
            self.reloaded = []
            self.triggered = []
            self.removed_schedules = []
            self.removed_tasks = []

        def reload_schedule(self, sid):
            self.reloaded.append(sid)

        def remove_schedule(self, sid):
            self.removed_schedules.append(sid)

        def trigger_now(self, sid):
            self.triggered.append(sid)
            return {"status": "accepted", "schedule_id": sid,
                    "task_id": 0, "action": "verify"}

        def remove_task_jobs(self, task_id):
            self.removed_tasks.append(task_id)

        def preview(self, cron, timezone="Asia/Shanghai", count=5):
            base = "2026-10-10T"
            return {"cron": cron, "timezone": timezone,
                    "next": [f"{base}09:{i:02d}:00+08:00" for i in range(count)]}

    app.state.verify_service = FakeVerify()
    app.state.orphan_service = FakeOrphan()
    app.state.scheduler = FakeScheduler()

    from app.core.security import MasterKeyManager
    from app.core.sessions import AuthService, LoginGuard, SessionStore
    app.state.key_manager = MasterKeyManager(tmp_path / "data" / "keys" / "master.key")
    app.state.sessions = SessionStore()
    app.state.guard = LoginGuard()
    app.state.auth = AuthService(app.state.settings, app.state.sessions, app.state.guard)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200, r.text
    # 返回 app.state 代理：测试用 st.xxx 直取装配对象
    return client, app.state, tid


# ---- 调度 CRUD / 预览 ----

def test_schedule_crud_and_reload(m4client):
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/schedules", json={
        "name": "每日校验", "action": "verify", "cron": "0 3 * * *",
        "action_params": {"method": "checksum"}})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert app.scheduler.reloaded == [sid]
    r = client.get(f"/api/tasks/{tid}/schedules")
    assert r.status_code == 200 and r.json()["schedules"][0]["action"] == "verify"
    r = client.put(f"/api/schedules/{sid}", json={"enabled": False, "cron": "30 4 * * *"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.delete(f"/api/schedules/{sid}")
    assert r.status_code == 204
    r = client.get(f"/api/tasks/{tid}/schedules")
    assert r.json()["schedules"] == []


def test_schedule_rejects_bad_cron_and_action(m4client):
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/schedules", json={
        "name": "x", "action": "verify", "cron": "bad"})
    assert r.status_code == 422
    r = client.post(f"/api/tasks/{tid}/schedules", json={
        "name": "x", "action": "format_disk", "cron": "0 2 * * *"})
    assert r.status_code == 422


def test_schedule_preview_endpoint(m4client):
    client, app, tid = m4client
    r = client.get("/api/schedules/preview", params={"cron": "0 9 * * *", "timezone": "UTC",
                                                     "count": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "UTC" and len(body["next"]) == 3


def test_schedule_trigger_endpoint(m4client):
    """立即触发：202 accepted + 调度器被调；不存在 sid 404；删除走 remove_schedule。"""
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/schedules", json={
        "name": "n", "action": "verify", "cron": "0 3 * * *"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = client.post(f"/api/schedules/{sid}/trigger")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "accepted" and r.json()["schedule_id"] == sid
    assert app.scheduler.triggered == [sid]
    r = client.post("/api/schedules/99999/trigger")
    assert r.status_code == 404
    # 停用计划仍允许手动触发
    client.put(f"/api/schedules/{sid}", json={"enabled": False})
    r = client.post(f"/api/schedules/{sid}/trigger")
    assert r.status_code == 202
    assert app.scheduler.triggered == [sid, sid]
    # 删除调度 → 走 remove_schedule（不再内联摸 _scheduler）
    client.delete(f"/api/schedules/{sid}")
    assert app.scheduler.removed_schedules == [sid]


def test_delete_task_removes_schedule_jobs(m4client):
    """删除任务：先取 sid 列表再删（CASCADE），随后清理调度器内存 job/队列。"""
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/schedules", json={
        "name": "n", "action": "cdc_start", "cron": "0 2 * * *"})
    assert r.status_code == 201, r.text
    r = client.delete(f"/api/tasks/{tid}")
    assert r.status_code == 204
    assert app.scheduler.removed_tasks == [tid]
    # schedule 行已随任务 CASCADE 删除
    r = client.get(f"/api/tasks/{tid}/schedules")
    assert r.status_code == 404   # 任务不存在
    r = client.get("/api/tasks")
    assert all(t["id"] != tid for t in r.json()["tasks"])


# ---- 校验 API ----

def test_verify_start_and_results(m4client):
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/verify", json={"method": "checksum"})
    assert r.status_code == 201, r.text
    assert app.verify_service.calls == [(tid, "checksum", "manual")]
    # 落一条成功报告后可查列表与详情
    vid = app.verify_repo.create(tid, method="checksum")
    app.verify_repo.finish(vid, state="success",
                           summary={"tables_checked": 2, "mismatch_tables": 0},
                           detail={"tables": {}})
    r = client.get("/api/verify-results", params={"task_id": tid})
    assert r.status_code == 200 and r.json()["total"] == 1
    r = client.get(f"/api/verify-results/{vid}")
    assert r.status_code == 200 and r.json()["state"] == "success"
    assert "detail" not in r.json()          # 列表/摘要不带行级 diff
    r = client.get(f"/api/verify-results/{vid}", params={"detail": "true"})
    assert r.json()["detail"] == {"tables": {}}


def test_verify_repair(m4client):
    client, app, tid = m4client
    vid = app.verify_repo.create(tid, method="checksum")
    r = client.post(f"/api/verify-results/{vid}/repair", json={"tables": ["t1"]})
    assert r.status_code == 200
    assert r.json() == {"verify_id": vid, "replaced": 2, "deleted": 1, "skipped": 0}


# ---- 孤儿清理端点 ----

def test_orphan_cleanup_endpoint(m4client):
    client, app, tid = m4client
    r = client.post(f"/api/tasks/{tid}/orphan-cleanup")
    assert r.status_code == 201, r.text
    assert r.json() == {"run_id": 555}
    assert app.orphan_service.calls == [(tid, "manual")]


# ---- 告警通道配置与测试 ----

def test_alert_channels_roundtrip_and_mask(m4client):
    client, app, tid = m4client
    r = client.put("/api/settings/alert-channels", json={
        "webhook_url": "https://example.com/hook",
        "smtp": {"host": "smtp.example.com", "port": 465, "username": "a@b.c",
                 "password": "pw123", "use_ssl": True, "from": "a@b.c",
                 "to": ["ops@b.c"]},
        "silence_minutes": 15})
    assert r.status_code == 200
    r = client.get("/api/settings")
    ch = r.json()["alert_channels"]
    assert ch["webhook_url"] == "******"   # P2：URL 含机器人 token，GET 掩码回显
    assert ch["silence_minutes"] == 15
    assert ch["smtp"]["password"] == "******"     # 掩码回显
    assert r.json()["channels"]["smtp"] is True
    # T5.5 安全自查：SMTP 密码必须以密文落库（enc:v1: 前缀），且可解密还原
    import json as _json
    saved = _json.loads(app.settings.get("alert_smtp_json"))
    cipher_text = saved["password"]
    assert cipher_text.startswith("enc:v1:")
    assert app.cipher.decrypt(cipher_text[len("enc:v1:"):]) == "pw123"
    # 掩码值提交 = 不变更（保留密文）
    r = client.put("/api/settings/alert-channels", json={
        "smtp": {"host": "smtp2.example.com", "password": "******"}})
    saved = _json.loads(app.settings.get("alert_smtp_json"))
    assert saved["host"] == "smtp2.example.com"
    assert saved["password"] == cipher_text


def test_alert_test_endpoint(m4client):
    client, app, tid = m4client
    r = client.post("/api/settings/alert-test")
    assert r.status_code == 200
    results = r.json()["results"]
    assert {x["channel"] for x in results} == {"webhook", "smtp"}


# ---- 映射高级特性（T4.5 API 面） ----

def test_task_mapping_update_validates_and_flags_subset(m4client):
    client, app, tid = m4client
    r = client.put(f"/api/tasks/{tid}", json={
        "mapping": {"row_filters": {"orders": {"col": "status", "op": "eq", "value": 1}}}})
    assert r.status_code == 200
    body = r.json()
    assert body["subset_sync"] is True
    assert any("子集" in w for w in body["mapping_warnings"])


def test_task_mapping_update_rejects_unsupported_predicate(m4client):
    client, app, tid = m4client
    r = client.put(f"/api/tasks/{tid}", json={
        "mapping": {"row_filters": {"orders": {"col": "name", "op": "like", "value": "%x%"}}}})
    assert r.status_code == 422
    assert "行过滤" in r.json()["error"]["message"]


def test_task_mapping_update_blocked_while_cdc_running(m4client):
    client, app, tid = m4client
    app.task_repo.update(tid, {"state": "cdc_running"})
    r = client.put(f"/api/tasks/{tid}", json={"mapping": {"scope": "all"}})
    assert r.status_code == 400          # 运行中禁止修改
    app.task_repo.update(tid, {"state": "paused"})
    r = client.put(f"/api/tasks/{tid}", json={"mapping": {"prefix": "bak_"}})
    assert r.status_code == 200          # 暂停态允许


def test_verify_repo_roundtrip(m4client):
    client, app, tid = m4client
    vid = app.verify_repo.create(tid, method="row_count", trigger="cron", tables_total=3)
    app.verify_repo.finish(vid, state="mismatch",
                           summary={"tables_checked": 3, "mismatch_tables": 1,
                                    "mismatch_rows": 5},
                           detail={"tables": {"a": {"status": "mismatch"}}})
    items, total = app.verify_repo.list(tid)
    assert total == 1 and items[0]["state"] == "mismatch"
    assert items[0]["summary"]["mismatch_rows"] == 5
    d = app.verify_repo.get(vid, with_detail=True)
    assert d["detail"]["tables"]["a"]["status"] == "mismatch"
    assert d["trigger"] == "cron"


def test_alert_webhook_mask_not_clobber(m4client):
    """P2 回归：webhook URL 掩码回显后，提交 ****** 不得覆盖真实 URL。"""
    client, app, tid = m4client
    client.put("/api/settings/alert-channels", json={"webhook_url": "https://qyapi.weixin.qq.com/hook?token=secret1"})
    r = client.get("/api/settings")
    assert r.json()["alert_channels"]["webhook_url"] == "******"
    client.put("/api/settings/alert-channels", json={"webhook_url": "******"})
    import json as _json
    assert app.settings.get("alert_webhook_url") == "https://qyapi.weixin.qq.com/hook?token=secret1"


# ---- 告警人工解除（POST /api/alerts/resolve） ----

def test_alert_resolve_closes_all_unresolved_of_key(m4client):
    """同 key 多条未解除告警一并关闭；审计落 alert.resolve。"""
    client, app, tid = m4client
    repo = app.alert_repo.repo
    repo.fire("cdc_target_write:9", "写失败1")
    repo.fire("cdc_target_write:9", "写失败2")
    repo.fire("cdc_lag:9", "延迟高")           # 其他 key 不受影响
    r = client.post("/api/alerts/resolve", json={"alert_key": "cdc_target_write:9"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    rows = {a["alert_key"]: a for a in repo.list_recent(10)}
    unresolved = [a for a in repo.list_recent(10)
                  if a["alert_key"] == "cdc_target_write:9" and not a["resolved_at"]]
    assert unresolved == []
    assert any(a["alert_key"] == "cdc_lag:9" and not a["resolved_at"]
               for a in repo.list_recent(10))
    # 审计记录
    audits = app.audit.list_recent(10)
    assert any(a["action"] == "alert.resolve" for a in audits)


def test_alert_resolve_no_unresolved_returns_404(m4client):
    """key 不存在或已全部解除 → 404。"""
    client, app, tid = m4client
    repo = app.alert_repo.repo
    repo.fire("cdc_parse:9", "解析失败")
    repo.resolve("cdc_parse:9")     # 先解除 → 再解除应 404
    r = client.post("/api/alerts/resolve", json={"alert_key": "cdc_parse:9"})
    assert r.status_code == 404
    r = client.post("/api/alerts/resolve", json={"alert_key": "not_exist:1"})
    assert r.status_code == 404


# ---- 异常态人工恢复（POST /api/tasks/{id}/recover） ----

def test_recover_error_task_to_idle(m4client):
    """error → idle：定时全量/仅备份/增量任务均可人工恢复；审计落 task.recover。"""
    client, app, tid = m4client
    t_cdc = app.task_repo.create({"name": "e1", "source_id": 1, "target_id": 2,
                                  "mode": "init_plus_cdc", "mapping": {"scope": "all"},
                                  "state": "error"})
    t_full = app.task_repo.create({"name": "e2", "source_id": 1, "target_id": 2,
                                   "mode": "scheduled_full", "mapping": {"scope": "all"},
                                   "state": "error"})
    t_bak = app.task_repo.create({"name": "e3", "source_id": 1, "target_id": 2,
                                  "mode": "backup_only", "mapping": {"scope": "all"},
                                  "state": "error"})
    for t in (t_cdc, t_full, t_bak):
        r = client.post(f"/api/tasks/{t}/recover")
        assert r.status_code == 200 and r.json()["state"] == "idle", r.text
        assert app.task_repo.get(t)["state"] == "idle"
    audits = app.audit.list_recent(10)
    assert any(a["action"] == "task.recover" for a in audits)


def test_recover_rejects_invalid_state_and_missing_task(m4client):
    """非 error 态恢复 → 409；任务不存在 → 404。"""
    client, app, tid = m4client     # fixture 任务为 idle 态
    r = client.post(f"/api/tasks/{tid}/recover")
    assert r.status_code == 409
    r = client.post("/api/tasks/999999/recover")
    assert r.status_code == 404

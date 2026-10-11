"""M3 API 测试：增量启停（T3.9 后端面）、DDL 决策闭环（E12）、延迟指标（T3.6）。

引擎以 FakeCdc 桩替换（线程级全链路联调依赖真实 MySQL，归入 T3.10 集成轮）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


class FakeCdc:
    """CdcEngine 桩：记录调用、返回固定值。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self._running: set[int] = set()
        self.lag: dict[int, float] = {}

    def is_running(self, tid):
        return tid in self._running

    def start(self, tid, trigger="manual"):
        self.calls.append(("start", tid, trigger))
        self._running.add(tid)
        return 4321

    def stop(self, tid, timeout=30.0):
        self.calls.append(("stop", tid))
        self._running.discard(tid)

    def pause(self, tid):
        self.calls.append(("pause", tid))

    def resume(self, tid):
        self.calls.append(("resume", tid))

    def apply_ddl(self, tid, ddl):
        self.calls.append(("apply_ddl", tid, ddl))

    def resume_after_ddl(self, tid):
        self.calls.append(("resume_after_ddl", tid))

    def get_lag(self, tid):
        return self.lag.get(tid)

    def get_stats(self, tid):
        return {"running": tid in self._running, "counters": {"inserted": 3, "updated": 1, "deleted": 2}}

    def lag_history(self, tid, minutes=1440):
        return [{"ts": 1700000000000, "lag_seconds": 1.5}]


@pytest.fixture()
def m3client(tmp_path, monkeypatch):
    import app.config as cfg
    from app.db.database import Database
    from app.models.m2_repository import (CdcRunRepository, DdlPendingRepository,
                                          PositionRepository, RunRepository, TaskRepository)
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
    app.state.alert_repo = AlertRepository(db)
    app.state.task_repo = TaskRepository(db)
    app.state.run_repo = RunRepository(db)
    app.state.position_repo = PositionRepository(db)
    app.state.ddl_repo = DdlPendingRepository(db)
    app.state.lag_repo = CdcRunRepository(db)
    from app.core.orchestrator import Orchestrator
    app.state.orch = Orchestrator(app.state.task_repo, app.state.broadcaster)

    ds_repo = DatasourceRepository(db)
    src = ds_repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"})
    dst = ds_repo.create({"name": "dst", "host": "127.0.0.1", "port": 3307, "username": "u"})
    tid = app.state.task_repo.create({"name": "t", "source_id": src, "target_id": dst,
                                      "mode": "init_plus_cdc", "mapping": {"scope": "all"},
                                      "state": "idle"})

    fake = FakeCdc()
    app.state.cdc = fake
    app.state.engine.cdc_engine = fake

    # bootstrap 管理员并登录（拿会话 cookie）；auth 需与新库重建绑定
    from app.core.security import MasterKeyManager
    from app.core.sessions import AuthService, LoginGuard, SessionStore
    app.state.key_manager = MasterKeyManager(tmp_path / "data" / "keys" / "master.key")
    app.state.sessions = SessionStore()
    app.state.guard = LoginGuard()
    app.state.auth = AuthService(app.state.settings, app.state.sessions, app.state.guard)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200, r.text
    return client, fake, tid


def test_start_requires_position(m3client):
    client, fake, tid = m3client
    r = client.post(f"/api/tasks/{tid}/start")
    assert r.status_code == 400   # AppError：尚未取得一致位点


def test_start_pause_resume_stop_cycle(m3client):
    client, fake, tid = m3client
    client.app.state.position_repo.save_p0(tid, {"binlog_file": "bin.000001", "binlog_pos": 4})
    r = client.post(f"/api/tasks/{tid}/start")
    assert r.status_code == 200 and r.json()["run_id"] == 4321
    assert ("start", tid, "manual") in fake.calls
    assert client.app.state.task_repo.get(tid)["state"] == "cdc_running"

    # 重复启动被拒
    assert client.post(f"/api/tasks/{tid}/start").status_code == 409

    r = client.post(f"/api/tasks/{tid}/pause")
    assert r.status_code == 200
    assert client.app.state.task_repo.get(tid)["state"] == "paused"

    r = client.post(f"/api/tasks/{tid}/resume")
    assert r.status_code == 200
    assert client.app.state.task_repo.get(tid)["state"] == "cdc_running"

    r = client.post(f"/api/tasks/{tid}/stop")
    assert r.status_code == 200 and r.json()["stopped"] is True
    assert ("stop", tid) in fake.calls
    assert client.app.state.task_repo.get(tid)["state"] == "idle"


def test_start_rejects_backup_only_mode(m3client):
    client, fake, tid = m3client
    tasks = client.app.state.task_repo
    ds = client.app.state.db
    from app.models.repository import DatasourceRepository
    dsr = DatasourceRepository(ds)
    src, dst = dsr.list()[0]["id"], dsr.list()[1]["id"]
    tid2 = tasks.create({"name": "t-bak", "source_id": src, "target_id": dst,
                         "mode": "backup_only", "mapping": {}, "state": "idle"})
    r = client.post(f"/api/tasks/{tid2}/start")
    assert r.status_code == 422


def test_get_task_includes_cdc_and_ddl(m3client):
    client, fake, tid = m3client
    fake._running.add(tid)
    fake.lag[tid] = 3.2
    r = client.get(f"/api/tasks/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["cdc_stats"]["counters"]["inserted"] == 3
    assert body["lag_seconds"] == 3.2
    assert body["ddl_pending"] == []


def test_ddl_decision_cdc_path_with_remember(m3client):
    """E12 闭环：CDC 拦截 → 决策 skip + 记住选择 → 规则持久化、回迁 cdc_running。"""
    client, fake, tid = m3client
    tasks, ddl = client.app.state.task_repo, client.app.state.ddl_repo
    tasks.update(tid, {"state": "cdc_running", "return_state": "cdc_running"})
    ddl.create(tid, "ALTER TABLE `users` ADD COLUMN c INT", "bin.000009:1234", ["users"])

    r = client.post(f"/api/tasks/{tid}/ddl-decision",
                    json={"decision": "skip", "remember": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "cdc" and body["decision"] == "skip"
    assert tasks.get(tid)["state"] == "cdc_running"
    assert ddl.list_pending(tid) == []
    rules = tasks.get(tid)["ddl_rules"]
    assert rules["remember"]["ALTER TABLE users"] == "skip"
    assert ("resume_after_ddl", tid) in fake.calls
    # 审计留痕
    audits = client.app.state.audit.list_recent(10)
    assert any(a["action"] == "task.ddl_decision.skip" for a in audits)


def test_ddl_decision_execute_applies_to_target(m3client):
    client, fake, tid = m3client
    tasks, ddl = client.app.state.task_repo, client.app.state.ddl_repo
    tasks.update(tid, {"state": "cdc_running", "return_state": "cdc_running"})
    ddl.create(tid, "CREATE TABLE tmp_a (id INT)", "bin.000009:1300", ["tmp_a"])
    r = client.post(f"/api/tasks/{tid}/ddl-decision", json={"decision": "execute"})
    assert r.status_code == 200
    assert any(c[0] == "apply_ddl" and c[2].startswith("CREATE TABLE") for c in fake.calls)


def test_ddl_decision_requires_pending(m3client):
    client, fake, tid = m3client
    r = client.post(f"/api/tasks/{tid}/ddl-decision", json={"decision": "skip"})
    assert r.status_code == 404


def test_metrics_lag_endpoint(m3client):
    client, fake, tid = m3client
    r = client.get(f"/api/metrics/lag?task_id={tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid and body["points"] and "current_lag" in body

"""仪表盘 API 测试：/api/dashboard/summary 聚合口径 + /api/metrics/lag-batch 批量。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


class FakeCdc:
    """CdcEngine 桩：与 test_m3_api 同款，lag_history 走固定返回。"""

    def __init__(self):
        self._running: set[int] = set()
        self.lag: dict[int, float] = {}

    def is_running(self, tid):
        return tid in self._running

    def get_lag(self, tid):
        return self.lag.get(tid)

    def get_stats(self, tid):
        return {"running": tid in self._running}

    def lag_history(self, tid, minutes=1440):
        return [{"ts": 1700000000000, "lag_seconds": 1.5}]


@pytest.fixture()
def dashclient(tmp_path, monkeypatch):
    import app.config as cfg
    from app.db.database import Database
    from app.models.m2_repository import CdcRunRepository, PositionRepository, RunRepository, TaskRepository
    from app.models.repository import (AlertRepository, AuditRepository,
                                       DatasourceRepository, SettingRepository)

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "data" / "meta.db")
    monkeypatch.setattr(cfg, "KEYS_DIR", tmp_path / "data" / "keys")
    monkeypatch.setattr(cfg, "MASTER_KEY_PATH", tmp_path / "data" / "keys" / "master.key")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    for d in (tmp_path / "data", tmp_path / "data" / "keys", tmp_path / "logs"):
        d.mkdir(parents=True, exist_ok=True)

    from app.main import create_app
    app = create_app()
    app.state.db = db = Database(tmp_path / "meta.db")
    db.init_schema()
    app.state.settings = SettingRepository(db)
    app.state.audit = AuditRepository(db)
    app.state.alert_repo = AlertRepository(db)
    app.state.task_repo = TaskRepository(db)
    app.state.run_repo = RunRepository(db)
    app.state.position_repo = PositionRepository(db)
    app.state.lag_repo = CdcRunRepository(db)
    app.state.cdc = FakeCdc()

    ds_repo = DatasourceRepository(db)
    src = ds_repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"})
    dst = ds_repo.create({"name": "dst", "host": "127.0.0.1", "port": 3307, "username": "u"})

    from app.core.security import MasterKeyManager
    from app.core.sessions import AuthService, LoginGuard, SessionStore
    app.state.key_manager = MasterKeyManager(tmp_path / "data" / "keys" / "master.key")
    app.state.sessions = SessionStore()
    app.state.guard = LoginGuard()
    app.state.auth = AuthService(app.state.settings, app.state.sessions, app.state.guard)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200, r.text
    return client, db


def _make_task(client, db, name: str, state: str, src: int = 1, dst: int = 2) -> int:
    tid = client.app.state.task_repo.create(
        {"name": name, "source_id": src, "target_id": dst,
         "mode": "init_plus_cdc", "mapping": {"scope": "all"}, "state": state})
    return tid


def _summary(client):
    return client.get("/api/dashboard/summary")


def test_summary_unauthorized(dashclient):
    client, _ = dashclient
    bare = TestClient(client.app, raise_server_exceptions=False)
    assert bare.get("/api/dashboard/summary").status_code == 401


def test_summary_empty_db(dashclient):
    client, _ = dashclient
    r = _summary(client)
    assert r.status_code == 200
    body = r.json()
    assert body["task_counts"] == {"total": 0, "running": 0, "error": 0,
                                   "paused": 0, "needs_reinit": 0}
    assert body["today_events"] == {"rows_inserted": 0, "rows_updated": 0,
                                    "rows_deleted": 0, "total": 0}
    assert body["unresolved_alerts"] == 0
    assert body["generated_at"]


def test_summary_task_counts(dashclient):
    client, _ = dashclient
    _make_task(client, client.app.state.db, "a", "cdc_running")
    _make_task(client, client.app.state.db, "b", "error")
    _make_task(client, client.app.state.db, "c", "error")
    _make_task(client, client.app.state.db, "d", "paused")
    _make_task(client, client.app.state.db, "e", "needs_reinit")
    _make_task(client, client.app.state.db, "f", "idle")
    body = _summary(client).json()
    assert body["task_counts"] == {"total": 6, "running": 1, "error": 2,
                                   "paused": 1, "needs_reinit": 1}


def test_today_events_rows_and_counters(dashclient):
    client, db = dashclient
    runs = client.app.state.run_repo
    tid = _make_task(client, db, "a", "idle")

    # run1：rows_* 列口径（全量 run）
    r1 = runs.start(task_id=tid, run_type="full_resync")
    runs.finish(r1, state="success", progress={"rows_inserted": 100, "rows_updated": 20,
                                               "tables_done": 1, "tables_total": 2})
    # run2：CDC 型——rows_* 全 0，计数在 report_json.counters
    r2 = runs.start(task_id=tid, run_type="cdc")
    runs.finish(r2, state="success", report={"counters": {"inserted": 7, "updated": 3, "deleted": 5}})
    # run3：昨日 run，不应计入
    r3 = runs.start(task_id=tid, run_type="full_resync")
    runs.finish(r3, state="success", progress={"rows_inserted": 999})
    with db.write_conn() as c:
        c.execute("UPDATE run_history SET started_at=datetime('now','localtime','-2 days') WHERE id=?",
                  (r3,))

    body = _summary(client).json()
    te = body["today_events"]
    # 注：finish(progress=...) 只回填 rows_inserted/tables_done/tables_total，
    # rows_updated/rows_deleted 列不落库（存量行为）——updated/deleted 实际来自 counters 兜底
    assert te["rows_inserted"] == 107   # 100 + counters 7
    assert te["rows_updated"] == 3      # 仅 counters
    assert te["rows_deleted"] == 5
    assert te["total"] == 115


def test_summary_alerts_unresolved(dashclient):
    client, db = dashclient
    alerts = client.app.state.alert_repo
    alerts.fire("lag_threshold", "延迟超阈值", task_id=1)      # 未解除
    alerts.fire("task_failed", "任务失败", task_id=1)          # 未解除
    alerts.fire("backup_failed", "备份失败", task_id=1)
    alerts.resolve("backup_failed")
    assert _summary(client).json()["unresolved_alerts"] == 2


def test_summary_cached(dashclient):
    client, db = dashclient
    assert _summary(client).json()["task_counts"]["total"] == 0
    _make_task(client, db, "later", "idle")
    # 5 秒 TTL 内命中缓存，仍返回旧值
    assert _summary(client).json()["task_counts"]["total"] == 0


def test_lag_batch_structure_and_truncation(dashclient):
    client, db = dashclient
    lag_repo = client.app.state.lag_repo
    for tid in range(1, 11):
        lag_repo.append(tid, 1700000000000, 2.5)

    r = client.get("/api/metrics/lag-batch", params={"task_ids": "1,2,3,x,5"})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 4          # x 被忽略
    assert [s["task_id"] for s in body["series"]] == [1, 2, 3, 5]
    assert body["series"][0]["points"] == [{"ts": 1700000000000, "lag_seconds": 1.5}]

    # 超过 8 个截断
    r2 = client.get("/api/metrics/lag-batch", params={"task_ids": ",".join(str(i) for i in range(1, 11))})
    body2 = r2.json()
    assert body2["requested"] == 10 and body2["returned"] == 8
    assert [s["task_id"] for s in body2["series"]] == list(range(1, 9))

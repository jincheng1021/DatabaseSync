"""M6 批量任务 API 测试：多库批量创建、命名去重、部分成功、防误清库、探测端点、批量初始化入队。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


@pytest.fixture()
def batch_client(tmp_path, monkeypatch):
    import app.api.tasks as tasks_api
    import app.config as cfg
    from app.db.database import Database
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
    # 注意：app.db.database 顶层 `from app.config import DB_PATH`，monkeypatch cfg 不影响
    # Database() 默认路径——必须在显式临时库上重建 API 触及的全部仓库（同 test_m4_api 做法）
    app.state.db = Database(tmp_path / "meta.db")
    app.state.db.init_schema()
    db = app.state.db
    from app.models.m2_repository import TaskRepository
    from app.services.datasource_service import DatasourceService
    app.state.settings = SettingRepository(db)
    app.state.audit = AuditRepository(db)
    app.state.task_repo = TaskRepository(db)
    ds_repo = DatasourceRepository(db)
    app.state.ds_service = DatasourceService(ds_repo, app.state.cipher, app.state.audit)
    app.state.alert_repo = FakeAlert(AlertRepository(db))
    src = ds_repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"})
    dst = ds_repo.create({"name": "dst", "host": "127.0.0.1", "port": 3307, "username": "u"})

    class FakeBatchInit:
        def __init__(self):
            self.calls = []

        def enqueue(self, task_ids, max_parallel=None):
            self.calls.append((list(task_ids), max_parallel))
            return list(task_ids)

        def status(self):
            return {"queued": [], "running": 0, "default_parallel": 2}

    fake_batch_init = FakeBatchInit()
    app.state.batch_init = fake_batch_init

    # 探测桩：不真连 MySQL；可按测试注入 {target_db: (exists, tables)}
    probe_state: dict[str, tuple[bool, int]] = {}

    def fake_probe(target, schemas):
        return {s: {"exists": probe_state.get(s, (False, 0))[0],
                    "tables": probe_state.get(s, (False, 0))[1]}
                for s in schemas}

    monkeypatch.setattr(tasks_api, "probe_databases", fake_probe)

    from app.core.security import MasterKeyManager
    from app.core.sessions import AuthService, LoginGuard, SessionStore
    app.state.key_manager = MasterKeyManager(tmp_path / "data" / "keys" / "master.key")
    app.state.sessions = SessionStore()
    app.state.guard = LoginGuard()
    app.state.auth = AuthService(app.state.settings, app.state.sessions, app.state.guard)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200, r.text

    client.probe_state = probe_state
    client.fake_batch_init = fake_batch_init
    client.src_id = src
    client.dst_id = dst
    return client, app.state


class FakeAlert:
    def __init__(self, repo):
        self.repo = repo

    def fire(self, key, message, **kw):
        return True

    def resolve(self, key):
        return True

    def list_recent(self, limit=50):
        return []


def _batch_body(client, **kw):
    body = {
        "name": "批量", "source_id": client.src_id, "target_id": client.dst_id,
        "mode": "init_plus_cdc",
        "db_pairs": [{"source_database": "order"}, {"source_database": "pay"},
                     {"source_database": "user"}],
    }
    body.update(kw)
    return body


# ---- 批量创建 ----

def test_batch_create_success_default_same_name(batch_client):
    client, st = batch_client
    r = client.post("/api/tasks/batch", json=_batch_body(client))
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["created"]) == 3 and body["failed"] == []
    names = {c["name"] for c in body["created"]}
    assert names == {"批量-order", "批量-pay", "批量-user"}
    for c in body["created"]:
        assert c["target_state"] == "missing"
        t = st.task_repo.get(c["task_id"])
        assert t["state"] == "draft"
        assert t["mapping"]["source_database"] == c["source_database"]
        assert t["mapping"]["target_database"] == c["target_database"]
    assert body["init_enqueued"] == []      # 未开 auto_start_init


def test_batch_create_manual_target_and_name_dedup(batch_client):
    client, st = batch_client
    # 预置同名任务（任务名 = {name}-{目标库}），验证 UNIQUE 冲突加序号
    st.task_repo.create({"name": "批量-pay_ods", "source_id": client.src_id,
                         "target_id": client.dst_id, "mode": "init_plus_cdc",
                         "mapping": {"scope": "all"}, "state": "draft"})
    r = client.post("/api/tasks/batch", json=_batch_body(client, db_pairs=[
        {"source_database": "pay", "target_database": "pay_ods"}]))
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert created[0]["name"] == "批量-pay_ods-2"
    assert created[0]["target_database"] == "pay_ods"


def test_batch_create_partial_failure_nonempty_target(batch_client):
    client, st = batch_client
    client.probe_state["pay"] = (True, 12)   # 已存在且非空
    r = client.post("/api/tasks/batch", json=_batch_body(client, auto_start_init=True,
                                                         max_parallel=2))
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["created"]) == 2
    assert len(body["failed"]) == 1
    assert body["failed"][0]["source_database"] == "pay"
    assert "非空" in body["failed"][0]["reason"]
    # auto_start_init：missing 的两个库自动入队
    assert body["init_enqueued"] == [c["task_id"] for c in body["created"]]
    assert client.fake_batch_init.calls and client.fake_batch_init.calls[0][1] == 2


def test_batch_create_scheduled_full_allows_nonempty_target(batch_client):
    client, st = batch_client
    client.probe_state["pay"] = (True, 12)
    r = client.post("/api/tasks/batch", json=_batch_body(client, mode="scheduled_full"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["created"]) == 3 and body["failed"] == []
    t = st.task_repo.get(body["created"][0]["task_id"])
    assert t["state"] == "idle"


def test_batch_create_rejects_bad_mode_and_dup_pairs(batch_client):
    client, st = batch_client
    r = client.post("/api/tasks/batch", json=_batch_body(client, mode="nope"))
    assert r.status_code == 422
    r = client.post("/api/tasks/batch", json=_batch_body(client, db_pairs=[
        {"source_database": "a"}, {"source_database": "a"}]))
    assert r.status_code == 422
    r = client.post("/api/tasks/batch", json=_batch_body(client, db_pairs=[
        {"source_database": "a", "target_database": "x"},
        {"source_database": "b", "target_database": "x"}]))
    assert r.status_code == 422   # 目标库重复：多源写一库表名冲突
    assert "目标库重复" in r.json()["error"]["message"]


def test_batch_create_invalid_mapping_pair_fails_individually(batch_client):
    client, st = batch_client
    # 行过滤谓词非法 → 逐对校验失败进入 failed（不抛 500、不落库）
    r = client.post("/api/tasks/batch", json=_batch_body(client, mapping_base={
        "scope": "all",
        "row_filters": {"t1": {"bad_op": {"col": "id", "op": "??", "value": 1}}}}))
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["created"]) == 0
    assert len(body["failed"]) == 3
    assert all("行过滤" in f["reason"] for f in body["failed"])
    assert body["init_enqueued"] == []


# ---- 探测端点 ----

def test_probe_targets_endpoint(batch_client):
    client, st = batch_client
    client.probe_state["pay"] = (True, 0)
    client.probe_state["user"] = (True, 5)
    r = client.post("/api/tasks/batch/probe-targets", json={
        "target_id": client.dst_id,
        "target_databases": ["order", "pay", "user", "pay"]})
    assert r.status_code == 200, r.text
    items = {i["target_database"]: i for i in r.json()["items"]}
    assert items["order"]["state"] == "missing"
    assert items["pay"]["state"] == "empty"
    assert items["user"]["state"] == "nonempty"


def test_probe_targets_requires_existing_datasource(batch_client):
    client, st = batch_client
    r = client.post("/api/tasks/batch/probe-targets", json={
        "target_id": 99999, "target_databases": ["a"]})
    assert r.status_code == 404


# ---- 批量初始化端点 ----

def test_batch_init_endpoint_and_status(batch_client):
    client, st = batch_client
    r = client.post("/api/tasks/batch", json=_batch_body(client))
    ids = [c["task_id"] for c in r.json()["created"]]
    client.fake_batch_init.calls.clear()
    r = client.post("/api/tasks/batch-init", json={"task_ids": ids, "max_parallel": 3})
    assert r.status_code == 200, r.text
    assert r.json()["enqueued"] == ids
    assert client.fake_batch_init.calls == [(ids, 3)]
    r = client.get("/api/tasks/batch-init/status")
    assert r.status_code == 200 and r.json()["default_parallel"] == 2

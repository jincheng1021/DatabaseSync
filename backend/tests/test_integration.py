"""T1.10 集成：bootstrap → login → 数据源 CRUD → 401 拦截 → 锁定 → WS 未授权拒绝（全链路脚本化）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.config as cfg
    import app.main as main_mod
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "data" / "meta.db")
    monkeypatch.setattr(cfg, "MASTER_KEY_PATH", tmp_path / "data" / "keys" / "master.key")
    monkeypatch.setattr(cfg, "KEYS_DIR", tmp_path / "data" / "keys")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    # Database / MasterKeyManager 均从 config 读取默认值，需替换 app.state 的引用
    app = create_app()
    app.state.db = __import__("app.db.database", fromlist=["Database"]).Database(tmp_path / "meta.db")
    app.state.db.init_schema()
    from app.core.security import CredentialCipher, MasterKeyManager
    from app.models.repository import AuditRepository, DatasourceRepository, SettingRepository
    from app.services.datasource_service import DatasourceService
    km = MasterKeyManager(tmp_path / "data" / "keys" / "master.key")
    app.state.key_manager = km
    app.state.settings = SettingRepository(app.state.db)
    app.state.audit = AuditRepository(app.state.db)
    app.state.auth = __import__("app.core.sessions", fromlist=["AuthService"]).AuthService(
        app.state.settings, app.state.sessions, app.state.guard)
    app.state.ds_service = DatasourceService(DatasourceRepository(app.state.db), CredentialCipher(km), app.state.audit)
    return TestClient(app, raise_server_exceptions=False)


def test_full_chain(client):
    # 1. 状态：未初始化
    r = client.get("/api/auth/status")
    assert r.status_code == 200 and r.json()["initialized"] is False

    # 2. 未登录访问受保护 API → 401（判定项 4）
    assert client.get("/api/datasources").status_code == 401

    # 3. bootstrap → 自动登录；二次 bootstrap 被拒
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-2"})
    assert r.status_code == 409

    # 4. /me
    r = client.get("/api/auth/me")
    assert r.json()["username"] == "admin"

    # 5. 健康探活
    assert client.get("/api/health").json()["ok"] is True

    # 6. 数据源 CRUD + 克隆 + 预检持久化结构
    r = client.post("/api/datasources", json={
        "name": "src", "host": "10.0.0.8", "port": 3306, "username": "repl",
        "password": "pw123456", "ssl_mode": "disabled"})
    assert r.status_code == 201
    ds_id = r.json()["id"]
    assert "password_enc" not in r.json()  # 密码只写不读

    r = client.post(f"/api/datasources/{ds_id}/clone")
    assert r.status_code == 201 and r.json()["name"] == "src-副本"

    r = client.put(f"/api/datasources/{ds_id}", json={"note": "主源库"})
    assert r.json()["note"] == "主源库"

    # 7. 参数校验错误 → 统一错误结构
    r = client.post("/api/datasources", json={"name": "", "host": "", "username": ""})
    assert r.status_code in (201, 422)

    # 8. 审计有记录（判定：写操作 100% 落审计）
    r = client.get("/api/audit-logs")
    actions = [i["action"] for i in r.json()["items"]]
    assert "datasource.create" in actions and "auth.login" in actions

    # 9. 登出后 API 恢复 401
    client.post("/api/auth/logout")
    assert client.get("/api/datasources").status_code == 401


def test_ws_unauthorized_rejected(client):
    """判定项 3：未授权 WS 连接被拒（4401）且落审计。"""
    with client.websocket_connect("/ws/events") as ws:
        # 握手被 accept 后服务端立即以 4401 关闭：读取时断开
        with pytest.raises(Exception):
            ws.receive_json()
    # 未登录，无审计读取通道；重启后审计表中应有 ws.unauthorized_connect
    items = client.app.state.audit.list_recent()
    assert any(a["action"] == "ws.unauthorized_connect" for a in items)


def test_ws_authorized_works(client):
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    with client.websocket_connect("/ws/events") as ws:
        first = ws.receive_json()
        assert first["type"] == "ws.connected"
        ws.send_json({"action": "subscribe", "tasks": []})
        snap = ws.receive_json()
        assert snap["type"] == "ws.snapshot"
        ws.send_json({"action": "ping"})
        assert ws.receive_json()["type"] == "ws.pong"


def test_login_lockout_http(client):
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": "admin-pass-1"})
    client.post("/api/auth/logout")
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "LOGIN_LOCKED"


def test_spa_fallback_blocks_path_traversal(tmp_path, monkeypatch):
    """B1 回归：SPA fallback 不得逃出 dist 目录读取任意文件（..%2f 路径穿越）。"""
    import app.config as cfg
    import app.main as main_mod
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "data" / "meta.db")
    monkeypatch.setattr(cfg, "MASTER_KEY_PATH", tmp_path / "data" / "keys" / "master.key")
    monkeypatch.setattr(cfg, "KEYS_DIR", tmp_path / "data" / "keys")
    monkeypatch.setattr(cfg, "LOG_DIR", tmp_path / "logs")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>index</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    secret = tmp_path / "secret.txt"          # dist 目录之外
    secret.write_text("TOP-SECRET", encoding="utf-8")
    monkeypatch.setattr(main_mod, "FRONTEND_DIST_DIR", dist)

    app = main_mod.create_app()
    c = TestClient(app, raise_server_exceptions=False)

    for evil in ("/%2e%2e/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        r = c.get(evil)
        assert "TOP-SECRET" not in r.text, evil
    # dist 内静态文件仍可正常访问，SPA 路由回退 index.html
    assert c.get("/app.js").text == "console.log(1)"
    assert c.get("/tasks/1/detail").text == "<html>index</html>"

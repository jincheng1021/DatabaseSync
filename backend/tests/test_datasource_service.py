"""T1.10 单元：数据源仓库（脱敏/克隆凭据独立/预检结果覆盖更新/引用检查）。"""
from __future__ import annotations

import json

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.services.datasource_service import DatasourceService


@pytest.fixture()
def svc(repos, cipher):
    return DatasourceService(repos["ds"], cipher, repos["audit"])


def _body(name="src-mysql", host="127.0.0.1", port=3306):
    return {"name": name, "host": host, "port": port, "username": "repl",
            "password": "secret123", "ssl_mode": "disabled", "charset": "utf8mb4",
            "connect_timeout": 5}


def test_create_masks_password(svc):
    d = svc.create(_body(), "admin", None)
    assert "password_enc" not in d and d["has_password"] is True
    assert "secret123" not in json.dumps(d)


def test_password_stored_ciphertext(repos, svc):
    svc.create(_body(), "admin", None)
    raw = repos["ds"].get(1)
    assert raw["password_enc"] and "secret123" not in raw["password_enc"]


def test_update_blank_keeps_password(svc, repos, cipher):
    svc.create(_body(), "admin", None)
    old_enc = repos["ds"].get(1)["password_enc"]
    svc.update(1, {"password": "", "note": "x"}, "admin", None)
    assert repos["ds"].get(1)["password_enc"] == old_enc  # 留空 = 不变更
    svc.update(1, {"password": "new-pass-9"}, "admin", None)
    assert repos["ds"].get(1)["password_enc"] != old_enc
    assert cipher.decrypt(repos["ds"].get(1)["password_enc"]) == "new-pass-9"


def test_clone_reencrypt_independent(svc, repos, cipher):
    svc.create(_body(), "admin", None)
    c = svc.clone(1, "admin", None)
    assert c["name"] == "src-mysql-副本"
    old_enc = repos["ds"].get(1)["password_enc"]
    new_enc = repos["ds"].get(2)["password_enc"]
    assert old_enc != new_enc                        # 独立加密（不同 nonce）
    assert cipher.decrypt(new_enc) == "secret123"    # 明文一致


def test_name_dup_rejected(svc):
    svc.create(_body(), "admin", None)
    with pytest.raises(ConflictError):
        svc.create(_body(name="src-mysql"), "admin", None)


def test_delete_not_found(svc):
    with pytest.raises(NotFoundError):
        svc.delete(999, "admin", None)


def test_precheck_result_persisted_and_overwritten(svc, repos, monkeypatch):
    """E9：预检结果持久化，每次覆盖更新（不造 mock MySQL，直接写仓库层验证覆盖语义）。"""
    svc.create(_body(), "admin", None)
    repos["ds"].save_precheck(1, "warn", [{"key": "version", "level": "warn", "message": "v8.0"}])
    repos["ds"].save_precheck(1, "pass", [{"key": "version", "level": "pass", "message": "v8.0"}])
    p = repos["ds"].get_precheck(1)
    assert p["overall"] == "pass"
    assert len(p["items"]) == 1


def test_audit_records_write_ops(repos, svc):
    svc.create(_body(), "alice", "1.2.3.4")
    svc.update(1, {"note": "n"}, "alice", "1.2.3.4")
    svc.clone(1, "alice", None)
    svc.delete(2, "alice", None)
    actions = [a["action"] for a in repos["audit"].list_recent()]
    for act in ("datasource.create", "datasource.update", "datasource.clone", "datasource.delete"):
        assert act in actions


def test_schemas_lists_business_dbs(svc, monkeypatch):
    """schemas()：返回业务库清单（排除系统库），未 close 的连接由 finally 兜底。"""
    svc.create(_body(), "admin", None)
    closed = []
    conn = type("FakeConn", (), {"close": staticmethod(lambda: closed.append(True))})()

    def fake_open(target, database):
        assert database is None
        return conn

    fake_rows = [{"schema_name": n} for n in ("appdb", "shopdb")]
    monkeypatch.setattr("app.services.mysql_client.open_connection", fake_open)
    monkeypatch.setattr("app.services.mysql_client.fetch_all_lc", lambda c, sql, *a: fake_rows)
    assert svc.schemas(1) == ["appdb", "shopdb"]
    assert closed == [True]


def test_schemas_not_found(svc):
    with pytest.raises(NotFoundError):
        svc.schemas(999)

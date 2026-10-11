"""T1.10 单元：统一错误结构 + 预检判定分支（纯逻辑，不依赖真实 MySQL）。"""
from __future__ import annotations

import pytest

from app.core.errors import AppError, AuthError, ConflictError
from app.services import precheck_service as ps
from app.services.mysql_client import MySQLTarget


def test_error_payload_structure():
    e = AuthError("会话过期", hint="请重新登录")
    payload = e.to_payload()["error"]
    assert set(payload.keys()) == {"code", "message", "hint"}
    assert payload["code"] == "AUTH_REQUIRED"
    assert e.status_code == 401


def test_error_codes():
    assert ConflictError().status_code == 409
    assert AppError().status_code == 400


# ---- 预检判定分支（monkeypatch 连接层） ----

class FakeConn:
    def __init__(self, vars_, grants, user_rows=None, tables=None):
        self._vars = vars_
        self._grants = grants
        self._user_rows = user_rows or []
        self.user = "repl"

    def close(self):
        pass


def _run_with(monkeypatch, vars_, grants, user_rows=None, mysql_proc_ok=True, event_ok=True):
    conn = FakeConn(vars_, grants, user_rows)
    monkeypatch.setattr(ps, "open_connection", lambda t: conn)
    monkeypatch.setattr(ps, "fetch_variables", lambda c: vars_)
    monkeypatch.setattr(ps, "fetch_all", lambda c, sql, args=(): 
                        [{"g": g} for g in c._grants] if "SHOW GRANTS" in sql.upper()
                        else (c._user_rows if "mysql.user" in sql else []))
    monkeypatch.setattr(ps, "fetch_one", lambda c, sql, args=():
                        ({"plugin": c._user_rows[0]["plugin"]} if c._user_rows and "mysql.user" in sql else None)
                        if "mysql.user" in sql else
                        ({"n": 1} if (mysql_proc_ok and "mysql.proc" in sql) or (event_ok and "EVENTS" in sql) else None))
    return ps.run_precheck(MySQLTarget("h", 3306, "repl", "p"), "both")


def test_precheck_all_green(monkeypatch):
    vars_ = {"version": "8.0.36", "sql_mode": "STRICT_TRANS_TABLES",
             "character_set_server": "utf8mb4", "max_allowed_packet": str(256 * 1024 * 1024),
             "log_bin": "ON", "binlog_format": "ROW", "binlog_row_image": "FULL",
             "gtid_mode": "ON", "server_id": "101", "have_ssl": "YES",
             "binlog_expire_logs_seconds": str(7 * 86400)}
    # 源库权限 + 目标库 DML/DDL 权限齐全（both 角色全绿）
    grants = ["GRANT SELECT, RELOAD, REPLICATION SLAVE, REPLICATION CLIENT, SHOW VIEW, PROCESS, "
              "SHOW_ROUTINE, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX ON *.* TO `repl`@`%`"]
    user_rows = [{"plugin": "mysql_native_password"}]
    overall, items = _run_with(monkeypatch, vars_, grants, user_rows)
    fails = [i for i in items if i["level"] == "fail"]
    assert overall == "pass", fails
    keys = {i["key"] for i in items}
    assert {"log_bin", "binlog_format", "binlog_row_image", "priv_reload",
            "priv_show_routine", "priv_event", "auth_plugin"} <= keys


def test_precheck_missing_reload_marks_affected(monkeypatch):
    """E1：缺 RELOAD → 失败并标注受影响功能（初始化/备份不可用）。"""
    vars_ = {"version": "8.0.36", "sql_mode": "", "character_set_server": "utf8mb4",
             "max_allowed_packet": "67108864", "log_bin": "ON", "binlog_format": "ROW",
             "binlog_row_image": "FULL", "gtid_mode": "ON", "server_id": "1",
             "have_ssl": "YES"}
    grants = ["GRANT SELECT, REPLICATION SLAVE ON *.* TO `repl`@`%`"]
    overall, items = _run_with(monkeypatch, vars_, grants, [{"plugin": "caching_sha2_password"}])
    assert overall == "fail"
    reload_item = next(i for i in items if i["key"] == "priv_reload")
    assert reload_item["level"] == "fail"
    assert any("备份" in a or "初始化" in a for a in reload_item["affected"])


def test_precheck_caching_sha2_no_ssl_blocked(monkeypatch):
    """8.0 caching_sha2 + 非 SSL → 判失败并给指引。"""
    vars_ = {"version": "8.0.36", "sql_mode": "", "character_set_server": "utf8mb4",
             "max_allowed_packet": "67108864", "log_bin": "ON", "binlog_format": "ROW",
             "binlog_row_image": "FULL", "gtid_mode": "ON", "server_id": "1",
             "have_ssl": "YES"}
    grants = ["GRANT ALL PRIVILEGES ON *.* TO `repl`@`%`"]
    overall, items = _run_with(monkeypatch, vars_, grants,
                               [{"plugin": "caching_sha2_password"}])
    # have_ssl=YES 但 ssl_mode=disabled → 未启用 SSL → 拦截
    ap = next(i for i in items if i["key"] == "auth_plugin")
    assert ap["level"] == "fail"
    assert "mysql_native_password" in ap["hint"] or "SSL" in ap["hint"]


def test_precheck_binlog_format_not_row(monkeypatch):
    vars_ = {"version": "5.7.44", "sql_mode": "", "character_set_server": "utf8",
             "max_allowed_packet": "67108864", "log_bin": "ON", "binlog_format": "MIXED",
             "binlog_row_image": "FULL", "gtid_mode": "OFF", "server_id": "1"}
    grants = ["GRANT ALL PRIVILEGES ON *.* TO `repl`@`%`"]
    overall, items = _run_with(monkeypatch, vars_, grants)
    bf = next(i for i in items if i["key"] == "binlog_format")
    assert bf["level"] == "fail"
    assert "增量" in "".join(bf["affected"])


def test_precheck_conn_refused(monkeypatch):
    import pymysql
    def boom(t):
        raise pymysql.err.OperationalError(2003, "Can't connect")
    monkeypatch.setattr(ps, "open_connection", boom)
    overall, items = ps.run_precheck(MySQLTarget("h", 3306, "u", "p"), "both")
    assert overall == "fail"
    assert items[0]["key"] == "tcp"


def test_precheck_auth_failed(monkeypatch):
    import pymysql
    def boom(t):
        raise pymysql.err.OperationalError(1045, "Access denied")
    monkeypatch.setattr(ps, "open_connection", boom)
    overall, items = ps.run_precheck(MySQLTarget("h", 3306, "u", "p"), "both")
    assert overall == "fail"
    assert items[0]["key"] == "auth"

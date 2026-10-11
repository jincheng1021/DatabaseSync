"""仓库层（T1.2 / §8）：业务层唯一的数据访问入口。"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from app.config import GLOBAL_DEFAULTS
from app.db.database import Database


class DatasourceRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- CRUD（密码等敏感字段一律密文出入，业务层负责加解密） ----
    def create(self, d: dict[str, Any]) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO datasource (name, db_type, host, port, username, password_enc,
                       ssl_mode, ssl_ca_enc, ssl_cert_enc, ssl_key_enc, default_schema,
                       charset, connect_timeout, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["name"], d.get("db_type", "mysql"), d["host"], d["port"], d["username"],
                 d.get("password_enc"), d.get("ssl_mode", "disabled"), d.get("ssl_ca_enc"),
                 d.get("ssl_cert_enc"), d.get("ssl_key_enc"), d.get("default_schema"),
                 d.get("charset", "utf8mb4"), d.get("connect_timeout", 10), d.get("note")),
            )
            return int(cur.lastrowid)

    def update(self, ds_id: int, d: dict[str, Any]) -> bool:
        sets, vals = [], []
        for col in ("name", "host", "port", "username", "password_enc", "ssl_mode",
                    "ssl_ca_enc", "ssl_cert_enc", "ssl_key_enc", "default_schema",
                    "charset", "connect_timeout", "note"):
            if col in d and d[col] is not None:
                sets.append(f"{col}=?")
                vals.append(d[col])
        if not sets:
            return False
        vals.append(ds_id)
        with self.db.write_conn() as c:
            cur = c.execute(f"UPDATE datasource SET {', '.join(sets)}, updated_at=datetime('now','localtime') WHERE id=?", vals)
            return cur.rowcount > 0

    def delete(self, ds_id: int) -> bool:
        with self.db.write_conn() as c:
            cur = c.execute("DELETE FROM datasource WHERE id=?", (ds_id,))
            return cur.rowcount > 0

    def get(self, ds_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM datasource WHERE id=?", (ds_id,)).fetchone()
            return dict(row) if row else None

    def get_by_name(self, name: str) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM datasource WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM datasource ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def count_tasks_referencing(self, ds_id: int) -> int:
        """任务引用检查（任务表 M2 建表后生效，当前恒 0，保留接口）。"""
        with self.db.conn() as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "sync_task" not in tables:
                return 0
            row = c.execute("SELECT COUNT(*) AS n FROM sync_task WHERE source_id=? OR target_id=?",
                            (ds_id, ds_id)).fetchone()
            return int(row["n"])

    # ---- 预检结果持久化（E9，每次覆盖更新） ----
    def save_precheck(self, ds_id: int, overall: str, items: list[dict]) -> None:
        with self.db.write_conn() as c:
            c.execute("DELETE FROM datasource_precheck_result WHERE datasource_id=?", (ds_id,))
            c.execute(
                "INSERT INTO datasource_precheck_result (datasource_id, overall, items_json) VALUES (?,?,?)",
                (ds_id, overall, json.dumps(items, ensure_ascii=False)),
            )

    def get_precheck(self, ds_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT datasource_id, checked_at, overall, items_json FROM datasource_precheck_result WHERE datasource_id=?",
                (ds_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["items"] = json.loads(d.pop("items_json"))
            return d


class AuditRepository:
    def __init__(self, db: Database):
        self.db = db

    def record(self, actor: str, action: str, *, ip: str | None = None,
               object_type: str | None = None, object_id: str | None = None,
               before: Any = None, after: Any = None) -> None:
        def enc(v: Any) -> str | None:
            return None if v is None else json.dumps(v, ensure_ascii=False, default=str)
        try:
            with self.db.write_conn() as c:
                c.execute(
                    "INSERT INTO audit_log (actor, ip, action, object_type, object_id, before_json, after_json) VALUES (?,?,?,?,?,?,?)",
                    (actor, ip, action, object_type, str(object_id) if object_id is not None else None,
                     enc(before), enc(after)),
                )
        except sqlite3.OperationalError:
            # 审计为旁路：写失败（如瞬时锁/只读抖动）不允许影响主业务 API
            logging.getLogger("dsync.audit").warning("审计写入失败（已忽略）：%s", action, exc_info=True)

    def list_recent(self, limit: int = 100) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]


class SettingRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> Any | None:
        with self.db.conn() as c:
            row = c.execute("SELECT value_json FROM app_setting WHERE key=?", (key,)).fetchone()
            return json.loads(row["value_json"]) if row else None

    def set(self, key: str, value: Any) -> None:
        with self.db.write_conn() as c:
            c.execute(
                "INSERT INTO app_setting (key, value_json) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=datetime('now','localtime')",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def ensure_defaults(self) -> None:
        """§9 全局默认参数首次落库（已存在的不覆盖）。"""
        for k, v in GLOBAL_DEFAULTS.items():
            if self.get(k) is None:
                self.set(k, v)


class AlertRepository:
    """告警落库（M2：磁盘不足/备份失败/取位点超时；通道推送 M4 接入）。"""

    def __init__(self, db: Database):
        self.db = db

    def fire(self, alert_key: str, message: str, *, level: str = "warning",
             task_id: int | None = None, silence_minutes: int = 30) -> bool:
        """同项静默期降噪：恢复前不重发。返回是否真正落库。"""
        with self.db.conn() as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "alert_log" not in tables:
                return False
            row = c.execute(
                "SELECT fired_at, resolved_at FROM alert_log WHERE alert_key=? ORDER BY id DESC LIMIT 1",
                (alert_key,)).fetchone()
            if row and row["fired_at"] and not row["resolved_at"]:
                # 恢复前不重发（未解除的告警静默期内/永久静默）
                from datetime import datetime, timedelta
                try:
                    last = datetime.fromisoformat(row["fired_at"])
                    if datetime.now() - last < timedelta(minutes=silence_minutes):
                        return False
                except ValueError:
                    pass
        with self.db.write_conn() as c:
            c.execute(
                "INSERT INTO alert_log (alert_key, level, task_id, message) VALUES (?,?,?,?)",
                (alert_key, level, task_id, message))
        return True

    def save_channel_results(self, alert_id: int, results: list[dict]) -> None:
        """通道触达结果落库（M4 T4.3）：发送失败不影响告警本身。"""
        try:
            with self.db.write_conn() as c:
                c.execute("UPDATE alert_log SET channel_results_json=? WHERE id=?",
                          (json.dumps(results, ensure_ascii=False), alert_id))
        except sqlite3.OperationalError:
            logging.getLogger("dsync.alert").warning("告警通道结果写入失败（已忽略）")

    def resolve(self, alert_key: str) -> bool:
        """告警解除（T3.6：延迟回落 50% 自动解除；返回是否有未解除项被关闭）。"""
        with self.db.write_conn() as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "alert_log" not in tables:
                return False
            cur = c.execute(
                "UPDATE alert_log SET resolved_at=datetime('now','localtime') "
                "WHERE alert_key=? AND resolved_at IS NULL", (alert_key,))
            return cur.rowcount > 0

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self.db.conn() as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "alert_log" not in tables:
                return []
            rows = c.execute("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def count_unresolved(self) -> int:
        """未解除告警计数（仪表盘统计卡口径）。"""
        with self.db.conn() as c:
            tables = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "alert_log" not in tables:
                return 0
            row = c.execute(
                "SELECT COUNT(*) AS n FROM alert_log WHERE resolved_at IS NULL").fetchone()
            return int(row["n"])

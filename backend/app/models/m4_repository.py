"""M4 仓库层：调度计划（schedule）与校验结果（verify_result）。

与 M1/M2 仓库层同级约定：业务层唯一数据访问入口，禁止直连 SQL。
"""
from __future__ import annotations

import json
from typing import Any

from app.db.database import Database

SCHEDULE_ACTIONS = ("cdc_start", "cdc_stop", "full_resync", "verify",
                    "backup_export", "orphan_cleanup")


class ScheduleRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, d: dict[str, Any]) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO schedule (task_id, name, action, cron, timezone, enabled,
                       action_params_json, overlap_policy, misfire_policy, next_fire_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (d["task_id"], d["name"], d["action"], d["cron"],
                 d.get("timezone", "Asia/Shanghai"),
                 1 if d.get("enabled", True) else 0,
                 json.dumps(d.get("action_params", {}), ensure_ascii=False),
                 d.get("overlap_policy", "skip"), d.get("misfire_policy", "skip"),
                 d.get("next_fire_at")),
            )
            return int(cur.lastrowid)

    _COLS = ("name", "action", "cron", "timezone", "enabled", "overlap_policy",
             "misfire_policy", "next_fire_at")

    def update(self, sid: int, d: dict[str, Any]) -> bool:
        sets, vals = [], []
        if "action_params" in d and d["action_params"] is not None:
            d = dict(d)
            d["action_params_json"] = json.dumps(d.pop("action_params"), ensure_ascii=False)
        for col in self._COLS:
            if col in d and d[col] is not None:
                v = d[col]
                if col == "enabled":
                    v = 1 if v else 0
                sets.append(f"{col}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(sid)
        with self.db.write_conn() as c:
            cur = c.execute(
                f"UPDATE schedule SET {', '.join(sets)}, updated_at=datetime('now','localtime') WHERE id=?",
                vals)
            return cur.rowcount > 0

    @staticmethod
    def _dec(row) -> dict | None:
        if not row:
            return None
        d = dict(row)
        d["action_params"] = json.loads(d.pop("action_params_json") or "{}")
        d["enabled"] = bool(d["enabled"])
        return d

    def get(self, sid: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM schedule WHERE id=?", (sid,)).fetchone()
            return self._dec(row)

    def list(self, task_id: int | None = None, enabled_only: bool = False) -> list[dict]:
        conds, vals = [], []
        if task_id is not None:
            conds.append("task_id=?"); vals.append(task_id)
        if enabled_only:
            conds.append("enabled=1")
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        with self.db.conn() as c:
            rows = c.execute(f"SELECT * FROM schedule {where} ORDER BY id", vals).fetchall()
            return [self._dec(r) for r in rows]

    def delete(self, sid: int) -> bool:
        with self.db.write_conn() as c:
            return c.execute("DELETE FROM schedule WHERE id=?", (sid,)).rowcount > 0

    def delete_by_task(self, task_id: int) -> None:
        with self.db.write_conn() as c:
            c.execute("DELETE FROM schedule WHERE task_id=?", (task_id,))


class VerifyResultRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, task_id: int, *, method: str, trigger: str = "manual",
               run_id: int | None = None, tables_total: int = 0) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO verify_result (task_id, run_id, method, trigger_type, tables_total)
                   VALUES (?,?,?,?,?)""",
                (task_id, run_id, method, trigger, tables_total))
            return int(cur.lastrowid)

    def set_tables_total(self, vid: int, tables_total: int) -> None:
        """校验线程解析出实际表清单后回填总数（创建时清单未定，create 传 0）。"""
        with self.db.write_conn() as c:
            c.execute("UPDATE verify_result SET tables_total=? WHERE id=?",
                      (int(tables_total), vid))

    @staticmethod
    def _dec(row, with_detail: bool = False) -> dict | None:
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
        if with_detail:
            d["detail"] = json.loads(d.pop("detail_json")) if d.get("detail_json") else None
        else:
            d.pop("detail_json")
        d["trigger"] = d.pop("trigger_type")
        return d

    def finish(self, vid: int, *, state: str, summary: dict | None = None,
               detail: dict | None = None) -> None:
        with self.db.write_conn() as c:
            c.execute(
                """UPDATE verify_result SET state=?, ended_at=datetime('now','localtime'),
                       tables_checked=?, mismatch_tables=?, summary_json=?, detail_json=?
                   WHERE id=?""",
                (state, int((summary or {}).get("tables_checked", 0)),
                 int((summary or {}).get("mismatch_tables", 0)),
                 json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                 json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
                 vid))

    def get(self, vid: int, *, with_detail: bool = False) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM verify_result WHERE id=?", (vid,)).fetchone()
            return self._dec(row, with_detail=with_detail)

    def list(self, task_id: int | None = None, page: int = 1, page_size: int = 20):
        conds, vals = [], []
        if task_id is not None:
            conds.append("task_id=?"); vals.append(task_id)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        with self.db.conn() as c:
            total = int(c.execute(
                f"SELECT COUNT(*) AS n FROM verify_result {where}", vals).fetchone()["n"])
            rows = c.execute(
                f"SELECT * FROM verify_result {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                vals + [page_size, (page - 1) * page_size]).fetchall()
        return [self._dec(r) for r in rows], total

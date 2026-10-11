"""M2 仓库层（T2.1/T2.9/T2.10）：任务、运行、位点、备份、DDL 待决策。

与 M1 repository.py 同级约定：业务层唯一数据访问入口，禁止直连 SQL。
"""
from __future__ import annotations

import json
from typing import Any

from app.db.database import Database

# ---- sync_task.state（§10 状态机，常量由 orchestrator 权威定义，此处仅做落库） ----


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, d: dict[str, Any]) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO sync_task (name, source_id, target_id, mode, mapping_json,
                       migrate_triggers, ddl_policy, ddl_rules_json, rate_limit_rows,
                       batch_size, batch_interval_ms, queue_capacity, lag_alert_sec,
                       safety_net_backup, orphan_cleanup, state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["name"], d["source_id"], d["target_id"], d["mode"],
                 json.dumps(d.get("mapping", {}), ensure_ascii=False),
                 1 if d.get("migrate_triggers") else 0,
                 d.get("ddl_policy", "intercept"),
                 json.dumps(d.get("ddl_rules", {}), ensure_ascii=False),
                 int(d.get("rate_limit_rows", 0)), int(d.get("batch_size", 5000)),
                 int(d.get("batch_interval_ms", 0)), int(d.get("queue_capacity", 50000)),
                 int(d.get("lag_alert_sec", 60)),
                 1 if d.get("safety_net_backup", True) else 0,
                 1 if d.get("orphan_cleanup", True) else 0,
                 d.get("state", "draft")),
            )
            return int(cur.lastrowid)

    _COLS = ("name", "mapping_json", "migrate_triggers", "ddl_policy", "ddl_rules_json",
             "rate_limit_rows", "batch_size", "batch_interval_ms", "queue_capacity",
             "lag_alert_sec", "safety_net_backup", "orphan_cleanup", "state", "return_state")

    def update(self, task_id: int, d: dict[str, Any]) -> bool:
        sets, vals = [], []
        if "mapping" in d and d["mapping"] is not None:
            d = dict(d)
            d["mapping_json"] = json.dumps(d.pop("mapping"), ensure_ascii=False)
        if "ddl_rules" in d and d["ddl_rules"] is not None:
            d = dict(d)
            d["ddl_rules_json"] = json.dumps(d.pop("ddl_rules"), ensure_ascii=False)
        for col in self._COLS:
            if col in d and d[col] is not None:
                v = d[col]
                if col in ("migrate_triggers", "safety_net_backup", "orphan_cleanup"):
                    v = 1 if v else 0
                sets.append(f"{col}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(task_id)
        with self.db.write_conn() as c:
            cur = c.execute(
                f"UPDATE sync_task SET {', '.join(sets)}, updated_at=datetime('now','localtime') WHERE id=?",
                vals)
            return cur.rowcount > 0

    def get(self, task_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM sync_task WHERE id=?", (task_id,)).fetchone()
            return self._dec(row)

    def list(self) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM sync_task ORDER BY id DESC").fetchall()
            return [self._dec(r) for r in rows]

    def delete(self, task_id: int) -> bool:
        with self.db.write_conn() as c:
            return c.execute("DELETE FROM sync_task WHERE id=?", (task_id,)).rowcount > 0

    @staticmethod
    def _dec(row) -> dict | None:
        if not row:
            return None
        d = dict(row)
        d["mapping"] = json.loads(d.pop("mapping_json") or "{}")
        d["ddl_rules"] = json.loads(d.pop("ddl_rules_json") or "{}")
        for k in ("migrate_triggers", "safety_net_backup", "orphan_cleanup"):
            d[k] = bool(d[k])
        return d

    # ---- 目标端引用检测（恢复预检用）：返回以某数据源为目标端的任务清单 ----
    def list_by_target(self, ds_id: int) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM sync_task WHERE target_id=?", (ds_id,)).fetchall()
            return [self._dec(r) for r in rows]


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    def start(self, *, task_id: int | None, run_type: str, trigger: str = "manual",
              backup_id: int | None = None, tables: list[str] | None = None) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO run_history (task_id, backup_id, run_type, trigger_type, state, tables_total)
                   VALUES (?,?,?,?, 'running', ?)""",
                (task_id, backup_id, run_type, trigger, len(tables or [])))
            run_id = int(cur.lastrowid)
            for t in tables or []:
                c.execute(
                    "INSERT INTO run_table_detail (run_id, table_name, state) VALUES (?,?, 'pending')",
                    (run_id, t))
            return run_id

    def log_state_change(self, *, task_id: int, from_state: str, to_state: str,
                         reason: str = "") -> None:
        """§10：状态迁移审计——每次迁移落 run_history（run_type=state_change）。"""
        with self.db.write_conn() as c:
            c.execute(
                """INSERT INTO run_history
                       (task_id, run_type, trigger_type, state, report_json)
                   VALUES (?, 'state_change', 'system', 'success', ?)""",
                (task_id,
                 json.dumps({"from": from_state, "to": to_state, "reason": reason},
                            ensure_ascii=False)))

    def finish(self, run_id: int, *, state: str, error: str | None = None,
               report: dict | None = None, progress: dict | None = None) -> None:
        """run 终态落库。progress 可选回填进度列（rows_inserted/tables_done/tables_total）——
        这些列此前从不更新，执行历史「表进度」恒显示 0/0。"""
        sets = ["state=?", "ended_at=datetime('now','localtime')",
                "error_summary=?", "report_json=?"]
        vals: list = [state, error,
                      json.dumps(report, ensure_ascii=False) if report is not None else None]
        if progress:
            for col in ("rows_inserted", "tables_done", "tables_total"):
                if progress.get(col) is not None:
                    sets.append(f"{col}=?")
                    vals.append(int(progress[col]))
        vals.append(run_id)
        with self.db.write_conn() as c:
            c.execute(f"UPDATE run_history SET {', '.join(sets)} WHERE id=?", vals)

    def get(self, run_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM run_history WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d.pop("trigger_type", None)
            d["trigger"] = row["trigger_type"]
            if d.get("report_json"):
                d["report"] = json.loads(d.pop("report_json"))
            return d

    def table_details(self, run_id: int) -> list[dict]:
        """run 下钻：表级明细（P2：从 API 层直连 SQL 收进仓库层）。"""
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM run_table_detail WHERE run_id=? ORDER BY id",
                             (run_id,)).fetchall()
            return [dict(r) for r in rows]

    def list(self, task_id: int | None = None, run_type: str | None = None,
             status: str | None = None, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
        conds, vals = [], []
        if task_id is not None:
            conds.append("task_id=?"); vals.append(task_id)
        if run_type:
            conds.append("run_type=?"); vals.append(run_type)
        if status:
            conds.append("state=?"); vals.append(status)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        with self.db.conn() as c:
            total = int(c.execute(f"SELECT COUNT(*) AS n FROM run_history {where}", vals).fetchone()["n"])
            rows = c.execute(
                f"""SELECT * FROM run_history {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
                vals + [page_size, (page - 1) * page_size]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("trigger_type", None)
            d["trigger"] = r["trigger_type"]
            if d.get("report_json"):
                d["report"] = json.loads(d.pop("report_json"))
            out.append(d)
        return out, total

    def today_events(self) -> dict:
        """今日（本地自然日）累计应用行数（仪表盘「今日事件量」口径）。

        数据源：run_history 中 started_at 落在今日的 run，累加 rows_* 列；
        CDC run 的计数在 report_json.counters（rows_* 列为 0，见 cdc_engine.finish），
        列值全 0 且 report_json 存在时改累加 counters。
        """
        with self.db.conn() as c:
            rows = c.execute(
                """SELECT rows_inserted, rows_updated, rows_deleted, report_json
                   FROM run_history
                   WHERE started_at >= datetime('now','localtime','start of day')""").fetchall()
        ins = upd = dele = 0
        for r in rows:
            ci = int(r["rows_inserted"] or 0)
            cu = int(r["rows_updated"] or 0)
            cd = int(r["rows_deleted"] or 0)
            if ci == 0 and cu == 0 and cd == 0 and r["report_json"]:
                try:
                    counters = (json.loads(r["report_json"]) or {}).get("counters") or {}
                    ci = int(counters.get("inserted") or 0)
                    cu = int(counters.get("updated") or 0)
                    cd = int(counters.get("deleted") or 0)
                except (TypeError, ValueError):
                    pass
            ins += ci
            upd += cu
            dele += cd
        return {"rows_inserted": ins, "rows_updated": upd, "rows_deleted": dele,
                "total": ins + upd + dele}

    # ---- 表级明细 ----
    def upsert_table(self, run_id: int, table: str, *, state: str | None = None,
                     rows_total: int | None = None, rows_done: int | None = None,
                     checkpoint: dict | None = None, error: str | None = None) -> None:
        sets, vals = [], []
        if state is not None:
            sets.append("state=?"); vals.append(state)
        if rows_total is not None:
            sets.append("rows_total=?"); vals.append(rows_total)
        if rows_done is not None:
            sets.append("rows_done=?"); vals.append(rows_done)
        if checkpoint is not None:
            sets.append("slice_checkpoint_json=?")
            vals.append(json.dumps(checkpoint, ensure_ascii=False))
        if error is not None:
            sets.append("error=?"); vals.append(error)
        if not sets:
            return
        vals += [run_id, table]
        with self.db.write_conn() as c:
            cur = c.execute(
                f"UPDATE run_table_detail SET {', '.join(sets)} WHERE run_id=? AND table_name=?", vals)
            if cur.rowcount == 0:
                c.execute(
                    "INSERT INTO run_table_detail (run_id, table_name) VALUES (?,?)", (run_id, table))
                c.execute(
                    f"UPDATE run_table_detail SET {', '.join(sets)} WHERE run_id=? AND table_name=?", vals)

    def get_table_checkpoint(self, run_id: int, table: str) -> dict | None:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT slice_checkpoint_json FROM run_table_detail WHERE run_id=? AND table_name=?",
                (run_id, table)).fetchone()
            if row and row["slice_checkpoint_json"]:
                return json.loads(row["slice_checkpoint_json"])
            return None

    def count_tables(self, run_id: int, state: str | None = None) -> int:
        with self.db.conn() as c:
            sql = "SELECT COUNT(*) AS n FROM run_table_detail WHERE run_id=?"
            vals: list = [run_id]
            if state:
                sql += " AND state=?"; vals.append(state)
            return int(c.execute(sql, vals).fetchone()["n"])


class PositionRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_p0(self, task_id: int, pos: dict) -> None:
        with self.db.write_conn() as c:
            c.execute(
                """INSERT INTO binlog_position (task_id, gtid_executed, binlog_file, binlog_pos,
                       server_id_used, last_commit_at, last_batch_stats_json)
                   VALUES (?,?,?,?,?, datetime('now','localtime'), ?)
                   ON CONFLICT(task_id) DO UPDATE SET gtid_executed=excluded.gtid_executed,
                       binlog_file=excluded.binlog_file, binlog_pos=excluded.binlog_pos,
                       server_id_used=excluded.server_id_used,
                       last_commit_at=excluded.last_commit_at""",
                (task_id, pos.get("gtid_executed"), pos.get("binlog_file"),
                 pos.get("binlog_pos"), pos.get("server_id_used"),
                 json.dumps(pos.get("extra", {}), ensure_ascii=False)))

    def get(self, task_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM binlog_position WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["extra"] = json.loads(d.pop("last_batch_stats_json") or "{}")
            return d

    def save_batch(self, task_id: int, pos: dict, stats: dict | None = None) -> None:
        """T3.2：每个提交批在目标库事务成功后调用——位点与批次统计原子写 SQLite。

        语义：at-least-once + 幂等写 = 效果 exactly-once。
        """
        with self.db.write_conn() as c:
            c.execute(
                """INSERT INTO binlog_position (task_id, gtid_executed, binlog_file, binlog_pos,
                       server_id_used, last_commit_at, last_batch_stats_json)
                   VALUES (?,?,?,?,?, datetime('now','localtime'), ?)
                   ON CONFLICT(task_id) DO UPDATE SET gtid_executed=excluded.gtid_executed,
                       binlog_file=excluded.binlog_file, binlog_pos=excluded.binlog_pos,
                       server_id_used=excluded.server_id_used,
                       last_commit_at=excluded.last_commit_at,
                       last_batch_stats_json=excluded.last_batch_stats_json""",
                (task_id, pos.get("gtid_executed"), pos.get("binlog_file"),
                 pos.get("binlog_pos"), pos.get("server_id_used"),
                 json.dumps(stats or {}, ensure_ascii=False)))


class ResyncStateRepository:
    """T3.8：重刷缓存队列磁盘溢写位（内存+磁盘混合防溢出）。"""

    def __init__(self, db: Database):
        self.db = db

    def record_spill(self, task_id: int, path: str, count: int) -> None:
        with self.db.write_conn() as c:
            c.execute(
                """INSERT INTO resync_cache_state (task_id, spill_path, spilled_ops, status)
                   VALUES (?,?,?, 'spilled')""",
                (task_id, path, count))


class CdcRunRepository:
    """T3.6：延迟 1 分钟粒度历史曲线（内存为主，重启丢失可接受；此处落最近样本供跨重启查看）。"""

    def __init__(self, db: Database):
        self.db = db

    def append(self, task_id: int, ts: int, lag_seconds: float) -> None:
        with self.db.write_conn() as c:
            c.execute("INSERT INTO lag_history (task_id, ts, lag_seconds) VALUES (?,?,?)",
                      (task_id, ts, round(float(lag_seconds), 3)))

    def list(self, task_id: int, since_ts: int) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT ts, lag_seconds FROM lag_history WHERE task_id=? AND ts>=? ORDER BY ts",
                (task_id, since_ts)).fetchall()
            return [{"ts": r["ts"], "lag_seconds": r["lag_seconds"]} for r in rows]


class BackupRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, d: dict) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO backup_file (task_id, datasource_id, file_name, file_path,
                       size_bytes, sha256, scope_json, rows_json, created_by, retention_tag)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (d.get("task_id"), d["datasource_id"], d["file_name"], d["file_path"],
                 d["size_bytes"], d["sha256"],
                 json.dumps(d.get("scope", {}), ensure_ascii=False),
                 json.dumps(d.get("rows_baseline"), ensure_ascii=False) if d.get("rows_baseline") else None,
                 d.get("created_by", "manual"), d.get("retention_tag", "manual")))
            return int(cur.lastrowid)

    def get(self, backup_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM backup_file WHERE id=?", (backup_id,)).fetchone()
            return self._dec(row)

    def list(self, tag: str | None = None) -> list[dict]:
        with self.db.conn() as c:
            if tag:
                rows = c.execute("SELECT * FROM backup_file WHERE retention_tag=? ORDER BY id DESC",
                                 (tag,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM backup_file ORDER BY id DESC").fetchall()
            return [self._dec(r) for r in rows]

    def delete(self, backup_id: int) -> bool:
        with self.db.write_conn() as c:
            return c.execute("DELETE FROM backup_file WHERE id=?", (backup_id,)).rowcount > 0

    def count_by_tag(self, tag: str) -> int:
        with self.db.conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM backup_file WHERE retention_tag=?",
                                 (tag,)).fetchone()["n"])

    def oldest_ids_by_tag(self, tag: str, keep: int) -> list[int]:
        """返回超出「保留最近 keep 份」之外应清理的 id（升序 = 最旧优先）。"""
        with self.db.conn() as c:
            rows = c.execute(
                """SELECT id FROM backup_file WHERE retention_tag=?
                   ORDER BY id DESC LIMIT -1 OFFSET ?""", (tag, keep)).fetchall()
            return sorted(int(r["id"]) for r in rows)  # 最旧优先（升序）

    def is_restoring(self, backup_id: int) -> bool:
        with self.db.conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n FROM run_history
                   WHERE backup_id=? AND run_type='restore' AND state='running'""",
                (backup_id,)).fetchone()
            return int(row["n"]) > 0

    @staticmethod
    def _dec(row) -> dict | None:
        if not row:
            return None
        d = dict(row)
        d["scope"] = json.loads(d.pop("scope_json") or "{}")
        d["rows_baseline"] = json.loads(d["rows_json"]) if d.get("rows_json") else None
        d.pop("rows_json")
        return d


class DdlPendingRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, task_id: int, ddl_text: str, source_position: str | None,
               affected_tables: list[str]) -> int:
        with self.db.write_conn() as c:
            cur = c.execute(
                """INSERT INTO ddl_pending (task_id, ddl_text, source_position, affected_tables_json)
                   VALUES (?,?,?,?)""",
                (task_id, ddl_text, source_position,
                 json.dumps(affected_tables, ensure_ascii=False)))
            return int(cur.lastrowid)

    def get(self, ddl_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM ddl_pending WHERE id=?", (ddl_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["affected_tables"] = json.loads(d.pop("affected_tables_json") or "[]")
            d["remember_choice"] = bool(d["remember_choice"])
            return d

    def list_pending(self, task_id: int | None = None) -> list[dict]:
        with self.db.conn() as c:
            if task_id is None:
                rows = c.execute("SELECT * FROM ddl_pending WHERE status='pending' ORDER BY id").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM ddl_pending WHERE status='pending' AND task_id=? ORDER BY id",
                    (task_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["affected_tables"] = json.loads(d.pop("affected_tables_json") or "[]")
                out.append(d)
            return out

    def decide(self, ddl_id: int, decision: str, remember: bool) -> None:
        with self.db.write_conn() as c:
            c.execute(
                """UPDATE ddl_pending SET status=?, decided_at=datetime('now','localtime'),
                       remember_choice=? WHERE id=?""",
                (decision, 1 if remember else 0, ddl_id))

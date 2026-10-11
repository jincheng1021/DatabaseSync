"""备份导出服务（T2.9 / §4.2.3，E13）。

产物：.sql 文本 + gzip + SHA-256。
- 文件头会话环境声明（E13）：SET NAMES / sql_mode / time_zone /
  FOREIGN_KEY_CHECKS=0 / UNIQUE_CHECKS=0 + 注释头 —— 保证 mysql 客户端直接回放。
- 主体：DROP TABLE IF EXISTS + CREATE TABLE + 分批 INSERT（5000 行/语句）。
- rows_json：各表行数与 AUTO_INCREMENT 基线（恢复校验比对基准，§4.2.5）。
- 磁盘余量低于阈值拒绝新备份并落 alert_log；保留最近 N 份滚动清理。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import pymysql

from app.config import BACKUP_DIR
from app.core.errors import AppError, NotFoundError, ValidationError
from app.models.m2_repository import BackupRepository, RunRepository
from app.models.repository import SettingRepository
from app.core.ws_broadcaster import emit_from_thread
from app.services.full_engine import PositionCaptureError
from app.services.mysql_client import MySQLTarget, fetch_all, fetch_one, open_connection

log = logging.getLogger("dsync.backup")

MAGIC = "-- DSync-Backup:1"
STATEMENT_BATCH_ROWS = 5000


def split_sql_statements(text: str) -> list[str]:
    """按语句切分 .sql 文本：处理 '...'、"..."、`...`、-- 注释与 # 注释、反斜杠转义。

    仅需兼容本工具产物的精确格式（O13 边界：勿过度设计外部格式）。
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:  # 转义字符整体吞入
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and text[i:i + 2] == "--" and (i + 2 >= n or text[i + 2] in " \t\r\n"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


class BackupService:
    def __init__(self, *, backups: BackupRepository, runs: RunRepository,
                 settings: SettingRepository, tracker=None, get_source: callable,
                 alert_repo=None, broadcaster=None):
        self.backups = backups
        self.runs = runs
        self.settings = settings
        self.tracker = tracker
        self._get_source = get_source
        self.alert_repo = alert_repo
        self.broadcaster = broadcaster

    def _emit(self, event_type: str, payload: dict, task_id=None):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload, task_id)

    # ---------- 配置 ----------
    def _min_free_bytes(self) -> int:
        # P2：默认值已登记 GLOBAL_DEFAULTS（设置页可见可改），此处仅兜底
        return int(self.settings.get("backup_min_free_bytes") or 1024 ** 3)

    def _retention_keep(self) -> int:
        return int(self.settings.get("backup_retention_per_tag") or 20)

    def _alert(self, key: str, message: str, level: str = "warning", task_id: int | None = None):
        if self.alert_repo:
            try:
                self.alert_repo.fire(key, message, level=level, task_id=task_id)
            except Exception:  # noqa: BLE001
                log.exception("告警落库失败")

    # ---------- 导出 ----------
    def export(self, *, datasource_id: int, schema: str, tables: list[str] | None = None,
               structure_only: bool = False, created_by: str = "manual",
               retention_tag: str = "manual", task_id: int | None = None,
               ds_row: dict | None = None) -> list[int]:
        """导出备份；返回创建的 backup_file id 列表（当前为单个）。"""
        ds = ds_row  # 由调用方解密注入；保持接口简单
        if ds is None:
            # 防御：缺 ds_row 时 _get_source(None) 会在 "source_id" in None 处
            # 抛出难排查的 TypeError，这里显式报错
            raise AppError("备份数据源信息缺失（ds_row 未传入）",
                           hint="调用方需注入解密后的数据源行或任务行")
        # ---- 磁盘余量检查（低于阈值拒绝） ----
        free = shutil.disk_usage(BACKUP_DIR).free
        if free < self._min_free_bytes():
            msg = f"磁盘余量 {free / 1024 ** 3:.2f}GB 低于阈值，拒绝新备份"
            self._alert("backup_disk_shortage", msg, level="error", task_id=task_id)
            raise AppError(msg, hint="请清理磁盘或下调备份保留份数后重试")

        target: MySQLTarget = self._get_source(ds)
        conn = open_connection(target)
        run_id = self.runs.start(task_id=task_id, run_type="backup", trigger="manual")
        rp = self.tracker.start(run_id, task_id, "backup") if self.tracker else None
        out_name = f"dsync_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id}.sql.gz"
        out_path = BACKUP_DIR / out_name
        rows_baseline: dict[str, dict] = {}
        use_tables: list[str] = []   # 失败分支 progress 用（可能早于确定表清单就失败）
        try:
            conn.select_db(schema)
            with conn.cursor() as cur:
                cur.execute("SHOW VARIABLES")
                variables = {r["Variable_name"]: str(r["Value"]) for r in cur.fetchall()}
            charset = variables.get("character_set_server", target.charset or "utf8mb4")
            sql_mode = variables.get("sql_mode", "")
            time_zone = variables.get("time_zone") or variables.get("system_time_zone") or "SYSTEM"

            all_tables = None  # noqa: F841 —— 使用 SHOW FULL TABLES 统一取 BASE TABLE
            full_tables = fetch_all(conn, "SHOW FULL TABLES")
            base_tables = [r[f"Tables_in_{schema}"] for r in full_tables
                           if r.get("Table_type") == "BASE TABLE"]
            use_tables = tables or base_tables
            unknown = [t for t in (tables or []) if t not in base_tables]
            if unknown:
                raise ValidationError(f"备份表不存在：{', '.join(unknown)}")

            # ---- 一致性（B2 修复）：导出连接单次 FTWRL——锁内取位点，持续持锁至导出结束 ----
            # 修复前：capture_position 在独立连接取位后解锁，导出连接再次 FTWRL，
            # 两锁间隙的写入会使文件头位点与导出数据不对应，用该备份接管增量可能丢事件。
            lock_wait = int(self.settings.get("ftwrl_lock_wait_timeout") or 10)
            with conn.cursor() as cur:
                cur.execute(f"SET SESSION lock_wait_timeout={lock_wait}")
                try:
                    cur.execute("FLUSH TABLES WITH READ LOCK")
                except pymysql.err.OperationalError as e:
                    blocked = fetch_all(conn, "SHOW FULL PROCESSLIST")
                    hot = [{"id": r.get("Id"), "user": r.get("User"), "time": r.get("Time"),
                            "state": r.get("State"), "info": (r.get("Info") or "")[:200]}
                           for r in blocked
                           if (r.get("Command") not in ("Sleep",)) and int(r.get("Time") or 0) >= 3]
                    log.warning("备份 FTWRL 超时，阻塞摘要：%s", hot[:5])
                    raise PositionCaptureError(
                        f"FLUSH TABLES WITH READ LOCK 等待超过 {lock_wait}s，"
                        f"疑似长事务/长查询阻塞（错误：{e}）",
                        hint="请处理长事务或在低峰期重试") from e
            position: dict = {"method": "ftwrl", "extra": {"lock_wait_timeout": lock_wait}}
            with conn.cursor() as cur:
                # 锁内取位点（与导出同一把锁，位点与导出数据严格对应）
                if variables.get("gtid_mode", "OFF") != "OFF":
                    cur.execute("SELECT @@GLOBAL.gtid_executed AS g")
                    position["gtid_executed"] = cur.fetchone()["g"]
                cur.execute("SHOW MASTER STATUS")
                mrow = cur.fetchone()
                if mrow:
                    position["binlog_file"] = mrow.get("File")
                    position["binlog_pos"] = int(mrow.get("Position") or 0)
            try:
                sha = hashlib.sha256()
                uncompressed_bytes = 0
                file_sha_holder = hashlib.sha256()
                with gzip.open(out_path, "wb") as gz:
                    def w(s: str):
                        nonlocal uncompressed_bytes
                        data = s.encode("utf-8")
                        uncompressed_bytes += len(data)
                        gz.write(data)
                        file_sha_holder.update(data)

                    w(f"{MAGIC}\n")
                    w("-- Generator: DSync 备份导出（数据库同步工具）\n")
                    w(f"-- created_at: {datetime.now().isoformat(timespec='seconds')}\n")
                    w(f"-- source_version: {variables.get('version', '')}\n")
                    w(f"-- schema: {schema}\n")
                    w(f"-- tables: {','.join(use_tables)}\n")
                    w(f"-- structure_only: {int(bool(structure_only))}\n")
                    w(f"-- position: {json.dumps(position, ensure_ascii=False)}\n")
                    w(f"-- charset: {charset}  sql_mode: {sql_mode}  time_zone: {time_zone}\n")
                    w(f"SET NAMES {charset};\n")
                    w(f"SET SESSION sql_mode = '{sql_mode}';\n")
                    w(f"SET SESSION time_zone = '{time_zone}';\n")
                    w("SET FOREIGN_KEY_CHECKS=0;\n")
                    w("SET UNIQUE_CHECKS=0;\n\n")

                    for tname in use_tables:
                        create_row = fetch_one(conn, f"SHOW CREATE TABLE `{tname}`")
                        ddl = create_row["Create Table"] if create_row else ""
                        cnt = fetch_one(conn, f"SELECT COUNT(*) AS n FROM `{tname}`")["n"]
                        ai_row = fetch_one(conn,
                                           "SELECT auto_increment FROM information_schema.tables "
                                           "WHERE table_schema=%s AND table_name=%s",
                                           (schema, tname))
                        ai = (ai_row.get("auto_increment") or ai_row.get("AUTO_INCREMENT")) if ai_row else None
                        rows_baseline[tname] = {"rows": int(cnt), "auto_increment": int(ai) if ai else None}
                        # 表级明细（此前备份 run 无明细 → 执行历史表进度 0/0、Run 详情空白）
                        self.runs.upsert_table(run_id, tname, state="done",
                                               rows_done=int(cnt), rows_total=int(cnt))
                        w(f"-- table: {tname} rows={cnt} auto_increment={ai or 0}\n")
                        w(f"DROP TABLE IF EXISTS `{tname}`;\n")
                        w(ddl.rstrip(";\n ") + ";\n\n")
                        if not structure_only and cnt:
                            with conn.cursor(pymysql.cursors.SSCursor) as cur:
                                cur.execute(f"SELECT * FROM `{tname}`")
                                while True:
                                    rows = cur.fetchmany(STATEMENT_BATCH_ROWS)
                                    if not rows:
                                        break
                                    cols = [d[0] for d in cur._result.description]
                                    w(self._insert_sql(tname, cols, rows))
                    w("\n-- DSync-Backup-End\n")

                size = out_path.stat().st_size
                sha256 = self._sha256_of_file(out_path)
                backup_id = self.backups.create({
                    "task_id": task_id, "datasource_id": datasource_id,
                    "file_name": out_name, "file_path": str(out_path),
                    "size_bytes": size, "sha256": sha256,
                    "scope": {"schema": schema, "tables": use_tables,
                              "structure_only": bool(structure_only),
                              "full": tables is None,
                              "uncompressed_bytes": uncompressed_bytes,
                              "source_version": variables.get("version", "")},
                    "rows_baseline": rows_baseline,
                    "created_by": created_by, "retention_tag": retention_tag,
                })
                if rp:
                    rp.phase = "done"
                self.runs.finish(run_id, state="success",
                                 report={"backup_id": backup_id, "tables": use_tables,
                                         "rows_baseline": rows_baseline,
                                         "position": {k: position.get(k) for k in
                                                      ("gtid_executed", "binlog_file", "binlog_pos", "method")}},
                                 progress={"rows_inserted": sum(m["rows"] for m in rows_baseline.values()),
                                           "tables_total": len(use_tables),
                                           "tables_done": len(use_tables)})
                self._emit("run.finished", {"run_id": run_id, "task_id": task_id,
                                            "state": "success", "backup_id": backup_id}, task_id)
                # 保留最近 N 份滚动清理
                self._prune(retention_tag)
                return [backup_id]
            finally:
                with conn.cursor() as cur:
                    cur.execute("UNLOCK TABLES")
        except AppError:
            self.runs.finish(run_id, state="failed", error="备份失败（详见日志）",
                             progress={"tables_total": len(use_tables)})
            self._emit("run.finished", {"run_id": run_id, "task_id": task_id, "state": "failed"}, task_id)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("备份导出失败")
            self.runs.finish(run_id, state="failed", error=str(e),
                             progress={"tables_total": len(use_tables)})
            self._emit("run.finished", {"run_id": run_id, "task_id": task_id,
                                        "state": "failed", "error": str(e)}, task_id)
            self._alert(f"backup_failed:{datasource_id}", f"备份导出失败：{e}", level="error",
                        task_id=task_id)
            raise AppError(f"备份导出失败：{e}") from e
        finally:
            conn.close()
            if self.tracker:
                self.tracker.remove(run_id)

    @staticmethod
    def _insert_sql(table: str, cols: list[str], rows: list[tuple]) -> str:
        vals = []
        for r in rows:
            parts = []
            for v in r:
                if v is None:
                    parts.append("NULL")
                elif isinstance(v, (bytes, bytearray)):
                    # B3 修复：十六进制字面量 X'...'——无反斜杠/引号歧义，空值 X'' 合法，
                    # 修复前 _binary'...'+latin1 替换只转义单引号，含 \ 的二进制回放会语法错误/数据错乱
                    parts.append("X'" + bytes(v).hex() + "'")
                elif isinstance(v, bool):
                    parts.append("1" if v else "0")
                elif isinstance(v, (int, float)):
                    parts.append(repr(v))
                else:
                    s = (str(v).replace("\\", "\\\\").replace("'", "\\'")
                         .replace("\n", "\\n").replace("\r", "\\r")
                         .replace("\0", "\\0").replace("\x1a", "\\Z"))
                    parts.append("'" + s + "'")
            vals.append("(" + ", ".join(parts) + ")")
        col_list = ", ".join(f"`{c}`" for c in cols)
        return f"INSERT INTO `{table}` ({col_list}) VALUES\n" + ",\n".join(vals) + ";\n\n"

    @staticmethod
    def _sha256_of_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _prune(self, tag: str) -> list[int]:
        keep = self._retention_keep()
        stale = self.backups.oldest_ids_by_tag(tag, keep)
        removed = []
        for bid in stale:
            row = self.backups.get(bid)
            if row:
                try:
                    Path(row["file_path"]).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            self.backups.delete(bid)
            removed.append(bid)
        return removed

    # ---------- 管理 ----------
    def delete(self, backup_id: int) -> None:
        row = self.backups.get(backup_id)
        if not row:
            raise NotFoundError(f"备份 {backup_id} 不存在")
        if self.backups.is_restoring(backup_id):
            raise AppError("该备份正在恢复中，禁止删除")
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self.backups.delete(backup_id)

    def verify_integrity(self, backup_id: int) -> dict:
        """SHA-256 + gzip 解压试读（恢复预检第 1 步）。"""
        row = self.backups.get(backup_id)
        if not row:
            raise NotFoundError(f"备份 {backup_id} 不存在")
        path = Path(row["file_path"])
        if not path.exists():
            raise NotFoundError("备份文件已被移动或删除")
        actual = self._sha256_of_file(path)
        sha_ok = actual == row["sha256"]
        gzip_ok = True
        try:
            with gzip.open(path, "rb") as f:
                while f.read(1024 * 1024):
                    pass
        except Exception:  # noqa: BLE001
            gzip_ok = False
        header_ok = False
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                first = f.readline().strip()
            header_ok = first == MAGIC
        except Exception:  # noqa: BLE001
            pass
        return {"sha256_ok": sha_ok, "gzip_ok": gzip_ok, "is_dsync_format": header_ok,
                "sha256": actual}

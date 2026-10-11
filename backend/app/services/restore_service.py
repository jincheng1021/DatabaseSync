"""备份一键恢复服务（T2.10 / §4.2.5，最终完整版 V2）。

- 预检：SHA-256 + gzip 试读完整性、目标连通/写权限、磁盘余量（解压体积 × 3）、
  目标版本不低于备份源版本、关联任务检测、非空库覆盖提示（不阻断）。
- 执行：gzip 流式解压 → 逐语句解析 → 先应用文件头环境声明 → DDL 顺序执行、
  INSERT 聚合为批（批大小复用全量批大小，每批一事务）→ 单 worker 顺序回放，
  写入经全局总闸限流。完整恢复语义：不管目标是否已有数据，一律恢复为备份状态；
  整库备份可清理目标库中备份之外的表。
- 收尾：按 rows_json 对齐 AUTO_INCREMENT、行数校验、恢复报告。
- 编排：同一备份恢复互斥；关联任务自动暂停/恢复（失败保持暂停）；中断标记"恢复未完成"。
- 边界（O13）：仅支持本工具产物（MAGIC 文件头校验），外部 mysqldump 文件明确拒绝。
"""
from __future__ import annotations

import gzip
import logging
import re
import shutil
import threading
import time
from pathlib import Path

import pymysql

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.progress import ProgressTracker
from app.models.m2_repository import BackupRepository, RunRepository, TaskRepository
from app.models.repository import SettingRepository
from app.core.ws_broadcaster import emit_from_thread
from app.services.backup_service import MAGIC, split_sql_statements
from app.services.mysql_client import MySQLTarget, fetch_all, fetch_one, open_connection

log = logging.getLogger("dsync.restore")

_DDL_PREFIXES = ("CREATE ", "DROP ", "ALTER ", "TRUNCATE ", "RENAME ")
_SET_RE = re.compile(r"^SET\b", re.IGNORECASE)
# 回放语句 → 表名（INSERT/REPLACE/CREATE/DROP/TRUNCATE/ALTER；split_sql_statements 已剥掉
# `-- table:` 注释头，必须按语句文本解析；同一 chunk 可能跨表，需逐语句判定）
_STMT_TABLE_RE = re.compile(
    r"^\s*(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|"
    r"CREATE\s+TABLE|DROP\s+TABLE|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE)"
    r"(?:\s+(?:IF\s+NOT\s+EXISTS|IF\s+EXISTS))?\s+`?([\w$]+)`?",
    re.IGNORECASE)


def stmt_table(stmt: str) -> str | None:
    """从回放语句提取目标表名；非表级语句（SET/LOCK 等）返回 None。"""
    m = _STMT_TABLE_RE.match(stmt)
    return m.group(1) if m else None


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def expected_rows(meta: dict) -> int:
    """备份基准行数；-1 = 未记录（不参与校验）。

    回归锚点：此前写法 `int(meta.get("rows") or -1)` 把合法空表 rows=0
    短路成 -1，导致「备份含任意空表 → 恢复必然提示 成功(校验不一致)」（误报）。
    rows=None（键存在但值为空）同样视为未记录。
    """
    rows = meta.get("rows")
    return -1 if rows is None else int(rows)


class RestoreService:
    def __init__(self, *, backups: BackupRepository, runs: RunRepository, tasks: TaskRepository,
                 settings: SettingRepository, tracker: ProgressTracker, limiter,
                 get_target: callable, backup_service, audit=None, alert_repo=None,
                 broadcaster=None, orch=None, cdc=None):
        self.backups = backups
        self.runs = runs
        self.tasks = tasks
        self.settings = settings
        self.tracker = tracker
        self.limiter = limiter
        self._get_target = get_target
        self.backup_service = backup_service
        self.audit = audit
        self.alert_repo = alert_repo
        self.broadcaster = broadcaster
        self.orch = orch      # B5：状态迁移唯一入口（编排器）
        self.cdc = cdc        # B5：恢复期间联动启停增量引擎
        self._threads: dict[int, threading.Thread] = {}

    def _emit(self, event_type: str, payload: dict):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload)

    # ============================================================
    # 预检（向导第 2 步）
    # ============================================================
    def precheck(self, backup_id: int, target_ds_id: int, target_db: str) -> dict:
        row = self.backups.get(backup_id)
        if not row:
            raise NotFoundError(f"备份 {backup_id} 不存在")
        scope = row.get("scope") or {}
        items: list[dict] = []
        # 1) 完整性
        integrity = self.backup_service.verify_integrity(backup_id)
        items.append({"key": "integrity_sha256", "level": "pass" if integrity["sha256_ok"] else "fail",
                      "message": "SHA-256 校验通过" if integrity["sha256_ok"] else "SHA-256 不一致，文件已损坏"})
        items.append({"key": "integrity_gzip", "level": "pass" if integrity["gzip_ok"] else "fail",
                      "message": "gzip 完整性通过" if integrity["gzip_ok"] else "gzip 解压试读失败"})
        if not integrity["is_dsync_format"]:
            items.append({"key": "format", "level": "fail",
                          "message": "非本工具生成的备份文件（缺少 DSync 文件头），外部 mysqldump 文件不在本期恢复范围",
                          "hint": "仅支持本工具产物回放（O13 边界）"})
        # 2) 目标连通 + 写权限
        ds = self.tasks.db.conn  # noqa: attribute hint only
        tgt: MySQLTarget = self._get_target(self._get_ds(target_ds_id))
        conn = None
        affected_tasks: list[dict] = []
        non_empty_tables: list[str] = []
        try:
            conn = open_connection(tgt)
            ver = fetch_one(conn, "SELECT VERSION() AS v")["v"]
            src_ver = scope.get("source_version", "")
            level = "pass" if parse_version(ver) >= parse_version(src_ver) else "fail"
            items.append({"key": "version", "level": level,
                          "message": f"目标版本 {ver} / 备份源版本 {src_ver or '未知'}",
                          "hint": None if level == "pass" else "目标版本不得低于备份源版本"})
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}`")
            conn.select_db(target_db)
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS `__dsync_restore_probe` (id INT PRIMARY KEY)")
                    cur.execute("DROP TABLE `__dsync_restore_probe`")
                items.append({"key": "write_permission", "level": "pass", "message": "目标库写权限通过"})
            except Exception as e:  # noqa: BLE001
                items.append({"key": "write_permission", "level": "fail",
                              "message": f"目标库写权限不足：{e}"})
            non_empty_tables = [list(r.values())[0] for r in fetch_all(conn, "SHOW TABLES")]
        except Exception as e:  # noqa: BLE001
            items.append({"key": "connectivity", "level": "fail", "message": f"目标连通失败：{e}"})
        finally:
            if conn:
                conn.close()
        # 3) 磁盘余量（解压体积 × 3）
        uncompressed = int(scope.get("uncompressed_bytes") or 0)
        need = uncompressed * 3
        free = shutil.disk_usage(Path(row["file_path"]).parent).free
        items.append({"key": "disk", "level": "pass" if free >= need else "fail",
                      "message": f"需约 {need / 1024 ** 3:.2f}GB（解压体积×3），当前余量 {free / 1024 ** 3:.2f}GB"})
        # 4) 关联任务检测
        for t in self.tasks.list_by_target(target_ds_id):
            tdb = (t.get("mapping") or {}).get("target_database") or t.get("_target_default_schema")
            affected_tasks.append({"task_id": t["id"], "name": t["name"], "state": t["state"],
                                   "target_db": tdb})
        # 5) 非空提示（不阻断：完整恢复语义，一律覆盖为备份状态）
        if non_empty_tables:
            items.append({"key": "target_non_empty", "level": "warn",
                          "message": f"目标库非空（{len(non_empty_tables)} 个对象）：将完整覆盖恢复为备份状态，"
                                     f"同名对象先删后建"
                          + ("，备份之外的表也将清理" if scope.get("full") else "")})
        return {"items": items, "affected_tasks": affected_tasks,
                "non_empty_tables": non_empty_tables[:50],
                "rows_baseline": row.get("rows_baseline"),
                "scope": scope}

    def _get_ds(self, ds_id: int) -> dict:
        # 借用数据源仓库（service 装配时注入 db；此处经 backup_service 的会话仓库不可达，独立查询）
        from app.models.repository import DatasourceRepository
        return DatasourceRepository(self.tasks.db).get(ds_id)  # type: ignore[arg-type]

    # ============================================================
    # 发起
    # ============================================================
    def start_restore(self, backup_id: int, *, target_ds_id: int, target_db: str,
                      safety_net: bool = True, auto_pause_related: bool = True,
                      drop_extra_tables: bool = True) -> int:
        """完整恢复到备份状态：不管目标库是否已有数据，一律覆盖式回放。

        - 目标库不存在则自动创建；已存在不再要求二次确认（完整恢复语义）。
        - drop_extra_tables 且备份为整库备份（scope.full）时，回放后清理目标库中
          备份之外的表，使目标库表集合与备份完全一致。
        """
        row = self.backups.get(backup_id)
        if not row:
            raise NotFoundError(f"备份 {backup_id} 不存在")
        if self.backups.is_restoring(backup_id):
            raise ConflictError("同一备份文件已有恢复正在进行（互斥）")
        # 目标库不存在则创建（完整恢复不依赖目标现状）
        conn = open_connection(self._get_target(self._get_ds(target_ds_id)))
        try:
            try:
                conn.select_db(target_db)
            except Exception:  # noqa: BLE001 —— 目标库不存在
                with conn.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}`")
                conn.select_db(target_db)
        finally:
            conn.close()
        precheck = self.precheck(backup_id, target_ds_id, target_db)
        failed = [i for i in precheck["items"] if i["level"] == "fail"]
        if failed:
            raise ValidationError("恢复预检存在失败项，禁止发起",
                                  hint="；".join(f"{i['key']}: {i['message']}" for i in failed))
        run_id = self.runs.start(task_id=None, run_type="restore", trigger="manual",
                                 backup_id=backup_id, tables=list((row.get("rows_baseline") or {}).keys()))
        t = threading.Thread(target=self._run_restore,
                             args=(run_id, row, target_ds_id, target_db, safety_net,
                                   auto_pause_related, drop_extra_tables),
                             daemon=True, name=f"restore-{run_id}")
        self._threads[run_id] = t
        t.start()
        return run_id

    def cancel(self, run_id: int) -> bool:
        return self.tracker.request_cancel(run_id)

    # ============================================================
    # 执行
    # ============================================================
    def _run_restore(self, run_id: int, backup_row: dict, target_ds_id: int,
                     target_db: str, safety_net: bool, auto_pause_related: bool,
                     drop_extra_tables: bool = True):
        rp = self.tracker.start(run_id, None, "restore")
        rp.phase = "restore"
        report: dict = {"run_id": run_id, "backup_id": backup_row["id"],
                        "target_db": target_db, "warnings": [], "replayed_statements": 0,
                        "replayed_rows": 0, "row_count_mismatch": [], "failed_statement": None}
        paused_tasks: list[dict] = []
        # 表级明细跟踪（run_table_detail 此前全程不更新 → 详情 10 张表永远「等待/0 行」、
        # 执行历史表进度 0/N）：table=当前回放表、rows=该表已回放行数、done=已完成校验的表
        baseline: dict = backup_row.get("rows_baseline") or {}
        track: dict = {"table": None, "rows": 0, "done": set()}
        try:
            # 目标定位必须在 try 内：数据源被删等异常若逃逸线程，run 将永远 running、
            # is_restoring 永真 → 该备份永久无法删除/再次恢复
            target: MySQLTarget = self._get_target(self._get_ds(target_ds_id))
            # ---- 关联任务编排：恢复期间自动暂停（B5：走编排器 + 引擎联动，禁止直写 state） ----
            related = [t for t in self.tasks.list_by_target(target_ds_id)
                       if ((t.get("mapping") or {}).get("target_database") or "") == target_db
                       or not (t.get("mapping") or {}).get("target_database")]
            if auto_pause_related:
                for t in related:
                    if t["state"] == "cdc_running":
                        try:
                            if self.cdc:
                                self.cdc.pause(t["id"])   # 先停拉增量（隔离写入）
                            if self.orch:
                                self.orch.transition(t["id"], "paused",
                                                     reason="备份恢复期间自动暂停（关联目标库）")
                            else:
                                self.tasks.update(t["id"], {"state": "paused"})  # 兼容未装配编排器
                            paused_tasks.append({"task": t, "prev_state": t["state"]})
                            report["warnings"].append(f"关联任务 {t['name']} 已自动暂停")
                        except Exception:  # noqa: BLE001
                            log.exception("关联任务 %s 暂停失败", t["id"])
                            report["warnings"].append(f"关联任务 {t['name']} 暂停失败，请人工确认")
                    elif t["state"] in ("full_resyncing", "initializing"):
                        rp = self.tracker.get_by_task(t["id"])
                        if rp:
                            try:
                                self.tracker.request_pause(rp.run_id)
                                paused_tasks.append({"task": t, "prev_state": t["state"]})
                                report["warnings"].append(f"关联任务 {t['name']} 的全量 run 已请求暂停")
                            except Exception:  # noqa: BLE001
                                log.exception("关联任务 %s 全量 run 暂停失败", t["id"])
            # ---- 安全网：恢复前自动备份目标库涉及表（B7：仅备份目标库实际存在的表） ----
            # 缺表本就是典型恢复场景，安全网不得反向阻断恢复；export 对不存在的表会抛错，
            # 故先与目标库 SHOW TABLES 求交集，缺表记 warning 跳过。
            if safety_net:
                try:
                    baseline_tables = list((backup_row.get("rows_baseline") or {}).keys())
                    conn_sn = open_connection(target)
                    try:
                        conn_sn.select_db(target_db)
                        existing = {list(r.values())[0] for r in fetch_all(conn_sn, "SHOW TABLES")}
                    finally:
                        conn_sn.close()
                    missing = [t for t in baseline_tables if t not in existing]
                    if missing:
                        report["warnings"].append(
                            f"目标库缺少 {len(missing)} 张表，安全网跳过这些表：{', '.join(missing[:10])}"
                            + ("…" if len(missing) > 10 else ""))
                    to_backup = [t for t in baseline_tables if t in existing]
                    if to_backup:
                        ids = self.backup_service.export(
                            datasource_id=target_ds_id, schema=target_db,
                            tables=to_backup,
                            created_by="auto", retention_tag="restore_safety_net",
                            ds_row=self._get_ds(target_ds_id))
                        report["safety_net_backups"] = ids
                    else:
                        report["warnings"].append("目标库无涉及表存在，安全网备份跳过")
                except AppError as e:
                    raise ValidationError(f"安全网备份失败，恢复中止：{e.message}") from e

            batch_rows_limit = int(self.settings.get("full_batch_size") or 5000)
            conn = open_connection(target)
            conn.select_db(target_db)
            file_size = Path(backup_row["file_path"]).stat().st_size
            executed_ddl = 0
            try:
                raw = open(backup_row["file_path"], "rb")
                with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as f:
                    buf: list[str] = []
                    header_done = False
                    for chunk in iter(lambda: f.read(1024 * 1024), ""):
                        if rp.cancel_requested:
                            raise AppError("恢复已取消")
                        buf.append(chunk)
                        progress = min(raw.tell() / max(file_size, 1), 1.0)
                        rp.rows_done = int(progress * 10000)  # 万分比（进度条复用）
                        text = "".join(buf)
                        statements = split_sql_statements(text)
                        # 最后一段可能不完整：保留到下一轮
                        tail = statements.pop() if statements else text
                        for stmt in statements:
                            if rp.cancel_requested:
                                raise AppError("恢复已取消")
                            n = self._apply_statement(conn, stmt, batch_rows_limit, rp, report)
                            self._track_replay(run_id, stmt, n, baseline, track)
                            if _SET_RE.match(stmt):
                                header_done = True
                            elif stmt.upper().startswith(("CREATE ", "DROP ")):
                                executed_ddl += 1
                            else:
                                report["replayed_statements"] += 1
                        buf = [tail if not tail.endswith(";") else ""]
                        if tail.endswith(";"):
                            n = self._apply_statement(conn, tail.rstrip(";"), batch_rows_limit, rp, report)
                            self._track_replay(run_id, tail, n, baseline, track)
                            if not _SET_RE.match(tail) and not tail.upper().startswith(("CREATE ", "DROP ")):
                                report["replayed_statements"] += 1
                    # flush 尾段
                    rest = "".join(buf).strip()
                    if rest:
                        for stmt in split_sql_statements(rest + ";"):
                            n = self._apply_statement(conn, stmt, batch_rows_limit, rp, report)
                            self._track_replay(run_id, stmt, n, baseline, track)
                            if not _SET_RE.match(stmt) and not stmt.upper().startswith(("CREATE ", "DROP ")):
                                report["replayed_statements"] += 1
                report["executed_ddl"] = executed_ddl
                if not header_done:
                    report["warnings"].append("文件头环境声明缺失（异常产物），已按目标库默认会话回放")
            finally:
                conn.close()
                # 当前表已回放行数落明细
                if track["table"]:
                    self.runs.upsert_table(run_id, track["table"], rows_done=track["rows"])

            # ---- 完整恢复到备份状态：整库备份清理目标库中备份之外的表 ----
            scope = backup_row.get("scope") or {}
            backup_tables = set(scope.get("tables")
                                or (backup_row.get("rows_baseline") or {}).keys())
            if drop_extra_tables and scope.get("full") and backup_tables:
                try:
                    conn = open_connection(target)
                    conn.select_db(target_db)
                    dropped: list[str] = []
                    try:
                        for r in fetch_all(conn, "SHOW FULL TABLES"):
                            tname = list(r.values())[0]
                            if r.get("Table_type") == "BASE TABLE" and tname not in backup_tables:
                                with conn.cursor() as cur:
                                    cur.execute(f"DROP TABLE IF EXISTS `{tname}`")
                                dropped.append(tname)
                        conn.commit()
                    finally:
                        conn.close()
                    if dropped:
                        report["dropped_extra_tables"] = dropped
                        report["warnings"].append(
                            f"已清理目标库中备份之外的 {len(dropped)} 张表：{', '.join(dropped[:10])}"
                            + ("…" if len(dropped) > 10 else ""))
                except Exception as e:  # noqa: BLE001
                    report["warnings"].append(f"备份外表清理失败（不影响已回放内容）：{e}")

            # ---- 收尾：自增对齐（基准 rows_json）+ 行数校验 + 表级明细回填 ----
            conn = open_connection(target)
            conn.select_db(target_db)
            try:
                for tname, meta in baseline.items():
                    ai = meta.get("auto_increment")
                    if ai:
                        with conn.cursor() as cur:
                            cur.execute(f"ALTER TABLE `{tname}` AUTO_INCREMENT={int(ai)}")
                    t = fetch_one(conn, f"SELECT COUNT(*) AS n FROM `{tname}`")["n"]
                    exp = expected_rows(meta)
                    actual = int(t)
                    mismatch = exp >= 0 and actual != exp
                    if mismatch:
                        report["row_count_mismatch"].append(
                            {"table": tname, "expected": meta.get("rows"), "actual": actual})
                    # 表级明细终态：done/mismatch + 实际行数（覆盖空表/未被回放的表）
                    self.runs.upsert_table(run_id, tname,
                                           state="mismatch" if mismatch else "done",
                                           rows_done=actual,
                                           rows_total=exp if exp >= 0 else None)
                    track["done"].add(tname)
            finally:
                conn.close()

            ok = not report["row_count_mismatch"]
            report["verify"] = "pass" if ok else "mismatch"
            self.runs.finish(run_id, state="success" if ok else "success_with_mismatch",
                             report=report,
                             progress={"rows_inserted": report["replayed_rows"],
                                       "tables_total": len(baseline),
                                       "tables_done": len(track["done"])})
            self._emit("run.finished", {"run_id": run_id, "task_id": None, "backup_id": backup_row["id"],
                                        "state": "success" if ok else "success_with_mismatch"})
            # ---- 关联任务自动恢复（B5：引擎联动 + 编排器迁移；失败保持暂停并提示人工确认） ----
            if auto_pause_related:
                for item in paused_tasks:
                    t, prev = item["task"], item["prev_state"]
                    try:
                        if prev == "cdc_running":
                            if self.cdc and self.cdc.is_running(t["id"]):
                                self.cdc.resume(t["id"])
                            elif self.cdc:
                                # 引擎不在（如恢复期间崩溃重启）：重新拉起，位点续传
                                try:
                                    self.cdc.start(t["id"], trigger="restore_resume")
                                except Exception as e:  # noqa: BLE001
                                    log.exception("关联任务 %s 增量引擎拉起失败", t["id"])
                                    report["warnings"].append(
                                        f"关联任务 {t['name']} 恢复失败（引擎拉起失败：{e}），请人工确认")
                                    continue
                            if self.orch:
                                self.orch.transition(t["id"], "cdc_running",
                                                     reason="备份恢复完成，自动恢复增量")
                            else:
                                self.tasks.update(t["id"], {"state": prev})  # 兼容未装配编排器
                        elif prev in ("full_resyncing", "initializing"):
                            rp = self.tracker.get_by_task(t["id"])
                            if rp:
                                self.tracker.request_resume(rp.run_id)
                        report["warnings"].append(f"关联任务 {t['name']} 已自动恢复")
                    except Exception:  # noqa: BLE001
                        log.exception("关联任务 %s 自动恢复失败", t["id"])
                        report["warnings"].append(f"关联任务 {t['name']} 恢复失败，请人工确认")
        except AppError as e:
            report["error"] = e.message
            report["verify"] = "restore_incomplete"
            self._mark_restore_failed_tables(run_id, baseline, track, e.message)
            self.runs.finish(run_id, state="failed", error=e.message, report=report,
                             progress={"tables_total": len(baseline),
                                       "tables_done": len(track["done"])})
            self._emit("run.finished", {"run_id": run_id, "task_id": None, "backup_id": backup_row["id"],
                                        "state": "failed", "error": e.message})
            self._alert(f"restore_failed:{backup_row['id']}",
                        f"恢复失败（目标库标记「恢复未完成」）：{e.message}", level="error")
        except Exception as e:  # noqa: BLE001
            log.exception("restore run %s failed", run_id)
            report["error"] = str(e)
            report["verify"] = "restore_incomplete"
            self._mark_restore_failed_tables(run_id, baseline, track, str(e))
            self.runs.finish(run_id, state="failed", error=str(e), report=report,
                             progress={"tables_total": len(baseline),
                                       "tables_done": len(track["done"])})
            self._emit("run.finished", {"run_id": run_id, "task_id": None, "backup_id": backup_row["id"],
                                        "state": "failed", "error": str(e)})
            self._alert(f"restore_failed:{backup_row['id']}",
                        f"恢复异常（目标库标记「恢复未完成」）：{e}", level="error")
        finally:
            self.tracker.remove(run_id)

    def _track_replay(self, run_id: int, stmt: str, rows: int, baseline: dict,
                      track: dict) -> None:
        """回放明细跟踪：识别当前表，表切换时落 copying + 基准行数（明细不再停留「等待」）。

        upsert_table 自带 INSERT-if-missing 兜底，runs.start 未预插的表也会被补上；
        每条 INSERT 即一个 ~5000 行的聚合批，逐语句落库量可忽略。
        """
        tname = stmt_table(stmt)
        if tname and tname != track["table"]:
            prev = track["table"]
            if prev:
                self.runs.upsert_table(run_id, prev, rows_done=track["rows"])
            track["table"] = tname
            track["rows"] = 0
            exp = expected_rows(baseline.get(tname) or {})
            self.runs.upsert_table(run_id, tname, state="copying",
                                   rows_total=exp if exp >= 0 else None)
        if rows:
            track["rows"] += rows
            self.runs.upsert_table(run_id, track["table"] or "_", rows_done=track["rows"])

    def _mark_restore_failed_tables(self, run_id: int, baseline: dict, track: dict,
                                    err: str) -> None:
        """失败收尾：当前表标 failed，其余未完成表标 skipped（不再停留 pending/等待）。"""
        try:
            done: set = track.get("done") or set()
            cur_table = track.get("table")
            for tname in baseline:
                if tname in done:
                    continue
                if tname == cur_table:
                    self.runs.upsert_table(run_id, tname, state="failed",
                                           rows_done=track.get("rows", 0), error=err[:200])
                else:
                    self.runs.upsert_table(run_id, tname, state="skipped")
        except Exception:  # noqa: BLE001 —— 明细标记失败不影响 run 终态落库
            log.exception("restore run %s 表级失败标记落库异常", run_id)

    def _apply_statement(self, conn, stmt: str, batch_rows_limit: int, rp, report: dict) -> int:
        """单语句回放：批级重试 3 次；经全局总闸限流。返回该语句回放行数（非 INSERT 返回 0）。"""
        is_insert = stmt.upper().startswith("INSERT ")
        rows_in_stmt = stmt.count("\n(") if is_insert else 0
        attempt = 0
        while True:
            try:
                if is_insert:
                    # P2-5：阻塞式取令牌（task_id=0 → 仅全局总闸），限流真正生效
                    self.limiter.acquire_blocking(0, max(rows_in_stmt, 1))
                with conn.cursor() as cur:
                    cur.execute(stmt)
                conn.commit()
                if is_insert:
                    report["replayed_rows"] += rows_in_stmt
                return rows_in_stmt
            except pymysql.err.OperationalError:
                attempt += 1
                if attempt >= 3:
                    report["failed_statement"] = stmt[:500]
                    raise AppError(f"恢复回放失败（重试耗尽）：{stmt[:120]}…",
                                   hint="目标库标记「恢复未完成」，请整体重跑恢复")
                time.sleep(0.5 * (2 ** (attempt - 1)))
            except Exception:
                conn.rollback()
                attempt += 1
                if attempt >= 3:
                    report["failed_statement"] = stmt[:500]
                    raise AppError(f"恢复回放失败（重试耗尽）：{stmt[:120]}…",
                                   hint="目标库标记「恢复未完成」，请整体重跑恢复")
                time.sleep(0.5 * (2 ** (attempt - 1)))

    def _alert(self, key: str, message: str, level: str = "warning"):
        if self.alert_repo:
            try:
                self.alert_repo.fire(key, message, level=level)
            except Exception:  # noqa: BLE001
                pass

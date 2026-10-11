"""全量引擎服务（T2.2~T2.6 / §4.2.1、T2.8 / §4.2.2）。

三场景共用：新库初始化（含清空重建）、全量重刷（REPLACE + 孤儿清理 + 安全网）、
备份导出的取位点复用。执行线程 = PyMySQL 同步连接 + SSCursor 流式读取；
进度经 ProgressTracker 由主循环每秒广播。

关键设计点：
- E7 取位点：FTWRL + 会话级 lock_wait_timeout=10 超时保护；GTID+全 InnoDB 一致快照无锁备选。
- E6 结构指纹：结构迁移后逐表登记；每表开拷前复核，不一致 → 中止该表转 DDL 待决策。
- 两级限流器接入；批级重试 3 次指数退避；失败表隔离继续。
- 暂停=停取新批、断点（keyset 位点）可恢复；取消=终止并标记。
"""
from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

import pymysql

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.limiter import TwoLevelLimiter
from app.core.orchestrator import (BACKING_UP, CDC_RUNNING, DRAFT, FULL_RESYNCING, IDLE,
                                   INITIALIZING, Orchestrator)
from app.core.progress import ProgressTracker
from app.core.ws_broadcaster import emit_from_thread
from app.models.m2_repository import PositionRepository, RunRepository, TaskRepository
from app.models.repository import SettingRepository
from app.services.mysql_client import (MySQLTarget, db_exists, fetch_all, fetch_all_lc,
                                       fetch_one, open_connection, open_connection_with_retry,
                                       probe_databases)
from app.services.cdc_transform import eval_predicate
from app.services.structure_fingerprint import clean_definer, fingerprint, normalize_ddl

log = logging.getLogger("dsync.full")


class PositionCaptureError(AppError):
    status_code = 500
    code = "POSITION_CAPTURE_FAILED"
    message = "取一致位点失败"


class TableCancelled(Exception):
    pass


class RunAborted(Exception):
    """整次运行被取消。"""


# ============================================================
# 位点捕获（T2.2，E7）
# ============================================================

def capture_position(target: MySQLTarget, lock_wait_timeout: int = 10,
                     lockless: bool = False) -> dict:
    """取一致位点 P0。默认 FTWRL 路径；GTID+全 InnoDB 可走无锁一致快照。"""
    conn = open_connection(target)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW VARIABLES")
            variables = {r["Variable_name"]: str(r["Value"]) for r in cur.fetchall()}
        gtid_on = variables.get("gtid_mode", "OFF") != "OFF"

        if lockless:
            if not gtid_on:
                raise ValidationError("无锁取位要求源库开启 GTID",
                                      hint="未开 GTID 时必须走 FTWRL 路径")
            engines = {r["engine"].upper() for r in fetch_all(
                conn, "SELECT DISTINCT engine FROM information_schema.tables "
                      "WHERE table_schema=%s", (conn.db.decode() if isinstance(conn.db, bytes) else target.default_schema or "mysql",))}
            if engines - {"INNODB"}:
                raise ValidationError("库内存在非 InnoDB 表，无锁一致快照不保证一致性",
                                      hint="含非事务表时必须走 FTWRL 路径")
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                cur.execute("SELECT @@GLOBAL.gtid_executed AS g")
                gtid = cur.fetchone()["g"]
                # 快照事务在数据读取连接中保持（简化：位点记录后立即结束，
                # 全量重放一致性由 P0 幂等吸收兜底——M3 增量接入后改为快照挂起）
                cur.execute("ROLLBACK")
            return {"gtid_executed": gtid, "binlog_file": None, "binlog_pos": None,
                    "method": "consistent_snapshot", "extra": {"lockless": True}}

        # ---- 默认 FTWRL 路径（10s 超时保护） ----
        pos: dict = {}
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION lock_wait_timeout={int(lock_wait_timeout)}")
            try:
                cur.execute("FLUSH TABLES WITH READ LOCK")
            except pymysql.err.OperationalError as e:
                blocked = fetch_all(conn, "SHOW FULL PROCESSLIST")
                hot = [{"id": r.get("Id"), "user": r.get("User"), "time": r.get("Time"),
                        "state": r.get("State"), "info": (r.get("Info") or "")[:200]}
                       for r in blocked
                       if (r.get("Command") not in ("Sleep",)) and int(r.get("Time") or 0) >= 3]
                raise PositionCaptureError(
                    f"FLUSH TABLES WITH READ LOCK 等待超过 {lock_wait_timeout}s，"
                    f"疑似长事务/长查询阻塞（错误：{e}）",
                    hint="请处理长事务或在低峰期重试；GTID+全 InnoDB 库可切换无锁取位") from e
            try:
                if gtid_on:
                    cur.execute("SELECT @@GLOBAL.gtid_executed AS g")
                    pos["gtid_executed"] = cur.fetchone()["g"]
                cur.execute("SHOW MASTER STATUS")
                row = cur.fetchone()
                if row:
                    pos["binlog_file"] = row.get("File")
                    pos["binlog_pos"] = int(row.get("Position") or 0)
            finally:
                cur.execute("UNLOCK TABLES")
        pos["method"] = "ftwrl"
        pos["extra"] = {"lock_wait_timeout": lock_wait_timeout}
        return pos
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 表元数据与映射
# ============================================================

def list_source_tables(src_conn, schema: str) -> list[dict]:
    rows = fetch_all(src_conn,
                     "SELECT table_name, table_type, engine, table_rows, auto_increment "
                     "FROM information_schema.tables WHERE table_schema=%s", (schema,))
    return [{"name": r["table_name"] if "table_name" in r else r["TABLE_NAME"],
             "type": r.get("table_type") or r.get("TABLE_TYPE"),
             "engine": r.get("engine") or r.get("ENGINE"),
             "rows_est": int(r.get("table_rows") or r.get("TABLE_ROWS") or 0),
             "auto_increment": r.get("auto_increment") or r.get("AUTO_INCREMENT")}
            for r in rows]


def get_primary_key(src_conn, schema: str, table: str) -> list[str]:
    rows = fetch_all(src_conn,
                     "SELECT column_name FROM information_schema.key_column_usage "
                     "WHERE table_schema=%s AND table_name=%s AND constraint_name='PRIMARY' "
                     "ORDER BY ordinal_position", (schema, table))
    return [r.get("column_name") or r.get("COLUMN_NAME") for r in rows]


class MappingResolver:
    """mapping_json（T2.1 本期范围）：同名整库 / 勾选表 / 表重命名 / 前后缀 / 库级重映射。"""

    def __init__(self, mapping: dict, source_schema: str | None, target_default_schema: str | None):
        m = mapping or {}
        self.scope = m.get("scope", "all")
        self.selected = m.get("tables") or []
        self.rename = m.get("rename") or {}
        self.prefix = m.get("prefix") or ""
        self.suffix = m.get("suffix") or ""
        self.source_schema = m.get("source_database") or source_schema
        # 库级重映射：源 A → 目标 B
        self.target_schema = m.get("target_database") or target_default_schema
        if not self.source_schema:
            raise ValidationError("未指定源库（default_schema 或映射 source_database）"
                                  "——可为源数据源设置默认库，或在任务表映射中补全源库")
        if not self.target_schema:
            raise ValidationError("未指定目标库（default_schema 或映射 target_database）"
                                  "——可为目标数据源设置默认库，或在任务表映射中填写目标库")

    def resolve(self, all_tables: list[str]) -> list[str]:
        if self.scope == "selected":
            missing = [t for t in self.selected if t not in all_tables]
            if missing:
                raise ValidationError(f"勾选表在源库不存在：{', '.join(missing)}")
            names = list(self.selected)
        else:
            names = list(all_tables)
        return names

    def target_name(self, src_table: str) -> str:
        name = self.rename.get(src_table, src_table)
        return f"{self.prefix}{name}{self.suffix}"


# ============================================================
# 引擎主体
# ============================================================

class FullEngine:
    def __init__(self, *, tasks: TaskRepository, runs: RunRepository, positions: PositionRepository,
                 settings: SettingRepository, orchestrator: Orchestrator,
                 tracker: ProgressTracker, limiter: TwoLevelLimiter,
                 get_source: callable, get_target: callable, backup_service=None,
                 broadcaster=None, cdc_engine=None, alert_repo=None, verify_service=None):
        self.tasks = tasks
        self.runs = runs
        self.positions = positions
        self.settings = settings
        self.orch = orchestrator
        self.tracker = tracker
        self.limiter = limiter
        self._get_source = get_source    # (ds_row) -> MySQLTarget（解密）
        self._get_target = get_target
        self.backup_service = backup_service  # T2.8 安全网复用备份导出；main 装配时注入
        self.broadcaster = broadcaster
        self.cdc_engine = cdc_engine          # M3：位点移交 + 重刷缓存编排回调
        self.alert_repo = alert_repo          # M4：任务失败告警（AlertService）
        self.verify_service = verify_service  # M4：全量/重刷成功后自动行数校验
        self._threads: dict[int, threading.Thread] = {}

    def _emit(self, event_type: str, payload: dict, task_id: int | None = None):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload, task_id)

    # ---------- 参数 ----------
    def _p(self, key: str, default):
        v = self.settings.get(key)
        return default if v is None else v

    def _tuned(self, target: MySQLTarget) -> MySQLTarget:
        """引擎链路建连超时调优：读全局设置（默认 120s）覆盖 mysql_client 的 30s 默认。

        低性能库单批读/写/commit fsync 超过 30s 即 2013；pymysql connect 的握手
        读包也用 read_timeout（run#38 直接死因）。
        """
        return dataclasses.replace(
            target,
            read_timeout=int(self._p("db_read_timeout_seconds", 120)),
            write_timeout=int(self._p("db_write_timeout_seconds", 120)),
        )

    def _reconnect(self, target: MySQLTarget, schema: str, *, fk_checks_off: bool = False):
        """断线重建：建连（带重试）+ select_db（pymysql 重连会丢掉会话选中的库）。"""
        conn = open_connection_with_retry(target)
        conn.select_db(schema)
        if fk_checks_off:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
        return conn

    def _ensure_alive(self, conn, target: MySQLTarget, schema: str):
        """收尾长连接探活：拷贝期间整段空闲，可能已被服务器 wait_timeout 掐断。

        ping(reconnect=True) 重连后必须重新 select_db（重连丢库）；探活失败整连重建。
        """
        try:
            conn.ping(reconnect=True)
            conn.select_db(schema)
            return conn
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return self._reconnect(target, schema)

    # ---------- 启动入口 ----------
    def _spawn(self, run_id: int, target, args: tuple, name: str) -> None:
        """登记并启动 run 线程；线程结束时回收引用（P2-1：原实现只增不清，长驻进程泄漏）。"""
        def _wrapped(*a):
            try:
                target(*a)
            finally:
                self._threads.pop(run_id, None)
        t = threading.Thread(target=_wrapped, args=args, daemon=True, name=name)
        self._threads[run_id] = t
        t.start()

    def start_init(self, task_id: int, *, confirm_rebuild: bool = False,
                   confirm_name: str | None = None, trigger: str = "manual") -> int:
        task = self._must_task(task_id)
        if task["state"] in (INITIALIZING, FULL_RESYNCING, BACKING_UP):
            raise ConflictError("任务正在运行中")
        self.orch.transition(task_id, INITIALIZING, reason="发起初始化")
        run_id = self.runs.start(task_id=task_id, run_type="init", trigger=trigger)
        self._spawn(run_id, self._run_init, (task, run_id, confirm_rebuild, confirm_name),
                    f"init-{run_id}")
        return run_id

    def start_full_resync(self, task_id: int, *, tables: list[str] | None = None,
                          safety_net: bool = True, orphan_cleanup: bool = True,
                          trigger: str = "manual") -> int:
        task = self._must_task(task_id)
        if task["state"] in (INITIALIZING, FULL_RESYNCING, BACKING_UP):
            raise ConflictError("任务正在运行中")
        self.orch.transition(task_id, FULL_RESYNCING, reason="发起全量重刷")
        run_id = self.runs.start(task_id=task_id, run_type="full_resync", trigger=trigger)
        self._spawn(run_id, self._run_resync,
                    (task, run_id, tables, safety_net, orphan_cleanup), f"resync-{run_id}")
        return run_id

    def pause(self, run_id: int) -> bool:
        return self.tracker.request_pause(run_id)

    def resume(self, run_id: int) -> bool:
        return self.tracker.request_resume(run_id)

    def cancel(self, run_id: int) -> bool:
        return self.tracker.request_cancel(run_id)

    def _finish(self, run_id: int, state: str, error: str | None, report: dict | None,
                task_id: int | None) -> None:
        # 执行历史进度回填：run_history 的 rows_inserted/tables_done/tables_total
        # 此前只在 start 写 tables_total=0 且终态从不更新 → 执行历史页「表进度」恒 0/0
        progress: dict | None = None
        rp = self.tracker.get(run_id)
        if rp is not None:
            progress = {"rows_inserted": rp.rows_done,
                        "tables_total": len(rp.tables),
                        "tables_done": sum(1 for t in rp.tables.values() if t.state == "done")}
        self.runs.finish(run_id, state=state, error=error, report=report, progress=progress)
        self._emit("run.finished", {"run_id": run_id, "task_id": task_id, "state": state,
                                    "error": error}, task_id)

    def _post_resync_state(self, task: dict, reason: str) -> None:
        """重刷/重做成功后的任务状态：增量引擎仍在运行时必须回 CDC_RUNNING，
        否则任务卡死在 IDLE（binlog 仍在写目标库，但 stop 要求 cdc_running、
        start 又报「已在运行」→ 无法停止也无法自然恢复）。"""
        if (task.get("mode") == "init_plus_cdc" and self.cdc_engine is not None
                and self.cdc_engine.is_running(task["id"])):
            self.orch.transition_from_current(task["id"], CDC_RUNNING, reason=reason + "，增量引擎仍在运行")
        else:
            self.orch.transition_from_current(task["id"], IDLE, reason=reason)

    # ---------- 决策后受影响表重做（E6：从该表结构阶段重做） ----------
    def start_table_redo(self, task_id: int, tables: list[str]) -> int:
        task = self._must_task(task_id)
        if task["state"] in (INITIALIZING, FULL_RESYNCING, BACKING_UP):
            raise ConflictError("任务正在运行中")
        if not tables:
            raise ValidationError("未指定需重做的表")
        self.orch.transition_from_current(task_id, FULL_RESYNCING, reason="DDL 决策后重做受影响表")
        run_id = self.runs.start(task_id=task_id, run_type="table_redo", trigger="manual",
                                 tables=tables)
        self._spawn(run_id, self._run_table_redo, (task, run_id, tables), f"redo-{run_id}")
        return run_id

    def _run_table_redo(self, task: dict, run_id: int, tables: list[str]):
        rp = self.tracker.start(run_id, task["id"], "table_redo")
        # M3 T3.8：重做期间涉及表增量事件入缓存，完成后重放
        if self.cdc_engine is not None:
            try:
                self.cdc_engine.begin_resync(task["id"], tables)
            except Exception:  # noqa: BLE001
                log.exception("重做缓存启动失败 task=%s", task["id"])
        report: dict = {"run_id": run_id, "redo_tables": tables, "warnings": [],
                        "failed_tables": [], "row_count_mismatch": []}
        try:
            src = self._tuned(self._get_source(self.tasks.get(task["id"])))
            tgt = self._tuned(self._get_target(self.tasks.get(task["id"])))
            mapping = dict(task["mapping"] or {})
            mapping.update({"scope": "selected", "tables": tables})
            resolver = MappingResolver(mapping, src.default_schema, tgt.default_schema)
            self.limiter.configure_task(task["id"], task["rate_limit_rows"])
            src_conn = open_connection_with_retry(src)
            tgt_conn = open_connection_with_retry(tgt)
            # 数据源未设 default_schema 时连接无默认库，未限定库名的语句（SHOW CREATE 等）报 1046
            src_conn.select_db(resolver.source_schema)
            try:
                # 结构阶段重做（DROP+CREATE）+ 指纹重登记
                rp.phase = "structure"
                fingerprints = self._migrate_structure(src_conn, tgt_conn, src, resolver,
                                                       task, rp, report)
                rp.phase = "copy"
                self._copy_tables(src, tgt, resolver, task, rp, run_id, tables,
                                  fingerprints, report, replace_mode=True, orphan_cleanup=False)
                rp.phase = "finalize"
                self._finalize(src_conn, tgt_conn, src, tgt, resolver, tables, fingerprints, rp, report)
                if rp.cancel_requested:
                    raise RunAborted()
                self._finish(run_id, "success", None, report, task["id"])
                self._post_resync_state(task, "受影响表重做完成")
            finally:
                src_conn.close()
                tgt_conn.close()
        except RunAborted:
            self._finish(run_id, "cancelled", "用户取消", report, task["id"])
            self._safe_transition_back(task, IDLE, "重做取消")
        except TableCancelled:
            # 拷贝阶段取消：按取消语义收尾（此前落入 except Exception → 误报失败+告警）
            self._finish_table_cancelled(task, run_id, report)
        except AppError as e:
            report["error"] = e.message
            self._finish(run_id, "failed", e.message, report, task["id"])
            self._safe_to_error(task["id"], e.message)
        except Exception as e:  # noqa: BLE001
            log.exception("table redo run %s failed", run_id)
            report["error"] = str(e)
            self._finish(run_id, "failed", str(e), report, task["id"])
            self._safe_to_error(task["id"], str(e))
        finally:
            # 受影响表重做属局部重刷：同样走缓存编排（T3.8）
            if self.cdc_engine is not None:
                try:
                    self.cdc_engine.end_resync(task["id"])
                except Exception:  # noqa: BLE001
                    log.exception("重做缓存重放触发失败 task=%s", task["id"])
            self.tracker.remove(run_id)
            self.limiter.remove_task(task["id"])

    # ---------- 初始化 ----------
    def _run_init(self, task: dict, run_id: int, confirm_rebuild: bool, confirm_name: str | None):
        rp = self.tracker.start(run_id, task["id"], "init")
        report: dict = {"run_id": run_id, "warnings": [], "skipped_objects": [],
                        "failed_tables": [], "fingerprint_changed": [], "row_count_mismatch": []}
        try:
            src = self._tuned(self._get_source(self.tasks.get(task["id"])))
            tgt = self._tuned(self._get_target(self.tasks.get(task["id"])))
            resolver = MappingResolver(task["mapping"], src.default_schema, tgt.default_schema)
            self.limiter.configure_task(task["id"], task["rate_limit_rows"])
            src_conn = open_connection_with_retry(src)
            tgt_conn = open_connection_with_retry(tgt)
            # 数据源未设 default_schema 时连接无默认库，未限定库名的语句（SHOW CREATE 等）报 1046
            src_conn.select_db(resolver.source_schema)
            try:
                # ---- 前置检查：目标库不存在则建库（批量创建/手输新库名场景）；
                #      存在则校验空/非空（§4.2.1 第 1 步） ----
                if not db_exists(tgt_conn, resolver.target_schema):
                    self._ensure_target_database(src_conn, tgt_conn, src, resolver)
                    existing: list[str] = []
                else:
                    tgt_conn.select_db(resolver.target_schema)
                    existing = [list(r.values())[0] for r in fetch_all(tgt_conn, "SHOW TABLES")]
                if existing:
                    if not confirm_rebuild:
                        raise ConflictError(
                            f"目标库 {resolver.target_schema} 非空（{len(existing)} 个对象）",
                            hint="请显式选择「清空重建」并手动输入目标库名确认")
                    if confirm_name != resolver.target_schema:
                        raise ValidationError("清空重建确认失败：输入的库名与目标库不一致")
                rp.phase = "position"
                # ---- P0 位点（E7） ----
                lockless = bool((task.get("ddl_rules") or {}).get("lockless_position"))
                pos = capture_position(src, int(self._p("ftwrl_lock_wait_timeout", 10)),
                                       lockless=lockless)
                if task["mode"] != "backup_only":
                    self.positions.save_p0(task["id"], pos)
                report["position"] = {k: pos.get(k) for k in ("gtid_executed", "binlog_file", "binlog_pos", "method")}

                # ---- 清空重建（确认通过后） ----
                if existing:
                    with tgt_conn.cursor() as cur:
                        cur.execute("SET FOREIGN_KEY_CHECKS=0")
                        for tname in existing:
                            cur.execute(f"DROP TABLE IF EXISTS `{tname}`")
                        cur.execute("SET FOREIGN_KEY_CHECKS=1")

                # ---- 结构迁移 + 指纹登记（E6） ----
                rp.phase = "structure"
                fingerprints = self._migrate_structure(src_conn, tgt_conn, src, resolver,
                                                       task, rp, report)

                # ---- 数据拷贝 ----
                rp.phase = "copy"
                tables = resolver.resolve([t["name"] for t in list_source_tables(src_conn, resolver.source_schema)])
                self._copy_tables(src, tgt, resolver, task, rp, run_id,
                                  tables, fingerprints, report, replace_mode=False,
                                  orphan_cleanup=False)

                # ---- 收尾 ----
                rp.phase = "finalize"
                self._finalize(src_conn, tgt_conn, src, tgt, resolver, tables, fingerprints, rp, report)
                if rp.cancel_requested:
                    raise RunAborted()
                report["position"] = report.get("position") or {}
                self._finish(run_id, "success", None, report, task["id"])
                if task["mode"] == "init_plus_cdc" and self.cdc_engine is not None:
                    # M3 移交（T2.6 → T3.2）：增量从 P0 启动，幂等写吸收快照期间重复变更
                    self.orch.transition(task["id"], CDC_RUNNING,
                                         reason="初始化完成，移交 P0 位点启动增量")
                    try:
                        self.cdc_engine.start(task["id"], trigger="auto_handoff")
                    except Exception:  # noqa: BLE001
                        log.exception("初始化后增量启动失败 task=%s", task["id"])
                        self.orch.transition_from_current(task["id"], IDLE,
                                                          reason="增量启动失败，转空闲待人工")
                else:
                    self.orch.transition(task["id"], IDLE,
                                         reason="初始化成功（仅全量）")
            finally:
                src_conn.close()
                tgt_conn.close()
        except RunAborted:
            self._finish(run_id, "cancelled", "用户取消（未完成初始化）", report, task["id"])
            self.orch.transition_from_current(task["id"], DRAFT, reason="取消：标记未完成初始化")
        except TableCancelled:
            # 拷贝阶段取消：按取消语义收尾（此前落入 except Exception → 误报失败+告警）
            self._finish_table_cancelled(task, run_id, report, back_state=DRAFT)
        except AppError as e:
            report["error"] = e.message
            self._finish(run_id, "failed", e.message, report, task["id"])
            self._safe_to_error(task["id"], e.message)
        except Exception as e:  # noqa: BLE001
            log.exception("init run %s failed", run_id)
            report["error"] = str(e)
            self._finish(run_id, "failed", str(e), report, task["id"])
            self._safe_to_error(task["id"], str(e))
        finally:
            self.tracker.remove(run_id)
            self.limiter.remove_task(task["id"])

    # ---------- 全量重刷 ----------
    def _run_resync(self, task: dict, run_id: int, tables: list[str] | None,
                    safety_net: bool, orphan_cleanup: bool):
        rp = self.tracker.start(run_id, task["id"], "full_resync")
        report: dict = {"run_id": run_id, "warnings": [], "failed_tables": [],
                        "orphan_deleted": {}, "row_count_mismatch": []}
        safety_backup_ids: list[int] = []
        try:
            src = self._tuned(self._get_source(self.tasks.get(task["id"])))
            tgt = self._tuned(self._get_target(self.tasks.get(task["id"])))
            resolver = MappingResolver(task["mapping"], src.default_schema, tgt.default_schema)
            self.limiter.configure_task(task["id"], task["rate_limit_rows"])
            src_conn = open_connection_with_retry(src)
            tgt_conn = open_connection_with_retry(tgt)
            # 数据源未设 default_schema 时连接无默认库，未限定库名的语句（SHOW CREATE 等）报 1046
            src_conn.select_db(resolver.source_schema)
            try:
                tgt_conn.select_db(resolver.target_schema)
                all_tables = [t["name"] for t in list_source_tables(src_conn, resolver.source_schema)]
                use_tables = tables or all_tables
                unknown = [t for t in use_tables if t not in all_tables]
                if unknown:
                    raise ValidationError(f"重刷表在源库不存在：{', '.join(unknown)}")
                # ---- 安全网：重刷前自动备份目标库涉及表（任务级开关默认开） ----
                effective_safety = safety_net and bool(task["safety_net_backup"])
                if effective_safety and self.backup_service is not None:
                    # 安全网备份目标库：表名必须经 rename/前后缀映射换成目标表名，
                    # 否则 rename 配置下目标库查无此表 → export 抛错 → 整次重刷启动失败
                    # P2 修复：带数据备份（修复前 structure_only=True 只备结构，
                    # 重刷破坏数据后安全网无法回滚，名不副实）
                    safety_backup_ids = self.backup_service.export(
                        datasource_id=int(task["target_id"]),
                        tables=[resolver.target_name(t) for t in use_tables],
                        schema=resolver.target_schema, structure_only=False,
                        created_by="auto", retention_tag="resync_safety_net",
                        task_id=task["id"],
                        # ds_row 必传：任务行含 source_id/target_id，_resolve_source 可据此
                        # 解密定位目标库（缺失时 export 拿到 None → "in None" TypeError）
                        ds_row=task)
                    report["safety_net_backups"] = safety_backup_ids
                # ---- 结构指纹快照（重刷不取一致位点，直接对源库当前态覆盖） ----
                fingerprints = {t["name"]: fingerprint(self._show_create(src_conn, t["name"]))
                                for t in list_source_tables(src_conn, resolver.source_schema)
                                if t["name"] in use_tables}
                # M3 T3.8：重刷期间涉及表增量事件转入缓存队列（不涉及表直通）
                if self.cdc_engine is not None:
                    self.cdc_engine.begin_resync(task["id"], use_tables)
                rp.phase = "copy"
                self._copy_tables(src, tgt, resolver, task, rp, run_id,
                                  use_tables, fingerprints, report, replace_mode=True,
                                  orphan_cleanup=orphan_cleanup and task["orphan_cleanup"])
                rp.phase = "finalize"
                self._finalize(src_conn, tgt_conn, src, tgt, resolver, use_tables, fingerprints, rp, report,
                               sample_checksum=True)
                if rp.cancel_requested:
                    raise RunAborted()
                self._finish(run_id, "success", None, report, task["id"])
                self._post_resync_state(task, "全量重刷成功")
                # M4 T4.2：全量/重刷成功后自动行数校验（trigger=auto）
                if self.verify_service is not None:
                    try:
                        self.verify_service.auto_row_count_after_resync(task["id"], run_id)
                    except Exception:  # noqa: BLE001
                        log.exception("重刷后自动校验触发失败 task=%s", task["id"])
            finally:
                src_conn.close()
                tgt_conn.close()
        except RunAborted:
            self._finish(run_id, "cancelled", "用户取消", report, task["id"])
            self._safe_transition_back(task, IDLE, "重刷取消")
        except TableCancelled:
            # 拷贝阶段取消：按取消语义收尾（此前落入 except Exception → 误报失败+告警）
            self._finish_table_cancelled(task, run_id, report)
        except AppError as e:
            report["error"] = e.message
            self._finish(run_id, "failed", e.message, report, task["id"])
            self._safe_to_error(task["id"], e.message)
        except Exception as e:  # noqa: BLE001
            log.exception("resync run %s failed", run_id)
            report["error"] = str(e)
            self._finish(run_id, "failed", str(e), report, task["id"])
            self._safe_to_error(task["id"], str(e))
        finally:
            # M3 T3.8：重刷结束（成功/取消/失败）触发缓存按 binlog 顺序重放，清空后恢复直通
            if self.cdc_engine is not None:
                try:
                    self.cdc_engine.end_resync(task["id"])
                except Exception:  # noqa: BLE001
                    log.exception("重刷缓存重放触发失败 task=%s", task["id"])
            self.tracker.remove(run_id)
            self.limiter.remove_task(task["id"])

    # ---------- 结构迁移（T2.3） ----------
    def _show_create(self, conn, table: str) -> str:
        row = fetch_one(conn, f"SHOW CREATE TABLE `{table}`")
        return row["Create Table"] if row and "Create Table" in row else (list(row.values())[1] if row else "")

    def _ensure_target_database(self, src_conn, tgt_conn, src: MySQLTarget,
                                resolver: MappingResolver) -> None:
        """目标库不存在则建库（幂等），并 select_db。

        字符集取源库 SCHEMATA 实际值（库级 DEFAULT_CHARACTER_SET_NAME），
        数据源连接 charset 仅作回退——避免连接配置与服务端实际字符集不一致导致漂移。
        """
        with tgt_conn.cursor() as cur:
            cs_rows = fetch_all_lc(src_conn,
                "SELECT DEFAULT_CHARACTER_SET_NAME AS charset_name FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME=%s", (resolver.source_schema,))
            db_charset = (cs_rows[0].get("charset_name") if cs_rows else None) or src.charset
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{resolver.target_schema}` "
                        f"DEFAULT CHARACTER SET {db_charset}")
        tgt_conn.select_db(resolver.target_schema)

    def target_db_state(self, task_id: int) -> str:
        """探测任务目标库状态：missing / empty / nonempty。

        供调度 full_resync 首触发路由（目标库不存在→先初始化建库）与批量创建校验复用。
        """
        task = self._must_task(task_id)
        src = self._get_source(self.tasks.get(task_id))
        tgt = self._get_target(self.tasks.get(task_id))
        resolver = MappingResolver(task.get("mapping") or {}, src.default_schema, tgt.default_schema)
        info = probe_databases(tgt, [resolver.target_schema]).get(resolver.target_schema) or {}
        if not info.get("exists"):
            return "missing"
        return "empty" if int(info.get("tables") or 0) == 0 else "nonempty"

    def _migrate_structure(self, src_conn, tgt_conn, src: MySQLTarget, resolver: MappingResolver,
                           task: dict, rp, report: dict) -> dict[str, str]:
        """顺序：表 → 视图 → 存储过程/函数 → 事件；返回指纹表。"""
        fingerprints: dict[str, str] = {}
        schema = resolver.source_schema
        self._ensure_target_database(src_conn, tgt_conn, src, resolver)

        tables = [t for t in list_source_tables(src_conn, schema) if t["type"] == "BASE TABLE"]
        names = resolver.resolve([t["name"] for t in tables])
        for t in tables:
            if t["name"] not in names:
                continue
            ddl = self._show_create(src_conn, t["name"])
            with tgt_conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{resolver.target_name(t['name'])}`")
                cur.execute(clean_definer(ddl).replace(
                    f"`{t['name']}`", f"`{resolver.target_name(t['name'])}`", 1))
            fingerprints[t["name"]] = fingerprint(ddl)
            self.runs.upsert_table(rp.run_id, t["name"], state="structure_done",
                                   rows_total=t["rows_est"])
            tp = rp.tables.setdefault(t["name"], _tp(t["name"]))
            tp.state = "structure_done"
            tp.rows_total = t["rows_est"]

        # 视图
        views = fetch_all(src_conn,
                          "SELECT table_name FROM information_schema.views WHERE table_schema=%s",
                          (schema,))
        for r in views:
            vname = r.get("table_name") or r.get("TABLE_NAME")
            try:
                row = fetch_one(src_conn, f"SHOW CREATE VIEW `{vname}`")
                ddl = clean_definer(row["Create View"])
                with tgt_conn.cursor() as cur:
                    cur.execute(f"DROP VIEW IF EXISTS `{resolver.target_name(vname)}`")
                    cur.execute(ddl.replace(f"`{vname}`", f"`{resolver.target_name(vname)}`", 1))
            except Exception as e:  # noqa: BLE001 —— 权限不足跳过并登记（不静默）
                report["skipped_objects"].append({"object": vname, "kind": "view", "reason": str(e)})

        # 存储过程/函数（8.0 SHOW_ROUTINE / 5.7 mysql.proc SELECT；权限不足跳过登记）
        routines = fetch_all(src_conn,
                             "SELECT routine_name, routine_type FROM information_schema.routines "
                             "WHERE routine_schema=%s", (schema,))
        for r in routines:
            rname = r.get("routine_name") or r.get("ROUTINE_NAME")
            rtype = (r.get("routine_type") or r.get("ROUTINE_TYPE") or "PROCEDURE").upper()
            kind = "procedure" if rtype == "PROCEDURE" else "function"
            try:
                row = fetch_one(src_conn, f"SHOW CREATE {rtype} `{rname}`")
                ddl = clean_definer(row.get("Create Procedure") or row.get("Create Function") or "")
                with tgt_conn.cursor() as cur:
                    cur.execute(f"DROP {rtype} IF EXISTS `{rname}`")
                    cur.execute(ddl)
            except Exception as e:  # noqa: BLE001
                report["skipped_objects"].append({"object": rname, "kind": kind, "reason": str(e)})

        # 事件
        try:
            events = fetch_all(src_conn,
                               "SELECT event_name FROM information_schema.events WHERE event_schema=%s",
                               (schema,))
            for r in events:
                ename = r.get("event_name") or r.get("EVENT_NAME")
                try:
                    row = fetch_one(src_conn, f"SHOW CREATE EVENT `{ename}`")
                    ddl = clean_definer(row.get("Create Event") or "")
                    with tgt_conn.cursor() as cur:
                        cur.execute(f"DROP EVENT IF EXISTS `{ename}`")
                        cur.execute(ddl)
                except Exception as e:  # noqa: BLE001
                    report["skipped_objects"].append({"object": ename, "kind": "event", "reason": str(e)})
        except Exception as e:  # noqa: BLE001
            report["skipped_objects"].append({"object": "*", "kind": "event", "reason": f"定义读取失败：{e}"})

        # 触发器：默认不迁移（目标库为只读副本，避免重复触发）；开关开启时补建并红色警示
        if task.get("migrate_triggers"):
            report["warnings"].append("已开启触发器迁移：目标库触发器可能导致变更重复执行，请逐表确认风险")
            try:
                trigs = fetch_all(src_conn,
                                  "SELECT trigger_name, event_object_table FROM information_schema.triggers "
                                  "WHERE event_object_schema=%s", (schema,))
                for r in trigs:
                    tname = r.get("trigger_name") or r.get("TRIGGER_NAME")
                    try:
                        row = fetch_one(src_conn, f"SHOW CREATE TRIGGER `{tname}`")
                        ddl = clean_definer(row.get("SQL Original Statement") or "")
                        with tgt_conn.cursor() as cur:
                            cur.execute(ddl)
                    except Exception as e:  # noqa: BLE001
                        report["skipped_objects"].append({"object": tname, "kind": "trigger",
                                                          "reason": str(e)})
            except Exception as e:  # noqa: BLE001
                report["skipped_objects"].append({"object": "*", "kind": "trigger", "reason": str(e)})
        return fingerprints

    # ---------- 数据拷贝（T2.4） ----------
    def _copy_tables(self, src: MySQLTarget, tgt: MySQLTarget, resolver: MappingResolver,
                     task: dict, rp, run_id: int, tables: list[str],
                     fingerprints: dict[str, str], report: dict,
                     *, replace_mode: bool, orphan_cleanup: bool) -> None:
        batch_size = int(task["batch_size"] or self._p("full_batch_size", 5000))
        workers = max(1, min(16, int(self._p("full_table_concurrency", 4))))
        schema = resolver.source_schema
        src_conn = open_connection_with_retry(src)
        try:
            metas = {t["name"]: t for t in list_source_tables(src_conn, schema)}
        finally:
            src_conn.close()
        # 大表先跑（按估算行数降序防长尾）
        ordered = sorted(tables, key=lambda n: -metas.get(n, {}).get("rows_est", 0))
        for n in ordered:
            rp.tables.setdefault(n, _tp(n))
            rp.tables[n].rows_total = metas.get(n, {}).get("rows_est")
        # 总进度分母 = Σ 表行数估算（此前结构阶段误把「表个数」写进 rows_total 且 copy 阶段
        # 从不覆盖 → rows_done 按行累加迅速超过分母，进度恒 100%、ETA 恒 None）
        rp.rows_total = sum(int(metas.get(n, {}).get("rows_est") or 0) for n in tables)
        errors: list[str] = []
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # 每表独立连接（PyMySQL 连接非线程安全）
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="copy") as pool:
            futures = {pool.submit(self._copy_one, src, tgt, resolver, task, rp,
                                   run_id, name, metas.get(name, {}), fingerprints, report,
                                   batch_size, replace_mode, orphan_cleanup, errors): name
                       for name in ordered}
            # P0 修复：逐个收集——此前 fut.result() 单异常即中断收集并跳过
            # failed_tables 登记（run#38 现场 failed_tables 为空的直接原因）
            first_escape: Exception | None = None
            for fut in as_completed(futures):
                try:
                    fut.result()
                except TableCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("拷贝线程异常逃逸（表 %s）：%s", futures[fut], e)
                    first_escape = first_escape or e
        # failed_tables 必须在逃逸异常上抛之前落 report（否则再次被短路）
        if errors:
            report["failed_tables"] = errors
        if first_escape:
            raise first_escape

    def _wait_if_paused(self, rp, run_id: int, table: str, checkpoint: dict | None) -> None:
        # P2-2：暂停期间 checkpoint 只落库一次（修复前每 0.5s 重复写同值，无谓 SQLite 写放大）
        if checkpoint is not None and rp.paused and not rp.cancel_requested:
            self.runs.upsert_table(run_id, table, checkpoint=checkpoint)
        while rp.paused and not rp.cancel_requested:
            time.sleep(0.5)
        if rp.cancel_requested:
            raise TableCancelled()

    @staticmethod
    def _pk_to_json(v):
        """主键值 → JSON 安全值（checkpoint 落库无 default，datetime/bytes 原样会崩）。"""
        if isinstance(v, (bytes, bytearray)):
            return {"__hex": bytes(v).hex()}
        if isinstance(v, datetime):
            return v.isoformat(sep=" ")     # MySQL 隐式转换字符串比较 datetime
        if isinstance(v, Decimal):
            return str(v)
        return v

    @staticmethod
    def _pk_from_json(v):
        """checkpoint 恢复：还原查询参数（__hex 还原为 bytes 交 pymysql 转义）。"""
        if isinstance(v, dict) and "__hex" in v:
            return bytes.fromhex(v["__hex"])
        return v

    def _iter_batches(self, src_conn, schema: str, table: str, cols: list[str],
                      pk: list[str], batch_size: int, checkpoint: dict | None) -> Iterator[list[tuple]]:
        """keyset 分批流式读取；无主键退化 LIMIT/OFFSET。

        P2-3 修复：复合主键用行构造元组 keyset `(pk1,pk2) > (%s,%s)` + 全主键
        ORDER BY——修复前复合主键表 checkpoint 存 None（断点失效，重启后
        int(None) 崩溃或整表重扫），且无 ORDER BY 的 OFFSET 分页在源库并发写时跳行。
        """
        col_list = ", ".join(f"`{c}`" for c in cols)
        if pk:
            pkc_list = ", ".join(f"`{c}`" for c in pk)
            pk_idxs = [cols.index(c) for c in pk]
            last = (checkpoint or {}).get("last_pk")
            if last is not None:
                last = [self._pk_from_json(v) for v in last] \
                    if isinstance(last, (list, tuple)) else [self._pk_from_json(last)]
            # P0 修复：每批一条独立 SQL，批间用本批最后一行主键推进 keyset。
            # 原实现单条 SQL 带 LIMIT 且批间不推进 → 生成器只 yield 第一批，
            # 有主键表每表只拷 batch_size 行即标 done（数据不完整，run#38 现场）。
            # 每批独立短查询对低性能源库最稳（无长游标挂起），断点续传语义天然正确。
            while True:
                if last is None:
                    where, args = "", ()
                elif len(last) == 1:
                    where, args = f" WHERE `{pk[0]}` > %s", (last[0],)
                else:
                    tpl = ", ".join(["%s"] * len(last))
                    where = f" WHERE ({pkc_list}) > ({tpl})"
                    args = tuple(last)
                sql = (f"SELECT {col_list} FROM `{schema}`.`{table}`{where}"
                       f" ORDER BY {pkc_list} LIMIT {int(batch_size)}")
                with src_conn.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute(sql, args)
                    rows = cur.fetchall()
                if not rows:
                    return
                yield rows
                last = [rows[-1][i] for i in pk_idxs]
        else:
            offset = int((checkpoint or {}).get("offset") or 0)
            while True:
                sql = (f"SELECT {col_list} FROM `{schema}`.`{table}` "
                       f"LIMIT {int(batch_size)} OFFSET {offset}")
                with src_conn.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
                if not rows:
                    break
                yield rows
                offset += len(rows)

    def _copy_one(self, src: MySQLTarget, tgt: MySQLTarget, resolver: MappingResolver,
                  task: dict, rp, run_id: int, table: str, meta: dict,
                  fingerprints: dict[str, str], report: dict, batch_size: int,
                  replace_mode: bool, orphan_cleanup: bool, errors: list[str]) -> None:
        tp = rp.tables[table]
        # B8 修复：统一 REPLACE INTO 幂等写。init 场景目标库为空时语义与 INSERT 等价，
        # 但 FTWRL 释放后才拷贝，断点重跑/批级重试在部分提交后重放不会撞主键——
        # 与「幂等写吸收快照期间重复变更」的设计声明一致。replace_mode 参数保留
        # 用于孤儿清理门控（仅重刷路径清理孤儿行）。
        verb = "REPLACE INTO"
        src_conn = None
        tgt_conn = None
        try:
            # 建连必须入 try：失败走表级失败登记，否则异常逃出线程 → run 悬死/failed_tables 空（run#38）
            src_conn = open_connection_with_retry(src)
            tgt_conn = open_connection_with_retry(tgt)
            tgt_conn.select_db(resolver.target_schema)
            src_conn.select_db(resolver.source_schema)
            with tgt_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
            # ---- E6 开拷前指纹复核 ----
            fp_now = fingerprint(self._show_create(src_conn, table))
            if table in fingerprints and fp_now != fingerprints[table]:
                tp.state = "ddl_blocked"
                tp.error = "结构指纹不一致：拷贝期检测到源库 DDL 变更"
                self.runs.upsert_table(run_id, table, state="ddl_blocked",
                                       checkpoint={"source_fingerprint": fp_now},
                                       error=tp.error)
                self.orch.enter_ddl_pending(task["id"], reason=f"表 {table} 结构指纹不一致，拷贝中止")
                raise TableCancelled()
            pk = get_primary_key(src_conn, resolver.source_schema, table)
            cols_rows = fetch_all(src_conn,
                                  "SELECT column_name FROM information_schema.columns "
                                  "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                                  (resolver.source_schema, table))
            all_cols = [r.get("column_name") or r.get("COLUMN_NAME") for r in cols_rows]
            # ---- M4 口径统一（§13 第 5 条）：全量链路应用列过滤/行过滤，与增量 cdc_transform
            #      共用同一份 mapping_json —— excluded 列不写目标库，谓词不满足行不拷贝 ----
            mapping = task.get("mapping") or {}
            excluded = set((mapping.get("column_filter") or {}).get(table) or []) - set(pk)
            pred = (mapping.get("row_filters") or {}).get(table)
            cols = [c for c in all_cols if c not in excluded]   # 写入目标库的映射列
            write_idx = [all_cols.index(c) for c in cols]
            if excluded:
                report["warnings"].append(
                    f"表 {table} 列过滤：排除列 {', '.join(sorted(excluded))} 不参与全量拷贝")
            if pred:
                report["warnings"].append(
                    f"表 {table} 行过滤：全量仅拷贝满足谓词的行（子集同步，孤儿清理自动禁用）")
            tgt_table = resolver.target_name(table)
            if not pk:
                report["warnings"].append(f"表 {table} 无主键：LIMIT/OFFSET 单线程拷贝，断点恢复能力受限")
            pk_idxs = [all_cols.index(c) for c in pk] if pk else []   # P2-3：全主键列索引
            checkpoint = self.runs.get_table_checkpoint(run_id, table) or {}
            self.runs.upsert_table(run_id, table, state="copying")
            tp.state = "copying"
            rows_total = 0
            # 扫描口径 offset：与 _iter_batches 的 LIMIT/OFFSET 语义一致（按「扫描」行数推进）。
            # 断点续扫：源侧断线（2013/2006）重建连接后，用推进后的位点重建生成器续跑。
            # resume_ckpt 与 _iter_batches 内部 keyset 恒等（每批扫描最后一行主键）。
            base_offset = int((checkpoint or {}).get("offset") or 0)
            scanned = 0
            resume_ckpt = dict(checkpoint)
            rebuilds = 0
            while True:
                try:
                    for rows in self._iter_batches(src_conn, resolver.source_schema, table,
                                                   all_cols, pk, batch_size, resume_ckpt):
                        scanned += len(rows)
                        # 行数估算（information_schema.table_rows）可能小于实际：
                        # 仅内存上调防进度 >100%，不落库（口径仍为估算）
                        if tp.rows_total is None or scanned > (tp.rows_total or 0):
                            tp.rows_total = scanned
                        scan_last = rows[-1]
                        if pk:
                            resume_ckpt["last_pk"] = [self._pk_to_json(scan_last[i]) for i in pk_idxs]
                        else:
                            resume_ckpt["offset"] = base_offset + scanned
                        # 行过滤（E3）：与增量链路同口径（静态谓词求值）
                        if pred:
                            rows = [r for r in rows if eval_predicate(pred, dict(zip(all_cols, r)))]
                            if not rows:
                                continue
                        self._wait_if_paused(rp, run_id, table,
                                             {"last_pk": resume_ckpt.get("last_pk"),
                                              "offset": resume_ckpt.get("offset")})
                        self.limiter.acquire_blocking(task["id"], len(rows))  # P2-5：阻塞式取令牌
                        # 写侧批级重试 3 次指数退避；断连（2013/2006）重建目标连接
                        # （REPLACE INTO 幂等，重放安全）
                        attempt = 0
                        while True:
                            try:
                                tpl = f"({', '.join(['%s'] * len(cols))})"
                                sql = (f"{verb} `{tgt_table}` ({', '.join(f'`{c}`' for c in cols)}) "
                                       f"VALUES {tpl}")
                                with tgt_conn.cursor() as cur:
                                    cur.executemany(sql, [tuple(r[i] for i in write_idx) for r in rows])
                                tgt_conn.commit()
                                break
                            except pymysql.err.InterfaceError:
                                attempt += 1
                                if attempt >= 3:
                                    raise
                                tgt_conn = self._reconnect(tgt, resolver.target_schema,
                                                           fk_checks_off=True)
                                time.sleep(0.5 * (2 ** (attempt - 1)))
                            except pymysql.err.OperationalError as e:
                                attempt += 1
                                if attempt >= 3:
                                    raise
                                if e.args and e.args[0] in (2013, 2006):
                                    try:
                                        tgt_conn.close()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    tgt_conn = self._reconnect(tgt, resolver.target_schema,
                                                               fk_checks_off=True)
                                time.sleep(0.5 * (2 ** (attempt - 1)))
                            except Exception:
                                tgt_conn.rollback()
                                raise
                        rows_total += len(rows)
                        tp.rows_done = rows_total
                        rp.add_rows(len(rows))   # P2-6：加锁累加，多拷贝线程并发不丢计数
                        self.runs.upsert_table(run_id, table, rows_done=rows_total)
                    break  # 生成器正常收尾
                except TableCancelled:
                    raise
                except pymysql.err.OperationalError as e:
                    # 读侧断线：源连接 2013/2006 → 重建后从断点续扫（最多 2 次）
                    if not (e.args and e.args[0] in (2013, 2006)) or rebuilds >= 2:
                        raise
                    rebuilds += 1
                    log.warning("源连接中断，重建后从断点续扫 table=%s（第 %s 次）：%s",
                                table, rebuilds, e)
                    try:
                        src_conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    src_conn = self._reconnect(src, resolver.source_schema)
            # ---- 孤儿行清理（T2.8/T4.4，仅重刷模式；行过滤子集表自动禁用，E14） ----
            if replace_mode and orphan_cleanup and pk and len(pk) == 1:
                if table in (task.get("mapping") or {}).get("row_filters", {}):
                    report["warnings"].append(f"表 {table} 行过滤子集：跳过孤儿行清理（E14）")
                else:
                    from app.services.orphan_service import cleanup_orphans
                    deleted = cleanup_orphans(src_conn, tgt_conn,
                                              resolver.source_schema, resolver.target_schema,
                                              table, resolver.target_name(table), pk[0])
                    report["orphan_deleted"][table] = deleted
            tp.state = "done"
            self.runs.upsert_table(run_id, table, state="done", rows_done=rows_total)
        except TableCancelled:
            raise
        except Exception as e:  # noqa: BLE001 —— 失败表隔离，其余继续
            log.warning("拷贝表 %s 失败：%s", table, e)
            tp.state = "failed"
            tp.error = str(e)
            errors.append(table)
            try:
                self.runs.upsert_table(run_id, table, state="failed", error=str(e))
            except Exception:  # noqa: BLE001 —— 登记失败不得顶替原始错误（否则逃出线程）
                log.exception("表失败登记落库失败 table=%s", table)
        finally:
            for c in (src_conn, tgt_conn):
                if c is not None:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ---------- 孤儿行清理已独立为 orphan_service.cleanup_orphans（T4.4：手动 + 定时复用） ----------

    # ---------- 收尾（T2.5） ----------
    def _finalize(self, src_conn, tgt_conn, src: MySQLTarget, tgt: MySQLTarget,
                  resolver: MappingResolver, tables: list[str],
                  fingerprints: dict[str, str], rp, report: dict,
                  *, sample_checksum: bool = False) -> None:
        # 收尾长连接探活：拷贝期间整段空闲，可能已被服务器 wait_timeout 掐断（重连丢库需补 select_db）
        src_conn = self._ensure_alive(src_conn, src, resolver.source_schema)
        tgt_conn = self._ensure_alive(tgt_conn, tgt, resolver.target_schema)
        with tgt_conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        tgt_conn.select_db(resolver.target_schema)
        for table in tables:
            tp = rp.tables.get(table)
            if tp and tp.state != "done":
                continue
            # 自增对齐（至源库当前值）
            row = fetch_one(src_conn,
                            "SELECT auto_increment FROM information_schema.tables "
                            "WHERE table_schema=%s AND table_name=%s",
                            (resolver.source_schema, table))
            ai = row.get("auto_increment") or row.get("AUTO_INCREMENT") if row else None
            if ai:
                with tgt_conn.cursor() as cur:
                    cur.execute(f"ALTER TABLE `{resolver.target_name(table)}` AUTO_INCREMENT={int(ai)}")
            # 行数校验
            s = fetch_one(src_conn, f"SELECT COUNT(*) AS n FROM `{resolver.source_schema}`.`{table}`")["n"]
            t = fetch_one(tgt_conn, f"SELECT COUNT(*) AS n FROM `{resolver.target_name(table)}`")["n"]
            if s != t:
                report["row_count_mismatch"].append({"table": table, "source": int(s), "target": int(t)})
            # 收尾指纹复检（变更表列入报告）
            if table in fingerprints:
                fp_now = fingerprint(self._show_create(src_conn, table))
                if fp_now != fingerprints[table]:
                    report["fingerprint_changed"].append(table)
        if sample_checksum:
            report["warnings"].append("抽样 checksum：M2 为简单抽样，完整分块 checksum 校验 M4 提供")

    # ---------- 工具 ----------
    def _must_task(self, task_id: int) -> dict:
        t = self.tasks.get(task_id)
        if not t:
            raise NotFoundError(f"任务 {task_id} 不存在")
        return t

    def _safe_to_error(self, task_id: int, msg: str) -> None:
        try:
            t = self.tasks.get(task_id)
            if t:
                self.orch.transition_from_current(task_id, "error", reason=msg[:200])
                # M4 T4.3：任务失败/熔断告警
                if self.alert_repo is not None:
                    self.alert_repo.fire(f"task_failed:{task_id}",
                                         f"任务「{t['name']}」失败/熔断：{msg[:200]}",
                                         level="error", task_id=task_id)
        except Exception:  # noqa: BLE001
            log.exception("状态迁移到 error 失败")

    def _safe_transition_back(self, task: dict, to_state: str, reason: str) -> None:
        """取消/收尾的状态回迁：非法迁移（如 DDL_PENDING 无直连路径）不逃逸线程，仅记日志。"""
        try:
            self.orch.transition_from_current(task["id"], to_state, reason=reason)
        except Exception:  # noqa: BLE001
            log.exception("状态回迁失败 task=%s → %s", task["id"], to_state)

    def _finish_table_cancelled(self, task: dict, run_id: int, report: dict,
                                back_state: str = IDLE) -> None:
        """拷贝阶段 TableCancelled 收尾：区分「用户取消」与「DDL 指纹拦截」。

        - 用户取消：run=cancelled，任务回迁（init→DRAFT / 重刷·重做→IDLE），不触发失败告警；
          此前 TableCancelled 落入 except Exception → run=failed + 任务熔断 + 告警，语义错误。
        - DDL 指纹拦截（任务已进 DDL_PENDING）：run=failed 说明中止原因，不再回迁状态
          （DDL_PENDING→IDLE 非法迁移），等用户在 DDL 决策页处理。
        """
        cur_state = (self.tasks.get(task["id"]) or {}).get("state")
        if cur_state == "ddl_pending":
            report["error"] = "结构指纹不一致，拷贝中止，任务转 DDL 待决策"
            self._finish(run_id, "failed", "结构指纹不一致：拷贝中止，等待 DDL 决策",
                         report, task["id"])
            return
        self._finish(run_id, "cancelled", "用户取消", report, task["id"])
        self._safe_transition_back(task, back_state, "用户取消（拷贝阶段）")

    def _ds_id_of_target(self, task: dict) -> int:
        return int(task["target_id"])


def _tp(name: str):
    from app.core.progress import TableProgress
    return TableProgress(name=name)

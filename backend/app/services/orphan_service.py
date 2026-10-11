"""孤儿行清理服务（T4.4 / §4.2.2 第 2 步 / E14）。

- 复用 M2 清理逻辑独立成动作：主键流式归并比对，删除「目标有源无」行。
- 手动触发 API + 定时调度动作（schedule action=orphan_cleanup）。
- 行过滤子集表自动禁用清理（E14）；任务开关 sync_task.orphan_cleanup 全链路生效。
- 单列主键走归并比对；复合主键/无主键表跳过并登记（M2 同口径）。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import pymysql

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.progress import ProgressTracker, TableProgress
from app.core.ws_broadcaster import emit_from_thread
from app.models.m2_repository import RunRepository, TaskRepository
from app.models.repository import AlertRepository
from app.services.full_engine import MappingResolver, get_primary_key, list_source_tables
from app.services.mysql_client import MySQLTarget, open_connection
from app.services.cdc_transform import eval_predicate

log = logging.getLogger("dsync.orphan")


def cleanup_orphans(src_conn, tgt_conn, source_schema: str, target_schema: str,
                    src_table: str, tgt_table: str, pk: str) -> int:
    """两端主键流式归并比对，删除目标库孤儿行（M2 逻辑独立成模块函数）。"""
    def stream(conn, schema: str, name: str) -> Iterator:
        with conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(f"SELECT `{pk}` FROM `{schema}`.`{name}` ORDER BY `{pk}`")
            while True:
                rows = cur.fetchmany(10000)
                if not rows:
                    break
                for r in rows:
                    yield r[0]

    gen_src = stream(src_conn, source_schema, src_table)
    deleted = 0
    batch: list = []
    cur_val_src = next(gen_src, None)
    for tgt_pk in stream(tgt_conn, target_schema, tgt_table):
        while cur_val_src is not None and cur_val_src < tgt_pk:
            cur_val_src = next(gen_src, None)
        if cur_val_src is None or cur_val_src != tgt_pk:
            batch.append(tgt_pk)
            if len(batch) >= 5000:
                with tgt_conn.cursor() as cur:
                    cur.executemany(f"DELETE FROM `{tgt_table}` WHERE `{pk}`=%s",
                                    [(v,) for v in batch])
                tgt_conn.commit()
                deleted += len(batch)
                batch = []
    if batch:
        with tgt_conn.cursor() as cur:
            cur.executemany(f"DELETE FROM `{tgt_table}` WHERE `{pk}`=%s", [(v,) for v in batch])
        tgt_conn.commit()
        deleted += len(batch)
    return deleted


class OrphanService:
    """手动 / 定时孤儿行清理动作。执行线程化，run_history 落 run_type=orphan_cleanup。"""

    def __init__(self, *, tasks: TaskRepository, runs: RunRepository, tracker: ProgressTracker,
                 get_source: callable, get_target: callable,
                 alert_repo: AlertRepository | None = None, broadcaster=None):
        self.tasks = tasks
        self.runs = runs
        self.tracker = tracker
        self._get_source = get_source
        self._get_target = get_target
        self.alert_repo = alert_repo
        self.broadcaster = broadcaster
        self._threads: dict[int, threading.Thread] = {}

    def _emit(self, event_type: str, payload: dict, task_id=None):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload, task_id)

    def start(self, task_id: int, *, trigger: str = "manual") -> int:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFoundError(f"任务 {task_id} 不存在")
        if not task["orphan_cleanup"]:
            raise ConflictError("任务已关闭孤儿行清理开关",
                                hint="在任务策略页开启「孤儿行清理」后再触发")
        if task["state"] in ("initializing", "full_resyncing", "backing_up", "cdc_running"):
            raise ConflictError("任务运行中禁止清理孤儿行",
                                hint="请先停止增量/等待全量结束（清理与运行态引擎互斥）")
        run_id = self.runs.start(task_id=task_id, run_type="orphan_cleanup", trigger=trigger)
        t = threading.Thread(target=self._run, args=(dict(task), run_id),
                             daemon=True, name=f"orphan-{run_id}")
        self._threads[run_id] = t
        t.start()
        return run_id

    def _run(self, task: dict, run_id: int) -> None:
        rp = self.tracker.start(run_id, task["id"], "orphan_cleanup")
        report: dict = {"run_id": run_id, "deleted": {}, "skipped": {}, "warnings": []}
        try:
            src: MySQLTarget = self._get_source(self.tasks.get(task["id"]))
            tgt: MySQLTarget = self._get_target(self.tasks.get(task["id"]))
            mapping = task["mapping"] or {}
            if not task["orphan_cleanup"]:
                raise ConflictError("孤儿行清理开关已关闭")
            resolver = MappingResolver(mapping, src.default_schema, tgt.default_schema)
            src_conn = open_connection(src)
            tgt_conn = open_connection(tgt)
            # 数据源未设 default_schema 时连接无默认库，未限定库名的 DELETE 报 1046
            src_conn.select_db(resolver.source_schema)
            tgt_conn.select_db(resolver.target_schema)
            try:
                tables = resolver.resolve(
                    [t["name"] for t in list_source_tables(src_conn, resolver.source_schema)])
                rp.rows_total = len(tables)
                row_filters = mapping.get("row_filters") or {}
                for table in tables:
                    if rp.cancel_requested:
                        raise RuntimeError("用户取消")
                    # E14：行过滤子集表自动禁用清理（目标库仅保留子集，全量比对会误删）
                    if table in row_filters:
                        report["skipped"][table] = "行过滤子集表，自动禁用孤儿清理"
                        rp.tables.setdefault(table, TableProgress(name=table, state="skipped"))
                        continue
                    pk = get_primary_key(src_conn, resolver.source_schema, table)
                    if not pk or len(pk) != 1:
                        report["skipped"][table] = "无主键/复合主键表暂不支持清理"
                        rp.tables.setdefault(table, TableProgress(name=table, state="skipped"))
                        continue
                    deleted = cleanup_orphans(src_conn, tgt_conn,
                                              resolver.source_schema, resolver.target_schema,
                                              table, resolver.target_name(table), pk[0])
                    report["deleted"][table] = deleted
                    # 表级进度登记（snapshot 按 state=="done" 统计 tables_done；RunProgress 无 tables_done 属性，
                    # 旧代码直接 += 1 会 AttributeError 逃逸到 except → 孤儿清理 run 恒 failed）
                    rp.tables.setdefault(table, TableProgress(name=table, state="done"))
                    rp.add_rows(deleted)   # 加锁累加（RunProgress.add_rows）
                self.runs.finish(run_id, state="success", report=report)
                self._emit("run.finished", {"run_id": run_id, "task_id": task["id"],
                                            "state": "success"}, task["id"])
            finally:
                src_conn.close()
                tgt_conn.close()
        except AppError as e:
            report["error"] = e.message
            self.runs.finish(run_id, state="failed", error=e.message, report=report)
            self._fail_alert(task["id"], e.message)
        except Exception as e:  # noqa: BLE001 —— 异常不逃逸线程（M2 规约）
            log.exception("孤儿清理 run %s failed", run_id)
            report["error"] = str(e)
            self.runs.finish(run_id, state="failed", error=str(e), report=report)
            self._fail_alert(task["id"], str(e))
        finally:
            self.tracker.remove(run_id)

    def _fail_alert(self, task_id: int, msg: str) -> None:
        if self.alert_repo:
            try:
                self.alert_repo.fire(f"orphan_cleanup_failed:{task_id}",
                                     f"孤儿行清理失败：{msg}", level="error", task_id=task_id)
            except Exception:  # noqa: BLE001
                pass

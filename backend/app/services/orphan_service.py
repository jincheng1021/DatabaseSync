"""孤儿行清理服务（T4.4 / §4.2.2 第 2 步 / E14）。

- 复用 M2 清理逻辑独立成动作：**SQL 侧集合差**（目标端临时表 + LEFT JOIN），
  删除「目标有源无」行。比较语义钉死在目标端 collation，与 REPLACE 写入一致。
- 手动触发 API + 定时调度动作（schedule action=orphan_cleanup）。
- 行过滤子集表自动禁用清理（E14）；任务开关 sync_task.orphan_cleanup 全链路生效。
- 单列主键走集合差清理；复合主键/无主键表跳过并登记（M2 同口径）。
- 保险丝：源表为空 / 待删量超阈值（max(min_rows, 目标行数×比例)）→ 整表拦截并告警，
  宁可漏清理不可误清理（P1-4 lw_bzun 现场教训：归并比较混用两种排序语言必误删）。

稳定性设计要点（2026-10-12 修复）：
- 临时表用**显式 DDL**（非 CTAS）——CREATE TEMPORARY ... AS SELECT 在
  enforce_gtid_consistency=ON 下全版本被禁；显式 DDL 在 GTID 下安全且精确镜像列类型。
- 任何一条 SQL 不得引用同一张临时表两次（MySQL 1137 can't reopen table）。
- 批删除 = Python 侧分批取值 + IN 分片（绕开「LIMIT & IN 子查询」限制与首批
  watermark 哨兵值的排序边界缺陷），1213/1205 指数退避重试，按已提交批天然幂等。
- pymysl DictCursor：聚合值必须起别名；information_schema 一律 fetch_all_lc。
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Iterator

import pymysql

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.progress import ProgressTracker, TableProgress
from app.core.ws_broadcaster import emit_from_thread
from app.models.m2_repository import RunRepository, TaskRepository
from app.models.repository import AlertRepository, SettingRepository
from app.services.full_engine import MappingResolver, get_primary_key, list_source_tables
from app.services.mysql_client import (
    MySQLTarget,
    fetch_all,
    fetch_all_lc,
    fetch_one,
    open_connection,
)

log = logging.getLogger("dsync.orphan")

_TMP_SRC = "_dsync_orphan_src"
_TMP_DEL = "_dsync_orphan_del"

# PK 列类型族（跨族即判定两端主键语义不兼容，跳过清理）
_TYPE_FAMILIES: dict[str, set[str]] = {
    "numeric": {"tinyint", "smallint", "mediumint", "int", "integer", "bigint",
                "float", "double", "decimal", "numeric", "real", "bit"},
    "string": {"varchar", "char", "text", "tinytext", "mediumtext", "longtext"},
    "binary": {"binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"},
    "temporal": {"date", "datetime", "timestamp", "time", "year"},
}


def _qi(name: str) -> str:
    """SQL 标识符引用（反引号双写转义）。"""
    return f"`{name.replace('`', '``')}`"


def _type_family(data_type: str | None) -> str | None:
    dt = (data_type or "").lower()
    for fam, members in _TYPE_FAMILIES.items():
        if dt in members:
            return fam
    return None


def _exec_with_retry(conn, sql: str, args, *, many: bool = False, attempts: int = 3):
    """1213（死锁）/1205（锁等待超时）指数退避重试；已提交批次天然幂等。

    many=True 时走 executemany（批量 INSERT），否则 execute。
    """
    attempt = 0
    while True:
        try:
            with conn.cursor() as cur:
                if many:
                    return cur.executemany(sql, args)
                return cur.execute(sql, args)
        except pymysql.err.OperationalError as e:
            if e.args and e.args[0] in (1213, 1205) and attempt < attempts - 1:
                attempt += 1
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            raise


def _skip(reason: str, **extra) -> dict:
    base = {"deleted": 0, "orphan_count": 0, "source_rows": 0, "target_rows": 0,
            "collation_conflicts": 0, "skipped_reason": reason, "guard": None,
            "sample_deleted": [], "after_check": None, "dry_run": False}
    base.update(extra)
    return base


def cleanup_orphans(src_conn, tgt_conn, source_schema: str, target_schema: str,
                    src_table: str, tgt_table: str, pk: str, *,
                    guard_max_ratio: float = 0.05, guard_min_rows: int = 1000,
                    dry_run: bool = False) -> dict:
    """SQL 侧集合差清理孤儿行（比较全部发生在目标端 MySQL 内，语义=目标 collation）。

    流程：预检类型族 → 显式 DDL 临时表（镜像目标 PK 列）→ 源端无序流读 + INSERT IGNORE
    → LEFT JOIN 求差集 → 保险丝 → 分批 IN 删除 → 删除后两端复核。
    """
    qpk = _qi(pk)
    res = {"deleted": 0, "orphan_count": 0, "source_rows": 0, "target_rows": 0,
           "collation_conflicts": 0, "skipped_reason": None, "guard": None,
           "sample_deleted": [], "after_check": None, "dry_run": bool(dry_run)}
    try:
        # ---- 1) 预检：两端 PK 列元数据（类型族 + 字符串长度收缩） ----
        src_meta = fetch_all_lc(
            src_conn,
            "SELECT data_type, column_type, character_maximum_length, "
            "character_set_name, collation_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
            (source_schema, src_table, pk))
        tgt_meta = fetch_all_lc(
            tgt_conn,
            "SELECT data_type, column_type, character_maximum_length, "
            "character_set_name, collation_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
            (target_schema, tgt_table, pk))
        if not src_meta or not tgt_meta:
            return _skip("pk_meta_missing")
        src_fam, tgt_fam = _type_family(src_meta[0].get("data_type")), _type_family(tgt_meta[0].get("data_type"))
        if not src_fam or not tgt_fam or src_fam != tgt_fam:
            return _skip("pk_type_mismatch")
        if src_fam == "string":
            src_len = src_meta[0].get("character_maximum_length")
            tgt_len = tgt_meta[0].get("character_maximum_length")
            if src_len and tgt_len and int(src_len) > int(tgt_len):
                # 源值超目标容量：REPLACE 写入语义下该值要么报错要么被截断，
                # 集合差无法安全判定 → 拦下人工处理
                return _skip("pk_length_mismatch")

        # ---- 2) 临时表：镜像【目标】PK 列类型/字符集/collation（显式 DDL，GTID 安全） ----
        tm = tgt_meta[0]
        charset_clause = ""
        if tm.get("character_set_name") and tm.get("collation_name"):
            charset_clause = (f" CHARACTER SET {tm['character_set_name']}"
                              f" COLLATE {tm['collation_name']}")
        col_ddl = f"{qpk} {tm.get('column_type')}{charset_clause} NOT NULL"
        for tmp in (_TMP_SRC, _TMP_DEL):
            with tgt_conn.cursor() as cur:
                cur.execute(f"DROP TEMPORARY TABLE IF EXISTS {tmp}")
        with tgt_conn.cursor() as cur:
            cur.execute(f"CREATE TEMPORARY TABLE {_TMP_SRC} ({col_ddl}, PRIMARY KEY ({qpk}))")
        with tgt_conn.cursor() as cur:
            cur.execute(f"CREATE TEMPORARY TABLE {_TMP_DEL} ({col_ddl}, PRIMARY KEY ({qpk}))")

        # ---- 3) 源端 PK 无序流读（免 ORDER BY filesort）→ 批量 INSERT IGNORE ----
        with src_conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(f"SELECT {qpk} FROM {_qi(source_schema)}.{_qi(src_table)}")
            while True:
                rows = cur.fetchmany(10000)
                if not rows:
                    break
                res["source_rows"] += len(rows)
                for i in range(0, len(rows), 1000):
                    chunk = [(r[0],) for r in rows[i:i + 1000]]
                    _exec_with_retry(
                        tgt_conn,
                        f"INSERT IGNORE INTO {_TMP_SRC} ({qpk}) VALUES (%s)", chunk, many=True)
        # IGNORE 吞掉的行 = 源端互异值在目标 collation 下塌缩（两端排序规则漂移信号）
        distinct = fetch_one(tgt_conn, f"SELECT COUNT(*) AS n FROM {_TMP_SRC}")["n"]
        res["collation_conflicts"] = max(0, res["source_rows"] - int(distinct or 0))

        # ---- 4) 一次性求差集（先算账，后动手） ----
        _exec_with_retry(
            tgt_conn,
            f"INSERT INTO {_TMP_DEL} ({qpk}) "
            f"SELECT t.{qpk} FROM {_qi(target_schema)}.{_qi(tgt_table)} t "
            f"LEFT JOIN {_TMP_SRC} s ON t.{qpk} = s.{qpk} WHERE s.{qpk} IS NULL",
            ())
        res["orphan_count"] = int(fetch_one(tgt_conn, f"SELECT COUNT(*) AS n FROM {_TMP_DEL}")["n"] or 0)
        res["target_rows"] = int(fetch_one(
            tgt_conn,
            f"SELECT COUNT(*) AS n FROM {_qi(target_schema)}.{_qi(tgt_table)}")["n"] or 0)
        if res["orphan_count"]:
            res["sample_deleted"] = [r[pk] for r in fetch_all(
                tgt_conn, f"SELECT {qpk} FROM {_TMP_DEL} ORDER BY {qpk} LIMIT 20")]

        # ---- 5) 保险丝（顺序：source_empty 先于 guard_ratio，均在任何 DELETE 之前） ----
        if res["source_rows"] == 0 and res["target_rows"] > 0:
            return _skip("source_empty", orphan_count=0, target_rows=res["target_rows"],
                         sample_deleted=[])
        threshold = max(float(guard_min_rows), res["target_rows"] * float(guard_max_ratio))
        if res["orphan_count"] > threshold:
            return _skip("guard_ratio", orphan_count=res["orphan_count"],
                         target_rows=res["target_rows"],
                         guard={"max_ratio": float(guard_max_ratio),
                                "min_rows": int(guard_min_rows),
                                "threshold": int(threshold),
                                "sample": res["sample_deleted"][:20]})

        # ---- 6) dry_run 到此为止 ----
        if dry_run:
            return res

        # ---- 7) 分批删除：Python 侧取批 + IN 分片（首批无 watermark 谓词，
        #         规避字符串 PK 空串/数值 PK 0 的 `> %s` 哨兵边界） ----
        last = None
        while True:
            sql = f"SELECT {qpk} FROM {_TMP_DEL}"
            args: tuple = ()
            if last is not None:
                sql += f" WHERE {qpk} > %s"
                args = (last,)
            sql += f" ORDER BY {qpk} LIMIT 5000"
            rows = fetch_all(tgt_conn, sql, args)
            if not rows:
                break
            pks = [r[pk] for r in rows]
            for i in range(0, len(pks), 1000):
                sl = pks[i:i + 1000]
                _exec_with_retry(
                    tgt_conn,
                    f"DELETE FROM {_qi(target_schema)}.{_qi(tgt_table)} "
                    f"WHERE {qpk} IN ({', '.join(['%s'] * len(sl))})", tuple(sl))
            tgt_conn.commit()
            res["deleted"] += len(pks)
            last = pks[-1]

        # ---- 8) 删除后两端复核 ----
        s_after = int(fetch_one(src_conn,
                                f"SELECT COUNT(*) AS n FROM {_qi(source_schema)}.{_qi(src_table)}")["n"] or 0)
        t_after = int(fetch_one(tgt_conn,
                                f"SELECT COUNT(*) AS n FROM {_qi(target_schema)}.{_qi(tgt_table)}")["n"] or 0)
        res["after_check"] = {"source": s_after, "target": t_after}
        return res
    finally:
        # TEMPORARY 表随会话消亡，此处防御性清理（手动路径同一连接连跑多表时必须，
        # 否则下一表 CREATE 报「已存在」）
        for tmp in (_TMP_SRC, _TMP_DEL):
            try:
                with tgt_conn.cursor() as cur:
                    cur.execute(f"DROP TEMPORARY TABLE IF EXISTS {tmp}")
            except Exception:  # noqa: BLE001
                pass


class OrphanService:
    """手动 / 定时孤儿行清理动作。执行线程化，run_history 落 run_type=orphan_cleanup。"""

    def __init__(self, *, tasks: TaskRepository, runs: RunRepository, tracker: ProgressTracker,
                 get_source: callable, get_target: callable,
                 alert_repo: AlertRepository | None = None, broadcaster=None,
                 settings: SettingRepository | None = None):
        self.tasks = tasks
        self.runs = runs
        self.tracker = tracker
        self._get_source = get_source
        self._get_target = get_target
        self.alert_repo = alert_repo
        self.broadcaster = broadcaster
        self.settings = settings
        self._threads: dict[int, threading.Thread] = {}

    def _emit(self, event_type: str, payload: dict, task_id=None):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload, task_id)

    # ---------- 参数 ----------
    def _p(self, key: str, default):
        v = self.settings.get(key) if self.settings else None
        return default if v is None else v

    def _tuned(self, target: MySQLTarget) -> MySQLTarget:
        """建连超时调优（镜像 FullEngine._tuned）：大表流读/慢库单批可超 30s 默认值。"""
        return dataclasses.replace(
            target,
            read_timeout=int(self._p("db_read_timeout_seconds", 120)),
            write_timeout=int(self._p("db_write_timeout_seconds", 120)),
        )

    def start(self, task_id: int, *, trigger: str = "manual", dry_run: bool = False) -> int:
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
        t = threading.Thread(target=self._run, args=(dict(task), run_id, bool(dry_run)),
                             daemon=True, name=f"orphan-{run_id}")
        self._threads[run_id] = t
        t.start()
        return run_id

    def _run(self, task: dict, run_id: int, dry_run: bool = False) -> None:
        rp = self.tracker.start(run_id, task["id"], "orphan_cleanup")
        report: dict = {"run_id": run_id, "deleted": {}, "skipped": {}, "guard": {},
                        "details": {}, "warnings": [], "dry_run": bool(dry_run)}
        try:
            src: MySQLTarget = self._tuned(self._get_source(self.tasks.get(task["id"])))
            tgt: MySQLTarget = self._tuned(self._get_target(self.tasks.get(task["id"])))
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
                    res = cleanup_orphans(src_conn, tgt_conn,
                                          resolver.source_schema, resolver.target_schema,
                                          table, resolver.target_name(table), pk[0],
                                          guard_max_ratio=float(self._p("orphan_guard_max_ratio", 0.05)),
                                          guard_min_rows=int(self._p("orphan_guard_min_rows", 1000)),
                                          dry_run=dry_run)
                    if res.get("collation_conflicts"):
                        report["warnings"].append(
                            f"表 {table}：{res['collation_conflicts']} 个源端主键值在目标端"
                            f"排序规则下塌缩（两端 collation 漂移信号），已按目标端语义判定")
                    if res.get("skipped_reason"):
                        report["skipped"][table] = res["skipped_reason"]
                        if res.get("guard"):
                            report["guard"][table] = res["guard"]
                        self._guard_alert(task["id"], table, res["skipped_reason"], res)
                        rp.tables.setdefault(table, TableProgress(name=table, state="skipped"))
                        continue
                    report["deleted"][table] = res["deleted"]
                    report["details"][table] = res
                    # 表级进度登记（snapshot 按 state=="done" 统计 tables_done；RunProgress 无 tables_done 属性，
                    # 旧代码直接 += 1 会 AttributeError 逃逸到 except → 孤儿清理 run 恒 failed）
                    rp.tables.setdefault(table, TableProgress(name=table, state="done"))
                    rp.add_rows(res["deleted"])   # 加锁累加（RunProgress.add_rows）
                    if res.get("after_check") and \
                            res["after_check"]["source"] != res["after_check"]["target"]:
                        report["warnings"].append(
                            f"表 {table} 清理后行数仍不一致：源 {res['after_check']['source']}"
                            f" / 目标 {res['after_check']['target']}（源库在清理窗口内有变更）")
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

    def _guard_alert(self, task_id: int, table: str, reason: str, res: dict) -> None:
        """保险丝触发告警：source_empty=error（疑似源库被清空/配错库），guard_ratio=warning。"""
        if not self.alert_repo:
            return
        try:
            if reason == "source_empty":
                self.alert_repo.fire(
                    f"orphan_source_empty:{task_id}:{table}",
                    f"孤儿清理拦截：表 {table} 源库 0 行而目标 {res.get('target_rows')} 行，"
                    f"疑似源库被清空/替换或任务源库配置错误，未执行清理", level="error", task_id=task_id)
            elif reason == "guard_ratio":
                g = res.get("guard") or {}
                self.alert_repo.fire(
                    f"orphan_guard:{task_id}:{table}",
                    f"孤儿清理拦截：表 {table} 待删 {res.get('orphan_count')} 行超过阈值"
                    f"（目标 {res.get('target_rows')} 行，threshold={g.get('threshold')}），"
                    f"请用 dry-run 预览确认后再手动处理", level="warning", task_id=task_id)
        except Exception:  # noqa: BLE001 —— 告警失败不影响主流程
            log.exception("孤儿清理保险丝告警发送失败 task=%s table=%s", task_id, table)

    def _fail_alert(self, task_id: int, msg: str) -> None:
        if self.alert_repo:
            try:
                self.alert_repo.fire(f"orphan_cleanup_failed:{task_id}",
                                     f"孤儿行清理失败：{msg}", level="error", task_id=task_id)
            except Exception:  # noqa: BLE001
                pass

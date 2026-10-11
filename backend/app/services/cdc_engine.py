"""增量引擎 · mysql-replication 适配器（T3.1/T3.2/T3.4/T3.5/T3.7/T3.8 / §4.3）。

实现 IncrementalEngine 接口（未来可替换 Debezium 适配器，上层零改动）：
- binlog 消费循环独立线程；事件经 EventTransformer 转幂等 Op 进有界队列（背压）。
- 应用线程攒批（满 1000 条或满 1 秒先到者提交）→ 两级限流 → 批量写目标库 →
  位点与批次统计原子落 SQLite（at-least-once + 幂等写 = 效果 exactly-once）。
- DDL（QUERY 事件）默认拦截：ddl_pending + 状态机 DDL 待决策 + WS ddl.pending，
  决策前阻塞消费；「同类 DDL 记住选择」按 ddl_rules_json 自动决策（重启后仍生效）。
- 异常：指数退避重连（1s→60s）、位点失效熔断 needs_reinit、目标写失败 3 次重试后
  暂停告警、解析异常记录原始事件十六进制摘要熔断待人工。
- T3.8 重刷编排：重刷期间涉及表事件入缓存队列（内存+磁盘混合），完成后按
  binlog 顺序重放，缓存清空后恢复直通；缓存/重放期间位点冻结不推进。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import (GtidEvent, HeartbeatLogEvent, QueryEvent,
                                      RotateEvent, XidEvent)
from pymysqlreplication.gtid import Gtid, GtidSet
from pymysqlreplication.row_event import (DeleteRowsEvent, UpdateRowsEvent,
                                          WriteRowsEvent)

from app.core.limiter import TwoLevelLimiter
from app.core.orchestrator import (CDC_RUNNING, DDL_PENDING, ERROR, NEEDS_REINIT, PAUSED,
                                   Orchestrator)
from app.core.ws_broadcaster import emit_from_thread
from app.models.m2_repository import (CdcRunRepository, DdlPendingRepository,
                                      PositionRepository, RunRepository, TaskRepository)
from app.models.repository import AlertRepository, AuditRepository, SettingRepository
from app.services.cdc_interface import IncrementalEngine
from app.services.cdc_transform import (EventTransformer, Op, ddl_kind,
                                        merge_ops, parse_affected_tables)
from app.services.full_engine import get_primary_key
from app.services.mysql_client import MySQLTarget, open_connection

log = logging.getLogger("dsync.cdc")


def _op_json_default(o):
    """溢写 JSONL 类型保真编码（P2-9：修复前 default=str 使 bytes 重放失真）。"""
    if isinstance(o, (bytes, bytearray)):
        return {"__t": "b64", "v": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, datetime):
        return {"__t": "dt", "v": o.isoformat(sep=" ")}
    if isinstance(o, Decimal):
        return {"__t": "dec", "v": str(o)}
    return str(o)


def _op_json_hook(d):
    t = d.get("__t") if isinstance(d, dict) else None
    if t == "b64":
        return base64.b64decode(d["v"])
    if t == "dt":
        return datetime.fromisoformat(d["v"])
    if t == "dec":
        return Decimal(d["v"])
    return d

# DDL 判定：QUERY 事件里排除事务控制语句
_TX_WORDS = {"BEGIN", "COMMIT", "ROLLBACK", "START", "XA", "SAVEPOINT"}


class PositionLostError(Exception):
    """重连时所需 binlog 已被源库清理（错误码 1236）→ 熔断 needs_reinit。"""


class ParseAbortedError(Exception):
    """事件解析异常 → 记录原始事件十六进制摘要，熔断待人工。"""


def new_gtid_set(s: str | None) -> GtidSet | None:
    """解析持久化的 gtid_executed 集合字符串（"uuid:1-5,uu:1-3"）。"""
    if not s:
        return None
    try:
        return GtidSet(s)
    except Exception:  # noqa: BLE001
        log.warning("GTID 集合解析失败，回退 file+position：%r", s)
        return None


@dataclass
class TaskRuntime:
    """单个任务的增量运行时（消费线程 + 应用线程共享）。"""
    task_id: int
    task: dict
    transformer: EventTransformer
    source: MySQLTarget
    target: MySQLTarget
    server_id: int
    run_id: int
    queue_limit: int
    commit_batch_rows: int
    commit_interval: float
    big_tx_threshold: int
    lag_alert_sec: int

    queue: deque = field(default_factory=deque)
    cv: threading.Condition = field(default_factory=threading.Condition)
    stopped: bool = False
    paused: bool = False
    ddl_block: threading.Event = field(default_factory=threading.Event)
    backpressure: bool = False
    high_water_since: float | None = None

    # 位点（reader 线程推进；op 携带快照，applier 持久化）
    log_file: str | None = None
    log_pos: int | None = None
    gtid_set: GtidSet | None = None       # auto position 所用已执行集合
    gtid_snapshot: str = ""               # 事件时已执行 GTID 集合快照（不可变字符串）
    gtid_mode: bool = False
    position_frozen: bool = False          # T3.8：缓存/重放期间位点冻结

    # 观测
    counters: dict = field(default_factory=lambda: {"inserted": 0, "updated": 0, "deleted": 0})
    filter_dropped: int = 0
    out_of_scope_dropped: int = 0
    last_src_ts: int | None = None         # 最近提交批最后事件源库时间戳
    last_lag: float | None = None
    last_commit_wall: float | None = None
    big_tx: bool = False
    big_tx_alerted: bool = False
    tx_ops: int = 0

    # T3.8 重刷缓存
    resync_tables: set | None = None
    caching: bool = False
    replaying: bool = False
    cache: list = field(default_factory=list)
    spill_path: Path | None = None
    spilled_ops: int = 0
    replay_mem_warn_at: int = 0          # B4：重放期间内存缓存告警水位
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    replay_thread: threading.Thread | None = None

    # 重连（T3.7）
    consecutive_failures: int = 0
    first_fail_ts: float | None = None

    table_meta: dict = field(default_factory=dict)   # table -> {"pk": [...]} or None
    tgt_conn: object | None = None
    reader_thread: threading.Thread | None = None
    applier_thread: threading.Thread | None = None
    pkless_warned: bool = False
    finalized: bool = False               # P2-7：run 终态已落库标记（防 stop/applier 双重 finish）

    def reader_pos(self) -> dict:
        return {"binlog_file": self.log_file, "binlog_pos": self.log_pos,
                "gtid_executed": self.gtid_snapshot or None,
                "server_id_used": self.server_id}


class CdcEngine(IncrementalEngine):
    def __init__(self, *, tasks: TaskRepository, runs: RunRepository,
                 positions: PositionRepository, settings: SettingRepository,
                 orchestrator: Orchestrator, limiter: TwoLevelLimiter,
                 get_source: callable, get_target: callable,
                 ddl_repo: DdlPendingRepository, alert_repo: AlertRepository,
                 audit: AuditRepository | None = None, broadcaster=None,
                 lag_repo: CdcRunRepository | None = None, cache_dir: Path | None = None):
        self.tasks = tasks
        self.runs = runs
        self.positions = positions
        self.settings = settings
        self.orch = orchestrator
        self.limiter = limiter
        self._get_source = get_source
        self._get_target = get_target
        self.ddl_repo = ddl_repo
        self.alert_repo = alert_repo
        self.audit = audit
        self.broadcaster = broadcaster
        self.lag_repo = lag_repo
        self.cache_dir = cache_dir or Path("data/cdc_cache")
        self._rts: dict[int, TaskRuntime] = {}
        self._lock = threading.Lock()

    # ---------- 参数 ----------
    def _p(self, key: str, default):
        v = self.settings.get(key)
        return default if v is None else v

    def _emit(self, rt: TaskRuntime, event_type: str, payload: dict):
        if self.broadcaster:
            emit_from_thread(self.broadcaster, event_type, payload, rt.task_id)

    def _alert(self, rt: TaskRuntime, key: str, message: str, level: str = "warning") -> None:
        try:
            fired = self.alert_repo.fire(
                f"{key}:{rt.task_id}", message, level=level, task_id=rt.task_id,
                silence_minutes=int(self._p("alert_silence_minutes", 30)))
            if fired:
                self._emit(rt, "alert.fired",
                           {"alert_key": key, "level": level, "task_id": rt.task_id,
                            "message": message})
        except Exception:  # noqa: BLE001
            log.exception("告警落库失败 task=%s key=%s", rt.task_id, key)

    def _resolve_alert(self, rt: TaskRuntime, key: str) -> None:
        try:
            if self.alert_repo.resolve(f"{key}:{rt.task_id}"):
                self._emit(rt, "alert.resolved",
                           {"alert_key": key, "task_id": rt.task_id})
        except Exception:  # noqa: BLE001
            pass

    # ============================================================
    # 生命周期（T3.1）
    # ============================================================
    def start(self, task_id: int, *, trigger: str = "manual") -> int:
        with self._lock:
            if task_id in self._rts:
                raise RuntimeError(f"任务 {task_id} 增量已在运行")
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(f"任务 {task_id} 不存在")
            pos = self.positions.get(task_id)
            if not pos:
                raise KeyError("尚未取得一致位点（P0），请先完成一次初始化")
            source = self._get_source(task)
            target = self._get_target(task)
            run_id = self.runs.start(task_id=task_id, run_type="cdc_session", trigger=trigger)
            rt = TaskRuntime(
                task_id=task_id, task=task,
                transformer=EventTransformer(task["mapping"], source.default_schema,
                                             target.default_schema),
                source=source, target=target,
                server_id=int(self._p("cdc_server_id_base", 30000)) + task_id,
                run_id=run_id,
                queue_limit=max(100, int(task["queue_capacity"] or self._p("queue_capacity", 50000))),
                commit_batch_rows=max(10, int(self._p("cdc_commit_batch_rows", 1000))),
                commit_interval=max(0.05, int(self._p("cdc_commit_interval_ms", 1000)) / 1000),
                big_tx_threshold=int(self._p("big_tx_threshold_rows", 100000)),
                lag_alert_sec=max(5, int(task["lag_alert_sec"] or self._p("lag_alert_seconds", 60))),
            )
            # 位点续传：GTID 优先（auto position），否则 file+position（T3.2）
            if pos.get("gtid_executed"):
                rt.gtid_set = new_gtid_set(pos["gtid_executed"])
                rt.gtid_snapshot = pos["gtid_executed"]
                rt.gtid_mode = rt.gtid_set is not None
            rt.log_file = pos.get("binlog_file")
            rt.log_pos = pos.get("binlog_pos")
            self.limiter.configure_task(task_id, task["rate_limit_rows"])
            self._rts[task_id] = rt
        rt.reader_thread = threading.Thread(target=self._reader_loop, args=(rt,),
                                            daemon=True, name=f"cdc-reader-{task_id}")
        rt.applier_thread = threading.Thread(target=self._applier_loop, args=(rt,),
                                             daemon=True, name=f"cdc-applier-{task_id}")
        rt.applier_thread.start()
        rt.reader_thread.start()
        log.info("增量引擎启动 task=%s run=%s 位点=%s", task_id, run_id,
                 f"{pos.get('binlog_file')}:{pos.get('binlog_pos')}" if not rt.gtid_mode
                 else f"GTID[{len(rt.gtid_set)} 组]")
        return run_id

    def is_running(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self._rts

    def pause(self, task_id: int) -> None:
        # P2-7：_rts 读写统一经 _lock，避免与 start/stop/applier 收尾竞态
        with self._lock:
            rt = self._rts.get(task_id)
        if rt:
            rt.paused = True   # reader 停拉新事件；applier 排空队列后位点落盘

    def resume(self, task_id: int) -> None:
        with self._lock:
            rt = self._rts.get(task_id)
        if rt:
            rt.paused = False

    def stop(self, task_id: int, *, timeout: float = 30.0) -> None:
        with self._lock:
            rt = self._rts.get(task_id)
        if not rt:
            return
        rt.stopped = True
        rt.paused = False
        with rt.cv:
            rt.cv.notify_all()
        rt.ddl_block.set()
        if rt.reader_thread and rt.reader_thread.is_alive():
            rt.reader_thread.join(timeout=timeout / 2)
        if rt.replay_thread and rt.replay_thread.is_alive():
            rt.replay_thread.join(timeout=timeout / 2)
        if rt.applier_thread and rt.applier_thread.is_alive():
            rt.applier_thread.join(timeout=timeout / 2)
        self._close_target(rt)
        with self._lock:
            self._rts.pop(task_id, None)
            self.limiter.remove_task(task_id)
        with rt.cv:
            already = rt.finalized
            rt.finalized = True
        if not already:
            # P2-7：applier 异常收尾可能已 finish（failed），此处不再覆盖为 success
            self.runs.finish(rt.run_id, state="success",
                             report={"counters": dict(rt.counters),
                                     "filter_dropped": rt.filter_dropped,
                                     "out_of_scope_dropped": rt.out_of_scope_dropped})

    # ============================================================
    # 观测（T3.1/T3.6）
    # ============================================================
    def get_lag(self, task_id: int) -> float | None:
        with self._lock:
            rt = self._rts.get(task_id)
        return rt.last_lag if rt else None

    def get_position(self, task_id: int) -> dict | None:
        with self._lock:
            rt = self._rts.get(task_id)
        if rt:
            pos = rt.reader_pos()
            pos["last_commit_at"] = rt.last_commit_wall
            return pos
        return None

    def get_stats(self, task_id: int) -> dict:
        with self._lock:
            rt = self._rts.get(task_id)
        if not rt:
            return {"running": False}
        with rt.cv:
            qsize = len(rt.queue)
        return {"running": True, "counters": dict(rt.counters), "queue_size": qsize,
                "queue_capacity": rt.queue_limit, "backpressure": rt.backpressure,
                "big_tx": rt.big_tx, "caching": rt.caching, "replaying": rt.replaying,
                "cache_size": len(rt.cache) + rt.spilled_ops,
                "filter_dropped": rt.filter_dropped,
                "out_of_scope_dropped": rt.out_of_scope_dropped,
                "paused": rt.paused, "lag_seconds": rt.last_lag}

    def monitor_tick(self) -> list[dict]:
        """供主循环每秒调用：返回运行中任务的延迟/统计快照（含告警判定，T3.6）。"""
        with self._lock:
            rts = list(self._rts.values())   # P2-7：快照在锁内取得，遍历不再与收尾竞态
        out = []
        for rt in rts:
            lag = rt.last_lag
            payload = {"task_id": rt.task_id, "lag_seconds": round(lag, 2) if lag is not None else None,
                       "timestamp": int(time.time() * 1000), "paused": rt.paused,
                       "stats": dict(rt.counters), "queue_size": len(rt.queue),
                       "queue_capacity": rt.queue_limit, "backpressure": rt.backpressure,
                       "big_tx": rt.big_tx, "caching": rt.caching, "replaying": rt.replaying}
            out.append(payload)
            self._emit(rt, "task.lag", payload)
            self._emit(rt, "task.stats", payload)
            # 阈值告警：超阈值触发，回落 50% 自动解除
            if lag is not None and not rt.paused:
                if lag > rt.lag_alert_sec:
                    self._alert(rt, "cdc_lag",
                                f"增量延迟 {lag:.0f}s 超过阈值 {rt.lag_alert_sec}s"
                                f"（任务 {rt.task_id}），检查源库压力或限流配置", level="warning")
                elif lag < rt.lag_alert_sec * 0.5:
                    self._resolve_alert(rt, "cdc_lag")
        return out

    def lag_history(self, task_id: int, minutes: int = 1440) -> list[dict]:
        since = int(time.time() * 1000) - minutes * 60_000
        if self.lag_repo:
            try:
                return self.lag_repo.list(task_id, since)
            except Exception:  # noqa: BLE001
                pass
        return []

    def record_minute_sample(self, task_id: int, ts_ms: int, lag: float) -> None:
        if self.lag_repo:
            try:
                self.lag_repo.append(task_id, ts_ms, lag)
            except Exception:  # noqa: BLE001
                pass

    # ============================================================
    # 消费线程（T3.2/T3.7）
    # ============================================================
    def _check_binlog_settings(self, rt: TaskRuntime) -> None:
        """源库 binlog 参数预检（旁路容错，绝不阻断 CDC 主流程）。

        mysql-replication 库要求源库 binlog_row_metadata / binlog_row_image = FULL
        才能在 UPDATE/DELETE 行事件中拿到精确列映射（MINIMAL 下依赖类型推断，
        源库 DDL 后可能解析不准）。非 FULL 时发告警（fire 自带静默期去重），
        恢复 FULL 后自动 resolve。
        """
        try:
            conn = open_connection(rt.source)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SHOW VARIABLES WHERE Variable_name IN "
                        "('binlog_row_metadata','binlog_row_image')")
                    rows = {r[0].lower(): str(r[1]).upper() for r in cur.fetchall()}
            finally:
                conn.close()
            meta = rows.get("binlog_row_metadata")
            image = rows.get("binlog_row_image")
            if meta == "FULL" and image == "FULL":
                self._resolve_alert(rt, "cdc_binlog_meta")
                return
            missing = [n for n, v in (("binlog_row_metadata", meta),
                                      ("binlog_row_image", image)) if v != "FULL"]
            self._alert(
                rt, "cdc_binlog_meta",
                f"源库 {rt.source.host}:{rt.source.port} 的 {', '.join(missing)} 未设为 FULL，"
                "binlog 缺少列元数据，增量列映射可能不准（源库 DDL 后风险更高）。"
                "修复：SET GLOBAL binlog_row_metadata=FULL; SET GLOBAL binlog_row_image=FULL;"
                "（需 SUPER/SYSTEM_VARIABLES_ADMIN 权限，动态生效无需重启）")
        except Exception:  # noqa: BLE001
            log.debug("binlog 参数预检失败（忽略）task=%s", rt.task_id, exc_info=True)

    def _open_stream(self, rt: TaskRuntime) -> BinLogStreamReader:
        conn = {
            "host": rt.source.host, "port": rt.source.port,
            "user": rt.source.username, "passwd": rt.source.password or "",
        }
        kwargs = dict(
            connection_settings=conn, server_id=rt.server_id,
            blocking=False, resume_stream=True,
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent,
                         QueryEvent, GtidEvent, XidEvent, RotateEvent, HeartbeatLogEvent],
            slave_heartbeat=10,
        )
        if rt.gtid_mode and rt.gtid_set is not None and str(rt.gtid_set):
            kwargs["auto_position"] = str(rt.gtid_set)
        elif rt.log_file:
            kwargs["log_file"] = rt.log_file
            kwargs["log_pos"] = int(rt.log_pos or 4)
        return BinLogStreamReader(**kwargs)

    def _reader_loop(self, rt: TaskRuntime):
        backoff = 1.0
        max_backoff = int(self._p("reconnect_backoff_max_seconds", 60))
        while not rt.stopped:
            try:
                self._check_binlog_settings(rt)
                stream = self._open_stream(rt)
                opened_at = time.time()   # 退避只在流稳定存活后才重置（防 1s 重连风暴）
                try:
                    while not rt.stopped:
                        if rt.paused or rt.ddl_block.is_set():
                            time.sleep(0.2)
                            continue
                        if rt.backpressure:
                            # 背压：队列高水位暂停拉取 binlog（§4.3.3）
                            if rt.high_water_since and time.time() - rt.high_water_since > 60:
                                self._alert(rt, "cdc_backpressure",
                                            "增量队列持续高水位（>60s），已暂停拉取；"
                                            "建议调高任务级限流或检查目标库吞吐")
                                rt.high_water_since = time.time()  # 静默期内不再连发
                            time.sleep(0.2)
                            continue
                        try:
                            ev = stream.fetchone()
                        except Exception as e:  # noqa: BLE001
                            raise self._classify_stream_error(e) from e
                        if ev is None:
                            time.sleep(0.05)
                            continue
                        self._handle_event(rt, ev)
                finally:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
            except PositionLostError:
                self._mark_needs_reinit(rt, "重连时所需 binlog 已被源库清理（位点失效）")
                return
            except ParseAbortedError as e:
                self._mark_parse_aborted(rt, str(e))
                return
            except Exception as e:  # noqa: BLE001
                if rt.stopped:
                    return
                rt.consecutive_failures += 1
                now = time.time()
                if rt.first_fail_ts is None:
                    rt.first_fail_ts = now
                if now - rt.first_fail_ts >= 1800:
                    self._alert(rt, "cdc_reconnect",
                                f"增量重连持续失败超过 30 分钟（{rt.consecutive_failures} 次）：{e}",
                                level="error")
                    rt.first_fail_ts = now  # 静默期后再次提醒
                survived = time.time() - opened_at
                # 流稳定存活超过阈值才视为恢复、重置退避；「建得开但马上断」沿用递增退避
                if survived >= max(0, int(self._p("reconnect_stable_seconds", 30))):
                    backoff = 1.0
                    rt.first_fail_ts = None
                    rt.consecutive_failures = 0
                log.warning("增量连接异常 task=%s：%s（流存活 %.1fs，%.1fs 后重连）",
                            rt.task_id, e, survived, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    @staticmethod
    def _classify_stream_error(e: Exception) -> Exception:
        """区分位点失效（binlog 已清理，1236）与一般连接异常（T3.2/T3.7）。"""
        import pymysql
        if isinstance(e, pymysql.err.OperationalError):
            code = e.args[0] if e.args else None
            msg = str(e.args[-1] if e.args else e)
            if code == 1236 or ("binlog" in msg.lower()
                                and ("purged" in msg.lower() or "purge" in msg.lower())):
                return PositionLostError(str(e))
        return e

    def _handle_event(self, rt: TaskRuntime, ev) -> None:
        try:
            if isinstance(ev, RotateEvent):
                rt.log_file = ev.next_binlog
                rt.log_pos = ev.position
                return
            log_pos = getattr(ev.packet, "log_pos", None)
            if log_pos:
                rt.log_pos = log_pos
            if isinstance(ev, GtidEvent):
                if rt.gtid_set is None:
                    rt.gtid_set = GtidSet(ev.gtid)
                else:
                    rt.gtid_set.merge_gtid(Gtid(ev.gtid))
                rt.gtid_snapshot = str(rt.gtid_set)
                rt.gtid_mode = True
                rt.tx_ops = 0
                rt.big_tx = False
                rt.big_tx_alerted = False
                return
            if isinstance(ev, XidEvent):
                rt.tx_ops = 0
                rt.big_tx = False
                rt.big_tx_alerted = False
                return
            if isinstance(ev, QueryEvent):
                self._handle_query(rt, ev)
                return
            if isinstance(ev, HeartbeatLogEvent):
                return
            if isinstance(ev, (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent)):
                self._handle_rows(rt, ev)
        except PositionLostError:
            raise
        except ParseAbortedError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ParseAbortedError(self._event_digest(ev, e)) from e

    @staticmethod
    def _event_digest(ev, err: Exception) -> str:
        """记录原始事件十六进制摘要（T3.7 解析异常熔断待人工）。"""
        import binascii
        raw = b""
        packet = getattr(ev, "packet", None)
        if packet is not None:
            raw = getattr(packet, "raw_data", b"") or b""
        hexed = binascii.hexlify(raw[:256]).decode("ascii", "ignore")
        return (f"事件 {type(ev).__name__} 解析失败：{err}；"
                f"原始事件十六进制摘要（前256字节）：{hexed or '（不可得）'}")

    # ---- ROW 事件（T3.3/T3.4/T3.8） ----
    def _handle_rows(self, rt: TaskRuntime, ev) -> None:
        table = ev.table
        meta = self._table_meta(rt, ev.schema, table)
        src_ts = int(getattr(ev, "timestamp", 0) or 0)
        log_file, log_pos, gtid_snap = rt.log_file, rt.log_pos, rt.gtid_snapshot
        if meta is None:
            # 无主键表不走 CDC（建任务预检标红降级比对）；事件丢弃计数 + 一次性告警
            rt.out_of_scope_dropped += len(ev.rows)
            if not rt.pkless_warned:
                rt.pkless_warned = True
                self._alert(rt, "cdc_pkless",
                            f"表 {ev.schema}.{table} 无主键，无法安全转换为幂等写，"
                            "事件已丢弃计数；请补主键或降级为定时比对模式")
            return
        pk_cols = meta["pk"]
        tr = rt.transformer
        rows = ev.rows
        if isinstance(ev, WriteRowsEvent):
            ops = tr.insert(ev.schema, table, [r["values"] for r in rows], pk_cols,
                            src_ts, log_file, log_pos, gtid_snap)
        elif isinstance(ev, UpdateRowsEvent):
            ops = tr.update(ev.schema, table, rows, pk_cols,
                            src_ts, log_file, log_pos, gtid_snap)
        else:
            ops = tr.delete(ev.schema, table, [r["values"] for r in rows], pk_cols,
                            src_ts, log_file, log_pos, gtid_snap)
        rt.tx_ops += len(ops)
        if rt.tx_ops > rt.big_tx_threshold and not rt.big_tx:
            rt.big_tx = True
            if not rt.big_tx_alerted:
                rt.big_tx_alerted = True
                self._alert(rt, "cdc_big_tx",
                            f"单事务超过大事务阈值（{rt.big_tx_threshold} 行），切换「边读边写」流式提交。"
                            "已知限制：流式提交拆散源事务边界（部分子变更先行可见），"
                            "最终一致由幂等写保证（E8）", level="warning")
        if not ops:
            return
        # T3.8：重刷期间涉及表 → 缓存队列；不涉及表直通
        if rt.caching and table in (rt.resync_tables or set()):
            with rt.cache_lock:
                if rt.caching:
                    rt.cache.extend(ops)
                    self._maybe_spill(rt)
                    return
        with rt.cv:
            while len(rt.queue) >= rt.queue_limit and not rt.stopped:
                if not rt.backpressure:
                    rt.backpressure = True
                    rt.high_water_since = time.time()
                rt.cv.wait(timeout=0.5)
            if not rt.stopped:
                rt.queue.extend(ops)

    def _handle_query(self, rt: TaskRuntime, ev) -> None:
        """T3.5：DDL 默认拦截。决策前阻塞消费（缓存后续事件由 binlog 服务端排队）。"""
        ddl = (ev.query or "").strip()
        if not ddl or ddl.split(None, 1)[0].upper() in _TX_WORDS:
            return
        schema = ev.schema or ""
        if rt.transformer.source_schema and schema and schema != rt.transformer.source_schema:
            rt.out_of_scope_dropped += 1
            return
        kind = ddl_kind(ddl)
        task = self.tasks.get(rt.task_id) or rt.task
        remembered = ((task.get("ddl_rules") or {}).get("remember") or {}).get(kind)
        src_pos = f"{rt.log_file}:{rt.log_pos}" if rt.log_file else None
        if remembered in ("execute", "skip"):
            # E12「记住选择」：自动决策（仍落 ddl_pending + 审计，重启后仍生效）
            ddl_id = self.ddl_repo.create(rt.task_id, ddl, src_pos,
                                          parse_affected_tables(ddl))
            self.ddl_repo.decide(ddl_id, remembered, True)
            if self.audit:
                self.audit.record("system", f"task.ddl_auto.{remembered}", object_type="task",
                                  object_id=rt.task_id,
                                  after={"ddl_id": ddl_id, "kind": kind, "rule": "remember"})
            if remembered == "execute":
                self._execute_ddl_on_target(rt, ddl)
            log.info("DDL 自动决策（记住选择）task=%s kind=%s → %s", rt.task_id, kind, remembered)
            return
        ddl_id = self.ddl_repo.create(rt.task_id, ddl, src_pos, parse_affected_tables(ddl))
        self.orch.enter_ddl_pending(rt.task_id, reason=f"DDL 拦截：{kind}")
        self._alert(rt, "ddl_pending", f"任务 {rt.task_id} 拦截到 DDL（{kind}），等待决策",
                    level="warning")
        self._emit(rt, "ddl.pending",
                   {"task_id": rt.task_id, "ddl_id": ddl_id, "ddl_text": ddl,
                    "source_position": src_pos, "kind": kind,
                    "affected_tables": parse_affected_tables(ddl)})
        rt.ddl_block.set()   # 拦截暂停：reader 循环等待决策

    def _execute_ddl_on_target(self, rt: TaskRuntime, ddl: str) -> None:
        """「执行」决策 / 记住选择自动执行：DDL 应用到目标库。"""
        conn = open_connection(rt.target)
        try:
            if rt.transformer.target_schema:
                conn.select_db(rt.transformer.target_schema)
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
        finally:
            conn.close()

    def resume_after_ddl(self, task_id: int) -> None:
        rt = self._rts.get(task_id)
        if rt:
            rt.ddl_block.clear()

    def apply_ddl(self, task_id: int, ddl_text: str) -> None:
        """「执行」决策入口（T3.5）：把 DDL 应用到目标库。"""
        rt = self._rts.get(task_id)
        if rt:
            self._execute_ddl_on_target(rt, ddl_text)
            return
        # 引擎未运行（如崩溃后决策）：直连目标库执行
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(f"任务 {task_id} 不存在")
        target = self._get_target(task)
        tr = EventTransformer(task["mapping"], None, target.default_schema)
        conn = open_connection(target)
        try:
            if tr.target_schema:
                conn.select_db(tr.target_schema)
            with conn.cursor() as cur:
                cur.execute(ddl_text)
            conn.commit()
        finally:
            conn.close()

    # ---- 表元数据 ----
    def _table_meta(self, rt: TaskRuntime, schema: str, table: str) -> dict | None:
        key = f"{schema}.{table}"
        if key in rt.table_meta:
            return rt.table_meta[key]
        conn = open_connection(rt.source)
        try:
            pk = get_primary_key(conn, schema, table)
        finally:
            conn.close()
        meta = {"pk": pk} if pk else None
        rt.table_meta[key] = meta
        return meta

    # ============================================================
    # 应用线程（T3.4 攒批/限流/大事务 + T3.2 位点持久化 + T3.7 目标写失败）
    # ============================================================
    def _applier_loop(self, rt: TaskRuntime):
        try:
            while True:
                batch = self._collect_batch(rt)
                if batch is None:
                    break                       # stopped 且队列已空
                if not batch:
                    time.sleep(0.02)
                    continue
                self._apply_batch(rt, batch)
        except _TargetWriteFailed as e:
            # T3.7：目标库不可写——批级重试 3 次后暂停，异常待处理 + 告警
            log.error("目标库写入失败暂停 task=%s：%s", rt.task_id, e)
            self._alert(rt, "cdc_target_write",
                        f"目标库写入失败（批级重试 3 次耗尽），任务已暂停待处理：{e}",
                        level="error")
            try:
                self.orch.transition_from_current(rt.task_id, ERROR,
                                                  reason=f"目标库写入失败：{str(e)[:150]}")
            except Exception:  # noqa: BLE001
                log.exception("状态迁移 error 失败")
            with rt.cv:
                rt.finalized = True   # P2-7：与 stop() 互斥，防双重 finish
            self.runs.finish(rt.run_id, state="failed", error=str(e))
            with self._lock:
                self._rts.pop(rt.task_id, None)
                self.limiter.remove_task(rt.task_id)
            self._close_target(rt)
        except Exception as e:  # noqa: BLE001
            log.exception("增量应用线程异常 task=%s", rt.task_id)
            # B6：兜底分支也必须迁状态，否则任务滞留 cdc_running 假运行态
            try:
                self.orch.transition_from_current(rt.task_id, ERROR,
                                                  reason=f"增量应用线程异常：{str(e)[:150]}")
            except Exception:  # noqa: BLE001
                log.exception("状态迁移 error 失败 task=%s（任务可能滞留运行态，需人工复位）",
                              rt.task_id)
            with rt.cv:
                rt.finalized = True   # P2-7：与 stop() 互斥，防双重 finish
            self.runs.finish(rt.run_id, state="failed", error=str(e))
            with self._lock:
                self._rts.pop(rt.task_id, None)
                self.limiter.remove_task(rt.task_id)
            self._close_target(rt)

    def _collect_batch(self, rt: TaskRuntime) -> list[Op] | None:
        """攒批：满 commit_batch_rows 条 或 满 commit_interval 秒，先到者提交。
        大事务流式：不等待间隔，小批尽快提交（E8 边读边写）。返回 None 表示终止。"""
        with rt.cv:
            while not rt.queue and not rt.stopped and not rt.big_tx:
                rt.cv.wait(timeout=0.5)
            if not rt.queue:
                return None if rt.stopped else []
            batch_limit = min(rt.commit_batch_rows,
                              500 if rt.big_tx else rt.commit_batch_rows)
            deadline = time.monotonic() + (0 if rt.big_tx else rt.commit_interval)
            batch: list[Op] = []
            while len(batch) < batch_limit:
                if rt.queue:
                    batch.append(rt.queue.popleft())
                    continue
                if rt.stopped or time.monotonic() >= deadline:
                    break
                rt.cv.wait(timeout=min(0.05, deadline - time.monotonic()))
            return batch

    def _apply_batch(self, rt: TaskRuntime, batch: list[Op]) -> None:
        if not batch:
            return
        # 位点取自原始顺序最后一条 op（合并会重排，不能用合并后列表）
        last = batch[-1]
        pos = {"gtid_executed": last.gtid_snapshot or None,
               "binlog_file": last.log_file, "binlog_pos": last.log_pos,
               "server_id_used": rt.server_id}
        merged = merge_ops(batch)
        rows_n = len(merged)
        # 两级限流（任务级令牌桶 + 全局总闸，E16）——P2-5：阻塞式取令牌，限流真正生效
        self.limiter.acquire_blocking(rt.task_id, rows_n)
        attempt = 0
        while True:
            try:
                conn = self._target_conn(rt)
                with conn.cursor() as cur:
                    for op in merged:          # merge_ops 已保证 DELETE 先行
                        sql, args = (op.replace_sql() if op.kind == "replace"
                                     else op.delete_sql())
                        cur.execute(sql, args)
                conn.commit()
                break
            except Exception as e:  # noqa: BLE001
                attempt += 1
                if attempt >= 3:
                    self._close_target(rt)
                    raise _TargetWriteFailed(str(e)) from e
                log.warning("批写入失败（第 %s 次重试）task=%s：%s", attempt, rt.task_id, e)
                self._close_target(rt)
                time.sleep(0.5 * (2 ** (attempt - 1)))
        # 目标事务成功 → 位点与批次统计原子落 SQLite（T3.2；缓存/重放期间冻结）
        for op in merged:
            if op.origin == "insert":
                rt.counters["inserted"] += 1
            elif op.origin == "update":
                rt.counters["updated"] += 1
            else:
                rt.counters["deleted"] += 1
        rt.filter_dropped = rt.transformer.filter_dropped
        rt.out_of_scope_dropped = rt.transformer.out_of_scope_dropped
        rt.last_src_ts = max((op.src_ts for op in batch if op.src_ts), default=None)
        now = time.time()
        rt.last_commit_wall = now
        if rt.last_src_ts:
            rt.last_lag = max(0.0, now - rt.last_src_ts)
        if not rt.position_frozen and (pos["binlog_file"] or pos["gtid_executed"]):
            try:
                self.positions.save_batch(rt.task_id, pos, {
                    "counters": dict(rt.counters), "lag_seconds": rt.last_lag,
                    "batch_rows": rows_n, "committed_at": int(now * 1000)})
            except Exception:  # noqa: BLE001
                log.exception("位点持久化失败 task=%s", rt.task_id)

    def _target_conn(self, rt: TaskRuntime):
        if rt.tgt_conn is None:
            rt.tgt_conn = open_connection(rt.target)
            if rt.transformer.target_schema:
                rt.tgt_conn.select_db(rt.transformer.target_schema)
            # REPLACE 前提（§4.3.2）：写后会话关闭外键检查，规避级联副作用
            with rt.tgt_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=0")
        return rt.tgt_conn

    def _close_target(self, rt: TaskRuntime) -> None:
        if rt.tgt_conn is not None:
            try:
                rt.tgt_conn.close()
            except Exception:  # noqa: BLE001
                pass
            rt.tgt_conn = None

    # ============================================================
    # T3.8 重刷-增量串行编排
    # ============================================================
    def begin_resync(self, task_id: int, tables: list[str]) -> None:
        rt = self._rts.get(task_id)
        if not rt:
            return
        with rt.cache_lock:
            rt.resync_tables = set(tables)
            rt.caching = True
            rt.position_frozen = True
            rt.spilled_ops = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        log.info("重刷开始：涉及表 %s 的增量事件转入缓存队列 task=%s", tables, task_id)

    def end_resync(self, task_id: int) -> None:
        rt = self._rts.get(task_id)
        if not rt or not rt.caching:
            return
        t = threading.Thread(target=self._replay_cache, args=(rt,), daemon=True,
                             name=f"cdc-replay-{task_id}")
        rt.replay_thread = t
        t.start()

    def _maybe_spill(self, rt: TaskRuntime) -> None:
        """内存+磁盘混合防溢出：超过 20 万条溢写到磁盘 JSONL。

        B4 修复：重放期间禁止再溢写——重放循环优先消费磁盘文件，若重放期间产生
        新溢写，其中「较晚」的事件会先于内存中「较早」的事件被重放，违反 binlog
        顺序（先重放 t2 的 DELETE、后重放 t1 的 REPLACE → 已删行复活且位点冻结
        期间无法自愈）。重放期间事件仅驻内存，由重放线程按序消费。
        """
        if len(rt.cache) < 200_000:
            return
        if rt.replaying:
            if len(rt.cache) - rt.replay_mem_warn_at >= 200_000:
                rt.replay_mem_warn_at = len(rt.cache)
                log.warning("重放期间缓存仅驻内存（防乱序，不再溢写）task=%s 当前 %d 条，"
                            "若持续增长请评估重刷窗口内源库写入速率", rt.task_id, len(rt.cache))
            return
        if rt.spill_path is None:
            rt.spill_path = self.cache_dir / f"resync_cache_{rt.task_id}.jsonl"
        with open(rt.spill_path, "a", encoding="utf-8") as f:
            for op in rt.cache:
                f.write(json.dumps({"k": op.kind, "t": op.table, "tt": op.target_table,
                                    "pk": op.pk_cols, "c": op.cols, "v": list(op.values or ()),
                                    "w": list(op.where_vals or ()), "ts": op.src_ts,
                                    "lf": op.log_file, "lp": op.log_pos,
                                    "g": op.gtid_snapshot, "o": op.origin},
                                   ensure_ascii=False, default=_op_json_default) + "\n")
        rt.spilled_ops += len(rt.cache)
        rt.cache.clear()
        log.info("重刷缓存溢写磁盘 task=%s 累计 %s 条", rt.task_id, rt.spilled_ops)

    def _replay_cache(self, rt: TaskRuntime) -> None:
        """重放衔接（§4.2.2）：重放期间新到涉及表事件继续追加缓存；缓存清空后恢复直通。"""
        rt.replaying = True
        try:
            while True:
                with rt.cache_lock:
                    if rt.spill_path is not None and rt.spilled_ops > 0:
                        # P2-10：分段读取溢写文件（修复前整文件读入内存，可达数 GB）
                        ops = self._load_spilled(rt)
                    elif rt.cache:
                        ops = rt.cache[:2000]
                        del rt.cache[:2000]
                    else:
                        # 缓存清空 → 恢复直通；此后 reader 的 append 才会进直通队列
                        rt.caching = False
                        rt.replaying = False
                        rt.resync_tables = None
                        rt.position_frozen = False   # 直通恢复后位点继续推进
                        rt.replay_mem_warn_at = 0
                        if rt.spill_path is not None:
                            try:
                                rt.spill_path.unlink(missing_ok=True)
                            except Exception:  # noqa: BLE001
                                pass
                        log.info("重刷缓存重放完成，恢复直通 task=%s", rt.task_id)
                        return
                if ops:
                    self._apply_replay_batch(rt, ops)
        except Exception as e:  # noqa: BLE001
            # 此前只置 replaying=False：caching/position_frozen/resync_tables 悬挂 →
            # 涉及表事件永久进入无消费者的缓存（内存/磁盘无限增长）、位点永不推进、延迟虚高。
            # 重放失败按不可恢复处理：告警 + 任务转 ERROR + run failed + 引擎下线。
            # 位点保持冻结，应用重启后从冻结位点重放 binlog 窗口（at-least-once + REPLACE
            # 幂等）自然收敛，不丢数据。
            log.exception("缓存重放异常 task=%s", rt.task_id)
            rt.replaying = False
            self._alert(rt, "cdc_replay_failed",
                        f"重刷缓存重放失败，任务已暂停待处理：{e}", level="error")
            try:
                self.orch.transition_from_current(rt.task_id, ERROR,
                                                  reason=f"缓存重放失败：{str(e)[:150]}")
            except Exception:  # noqa: BLE001
                log.exception("状态迁移 error 失败 task=%s（任务可能滞留运行态，需人工复位）",
                              rt.task_id)
            try:
                with rt.cv:
                    rt.finalized = True   # 与 stop() 互斥，防双重 finish
                self.runs.finish(rt.run_id, state="failed", error=str(e))
            except Exception:  # noqa: BLE001
                log.exception("重放失败收尾落库失败 task=%s", rt.task_id)
            with self._lock:
                self._rts.pop(rt.task_id, None)
                self.limiter.remove_task(rt.task_id)
            self._close_target(rt)

    def _load_spilled(self, rt: TaskRuntime, limit: int = 20000) -> list[Op]:
        """读取溢写文件前 limit 行（P2-10：分段消费，防整文件入内存）。

        仅重放线程在 cache_lock 内调用；B4 修复保证重放期间无新溢写，
        因此「读前 limit 行 + 原子回写剩余行」不会与写入方竞争。
        """
        out: list[Op] = []
        rest: list[str] = []
        with open(rt.spill_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < limit:
                    d = json.loads(line, object_hook=_op_json_hook)
                    out.append(Op(kind=d["k"], table=d["t"], target_table=d["tt"],
                                  pk_cols=d["pk"], cols=d["c"], values=tuple(d["v"]),
                                  where_vals=tuple(d["w"]), pk_key=tuple(d["w"] or d["v"]),
                                  src_ts=d["ts"], log_file=d["lf"], log_pos=d["lp"],
                                  gtid_snapshot=d.get("g", ""), origin=d.get("o", "insert")))
                else:
                    rest.append(line)
        if rest:
            tmp = rt.spill_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rest)
            tmp.replace(rt.spill_path)   # 原子替换，避免中途崩溃截断缓存
        else:
            rt.spill_path.unlink(missing_ok=True)
        rt.spilled_ops = max(0, rt.spilled_ops - len(out))
        return out

    def _apply_replay_batch(self, rt: TaskRuntime, ops: list[Op]) -> None:
        with rt.cache_lock:
            # 重放期间涉及表新事件已由 reader 继续追加缓存（caching 保持 True）；
            # 位点冻结保持，直至缓存彻底清空
            rt.position_frozen = True
        self._apply_batch(rt, ops)

    # ============================================================
    # 异常收尾（T3.2/T3.7）
    # ============================================================
    def _mark_needs_reinit(self, rt: TaskRuntime, msg: str) -> None:
        log.error("位点失效熔断 task=%s：%s", rt.task_id, msg)
        self._alert(rt, "cdc_position_lost",
                    f"{msg}。任务 {rt.task_id} 已熔断进入「需重新初始化」，"
                    "请在 UI 一键全量重刷", level="error")
        try:
            self.orch.transition_from_current(rt.task_id, NEEDS_REINIT, reason=msg[:200])
        except Exception:  # noqa: BLE001
            log.exception("状态迁移 needs_reinit 失败")
        self.runs.finish(rt.run_id, state="failed", error=msg)
        with self._lock:
            self._rts.pop(rt.task_id, None)
            self.limiter.remove_task(rt.task_id)
        self._close_target(rt)

    def _mark_parse_aborted(self, rt: TaskRuntime, digest: str) -> None:
        log.error("事件解析异常熔断 task=%s：%s", rt.task_id, digest)
        self._alert(rt, "cdc_parse", f"binlog 事件解析异常，已熔断待人工：{digest}",
                    level="error")
        try:
            self.orch.transition_from_current(rt.task_id, ERROR,
                                              reason="事件解析异常熔断待人工")
        except Exception:  # noqa: BLE001
            log.exception("状态迁移 error 失败")
        self.runs.finish(rt.run_id, state="failed", error=digest[:500])
        with self._lock:
            self._rts.pop(rt.task_id, None)
            self.limiter.remove_task(rt.task_id)
        self._close_target(rt)

    # ============================================================
    # 启动自检（T3.7）：应用崩溃/重启后恢复任务与位点自动续跑
    # ============================================================
    def recover_on_startup(self) -> list[int]:
        recovered: list[int] = []
        try:
            running_runs, _ = self.runs.list(run_type="cdc_session", status="running", page=1,
                                             page_size=500)
            for r in running_runs:
                self.runs.finish(r["id"], state="failed",
                                 error="应用重启，上次增量会话中断（由启动自检接管）")
        except Exception:  # noqa: BLE001
            log.exception("清理残留 cdc_session 失败")
        for task in self.tasks.list():
            if task["mode"] == "init_plus_cdc" and task["state"] == CDC_RUNNING:
                try:
                    self.start(task["id"], trigger="auto_recover")
                    recovered.append(task["id"])
                    log.info("启动自检：任务 %s 增量自动续跑", task["id"])
                except Exception as e:  # noqa: BLE001
                    log.error("启动自检恢复任务 %s 失败：%s", task["id"], e)
        return recovered


class _TargetWriteFailed(Exception):
    """目标库批写入重试耗尽（T3.7）。"""

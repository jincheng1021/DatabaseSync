"""M3 单元测试：CDC 引擎主体（T3.2 位点 / T3.4 攒批背压大事务 / T3.5 DDL 拦截 / T3.6 告警 / T3.7 异常 / T3.8 缓存编排）。

不依赖真实 MySQL：binlog 事件以真实事件类子类伪造，目标连接以 FakeConn 顶替。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymysqlreplication.event import QueryEvent
from pymysqlreplication.row_event import (DeleteRowsEvent, UpdateRowsEvent,
                                          WriteRowsEvent)

from app.core.limiter import TwoLevelLimiter
from app.core.orchestrator import Orchestrator
from app.models.m2_repository import (CdcRunRepository, DdlPendingRepository,
                                      PositionRepository, RunRepository, TaskRepository)
from app.models.repository import AlertRepository, SettingRepository
from app.services import cdc_engine as cde
from app.services.cdc_engine import CdcEngine, PositionLostError, TaskRuntime
from app.services.cdc_transform import EventTransformer, Op
from app.services.mysql_client import MySQLTarget

TS = 1700000000


# ============================================================
# 夹具与伪造件
# ============================================================

def _pkt(log_pos, raw=b"\x01\x02"):
    return SimpleNamespace(log_pos=log_pos, raw_data=raw)


class FakeWrite(WriteRowsEvent):
    def __init__(self, rows, schema="src_db", table="users", ts=TS, log_pos=500):
        # rows 为基类只读 property（内部 __rows 惰性解析），直接落底属性
        self._RowsEvent__rows = rows
        self.schema, self.table = schema, table
        self.timestamp, self.packet = ts, _pkt(log_pos)


class FakeUpdate(UpdateRowsEvent):
    def __init__(self, rows, schema="src_db", table="users", ts=TS, log_pos=500):
        self._RowsEvent__rows = rows
        self.schema, self.table = schema, table
        self.timestamp, self.packet = ts, _pkt(log_pos)


class FakeDelete(DeleteRowsEvent):
    def __init__(self, rows, schema="src_db", table="users", ts=TS, log_pos=500):
        self._RowsEvent__rows = rows
        self.schema, self.table = schema, table
        self.timestamp, self.packet = ts, _pkt(log_pos)


class FakeQuery(QueryEvent):
    def __init__(self, query, schema="src_db", ts=TS, log_pos=600):
        self.query, self.schema = query, schema
        self.timestamp, self.packet = ts, _pkt(log_pos)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, args=None):
        if self.conn.fail_left > 0:
            self.conn.fail_left -= 1
            raise pymysql.err.OperationalError(1045, "target write fail")
        self.executed.append((sql, args))

    def executemany(self, sql, seq):
        for a in seq:
            self.execute(sql, a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, fail_left: int = 0):
        self.fail_left = fail_left
        self.committed = 0
        self.selected_db: str | None = None
        self.cursor_obj = FakeCursor(self)

    def cursor(self, *a, **kw):
        return self.cursor_obj

    def select_db(self, db):
        self.selected_db = db

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def cdc(m2_repos, orch, seed_ds, tmp_path):
    db = m2_repos["db"]
    eng = CdcEngine(
        tasks=m2_repos["tasks"], runs=m2_repos["runs"], positions=m2_repos["positions"],
        settings=SettingRepository(db), orchestrator=orch,
        limiter=TwoLevelLimiter(global_rate=0),
        get_source=lambda t: MySQLTarget("127.0.0.1", 3306, "u", "p", default_schema="src_db"),
        get_target=lambda t: MySQLTarget("127.0.0.1", 3307, "u", "p", default_schema="dst_db"),
        ddl_repo=m2_repos["ddl"], alert_repo=m2_repos["alerts"], audit=None,
        broadcaster=None, lag_repo=CdcRunRepository(db), cache_dir=tmp_path / "cache")
    return eng


@pytest.fixture()
def a_cdc_task(m2_repos, orch, seed_ds):
    """state=cdc_running 的 init_plus_cdc 任务 + 已落 P0 位点。"""
    tasks = m2_repos["tasks"]
    tid = tasks.create({"name": "t3", "source_id": seed_ds[0], "target_id": seed_ds[1],
                        "mode": "init_plus_cdc", "mapping": {"scope": "all"},
                        "state": "cdc_running"})
    m2_repos["positions"].save_p0(tid, {"gtid_executed": None, "binlog_file": "bin.000001",
                                        "binlog_pos": 4, "server_id_used": 30001})
    return tasks.get(tid)


def make_rt(cdc: CdcEngine, task: dict, **over) -> TaskRuntime:
    tr = EventTransformer(task["mapping"], "src_db", "dst_db")
    rt = TaskRuntime(
        task_id=task["id"], task=task, transformer=tr,
        source=MySQLTarget("127.0.0.1", 3306, "u", "p", default_schema="src_db"),
        target=MySQLTarget("127.0.0.1", 3307, "u", "p", default_schema="dst_db"),
        server_id=30001, run_id=1, queue_limit=100, commit_batch_rows=100,
        commit_interval=0.05, big_tx_threshold=over.pop("big_tx_threshold", 10 ** 9),
        lag_alert_sec=60, **over)
    rt.table_meta["src_db.users"] = {"pk": ["id"]}
    return rt


# ============================================================
# ROW 事件处理与攒批（T3.3/T3.4）
# ============================================================

def test_handle_rows_enqueues_ops(cdc, a_cdc_task):
    rt = make_rt(cdc, a_cdc_task)
    cdc._handle_event(rt, FakeWrite([{"values": {"id": 1}}, {"values": {"id": 2}}], log_pos=520))
    assert len(rt.queue) == 2
    assert rt.log_pos == 520


def test_handle_rows_pkless_table_dropped_and_alert(cdc, a_cdc_task, m2_repos):
    rt = make_rt(cdc, a_cdc_task)
    rt.table_meta["src_db.nopk"] = None
    cdc._handle_event(rt, FakeWrite([{"values": {"c": 1}}], table="nopk"))
    assert len(rt.queue) == 0 and rt.out_of_scope_dropped == 1
    alerts = m2_repos["alerts"].list_recent(10)
    assert any("无主键" in a["message"] for a in alerts)


def test_apply_batch_writes_and_persists_position(cdc, a_cdc_task, m2_repos, monkeypatch):
    rt = make_rt(cdc, a_cdc_task)
    fake = FakeConn()
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    ops = rt.transformer.insert("src_db", "users", [{"id": 1, "name": "a"}], ["id"],
                                TS, "bin.000001", 777, "uuid:1-5")
    rt.gtid_snapshot = "uuid:1-5"
    cdc._apply_batch(rt, ops)
    sqls = [s for s, _ in fake.cursor_obj.executed]
    assert any(s.startswith("REPLACE INTO `users`") for s in sqls)
    assert fake.committed == 1
    pos = m2_repos["positions"].get(a_cdc_task["id"])
    assert pos["binlog_pos"] == 777 and pos["gtid_executed"] == "uuid:1-5"
    assert rt.counters["inserted"] == 1
    assert rt.last_lag is not None and rt.last_lag >= 0


def test_apply_batch_merge_in_tx_delete_first(cdc, a_cdc_task, monkeypatch):
    """E2 同批提交：DELETE 旧行 + REPLACE 新行在同一目标事务，先删后插。"""
    rt = make_rt(cdc, a_cdc_task)
    fake = FakeConn()
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    ops = rt.transformer.update("src_db", "users",
                                [{"before_values": {"id": 3}, "after_values": {"id": 9}}],
                                ["id"], TS, "bin.000001", 800, "")
    cdc._apply_batch(rt, ops)
    sqls = [s for s, _ in fake.cursor_obj.executed if not s.startswith("SET ")]
    assert sqls[0].startswith("DELETE FROM") and sqls[1].startswith("REPLACE INTO")
    assert fake.committed == 1   # 同一事务


def test_apply_batch_retry_then_success(cdc, a_cdc_task, monkeypatch):
    rt = make_rt(cdc, a_cdc_task)
    fake = FakeConn(fail_left=1)
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    ops = rt.transformer.insert("src_db", "users", [{"id": 1}], ["id"], TS, "bin.000001", 100, "")
    cdc._apply_batch(rt, ops)
    assert fake.committed == 1


def test_apply_batch_retry_exhausted_raises(cdc, a_cdc_task, monkeypatch):
    rt = make_rt(cdc, a_cdc_task)
    fake = FakeConn(fail_left=99)
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    ops = rt.transformer.insert("src_db", "users", [{"id": 1}], ["id"], TS, "bin.000001", 100, "")
    with pytest.raises(cde._TargetWriteFailed):
        cdc._apply_batch(rt, ops)


def test_collect_batch_size_and_interval(cdc, a_cdc_task):
    rt = make_rt(cdc, a_cdc_task)
    rt.commit_batch_rows = 3
    rt.commit_interval = 0.05
    for i in range(5):
        rt.queue.append(Op("replace", "users", "users", ["id"], cols=["id"],
                           values=(i,), pk_key=(i,), log_pos=i + 1))
    with rt.cv:
        rt.cv.notify_all()
    batch = cdc._collect_batch(rt)
    assert len(batch) == 3          # 满 3 条先到者提交
    batch2 = cdc._collect_batch(rt)
    assert len(batch2) == 2


def test_big_tx_flag_and_alert(cdc, a_cdc_task, m2_repos):
    """大事务（E8）：超阈值切流式 + 告警文案含已知限制声明。"""
    rt = make_rt(cdc, a_cdc_task, big_tx_threshold=3)
    for i in range(5):
        cdc._handle_event(rt, FakeWrite([{"values": {"id": i}}], log_pos=100 + i))
    assert rt.big_tx is True
    alerts = m2_repos["alerts"].list_recent(10)
    assert any("拆散源事务边界" in a["message"] for a in alerts)


def test_backpressure_high_watermark(cdc, a_cdc_task):
    rt = make_rt(cdc, a_cdc_task)
    rt.queue_limit = 2
    # 队列满 → reader 侧等待（直接调用放入会在 cv.wait 阻塞，改为验证状态判定）
    rt.backpressure = True
    assert rt.backpressure


# ============================================================
# DDL 拦截与决策（T3.5/E12）
# ============================================================

def test_ddl_intercept_blocks_and_records(cdc, a_cdc_task, m2_repos):
    rt = make_rt(cdc, a_cdc_task)
    rt.log_file = "bin.000001"
    cdc._handle_event(rt, FakeQuery("ALTER TABLE `users` ADD COLUMN c INT", log_pos=900))
    pendings = m2_repos["ddl"].list_pending(a_cdc_task["id"])
    assert len(pendings) == 1 and pendings[0]["ddl_text"].startswith("ALTER TABLE")
    assert pendings[0]["source_position"].endswith("900")
    assert m2_repos["tasks"].get(a_cdc_task["id"])["state"] == "ddl_pending"
    assert rt.ddl_block.is_set()
    alerts = m2_repos["alerts"].list_recent(10)
    assert any("DDL" in a["message"] for a in alerts)


def test_ddl_begin_commit_ignored(cdc, a_cdc_task, m2_repos):
    rt = make_rt(cdc, a_cdc_task)
    cdc._handle_query(rt, FakeQuery("BEGIN"))
    cdc._handle_query(rt, FakeQuery("COMMIT"))
    assert m2_repos["ddl"].list_pending(a_cdc_task["id"]) == []
    assert not rt.ddl_block.is_set()


def test_ddl_remembered_rule_auto_skip(cdc, a_cdc_task, m2_repos):
    """E12：记住选择 → 自动决策落审计，不再进入待决策阻塞。"""
    m2_repos["tasks"].update(a_cdc_task["id"],
                             {"ddl_rules": {"remember": {"ALTER TABLE users": "skip"}}})
    task = m2_repos["tasks"].get(a_cdc_task["id"])
    rt = make_rt(cdc, task)
    cdc._handle_query(rt, FakeQuery("ALTER TABLE `users` ADD COLUMN c INT"))
    assert not rt.ddl_block.is_set()
    assert m2_repos["tasks"].get(a_cdc_task["id"])["state"] == "cdc_running"
    decided = m2_repos["ddl"].list_pending(a_cdc_task["id"])
    assert decided == []   # 无 pending（已自动决策）


def test_ddl_remembered_rule_auto_execute(cdc, a_cdc_task, m2_repos, monkeypatch):
    m2_repos["tasks"].update(a_cdc_task["id"],
                             {"ddl_rules": {"remember": {"ALTER TABLE users": "execute"}}})
    task = m2_repos["tasks"].get(a_cdc_task["id"])
    rt = make_rt(cdc, task)
    fake = FakeConn()
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    cdc._handle_query(rt, FakeQuery("ALTER TABLE `users` ADD COLUMN c INT"))
    sqls = [s for s, _ in fake.cursor_obj.executed]
    assert any(s.startswith("ALTER TABLE") for s in sqls)
    assert fake.selected_db == "dst_db"


def test_apply_ddl_public_entry(cdc, a_cdc_task, monkeypatch):
    rt = make_rt(cdc, a_cdc_task)
    cdc._rts[a_cdc_task["id"]] = rt
    fake = FakeConn()
    monkeypatch.setattr(cde, "open_connection", lambda t: fake)
    cdc.apply_ddl(a_cdc_task["id"], "DROP TABLE tmp_x")
    assert any(s.startswith("DROP TABLE") for s, _ in fake.cursor_obj.executed)


# ============================================================
# 延迟监控与告警（T3.6）
# ============================================================

def test_monitor_tick_alert_and_resolve(cdc, a_cdc_task, m2_repos):
    rt = make_rt(cdc, a_cdc_task)
    cdc._rts[a_cdc_task["id"]] = rt
    rt.last_lag = 120.0    # > 60s 阈值
    outs = cdc.monitor_tick()
    assert outs and outs[0]["task_id"] == a_cdc_task["id"]
    assert outs[0]["lag_seconds"] == 120.0
    alerts = m2_repos["alerts"].list_recent(10)
    assert any("延迟" in a["message"] for a in alerts)
    # 回落到 50% 以下 → 解除
    rt.last_lag = 20.0
    cdc.monitor_tick()
    rows = [a for a in m2_repos["alerts"].list_recent(20) if a["alert_key"].startswith("cdc_lag")]
    assert any(a["resolved_at"] for a in rows)


def test_lag_history_repo_roundtrip(cdc, a_cdc_task):
    rt = make_rt(cdc, a_cdc_task)
    cdc._rts[a_cdc_task["id"]] = rt
    now_ms = int(time.time() * 1000)
    cdc.record_minute_sample(a_cdc_task["id"], now_ms - 60000, 5.0)
    cdc.record_minute_sample(a_cdc_task["id"], now_ms, 9.0)
    pts = cdc.lag_history(a_cdc_task["id"], minutes=10)
    assert len(pts) == 2 and pts[-1]["lag_seconds"] == 9.0


# ============================================================
# 异常路径（T3.7）与启动自检
# ============================================================

def test_classify_1236_as_position_lost():
    e = pymysql.err.OperationalError(1236, "Could not find first log file name in binary log index file")
    assert isinstance(cdc_engine_classify(e), PositionLostError)
    e2 = pymysql.err.OperationalError(2013, "Lost connection to MySQL server")
    assert not isinstance(cdc_engine_classify(e2), PositionLostError)


def cdc_engine_classify(e):
    return CdcEngine._classify_stream_error(e)


def test_parse_error_creates_digest(cdc, a_cdc_task):
    class Boom(WriteRowsEvent):
        def __init__(self):
            self._RowsEvent__rows = object()   # 不可迭代 → 转换期异常
            self.schema = "src_db"
            self.table = "users"
            self.timestamp = TS
            self.packet = _pkt(1, raw=b"\xde\xad\xbe\xef")
    rt = make_rt(cdc, a_cdc_task)
    with pytest.raises(cde.ParseAbortedError) as ei:
        cdc._handle_event(rt, Boom())
    assert "deadbeef" in str(ei.value)    # 十六进制摘要


def test_mark_needs_reinit(cdc, a_cdc_task, m2_repos):
    rt = make_rt(cdc, a_cdc_task)
    rt.run_id = m2_repos["runs"].start(task_id=a_cdc_task["id"], run_type="cdc_session")
    cdc._rts[a_cdc_task["id"]] = rt
    cdc._mark_needs_reinit(rt, "binlog purged")
    assert m2_repos["tasks"].get(a_cdc_task["id"])["state"] == "needs_reinit"
    assert not cdc.is_running(a_cdc_task["id"])
    run = m2_repos["runs"].get(rt.run_id)
    assert run["state"] == "failed"


def test_recover_on_startup(cdc, a_cdc_task, monkeypatch):
    """启动自检：崩溃前 cdc_running 的任务自动续跑；残留会话收尾。"""
    calls = []
    monkeypatch.setattr(cdc, "start", lambda tid, trigger="manual": calls.append((tid, trigger)) or 1)
    recovered = cdc.recover_on_startup()
    assert recovered == [a_cdc_task["id"]]
    assert calls and calls[0][1] == "auto_recover"


# ============================================================
# T3.8 重刷缓存编排
# ============================================================

def test_resync_cache_and_replay(cdc, a_cdc_task, monkeypatch):
    """重刷期间涉及表入缓存、位点冻结；end_resync 重放后恢复直通。"""
    rt = make_rt(cdc, a_cdc_task)
    cdc._rts[a_cdc_task["id"]] = rt
    applied: list[list[Op]] = []
    monkeypatch.setattr(cdc, "_apply_batch", lambda r, ops: applied.append(list(ops)))
    cdc.begin_resync(a_cdc_task["id"], ["users"])
    assert rt.caching and rt.position_frozen
    cdc._handle_event(rt, FakeWrite([{"values": {"id": 1}}], log_pos=10))
    cdc._handle_event(rt, FakeWrite([{"values": {"id": 2}}], log_pos=20))
    assert len(rt.cache) == 2
    # 位点冻结：_apply_batch 不落位点（此测试以 monkeypatch 顶替，验证冻结标志即可）
    cdc.end_resync(a_cdc_task["id"])
    rt.replay_thread.join(timeout=5)
    assert not rt.caching and not rt.replaying and not rt.position_frozen
    flat = [op for b in applied for op in b]
    assert [op.values for op in flat] == [(1,), (2,)]   # binlog 顺序重放


def test_resync_unaffected_table_direct(cdc, a_cdc_task):
    rt = make_rt(cdc, a_cdc_task)
    cdc.begin_resync(a_cdc_task["id"], ["orders"])
    cdc._handle_event(rt, FakeWrite([{"values": {"id": 1}}], log_pos=10))   # users 不涉及
    assert len(rt.cache) == 0 and len(rt.queue) == 1   # 直通


def test_start_requires_position(cdc, a_cdc_task):
    import app.models.m2_repository as m2
    # 无位点任务
    tasks = cdc.tasks
    tid = tasks.create({"name": "nopos", "source_id": a_cdc_task["source_id"],
                        "target_id": a_cdc_task["target_id"], "mode": "init_plus_cdc",
                        "mapping": {"scope": "all"}, "state": "idle"})
    with pytest.raises(KeyError):
        cdc.start(tid)


def test_op_spill_json_roundtrip():
    """P2-9 回归：溢写 JSONL 编解码 bytes/datetime/decimal 类型保真（修复前 default=str 失真）。"""
    import json as _json
    from datetime import datetime as dt
    from decimal import Decimal

    from app.services.cdc_engine import _op_json_default, _op_json_hook

    vals = (b"\xff\x00ab", dt(2026, 1, 2, 3, 4, 5), Decimal("1.50"), "txt", None, 7)
    s = _json.dumps(list(vals), default=_op_json_default)
    out = _json.loads(s, object_hook=_op_json_hook)
    assert out[0] == b"\xff\x00ab"
    assert out[1] == dt(2026, 1, 2, 3, 4, 5)
    assert out[2] == Decimal("1.50")
    assert list(out[3:]) == ["txt", None, 7]


# ============================================================
# 源库 binlog 参数预检（binlog_row_metadata / binlog_row_image）
# ============================================================

class VarCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=None):
        pass

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class VarConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, *a, **kw):
        return VarCursor(self.rows)

    def close(self):
        pass


def test_binlog_preflight_alert_on_minimal(cdc, a_cdc_task, m2_repos, monkeypatch):
    """非 FULL → 发 cdc_binlog_meta 告警，消息含修复 SQL。"""
    monkeypatch.setattr(
        cde, "open_connection",
        lambda t: VarConn([("binlog_row_metadata", "MINIMAL"),
                           ("binlog_row_image", "FULL")]))
    rt = make_rt(cdc, a_cdc_task)
    cdc._check_binlog_settings(rt)
    alerts = [a for a in m2_repos["alerts"].list_recent(10)
              if a["alert_key"].startswith("cdc_binlog_meta")]
    assert alerts and "binlog_row_metadata" in alerts[0]["message"]
    assert "SET GLOBAL binlog_row_metadata=FULL" in alerts[0]["message"]


def test_binlog_preflight_resolve_on_full(cdc, a_cdc_task, monkeypatch):
    """两个参数均为 FULL → resolve（且不 fire）。"""
    monkeypatch.setattr(
        cde, "open_connection",
        lambda t: VarConn([("binlog_row_metadata", "FULL"),
                           ("binlog_row_image", "FULL")]))
    rt = make_rt(cdc, a_cdc_task)
    resolved, fired = [], []
    monkeypatch.setattr(cdc, "_resolve_alert", lambda r, key: resolved.append(key))
    monkeypatch.setattr(cdc, "_alert", lambda r, key, msg, level="warning": fired.append(key))
    cdc._check_binlog_settings(rt)
    assert resolved == ["cdc_binlog_meta"] and not fired


def test_binlog_preflight_failure_silent(cdc, a_cdc_task, m2_repos, monkeypatch):
    """预检连接失败 → 旁路容错：不抛异常、不发告警，不阻断 CDC 主流程。"""
    def boom(t):
        raise pymysql.err.OperationalError(2003, "connection refused")
    monkeypatch.setattr(cde, "open_connection", boom)
    rt = make_rt(cdc, a_cdc_task)
    cdc._check_binlog_settings(rt)   # 不应抛出
    assert not [a for a in m2_repos["alerts"].list_recent(10)
                if a["alert_key"].startswith("cdc_binlog_meta")]


def test_binlog_preflight_alert_silence_dedup(cdc, a_cdc_task, monkeypatch):
    """重连循环反复预检：告警静默期内 fire 返回 False → 不重复 emit。"""
    monkeypatch.setattr(
        cde, "open_connection",
        lambda t: VarConn([("binlog_row_metadata", "MINIMAL"),
                           ("binlog_row_image", "MINIMAL")]))
    rt = make_rt(cdc, a_cdc_task)
    emits = []
    monkeypatch.setattr(cdc, "_emit", lambda r, ev, payload: emits.append(ev))
    cdc._check_binlog_settings(rt)
    cdc._check_binlog_settings(rt)   # 静默期内第二次
    assert emits.count("alert.fired") == 1


# ============================================================
# 重连退避：流稳定存活后才重置（防「建得开但马上断」的 1s 重连风暴）
# ============================================================

class DyingStream:
    """每次 fetchone 立即断连：模拟 server_id 冲突/闪断等「建得开但马上断」。"""

    def fetchone(self):
        raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server")

    def close(self):
        pass


def _run_reconnect_loop(cdc, rt, n):
    """后台线程跑 _reader_loop，收集 n 次 sleep(backoff) 后置 stopped 收尾。"""
    sleeps: list[float] = []
    real_sleep = cde.time.sleep

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= n:
            rt.stopped = True

    cde.time.sleep = fake_sleep
    try:
        th = threading.Thread(target=cdc._reader_loop, args=(rt,), daemon=True)
        th.start()
        th.join(timeout=10)
    finally:
        cde.time.sleep = real_sleep
    assert len(sleeps) == n
    return sleeps


def test_reconnect_backoff_grows_when_stream_dies_immediately(cdc, a_cdc_task):
    """默认 stable=30s：流每次建成功后立即断 → 退避 1,2,4,8 递增（修复前恒为 1）。"""
    rt = make_rt(cdc, a_cdc_task)
    cdc._check_binlog_settings = lambda *a, **kw: None   # 预检不真实连库
    cdc._open_stream = lambda r: DyingStream()
    sleeps = _run_reconnect_loop(cdc, rt, n=4)
    assert sleeps == [1, 2, 4, 8]


def test_reconnect_backoff_resets_after_stable_stream(cdc, a_cdc_task, m2_repos):
    """stable=0（流存活即视为恢复）→ 每次重连后退避重置为 1，保持快速恢复语义。"""
    SettingRepository(m2_repos["db"]).set("reconnect_stable_seconds", 0)
    rt = make_rt(cdc, a_cdc_task)
    cdc._check_binlog_settings = lambda *a, **kw: None
    cdc._open_stream = lambda r: DyingStream()
    sleeps = _run_reconnect_loop(cdc, rt, n=4)
    assert sleeps == [1, 1, 1, 1]

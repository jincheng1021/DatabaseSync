"""M6 拷贝链路修复测试：keyset 分批推进、断线重连、建连失败表级登记、超时配置注入。

不依赖真实 MySQL：源/目标连接以 FakeConn 顶替（仿 test_m3_engine 模式），
monkeypatch full_engine.open_connection_with_retry / get_primary_key / fetch_all / _show_create。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.limiter import TwoLevelLimiter  # noqa: E402
from app.core.orchestrator import Orchestrator  # noqa: E402
from app.core.progress import ProgressTracker  # noqa: E402
from app.models.m2_repository import PositionRepository, RunRepository, TaskRepository  # noqa: E402
from app.services import full_engine as fe  # noqa: E402
from app.services.full_engine import FullEngine, MappingResolver  # noqa: E402
from app.services.mysql_client import MySQLTarget  # noqa: E402

SRC = MySQLTarget(host="src", port=3306, username="u", password=None)
TGT = MySQLTarget(host="tgt", port=3307, username="u", password=None)

DATA = [(1, "a"), (2, "b"), (3, "c"), (4, "d")]  # 主键表 t1：4 行，batch_size=2 → 2 批


class FakeSettings:
    def __init__(self, d=None):
        self.d = d or {}

    def get(self, k):
        return self.d.get(k)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def execute(self, sql, args=None):
        s = sql.strip().upper()
        if s.startswith("SELECT"):
            last = args[0] if args else None
            rows = [r for r in self.conn.data if last is None or r[0] > last]
            self._result = rows[:self.conn.batch]
        else:
            self._result = []
        return self._result

    def executemany(self, sql, seq):
        if self.conn.fail_replaces_left > 0:
            self.conn.fail_replaces_left -= 1
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query (timed out)")
        self.conn.written.extend(seq)

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


class _CursorCtx:
    def __init__(self, conn):
        self.cur = FakeCursor(conn)

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, data=None, batch=2, fail_replaces_left=0):
        self.data = data or []
        self.batch = batch
        self.fail_replaces_left = fail_replaces_left
        self.selects = []
        self.written = []
        self.commits = 0
        self.closed = False

    def select_db(self, db):
        self.selects.append(db)

    def cursor(self, cursor_class=None):
        return _CursorCtx(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def ping(self, reconnect=True):
        return True

    def close(self):
        self.closed = True


@pytest.fixture()
def copy_env(m2_repos, seed_ds, orch, tracker, limiter):
    """FullEngine 装配 + 已建 run 行的 rp。"""
    tasks: TaskRepository = m2_repos["tasks"]
    tid = tasks.create({
        "name": "copy-fix", "source_id": seed_ds[0], "target_id": seed_ds[1],
        "mode": "init_plus_cdc", "mapping": {"scope": "all"}, "state": "draft",
    })
    task = tasks.get(tid)
    engine = FullEngine(
        tasks=tasks, runs=m2_repos["runs"], positions=PositionRepository(m2_repos["db"]),
        settings=FakeSettings(), orchestrator=orch, tracker=tracker,
        limiter=TwoLevelLimiter(global_rate=100000),
        get_source=lambda x: SRC, get_target=lambda x: TGT)
    run_id = m2_repos["runs"].start(task_id=tid, run_type="init", trigger="manual")
    rp = tracker.start(run_id, tid, "init")
    resolver = MappingResolver({"source_database": "srcdb", "target_database": "tgtdb"}, None, None)
    return engine, m2_repos, task, rp, run_id, resolver


def _prep_table(rp, table="t1"):
    rp.tables[table] = fe._tp(table)


# ---- keyset 分批推进（P0 缺陷修复） ----

def test_iter_batches_advances_keyset(copy_env):
    engine, *_ = copy_env
    conn = FakeConn(data=DATA, batch=2)
    batches = list(engine._iter_batches(conn, "srcdb", "t1", ["id", "v"], ["id"], 2, {}))
    assert batches == [[(1, "a"), (2, "b")], [(3, "c"), (4, "d")]]
    # FakeCursor 无记录钩子：直接用第二个连接验证 SQL 形状——批间 WHERE 推进
    conn2 = FakeConn(data=DATA, batch=2)
    seen_sql = []

    orig_execute = FakeCursor.execute

    def spy_execute(self, sql, args=None):
        seen_sql.append((sql, args))
        return orig_execute(self, sql, args)

    FakeCursor.execute = spy_execute
    try:
        list(engine._iter_batches(conn2, "srcdb", "t1", ["id", "v"], ["id"], 2, {}))
    finally:
        FakeCursor.execute = orig_execute
    assert len(seen_sql) == 3            # 批1 + 批2 + 空批收尾
    assert "WHERE" not in seen_sql[0][0]
    assert "`id` > %s" in seen_sql[1][0] and seen_sql[1][1] == (2,)
    assert "`id` > %s" in seen_sql[2][0] and seen_sql[2][1] == (4,)


def test_iter_batches_resumes_from_checkpoint(copy_env):
    engine, *_ = copy_env
    conn = FakeConn(data=DATA, batch=2)
    batches = list(engine._iter_batches(conn, "srcdb", "t1", ["id", "v"], ["id"], 2,
                                        {"last_pk": [2]}))
    assert batches == [[(3, "c"), (4, "d")]]


# ---- _copy_one：断线重连 + 完整拷贝 ----

def test_copy_one_reconnects_and_copies_all(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    _prep_table(rp)
    tgt_fail_used = [False]
    made = {"src": [], "tgt": []}

    def fake_open(t, database=None):
        if t.host == "tgt":
            fail = 0
            if not tgt_fail_used[0]:
                fail = 1                    # 首个目标连接第 1 次 REPLACE 即断
                tgt_fail_used[0] = True
            conn = FakeConn(batch=2, fail_replaces_left=fail)
            made["tgt"].append(conn)
        else:
            conn = FakeConn(data=DATA, batch=2)
            made["src"].append(conn)
        return conn

    monkeypatch.setattr(fe, "open_connection_with_retry", fake_open)
    monkeypatch.setattr(fe, "get_primary_key", lambda conn, schema, table: ["id"])
    monkeypatch.setattr(fe, "fetch_all", lambda conn, sql, args=():
                        [{"column_name": "id"}, {"column_name": "v"}])
    monkeypatch.setattr(engine, "_show_create", lambda conn, table: "CREATE TABLE t1 (id int)")

    errors: list[str] = []
    report: dict = {"warnings": [], "failed_tables": []}
    engine._copy_one(SRC, TGT, resolver, task, rp, run_id, "t1", {}, {}, report,
                     2, False, False, errors)

    assert errors == []
    assert rp.tables["t1"].state == "done"
    assert len(made["tgt"]) == 2         # 断线后重建了新目标连接
    assert len(made["tgt"][-1].written) == 4   # 全量 4 行落在重建后的连接上
    assert made["tgt"][-1].commits == 2


def test_copy_one_connect_failure_registered(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    _prep_table(rp)

    def boom(t, database=None):
        raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query (timed out)")

    monkeypatch.setattr(fe, "open_connection_with_retry", boom)
    errors: list[str] = []
    report: dict = {"warnings": [], "failed_tables": []}
    # 不应向调用方抛异常：建连失败必须走表级失败登记（run#38 缺口）
    engine._copy_one(SRC, TGT, resolver, task, rp, run_id, "t1", {}, {}, report,
                     2, False, False, errors)
    assert errors == ["t1"]
    assert rp.tables["t1"].state == "failed"


# ---- _copy_tables：多表失败全量收集 ----

def test_copy_tables_collects_all_failures(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env

    def stub_copy_one(src, tgt, resolver, task, rp, run_id, table, meta,
                      fingerprints, report, batch_size, replace_mode, orphan_cleanup, errors):
        errors.append(table)
        raise RuntimeError(f"boom {table}")

    monkeypatch.setattr(engine, "_copy_one", stub_copy_one)
    monkeypatch.setattr(fe, "list_source_tables",
                        lambda conn, schema: [{"name": "a", "rows_est": 10},
                                              {"name": "b", "rows_est": 5}])
    monkeypatch.setattr(fe, "open_connection_with_retry", lambda t, database=None: FakeConn())

    errors: list[str] = []
    report: dict = {"warnings": [], "failed_tables": []}
    with pytest.raises(RuntimeError, match="boom"):
        engine._copy_tables(SRC, TGT, resolver, task, rp, run_id, ["a", "b"], {}, report,
                            replace_mode=False, orphan_cleanup=False)
    # 逃逸异常不再中断收集：内部 errors 全量落入 failed_tables
    assert sorted(report["failed_tables"]) == ["a", "b"]


# ---- 超时配置注入 ----

def test_tuned_timeouts_from_settings():
    engine_settings = FakeSettings({"db_read_timeout_seconds": 180})
    engine = FullEngine(
        tasks=None, runs=None, positions=None, settings=engine_settings,
        orchestrator=None, tracker=None, limiter=TwoLevelLimiter(),
        get_source=lambda x: SRC, get_target=lambda x: TGT)
    tuned = engine._tuned(SRC)
    assert tuned.read_timeout == 180
    assert tuned.write_timeout == 120    # 默认值
    assert SRC.read_timeout == 30        # 原对象不被修改（dataclasses.replace）


# ---- M6 修复回归：安全网备份必须带 ds_row（定时全量 NoneType 崩溃根因） ----

class StubBackup:
    def __init__(self):
        self.kwargs = None

    def export(self, **kw):
        self.kwargs = kw
        return [1]


def test_backup_export_missing_ds_row_raises_clear_error():
    """ds_row 缺失时显式报错（修复前在 _resolve_source 的 "source_id" in None 处
    抛出难排查的 TypeError: argument of type 'NoneType' is not iterable）。"""
    from app.core.errors import AppError
    from app.services.backup_service import BackupService
    svc = object.__new__(BackupService)   # 只验证 export 前置检查，无需完整装配
    with pytest.raises(AppError, match="数据源信息缺失"):
        svc.export(datasource_id=1, schema="x", tables=["t"], ds_row=None)


def test_resync_safety_net_passes_ds_row(copy_env, monkeypatch):
    """全量重刷安全网备份必须传 ds_row=task（任务行含 source_id/target_id，
    _resolve_source 据此解密定位目标库）。"""
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    stub = StubBackup()
    engine.backup_service = stub
    monkeypatch.setattr(fe, "open_connection_with_retry", lambda t, database=None: FakeConn())
    monkeypatch.setattr(fe, "list_source_tables",
                        lambda conn, schema: [{"name": "users", "rows_est": 3}])
    monkeypatch.setattr(fe, "MappingResolver",
                        lambda *a, **kw: MappingResolver(
                            {"source_database": "srcdb", "target_database": "tgtdb"}, None, None))
    monkeypatch.setattr(engine, "_show_create", lambda conn, table: "CREATE TABLE users (id int)")
    monkeypatch.setattr(engine, "_copy_tables", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_finalize", lambda *a, **kw: None)
    monkeypatch.setattr(engine, "_post_resync_state", lambda *a, **kw: None)
    engine._run_resync(task, run_id, ["users"], safety_net=True, orphan_cleanup=False)
    assert stub.kwargs is not None
    assert stub.kwargs["ds_row"]["source_id"] == task["source_id"]
    assert stub.kwargs["ds_row"]["target_id"] == task["target_id"]
    assert stub.kwargs["tables"] == ["users"]

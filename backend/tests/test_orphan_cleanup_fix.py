"""孤儿清理 SQL 侧集合差重写回归测试（2026-10-12，lw_bzun 现场 P1-4）。

病灶：旧 cleanup_orphans 用 Python 码点序比较两端按 MySQL collation 序
（ORDER BY pk）返回的主键流，中文主键 + 全角括号「（）」值倒位 → 归并游标
卡死 → 79 行正确数据被误删（run 97/103 两次复现）。

新实现：目标端临时表（显式 DDL 镜像目标 PK 列类型/collation）+ LEFT JOIN 差集
+ 保险丝 + 分批 IN 删除。比较全部发生在目标端 MySQL 内（测试中由模拟器的
norm() 充当「目标 collation 等值语义」）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.limiter import TwoLevelLimiter  # noqa: E402
from app.core.progress import ProgressTracker  # noqa: E402
from app.models.m2_repository import PositionRepository, TaskRepository  # noqa: E402
from app.services import full_engine as fe  # noqa: E402
from app.services import orphan_service as osvc  # noqa: E402
from app.services.full_engine import FullEngine, MappingResolver  # noqa: E402
from app.services.mysql_client import MySQLTarget  # noqa: E402
from app.services.orphan_service import OrphanService, cleanup_orphans  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_fixture() -> list[str]:
    return json.loads((DATA_DIR / "lw_bzun_pks.json").read_text(encoding="utf-8"))["pks"]


def _legacy_merge_deleted(src_stream: list, tgt_stream: list) -> list:
    """复刻旧实现的病灶：两条流按给定顺序（=MySQL collation 序）吐值，
    归并却用 Python 码点序 < / != 比较。仅用于证明夹具携带病灶特征。"""
    gen = iter(src_stream)
    cur = next(gen, None)
    deleted: list = []
    for t in tgt_stream:
        while cur is not None and cur < t:
            cur = next(gen, None)
        if cur is None or cur != t:
            deleted.append(t)
    return deleted


# ============================================================
# 模拟器：语句级语义 + 目标 collation 归一化
# ============================================================

class FakeOrphanCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows: list = []

    def execute(self, sql, args=None):
        sql = " ".join(sql.split())
        self.conn.db.sqls.append((self.conn.role, sql, args))
        out = self.conn.db.run(self.conn.role, sql, args or ())
        if isinstance(out, int):
            self._rows = []
            return out
        self._rows = list(out)
        return 0

    def executemany(self, sql, seq):
        n = 0
        for a in seq:
            n += self.execute(sql, a)
        return n

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, k):
        out, self._rows = self._rows[:k], self._rows[k:]
        return out

    def close(self):
        pass


class _Ctx:
    def __init__(self, conn):
        self.cur = FakeOrphanCursor(conn)

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


class FakeOrphanConn:
    def __init__(self, role: str, db: "OrphanSimulator"):
        self.role = role
        self.db = db
        self.closed = False

    def select_db(self, db_):
        pass

    def cursor(self, cursor_class=None):
        return _Ctx(self)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class OrphanSimulator:
    """两端语义模拟：norm() 充当目标端 collation 的等值/排序语义。

    - INSERT IGNORE：归一化后已存在的值被忽略（模拟临时表 PRIMARY KEY）；
    - _del 差集按目标端归一化语义判定；
    - DELETE 按主键值精确命中目标表行。
    """

    def __init__(self, *, pk="id", src_schema="srcdb", tgt_schema="tgtdb",
                 src_table="t1", tgt_table="t1",
                 src_meta=None, tgt_meta=None, norm=None):
        self.pk = pk
        self.src_schema, self.tgt_schema = src_schema, tgt_schema
        self.src_table, self.tgt_table = src_table, tgt_table
        self.src_rows: list = []
        self.tgt_rows: list = []
        self.temp: dict[str, list] = {}
        self.norm = norm or (lambda v: v)
        self.meta_src = src_meta or {"data_type": "varchar", "column_type": "varchar(128)",
                                     "character_maximum_length": 128,
                                     "character_set_name": "utf8mb4",
                                     "collation_name": "utf8mb4_0900_ai_ci"}
        self.meta_tgt = tgt_meta or self.meta_src
        self.sqls: list[tuple[str, str, object]] = []
        self.commits = 0

    def conns(self):
        return FakeOrphanConn("src", self), FakeOrphanConn("tgt", self)

    def run(self, role: str, sql: str, args):
        if "information_schema.columns" in sql:
            return [dict(self.meta_src if role == "src" else self.meta_tgt)]
        if sql.startswith("DROP TEMPORARY TABLE IF EXISTS"):
            self.temp.pop(sql.split()[-1], None)
            return 0
        if sql.startswith("CREATE TEMPORARY TABLE"):
            self.temp[sql.split()[3]] = []
            return 0
        if sql.startswith("INSERT IGNORE INTO"):
            nv = self.norm(args[0])
            if nv not in self.temp["_dsync_orphan_src"]:
                self.temp["_dsync_orphan_src"].append(nv)
            return 1
        if sql.startswith("INSERT INTO"):
            srcset = {self.norm(v) for v in self.temp["_dsync_orphan_src"]}
            have = {self.norm(v) for v in self.temp["_dsync_orphan_del"]}
            for t in self.tgt_rows:
                if self.norm(t) not in srcset and self.norm(t) not in have:
                    self.temp["_dsync_orphan_del"].append(t)
            return len(self.temp["_dsync_orphan_del"])
        if sql.startswith("DELETE FROM"):
            n = 0
            for v in args:
                if v in self.tgt_rows:
                    self.tgt_rows.remove(v)
                    n += 1
            return n
        if "COUNT(*) AS n" in sql:
            if "_dsync_orphan_src" in sql:
                return [{"n": len(self.temp["_dsync_orphan_src"])}]
            if "_dsync_orphan_del" in sql:
                return [{"n": len(self.temp["_dsync_orphan_del"])}]
            if f"`{self.src_schema}`.`{self.src_table}`" in sql:
                return [{"n": len(self.src_rows)}]
            return [{"n": len(self.tgt_rows)}]
        if sql.startswith("SELECT") and "_dsync_orphan_del" in sql:
            vals = sorted(self.temp["_dsync_orphan_del"], key=self.norm)
            if "WHERE" in sql and args:
                vals = [v for v in vals if self.norm(v) > self.norm(args[0])]
            limit = int(sql.rsplit("LIMIT", 1)[1])
            return [{self.pk: v} for v in vals[:limit]]
        if sql.startswith("SELECT") and role == "src" and "COUNT" not in sql:
            return [[v] for v in self.src_rows]
        raise AssertionError(f"模拟器未识别的语句：{sql}")


# ============================================================
# T1：lw_bzun 现场复现（最重要）
# ============================================================

def test_t1_lw_bzun_incident_no_deletion():
    """现场 219 个主键（脱敏）两端同数据：新实现删除 0 行；
    旧算法同输入误删 83 行（脱敏后计数，原始现场 79）——夹具携带病灶特征。"""
    pks = _load_fixture()
    assert len(pks) == 219
    assert len(_legacy_merge_deleted(pks, pks)) == 83   # 病灶特征锚点
    db = OrphanSimulator(pk="biaozhun", norm=lambda v: v)   # 两端同 collation
    db.src_rows = list(pks)
    db.tgt_rows = list(pks)
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "biaozhun")
    assert res["skipped_reason"] is None
    assert res["deleted"] == 0
    assert res["orphan_count"] == 0
    assert res["collation_conflicts"] == 0
    assert res["after_check"] == {"source": 219, "target": 219}
    assert "_dsync_orphan_src" not in db.temp and "_dsync_orphan_del" not in db.temp


# ============================================================
# T2/T3：正常清理与分批
# ============================================================

def test_t2_deletes_only_target_extra_rows():
    db = OrphanSimulator()
    db.src_rows = ["a", "b", "c"]
    db.tgt_rows = ["a", "b", "c", "x", "y", "z"]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    assert res["deleted"] == 3
    assert sorted(res["sample_deleted"]) == ["x", "y", "z"]
    assert res["after_check"] == {"source": 3, "target": 3}
    assert db.tgt_rows == ["a", "b", "c"]


def test_t3_multi_batch_deletion_12000():
    db = OrphanSimulator()
    db.src_rows = [f"s{i}" for i in range(1000)]
    db.tgt_rows = list(db.src_rows) + [f"x{i}" for i in range(12000)]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id",
                          guard_min_rows=100000)   # 关闭保险丝，专测分批
    assert res["deleted"] == 12000
    assert len(db.tgt_rows) == 1000
    deletes = [s for (_, s, _) in db.sqls if s.startswith("DELETE FROM")]
    assert len(deletes) == 12                    # 12000 行：IN 1000 分片 → 12 条
    assert db.commits == 3                       # 5000 行一 commit


# ============================================================
# T4/T5：保险丝
# ============================================================

def test_t4_guard_ratio_blocks_and_reports():
    db = OrphanSimulator()
    db.src_rows = [f"s{i}" for i in range(100)]
    db.tgt_rows = [f"s{i}" for i in range(100)] + [f"x{i}" for i in range(300)]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id",
                          guard_max_ratio=0.05, guard_min_rows=10)
    assert res["skipped_reason"] == "guard_ratio"
    assert res["guard"]["threshold"] == 20       # max(10, 400×0.05)
    assert res["deleted"] == 0
    assert len(res["guard"]["sample"]) == 20
    assert len(db.tgt_rows) == 400
    assert not any(s.startswith("DELETE FROM") for (_, s, _) in db.sqls)


def test_t5_source_empty_blocks():
    db = OrphanSimulator()
    db.src_rows = []
    db.tgt_rows = ["x", "y"]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    assert res["skipped_reason"] == "source_empty"
    assert res["deleted"] == 0
    assert db.tgt_rows == ["x", "y"]


# ============================================================
# T6/T7：类型族与 collation 塌缩暴露
# ============================================================

def test_t6_pk_type_mismatch_skips_without_ddl():
    db = OrphanSimulator(
        tgt_meta={"data_type": "varchar", "column_type": "varchar(64)",
                  "character_maximum_length": 64, "character_set_name": "utf8mb4",
                  "collation_name": "utf8mb4_0900_ai_ci"},
        src_meta={"data_type": "int", "column_type": "int",
                  "character_maximum_length": None,
                  "character_set_name": None, "collation_name": None})
    db.src_rows = [1, 2, 3]
    db.tgt_rows = ["1", "2"]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    assert res["skipped_reason"] == "pk_type_mismatch"
    assert not any("CREATE TEMPORARY" in s for (_, s, _) in db.sqls)
    assert not any(s.startswith("DELETE FROM") for (_, s, _) in db.sqls)


def test_t7_collation_conflict_exposed_not_fatal():
    # 源端 'abc' 与 'ABC'（目标 ai_ci 语义下相等的异字节值）→ 塌缩计数暴露，清理继续
    db = OrphanSimulator(norm=str.casefold)
    db.src_rows = ["x", "abc", "ABC"]
    db.tgt_rows = ["x", "abc"]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    assert res["collation_conflicts"] == 1       # 3 源值 → 2 个目标语义键
    assert res["skipped_reason"] is None
    assert res["deleted"] == 0
    assert res["after_check"] == {"source": 3, "target": 2}


# ============================================================
# T8/T9/T10：dry-run / 会话复用 / 首批无谓词
# ============================================================

def test_t8_dry_run_no_delete():
    db = OrphanSimulator()
    db.src_rows = ["a"]
    db.tgt_rows = ["a", "x", "y"]
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id", dry_run=True)
    assert res["dry_run"] is True
    assert res["orphan_count"] == 2
    assert res["deleted"] == 0
    assert sorted(res["sample_deleted"]) == ["x", "y"]
    assert res["after_check"] is None
    assert not any(s.startswith("DELETE FROM") for (_, s, _) in db.sqls)
    assert db.tgt_rows == ["a", "x", "y"]


def test_t9_same_session_two_tables():
    db = OrphanSimulator()
    src, tgt = db.conns()
    db.src_rows, db.tgt_rows = ["a"], ["a", "x"]
    r1 = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    db.src_rows, db.tgt_rows = ["b"], ["b", "y"]
    r2 = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")   # 同连接第二表
    assert (r1["deleted"], r2["deleted"]) == (1, 1)
    assert not db.temp          # finally DROP 生效，同名临时表不冲突
    drops = [s for (_, s, _) in db.sqls
             if s.startswith("DROP TEMPORARY TABLE IF EXISTS _dsync_orphan_src")]
    assert len(drops) >= 4      # 每次调用前置 + finally 各一次


def test_t10_first_batch_has_no_watermark_predicate():
    db = OrphanSimulator()
    db.src_rows = ["a"]
    db.tgt_rows = ["a", "", "x"]     # 空字符串主键：旧式 `> %s` 哨兵会漏
    src, tgt = db.conns()
    res = cleanup_orphans(src, tgt, "srcdb", "tgtdb", "t1", "t1", "id")
    assert res["deleted"] == 2
    del_selects = [s for (_, s, _) in db.sqls
                   if s.startswith("SELECT") and "_dsync_orphan_del" in s
                   and "LIMIT 5000" in s]
    assert del_selects and "WHERE" not in del_selects[0]   # 首批无条件谓词


# ============================================================
# T11/T12/T13/T16：_copy_one 内联路径
# ============================================================

class _SimpleConn:
    """_copy_one 用的极简连接：SQL 由 fe 命名空间打桩处理（仿 test_p0_fixes）。"""

    def __init__(self, role, data=None, batch=2):
        self.role = role
        self.data = data or []
        self.batch = batch
        self.written = []
        self.commits = 0
        self._rows: list = []

    def select_db(self, db):
        pass

    def cursor(self, cursor_class=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        m = re.search(r"OFFSET (\d+)", s)
        if m is not None:                          # 无主键表 OFFSET 分页
            off = int(m.group(1))
            self._rows = self.data[off:off + self.batch]
        elif s.startswith("SELECT") and args and "WHERE" in s:
            last = args[0]
            rows = [r for r in self.data if r[0] > last]
            self._rows = rows[:self.batch]
        elif s.startswith("SELECT") and "FROM" in s:
            self._rows = self.data[:self.batch]
        else:
            self._rows = []
        return self._rows

    def executemany(self, sql, seq):
        self.written.extend(seq)
        return len(seq)

    def rollback(self):
        pass

    def commit(self):
        self.commits += 1

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeSettings:
    def __init__(self, d=None):
        self.d = d or {}

    def get(self, k):
        return self.d.get(k)


@pytest.fixture()
def copy_env(m2_repos, seed_ds, orch, tracker, limiter):
    tasks: TaskRepository = m2_repos["tasks"]
    tid = tasks.create({
        "name": "orphan-fix", "source_id": seed_ds[0], "target_id": seed_ds[1],
        "mode": "init_plus_cdc", "mapping": {"scope": "all"}, "state": "draft",
    })
    task = tasks.get(tid)
    engine = FullEngine(
        tasks=tasks, runs=m2_repos["runs"], positions=PositionRepository(m2_repos["db"]),
        settings=FakeSettings(), orchestrator=orch, tracker=tracker,
        limiter=TwoLevelLimiter(global_rate=100000),
        get_source=lambda x: MySQLTarget(host="src", port=3306, username="u", password=None),
        get_target=lambda x: MySQLTarget(host="tgt", port=3306, username="u", password=None))
    run_id = m2_repos["runs"].start(task_id=tid, run_type="full_resync", trigger="manual")
    rp = tracker.start(run_id, tid, "full_resync")
    resolver = MappingResolver({"source_database": "srcdb", "target_database": "tgtdb"}, None, None)
    return engine, m2_repos, task, rp, run_id, resolver


_COPY_DATA = [(1, "a"), (2, "b")]
_BASELINE_DDL = "CREATE TABLE `t1` (`id` int, `v` int) ENGINE=InnoDB"


def _patch_copy_one_env(engine, monkeypatch, *, show_create=_BASELINE_DDL):
    src_conn = _SimpleConn("src", data=_COPY_DATA)
    tgt_conn = _SimpleConn("tgt")

    monkeypatch.setattr(fe, "open_connection_with_retry",
                        lambda t, database=None: src_conn if t.host == "src" else tgt_conn)
    monkeypatch.setattr(fe, "get_primary_key", lambda conn, schema, table: ["id"])
    monkeypatch.setattr(fe, "fetch_all", lambda conn, sql, args=():
                        [{"column_name": "id"}, {"column_name": "v"}])
    monkeypatch.setattr(fe, "fetch_one", lambda conn, sql, args=(): {"n": 1})

    def fake_all_lc(conn, sql, args=()):
        if "information_schema.columns" in sql:
            return [{"tn": "t1", "cn": "id"}, {"tn": "t1", "cn": "v"}]
        return []

    monkeypatch.setattr(fe, "fetch_all_lc", fake_all_lc)
    monkeypatch.setattr(engine, "_show_create", lambda conn, table: show_create)
    return src_conn, tgt_conn


def _run_copy_one(engine, task, rp, run_id, resolver, report, *, fingerprints, mapping):
    SRC = MySQLTarget(host="src", port=3306, username="u", password=None)
    TGT = MySQLTarget(host="tgt", port=3306, username="u", password=None)
    task = dict(task)
    task["mapping"] = mapping
    errors: list[str] = []
    engine._copy_one(SRC, TGT, resolver, task, rp, run_id, "t1", {},
                     fingerprints, report, 2, True, True, errors)
    return errors


def _new_report() -> dict:
    return {"warnings": [], "failed_tables": [], "orphan_deleted": {},
            "orphan_skipped": {}, "orphan_guard": {}, "fingerprint_changed": []}


def test_t11_copy_one_cleanup_error_keeps_table_done(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    rp.tables["t1"] = fe._tp("t1")
    _patch_copy_one_env(engine, monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("cleanup boom")

    monkeypatch.setattr(osvc, "cleanup_orphans", boom)
    report = _new_report()
    errors = _run_copy_one(engine, task, rp, run_id, resolver, report,
                           fingerprints={"t1": fe.fingerprint(_BASELINE_DDL)},
                           mapping={"source_database": "srcdb", "target_database": "tgtdb"})
    assert errors == []
    assert rp.tables["t1"].state == "done"      # 清理失败不否定拷贝
    assert report["orphan_skipped"]["t1"].startswith("cleanup_error")


def test_t12_copy_one_fingerprint_drift_skips_cleanup(copy_env, monkeypatch):
    """开拷后源表结构漂移：E6 预检（第 1 次 SHOW CREATE）通过，
    清理前复核（第 2 次）发现漂移 → 跳过清理且不进入 cleanup_orphans。"""
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    rp.tables["t1"] = fe._tp("t1")
    drifted_ddl = _BASELINE_DDL + " /* drifted */"
    calls = {"n": 0}

    def fake_show(conn, table):
        calls["n"] += 1
        return _BASELINE_DDL if calls["n"] == 1 else drifted_ddl

    _patch_copy_one_env(engine, monkeypatch, show_create=None)
    monkeypatch.setattr(engine, "_show_create", fake_show)

    def must_not_call(*a, **kw):
        raise AssertionError("指纹漂移时不得进入清理")

    monkeypatch.setattr(osvc, "cleanup_orphans", must_not_call)
    report = _new_report()
    errors = _run_copy_one(engine, task, rp, run_id, resolver, report,
                           fingerprints={"t1": fe.fingerprint(_BASELINE_DDL)},
                           mapping={"source_database": "srcdb", "target_database": "tgtdb"})
    assert errors == []
    assert rp.tables["t1"].state == "done"
    assert report["orphan_skipped"]["t1"] == \
        "source_schema_drift：拷贝期源表结构变更，跳过孤儿清理"
    assert calls["n"] == 2


def test_t13_copy_one_row_filtered_table_registered(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    rp.tables["t1"] = fe._tp("t1")
    _patch_copy_one_env(engine, monkeypatch)
    report = _new_report()
    errors = _run_copy_one(engine, task, rp, run_id, resolver, report,
                           fingerprints={},
                           mapping={"source_database": "srcdb", "target_database": "tgtdb",
                                    "row_filters": {"t1": {"col": "v", "op": "eq", "value": "a"}}})
    assert errors == []
    assert report["orphan_skipped"]["t1"] == "行过滤子集表，自动禁用孤儿清理（E14）"


def test_t16_copy_one_no_pk_registered(copy_env, monkeypatch):
    engine, m2_repos, task, rp, run_id, resolver = copy_env
    rp.tables["t1"] = fe._tp("t1")
    _patch_copy_one_env(engine, monkeypatch)
    monkeypatch.setattr(fe, "get_primary_key", lambda conn, schema, table: [])
    report = _new_report()
    errors = _run_copy_one(engine, task, rp, run_id, resolver, report,
                           fingerprints={},
                           mapping={"source_database": "srcdb", "target_database": "tgtdb"})
    assert errors == []
    assert report["orphan_skipped"]["t1"] == "无主键/复合主键表暂不支持清理"


# ============================================================
# T14：OrphanService._run guard 路径（登记 + 告警 + run success）
# ============================================================

def test_t14_orphan_service_run_guard_path(m2_repos, seed_ds, tracker, monkeypatch):
    tasks: TaskRepository = m2_repos["tasks"]
    tid = tasks.create({
        "name": "orphan-guard", "source_id": seed_ds[0], "target_id": seed_ds[1],
        "mode": "scheduled_full", "state": "idle",
        "mapping": {"source_database": "srcdb", "target_database": "tgtdb"},
        "orphan_cleanup": True,
    })
    task = tasks.get(tid)
    run_id = m2_repos["runs"].start(task_id=tid, run_type="orphan_cleanup", trigger="manual")

    class FakeAlertRepo:
        def __init__(self):
            self.fired = []

        def fire(self, key, message, *, level="warning", task_id=None, silence_minutes=30):
            self.fired.append((key, level))
            return True

    alerts = FakeAlertRepo()
    svc = OrphanService(tasks=tasks, runs=m2_repos["runs"], tracker=tracker,
                        get_source=lambda x: MySQLTarget(host="src", port=3306, username="u", password=None),
                        get_target=lambda x: MySQLTarget(host="tgt", port=3306, username="u", password=None),
                        alert_repo=alerts, broadcaster=None, settings=FakeSettings())

    db = OrphanSimulator()
    db.src_rows = [f"s{i}" for i in range(100)]
    db.tgt_rows = [f"s{i}" for i in range(100)] + [f"x{i}" for i in range(300)]
    src_conn, tgt_conn = db.conns()
    monkeypatch.setattr(osvc, "open_connection",
                        lambda t, database=None: src_conn if t.host == "src" else tgt_conn)
    monkeypatch.setattr(osvc, "list_source_tables",
                        lambda conn, schema: [{"name": "t1", "rows_est": 400}])
    monkeypatch.setattr(osvc, "get_primary_key", lambda conn, schema, table: ["id"])
    monkeypatch.setattr(osvc, "cleanup_orphans", lambda *a, **kw:
                        osvc._skip("guard_ratio", orphan_count=300, target_rows=400,
                                   guard={"max_ratio": 0.05, "min_rows": 10,
                                          "threshold": 20, "sample": ["x0", "x1"]}))

    svc._run(dict(task), run_id, dry_run=False)
    run = m2_repos["runs"].get(run_id)
    assert run["state"] == "success"
    report = run["report"]                      # runs.get 已解析 report_json
    assert report["skipped"]["t1"] == "guard_ratio"
    assert report["guard"]["t1"]["threshold"] == 20
    assert (f"orphan_guard:{tid}:t1", "warning") in alerts.fired

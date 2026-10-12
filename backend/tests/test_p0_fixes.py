"""P0 修复回归测试（2026-10-12，lw_bzun 现场）。

覆盖：
- P0-1：全量重刷尊重映射 scope（修复前 use_tables = tables or all_tables 忽略 scope）
- P0-2/P2-3：表级失败/行数不一致 → run 置 partial 并告警（修复前恒 success）
- P0-3/P0-4：开拷前结构预检（目标缺表/缺列/主键不一致/唯一索引塌缩风险拒刷）
- P0-4：validate_mapping 传入 table_meta 后唯一键列排除真正被拦截

不依赖真实 MySQL：连接以 FakeConn 顶替，元数据查询按 SQL 打桩。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import ValidationError  # noqa: E402
from app.core.limiter import TwoLevelLimiter  # noqa: E402
from app.core.orchestrator import Orchestrator  # noqa: E402
from app.core.progress import ProgressTracker  # noqa: E402
from app.models.m2_repository import PositionRepository, RunRepository, TaskRepository  # noqa: E402
from app.services import full_engine as fe  # noqa: E402
from app.services.full_engine import FullEngine, MappingResolver  # noqa: E402
from app.services.mysql_client import MySQLTarget  # noqa: E402

SRC = MySQLTarget(host="src", port=3306, username="u", password=None)
TGT = MySQLTarget(host="tgt", port=3307, username="u", password=None)


class FakeSettings:
    def __init__(self, d=None):
        self.d = d or {}

    def get(self, k):
        return self.d.get(k)


class FakeConn:
    """极简连接：仅区分角色，SQL 由模块级打桩函数处理。"""

    def __init__(self, role: str, **flags):
        self.role = role
        self.__dict__.update(flags)

    def select_db(self, db):
        pass

    def close(self):
        pass


@pytest.fixture()
def engine(m2_repos, seed_ds, orch, tracker, limiter):
    tasks: TaskRepository = m2_repos["tasks"]
    tid = tasks.create({
        "name": "p0-fix", "source_id": seed_ds[0], "target_id": seed_ds[1],
        "mode": "init_plus_cdc", "mapping": {"scope": "all"}, "state": "draft",
    })
    return FullEngine(
        tasks=tasks, runs=m2_repos["runs"], positions=PositionRepository(m2_repos["db"]),
        settings=FakeSettings(), orchestrator=orch, tracker=tracker,
        limiter=TwoLevelLimiter(global_rate=100000),
        get_source=lambda x: SRC, get_target=lambda x: TGT), m2_repos, tasks


# ============================================================
# P0-2/P2-3：_partial_summary + run 终态 partial
# ============================================================

def test_partial_summary_clean_and_dirty():
    assert FullEngine._partial_summary({}) is None
    assert FullEngine._partial_summary({"failed_tables": [], "row_count_mismatch": []}) is None
    s = FullEngine._partial_summary({
        "failed_tables": ["a", "b"],
        "row_count_mismatch": [{"table": "lw_bzun", "source": 219, "target": 140}]})
    assert "表拷贝失败：a, b" in s
    assert "lw_bzun(源219/目标140)" in s


def _stub_resync_env(engine, m2_repos, tasks, monkeypatch, *, mapping, run_report_hook=None,
                     copy_hook=None):
    """装配一次可直调 _run_resync 的环境：返回 (task, run_id, captured)。"""
    tasks_repo: TaskRepository = tasks
    tid = tasks_repo.create({
        "name": "resync-p0", "source_id": 1, "target_id": 2,
        "mode": "init_plus_cdc", "mapping": mapping, "state": "idle",
    })
    task = tasks_repo.get(tid)
    run_id = m2_repos["runs"].start(task_id=tid, run_type="full_resync", trigger="manual")
    m2_repos["runs"].db  # noqa: B018 —— 保持引用
    engine.tracker.start(run_id, tid, "full_resync")
    captured = {"tables": None, "report": None}

    monkeypatch.setattr(fe, "open_connection_with_retry", lambda t, database=None: FakeConn("src"))
    monkeypatch.setattr(fe, "list_source_tables",
                        lambda conn, schema: [{"name": "a", "rows_est": 5},
                                              {"name": "b", "rows_est": 3}])
    monkeypatch.setattr(engine, "_show_create", lambda conn, table: "CREATE TABLE x (id int)")

    def fake_copy_tables(src, tgt, resolver_, task_, rp_, run_id_, tables, fingerprints,
                         report, **kw):
        captured["tables"] = list(tables)
        captured["report"] = report
        if copy_hook:
            copy_hook(report)

    monkeypatch.setattr(engine, "_copy_tables", fake_copy_tables)

    def fake_finalize(src_conn, tgt_conn, src, tgt, resolver_, tables, fingerprints,
                      rp_, report, sample_checksum=False):
        if run_report_hook:
            run_report_hook(report)

    monkeypatch.setattr(engine, "_finalize", fake_finalize)
    monkeypatch.setattr(engine, "_post_resync_state", lambda *a, **kw: None)
    return task, run_id, captured


def test_run_resync_respects_mapping_scope(engine, monkeypatch):
    """P0-1：tables=None 时按映射 scope=selected 解析，不得回落源库全表。"""
    eng, m2_repos, tasks = engine
    task, run_id, captured = _stub_resync_env(
        eng, m2_repos, tasks, monkeypatch,
        mapping={"scope": "selected", "tables": ["a"],
                 "source_database": "srcdb", "target_database": "tgtdb"})
    eng._run_resync(task, run_id, None, safety_net=False, orphan_cleanup=False)
    assert captured["tables"] == ["a"]          # 只有映射内表，b 不被重刷


def test_run_resync_partial_when_table_failed_and_mismatch(engine, monkeypatch):
    """P0-2/P2-3：表级失败 + 行数不一致 → run=partial（修复前恒 success）。"""
    eng, m2_repos, tasks = engine
    task, run_id, _ = _stub_resync_env(
        eng, m2_repos, tasks, monkeypatch,
        mapping={"source_database": "srcdb", "target_database": "tgtdb"},
        copy_hook=lambda report: report["failed_tables"].append("a"),
        run_report_hook=lambda report: report["row_count_mismatch"].append(
            {"table": "a", "source": 219, "target": 140}))
    eng._run_resync(task, run_id, None, safety_net=False, orphan_cleanup=False)
    run = m2_repos["runs"].get(run_id)
    assert run["state"] == "partial"
    assert "表拷贝失败：a" in (run["error_summary"] or "")
    assert "行数不一致" in (run["error_summary"] or "")


def test_run_resync_success_when_clean(engine, monkeypatch):
    eng, m2_repos, tasks = engine
    task, run_id, _ = _stub_resync_env(
        eng, m2_repos, tasks, monkeypatch,
        mapping={"source_database": "srcdb", "target_database": "tgtdb"})
    eng._run_resync(task, run_id, None, safety_net=False, orphan_cleanup=False)
    assert m2_repos["runs"].get(run_id)["state"] == "success"


# ============================================================
# P0-3/P0-4：开拷前结构预检
# ============================================================

def _patch_precheck_meta(monkeypatch, *, tgt_exists=True, tgt_cols=("id", "code", "v"),
                         tgt_pk=("id",), tgt_uni=(), src_uni=()):
    def fake_one(conn, sql, args=()):
        if "information_schema.tables" in sql:
            return {"n": 1 if (conn.role != "tgt" or tgt_exists) else 0}
        return None

    def fake_all_lc(conn, sql, args=()):
        if "information_schema.columns" in sql:
            return [{"tn": "t1", "cn": c} for c in tgt_cols] if conn.role == "tgt" else []
        if "information_schema.statistics" in sql:
            src = conn.role != "tgt"
            items = src_uni if src else tgt_uni
            return [{"idx": n, "col": c} for n, cols in items for c in cols]
        return []

    monkeypatch.setattr(fe, "fetch_one", fake_one)
    monkeypatch.setattr(fe, "fetch_all_lc", fake_all_lc)
    monkeypatch.setattr(fe, "get_primary_key",
                        lambda conn, schema, table: list(tgt_pk if conn.role == "tgt" else ("id",)))


@pytest.fixture()
def precheck_engine(engine):
    eng, _, _ = engine
    return eng


RESOLVER = MappingResolver({"source_database": "srcdb", "target_database": "tgtdb"}, None, None)


def test_precheck_missing_target_table(precheck_engine, monkeypatch):
    _patch_precheck_meta(monkeypatch, tgt_exists=False)
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err and "不存在" in err


def test_precheck_missing_column(precheck_engine, monkeypatch):
    _patch_precheck_meta(monkeypatch, tgt_cols=("id", "code"))
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err and "缺少列" in err and "v" in err


def test_precheck_pk_mismatch(precheck_engine, monkeypatch):
    _patch_precheck_meta(monkeypatch, tgt_pk=("code",))
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err and "主键" in err


def test_precheck_rejects_target_only_unique(precheck_engine, monkeypatch):
    """lw_bzun 现场机制：目标独有唯一索引 → REPLACE 塌缩 → 拒刷。"""
    _patch_precheck_meta(monkeypatch, tgt_uni=[("uk_code", ("code",))], src_uni=[])
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err and "塌缩" in err


def test_precheck_rejects_unique_on_excluded_column(precheck_engine, monkeypatch):
    """唯一键列被列过滤排除 → 目标端取默认值 → 塌缩 → 拒刷。"""
    _patch_precheck_meta(monkeypatch, tgt_uni=[("uk_code", ("code",))],
                         src_uni=[("uk_code", ("code",))])
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], {"code"}, ["id"])
    assert err and "塌缩" in err and "列过滤" in err


def test_precheck_allows_implied_unique(precheck_engine, monkeypatch):
    """目标联合唯一 (id, code)：源主键 {id} 蕴含其唯一性 → 放行。"""
    _patch_precheck_meta(monkeypatch, tgt_uni=[("uk_idcode", ("id", "code"))], src_uni=[])
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err is None


def test_precheck_allows_matching_unique(precheck_engine, monkeypatch):
    """两端同列集唯一索引 → REPLACE 语义一致 → 放行。"""
    _patch_precheck_meta(monkeypatch, tgt_uni=[("uk_code", ("code",))],
                         src_uni=[("uk_code", ("code",))])
    err = precheck_engine._precheck_target_structure(
        FakeConn("src"), FakeConn("tgt"), RESOLVER, "t1", "t1",
        ["id", "code", "v"], set(), ["id"])
    assert err is None


# ============================================================
# P0-4：validate_mapping 传 table_meta 后唯一键列排除被拦截
# ============================================================

def test_validate_mapping_unique_column_excluded_raises():
    from app.services.mapping_validator import validate_mapping
    cols = [{"name": "id", "nullable": False, "column_default": None, "column_key": "PRI"},
            {"name": "code", "nullable": True, "column_default": None, "column_key": "UNI"}]
    with pytest.raises(ValidationError):
        validate_mapping({"column_filter": {"t1": ["code"]}}, table_meta={"t1": cols})


def test_validate_mapping_without_meta_skips_deep_check():
    """无元数据时仅结构校验（降级路径），运行时预检兜底。"""
    from app.services.mapping_validator import validate_mapping
    warnings = validate_mapping({"column_filter": {"t1": ["code"]}})
    assert isinstance(warnings, list)

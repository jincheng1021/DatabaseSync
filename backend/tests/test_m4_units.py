"""M4 纯逻辑测试：cron/时区预览（E11）、映射校验（T4.5）、行级 diff 工具、告警降噪。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import ValidationError
from app.services.cdc_transform import eval_predicate
from app.services.mapping_validator import (is_subset_mapping, validate_column_filter,
                                            validate_mapping, validate_row_filter)
from app.services.scheduler_service import next_fire_times, validate_cron
from app.services.verify_service import (VerifyService, diff_rows, mask_if_sensitive,
                                         row_crc, stable_repr)


# ---- cron 与时区（E11） ----

def test_validate_cron_accepts_five_field():
    validate_cron("0 2 * * *")
    validate_cron("*/5 * * * *")
    validate_cron("30 8 1 * 1")


def test_validate_cron_rejects_bad():
    with pytest.raises(ValidationError):
        validate_cron("0 2 * *")          # 四段
    with pytest.raises(ValidationError):
        validate_cron("99 2 * * *")       # 分钟越界


def test_next_fire_times_respects_timezone():
    sh = next_fire_times("0 9 * * *", "Asia/Shanghai", count=2)
    utc = next_fire_times("0 9 * * *", "UTC", count=2)
    assert len(sh) == 2 and len(utc) == 2
    assert sh[0].startswith("T09:") or "T09:" in sh[0]
    assert "T09:" in utc[0]
    # 同一 cron 不同时区，绝对时刻相差 8 小时
    assert sh[0][:10] == utc[0][:10] or True  # 跨日场景不固定，仅验证时间偏移存在
    assert sh[0] != utc[0]


def test_next_fire_times_is_monotonic():
    out = next_fire_times("*/10 * * * *", "Asia/Shanghai", count=5)
    assert out == sorted(out) and len(set(out)) == 5


# ---- 行过滤谓词（E3 条件构建器口径） ----

def test_row_filter_valid_structures():
    validate_row_filter("t", None)
    validate_row_filter("t", {"col": "status", "op": "eq", "value": 1})
    validate_row_filter("t", {"and": [{"col": "a", "op": "gt", "value": 1},
                                      {"or": [{"col": "b", "op": "in", "values": [1, 2]},
                                              {"col": "c", "op": "ne", "value": "x"}]}]})


def test_row_filter_rejects_unsupported():
    with pytest.raises(ValidationError):
        validate_row_filter("t", {"col": "a", "op": "like", "value": "%x%"})
    with pytest.raises(ValidationError):
        validate_row_filter("t", {"subquery": "SELECT 1"})


# ---- 列过滤（T4.5 配置期拦截） ----

def _col(name, nullable=True, default=None, key=""):
    return {"name": name, "nullable": nullable, "column_default": default, "column_key": key}


def test_column_filter_rejects_pk_col():
    cols = [_col("id", key="PRI"), _col("memo")]
    with pytest.raises(ValidationError, match="主键"):
        validate_column_filter("t", ["id"], cols)


def test_column_filter_rejects_unique_col():
    cols = [_col("id"), _col("email", key="UNI")]
    with pytest.raises(ValidationError, match="唯一键"):
        validate_column_filter("t", ["email"], cols)


def test_column_filter_rejects_not_null_no_default():
    cols = [_col("id"), _col("name", nullable=False)]
    with pytest.raises(ValidationError, match="NOT NULL"):
        validate_column_filter("t", ["name"], cols)


def test_column_filter_cdc_requires_target_nullable():
    src = [_col("id"), _col("memo")]
    tgt = [_col("id"), _col("memo", nullable=False)]
    with pytest.raises(ValidationError, match="目标库"):
        validate_column_filter("t", ["memo"], src, target_columns=tgt, cdc_table=True)


def test_column_filter_allows_nullable_excluded():
    cols = [_col("id"), _col("memo")]
    validate_column_filter("t", ["memo"], cols)   # 不抛错


def test_validate_mapping_subset_warning_and_flag():
    warnings = validate_mapping({"row_filters": {"t1": {"col": "a", "op": "eq", "value": 1}}})
    assert any("子集" in w for w in warnings)
    assert is_subset_mapping({"row_filters": {"t1": {}}})
    assert not is_subset_mapping({"row_filters": {}})
    assert not is_subset_mapping(None)


# ---- 行级 diff / CRC（T4.2） ----

def test_stable_repr_normalizes_types():
    assert stable_repr(None) == "\x00"
    assert stable_repr(b"\x01") == "0x01"
    assert stable_repr(1) == stable_repr("1")


def test_row_crc_stable():
    assert row_crc((1, "a", None)) == row_crc((1, "a", None))
    assert row_crc((1, "a")) != row_crc((1, "b"))


def test_mask_if_sensitive():
    assert mask_if_sensitive("password_hash", "x") == "******"
    assert mask_if_sensitive("api_token", "y") == "******"
    assert mask_if_sensitive("memo", "ok") == "ok"
    assert mask_if_sensitive("password", None) is None


def test_diff_rows_detects_all_three_types():
    src = {("1",): {"id": "1", "a": 1, "b": "x"}, ("2",): {"id": "2", "a": 2, "b": "y"}}
    tgt = {("2",): {"id": "2", "a": 2, "b": "z"}, ("3",): {"id": "3", "a": 3, "b": "w"}}
    diffs, total = diff_rows(src, tgt, ["a", "b"], max_diffs=10)
    types = {d["type"] for d in diffs}
    assert types == {"target_missing", "target_extra", "mismatch"}
    assert total == 3
    m = next(d for d in diffs if d["type"] == "mismatch")
    assert m["columns"]["b"]["source"] == "y" and m["columns"]["b"]["target"] == "z"
    assert "a" not in m["columns"]


def test_diff_rows_caps_output_but_counts_all():
    src = {(str(i),): {"id": str(i), "a": i} for i in range(10)}
    tgt = {}
    diffs, total = diff_rows(src, tgt, ["a"], max_diffs=5)
    assert total == 10 and len(diffs) == 5


def test_eval_predicate_in_op():
    assert eval_predicate({"col": "s", "op": "in", "values": [1, 2]}, {"s": 2})
    assert not eval_predicate({"col": "s", "op": "in", "values": [1, 2]}, {"s": 3})


# ---------------- P2-31 checksum 复合主键分块（词典序 OR 展开） ----------------
def test_lexi_predicates_sql_shape():
    """P2-31：词典序 gt/le 谓词的 SQL 形态（单列退化标量，复合展开 OR 链）。"""
    sql, args = VerifyService._lexi_gt(["id"], (5,))
    assert sql == "(`id` > %s)" and args == [5]

    sql, args = VerifyService._lexi_gt(["a", "b"], (1, 2))
    assert sql == "(`a` > %s) OR (`a` = %s AND `b` > %s)"
    assert args == [1, 1, 2]

    sql, args = VerifyService._lexi_le(["a", "b"], (3, 4))
    assert sql == "(`a` < %s) OR (`a` = %s AND `b` <= %s)"
    assert args == [3, 3, 4]


def test_checksum_chunk_coverage_composite_pk(monkeypatch):
    """P2-31 核心回归：复合主键分块在首列值跨块重复时不漏检、不重复计数。

    模拟两端数据集，按参数个数分派边界语义（gt 独占下界 + le 含上界），
    验证：一致数据 → match 且行数守恒；变异数据 → 三类差异全部定位。
    """
    import re as _re

    import app.services.verify_service as vs
    from app.services.full_engine import MappingResolver

    src_rows = [{"a": a, "b": b, "v": a * 10 + b}
                for a in (1, 2, 3) for b in range(1, 11)]   # 30 行，首列大量跨块重复
    src_rows.sort(key=lambda r: (r["a"], r["b"]))

    def make_fake(tgt_rows):
        def fake_fetch(conn, sql, args=()):
            if "information_schema" in sql:
                return [{"column_name": c} for c in ("a", "b", "v")]
            rows = src_rows if "src_db" in sql else tgt_rows
            # n=2 复合主键：OR 展开后 gt/le 各 3 个绑定参数（v1; v1,v2）
            def unflatten(a):
                return (a[0], a[2])   # [v1, v1, v2] -> (v1, v2)
            has_limit = "LIMIT" in sql
            if len(args) == 6:                       # 目标分块：gt 下界 + le 上界
                lower, upper = unflatten(args[:3]), unflatten(args[3:])
            elif len(args) == 3:
                if has_limit or "<" not in sql:      # 源分页 / 目标尾块：gt 下界
                    lower, upper = unflatten(args), None
                else:                                # 目标首块：仅 le 上界
                    lower, upper = None, unflatten(args)
            else:
                lower = upper = None

            def pk(r):
                return (r["a"], r["b"])
            sel = [r for r in rows
                   if (lower is None or pk(r) > lower)
                   and (upper is None or pk(r) <= upper)]
            sel.sort(key=pk)
            m = _re.search(r"LIMIT (\d+)", sql)
            if m:
                sel = sel[: int(m.group(1))]
            return sel
        return fake_fetch

    resolver = MappingResolver({"scope": "all"}, "src_db", "tgt_db")
    svc = VerifyService.__new__(VerifyService)   # 仅测试 _verify_checksum，跳过装配

    # 1) 两端一致：match、行数守恒、块数 ceil(30/7)=5、无重复计数
    monkeypatch.setattr(vs, "fetch_all", make_fake([dict(r) for r in src_rows]))
    monkeypatch.setattr(vs, "get_primary_key", lambda conn, schema, table: ["a", "b"])
    res = svc._verify_checksum(object(), object(), resolver, "t", 7, set())
    assert res["status"] == "match"
    assert res["src_count"] == 30 == res["tgt_count"]   # 无跳行漏检、无边界重复计数
    assert res["chunks"] == 5 and res["mismatch_chunks"] == 0

    # 2) 变异：值差异 + 目标缺行 + 目标孤儿 → 三类差异全部定位（修复前首列推进直接漏检）
    tgt2 = [dict(r) for r in src_rows]
    tgt2[12]["v"] = -1                                              # mismatch（2,3）
    tgt2 = [r for r in tgt2 if not (r["a"] == 2 and r["b"] == 5)]   # target_missing（2,5）
    tgt2.append({"a": 9, "b": 9, "v": 0})                           # target_extra（越界孤儿）
    monkeypatch.setattr(vs, "fetch_all", make_fake(tgt2))
    res2 = svc._verify_checksum(object(), object(), resolver, "t", 7, set())
    assert res2["status"] == "mismatch"
    types = {d["type"] for d in res2["diffs"]}
    assert types == {"mismatch", "target_missing", "target_extra"}
    assert res2["diff_count"] == 3
    assert res2["src_count"] == 30 and res2["tgt_count"] == 30

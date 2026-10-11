"""M3 单元测试：事件转换规则（T3.3，§4.3.2 / E2 / E3）与批内合并（T3.4）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cdc_transform import (EventTransformer, Op, PredicateError,
                                        ddl_kind, eval_predicate, merge_ops,
                                        parse_affected_tables)

TS, LF, LP, GS = 1700000000, "bin.000001", 500, "uuid:1-5"


def tr(mapping: dict | None = None) -> EventTransformer:
    return EventTransformer(mapping or {}, "src_db", "dst_db")


# ============================================================
# 谓词静态求值器（E3 前置）
# ============================================================

def test_predicate_cmp_and_logic():
    pred = {"and": [{"col": "age", "op": "ge", "value": 18},
                    {"or": [{"col": "city", "op": "eq", "value": "sh"},
                            {"col": "vip", "op": "eq", "value": 1}]}]}
    assert eval_predicate(pred, {"age": 20, "city": "sh", "vip": 0})
    assert eval_predicate(pred, {"age": 20, "city": "bj", "vip": 1})
    assert not eval_predicate(pred, {"age": 17, "city": "sh", "vip": 0})
    assert not eval_predicate(pred, {"age": 20, "city": "bj", "vip": 0})


def test_predicate_in_and_null():
    assert eval_predicate({"col": "k", "op": "in", "values": [1, 2]}, {"k": 2})
    assert not eval_predicate({"col": "k", "op": "in", "values": [1, 2]}, {"k": 3})
    # NULL 语义：比较为假
    assert not eval_predicate({"col": "k", "op": "gt", "value": 1}, {"k": None})


def test_predicate_rejects_subquery_and_functions():
    with pytest.raises(PredicateError):
        eval_predicate({"col": "k", "op": "like", "value": "%x%"}, {"k": "x"})
    with pytest.raises(PredicateError):
        eval_predicate({"f": "NOW()"}, {})
    with pytest.raises(PredicateError):
        eval_predicate({"subquery": "SELECT 1"}, {})


# ============================================================
# 事件转换（§4.3.2）
# ============================================================

def test_insert_to_replace():
    ops = tr().insert("src_db", "users", [{"id": 1, "name": "a"}], ["id"], TS, LF, LP, GS)
    assert len(ops) == 1
    sql, args = ops[0].replace_sql()
    assert sql == "REPLACE INTO `users` (`id`, `name`) VALUES (%s, %s)"
    assert args == (1, "a")
    assert ops[0].origin == "insert"
    assert ops[0].gtid_snapshot == GS and ops[0].log_pos == LP


def test_update_default_replace_after_image():
    ops = tr().update("src_db", "users",
                      [{"before_values": {"id": 1, "name": "a"},
                        "after_values": {"id": 1, "name": "b"}}], ["id"], TS, LF, LP, GS)
    assert len(ops) == 1 and ops[0].kind == "replace" and ops[0].values == (1, "b")


def test_update_pk_change_e2_delete_plus_replace():
    """E2：主键值变更 → 同批 DELETE 旧行 + REPLACE 新行（同一目标事务提交）。"""
    ops = tr().update("src_db", "users",
                      [{"before_values": {"id": 3, "name": "x"},
                        "after_values": {"id": 9, "name": "x"}}], ["id"], TS, LF, LP, GS)
    assert [(o.kind, o.pk_key) for o in ops] == [("delete", (3,)), ("replace", (9,))]
    assert all(o.origin == "update" for o in ops)
    sql, args = ops[0].delete_sql()
    assert sql == "DELETE FROM `users` WHERE `id`=%s" and args == (3,)


def test_update_row_filter_exit_scope_e3():
    """E3：行过滤表 UPDATE 退出范围 → 转 DELETE；进入范围 → REPLACE。"""
    t = tr({"row_filters": {"users": {"col": "city", "op": "eq", "value": "sh"}}})
    # before 满足、after 不满足 → DELETE（防越界行污染目标库）
    ops = t.update("src_db", "users",
                   [{"before_values": {"id": 1, "city": "sh"},
                     "after_values": {"id": 1, "city": "bj"}}], ["id"], TS, LF, LP, GS)
    assert len(ops) == 1 and ops[0].kind == "delete" and ops[0].where_vals == (1,)
    # before 不满足、after 满足 → REPLACE 完整 after 镜像
    ops = t.update("src_db", "users",
                   [{"before_values": {"id": 2, "city": "bj"},
                     "after_values": {"id": 2, "city": "sh"}}], ["id"], TS, LF, LP, GS)
    assert len(ops) == 1 and ops[0].kind == "replace" and ops[0].values == (2, "sh")
    # 前后均不满足 → 丢弃计数
    ops = t.update("src_db", "users",
                   [{"before_values": {"id": 3, "city": "bj"},
                     "after_values": {"id": 3, "city": "gz"}}], ["id"], TS, LF, LP, GS)
    assert ops == [] and t.filter_dropped == 1


def test_insert_row_filter_out_of_scope_dropped():
    t = tr({"row_filters": {"users": {"col": "city", "op": "eq", "value": "sh"}}})
    ops = t.insert("src_db", "users", [{"id": 1, "city": "bj"}], ["id"], TS, LF, LP, GS)
    assert ops == [] and t.filter_dropped == 1


def test_delete_by_pk_idempotent():
    ops = tr().delete("src_db", "users", [{"id": 7}], ["id"], TS, LF, LP, GS)
    sql, args = ops[0].delete_sql()
    assert sql == "DELETE FROM `users` WHERE `id`=%s" and args == (7,)
    assert ops[0].origin == "delete"


def test_out_of_scope_schema_and_table_counted():
    t = tr({"scope": "selected", "tables": ["users"]})
    assert t.insert("other_db", "users", [{"id": 1}], ["id"], TS, LF, LP, GS) == []
    assert t.insert("src_db", "logs", [{"id": 1}], ["id"], TS, LF, LP, GS) == []
    assert t.out_of_scope_dropped == 2


def test_column_filter_alignment():
    """列过滤：排除列不参与 REPLACE，主键列永不剔除。"""
    t = tr({"column_filter": {"users": ["name"]}})
    ops = t.insert("src_db", "users", [{"id": 1, "name": "a", "age": 3}], ["id"], TS, LF, LP, GS)
    sql, args = ops[0].replace_sql()
    assert "`id`" in sql and "`name`" not in sql and "`age`" in sql
    assert args == (1, 3)


def test_composite_pk_delete_where():
    ops = tr().delete("src_db", "t", [{"a": 1, "b": 2}], ["a", "b"], TS, LF, LP, GS)
    sql, args = ops[0].delete_sql()
    assert sql == "DELETE FROM `t` WHERE `a`=%s AND `b`=%s" and args == (1, 2)


def test_rename_and_prefix():
    t = tr({"rename": {"users": "members"}, "prefix": "x_", "suffix": "_c"})
    assert t.target_name("users") == "x_members_c"


# ============================================================
# 批内合并（T3.4：同主键保留最终态；先 DELETE 后 REPLACE）
# ============================================================

def _op(kind, key, table="users", origin="insert"):
    if kind == "delete":
        return Op("delete", table, table, ["id"], where_vals=key, pk_key=key,
                  origin=origin, log_pos=1)
    return Op("replace", table, table, ["id"], cols=["id"], values=key, pk_key=key,
              origin=origin, log_pos=1)


def test_merge_last_op_wins():
    merged = merge_ops([_op("replace", (1,)), _op("replace", (1,)), _op("delete", (1,))])
    assert len(merged) == 1 and merged[0].kind == "delete"


def test_merge_pk_change_not_merged_away():
    """E2 场景：主键变更 UPDATE 拆为 delete(3)+replace(9)，合并不误吞旧行清理。"""
    ops = []
    ops += tr().update("src_db", "users",
                       [{"before_values": {"id": 3}, "after_values": {"id": 9}}],
                       ["id"], TS, LF, LP, GS)
    ops += tr().insert("src_db", "users", [{"id": 9}], ["id"], TS, LF, LP, GS)
    merged = merge_ops(ops)
    kinds = {(o.kind, o.pk_key) for o in merged}
    assert ("delete", (3,)) in kinds and ("replace", (9,)) in kinds
    # 执行顺序：DELETE 全部在前
    assert merged[0].kind == "delete"


def test_merge_pk_swap_deletes_first():
    """存活 DELETE 排在 REPLACE 之前执行；REPLACE「先删后插」自身兜底唯一键互换。"""
    # 键1：先插入后被删 → 最终删除；键2：保持替换
    ops = [_op("replace", (1,)), _op("delete", (1,)), _op("replace", (2,))]
    merged = merge_ops(ops)
    assert [o.kind for o in merged] == ["delete", "replace"]
    assert merged[0].pk_key == (1,)
    # 键 2 先替换后被删（源库该行已删除）→ 合并保留最终删除态，且排前执行
    ops = [_op("replace", (2,)), _op("delete", (2,)), _op("replace", (1,))]
    merged = merge_ops(ops)
    assert [o.kind for o in merged] == ["delete", "replace"]
    assert merged[0].pk_key == (2,)


# ============================================================
# DDL 解析辅助（T3.5/E12）
# ============================================================

def test_parse_affected_tables_and_kind():
    assert parse_affected_tables("ALTER TABLE `users` ADD COLUMN c INT") == ["users"]
    assert parse_affected_tables("ALTER TABLE src_db.orders ADD COLUMN c INT") == ["orders"]
    assert parse_affected_tables("TRUNCATE TABLE logs") == ["logs"]
    assert ddl_kind("ALTER TABLE `users` ADD COLUMN c INT") == "ALTER TABLE users"
    assert ddl_kind("DROP TABLE logs") == "DROP TABLE logs"

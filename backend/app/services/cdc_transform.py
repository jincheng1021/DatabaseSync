"""事件转换层（T3.3 / §4.3.2）：binlog ROW 事件 → 幂等 SQL 操作。

规则（§4.3.2 / E2 / E3）：
- INSERT（WRITE_ROWS）        → REPLACE INTO（与覆盖策略统一，幂等）
- UPDATE（UPDATE_ROWS）       → 默认按 after 镜像 REPLACE；
    E2 主键值变更（before ≠ after）→ 同批「按 before 主键 DELETE + after 镜像 REPLACE」，
      同一目标事务提交，避免旧行残留成孤儿；
    E3 行过滤表：after 不满足谓词 → 转按 before 主键 DELETE；
      before 不满足且 after 满足 → REPLACE 完整 after 镜像。
- DELETE（DELETE_ROWS）       → 按主键 DELETE（幂等）
- 映射范围外事件              → 过滤丢弃仅计数
- 列过滤：binlog 事件列与映射排除列对齐后再转 SQL（被排除列不参与 REPLACE）

REPLACE 前提（§4.3.2）：写入携带全部映射列完整镜像（binlog_row_image=FULL 预检强制）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ============================================================
# 行过滤谓词静态求值器（E3，M4 UI 条件构建器的前置）
# 仅支持「列 + 比较/IN + 字面量」的 AND/OR 组合；
# 不支持子查询与非确定性函数（增量链路需对行镜像静态求值）。
# ============================================================

_CMP_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and b is not None and a > b,
    "ge": lambda a, b: a is not None and b is not None and a >= b,
    "lt": lambda a, b: a is not None and b is not None and a < b,
    "le": lambda a, b: a is not None and b is not None and a <= b,
}


class PredicateError(ValueError):
    """谓词含不支持结构（子查询/非确定性函数等），拒绝静态求值。"""


def _forbidden(node: dict) -> None:
    bad = set(node.keys()) - {"col", "op", "value", "values", "and", "or", "not"}
    if bad:
        raise PredicateError(f"行过滤谓词含不支持的结构：{bad}（仅支持 列+比较/IN+字面量 的 AND/OR）")
    op = node.get("op")
    if isinstance(op, str) and op.lower() in ("like", "regexp", "rlike", "match"):
        raise PredicateError(f"行过滤谓词不支持 {op.upper()}（非静态可求值）")


def eval_predicate(pred: dict | None, row: dict) -> bool:
    """对行镜像静态求值。pred=None 表示无过滤（恒真）。"""
    if not pred:
        return True
    _forbidden(pred)
    if "and" in pred:
        return all(eval_predicate(p, row) for p in pred["and"])
    if "or" in pred:
        return any(eval_predicate(p, row) for p in pred["or"])
    if "not" in pred:
        return not eval_predicate(pred["not"], row)
    col = pred["col"]
    op = str(pred.get("op", "eq")).lower()
    if op == "in":
        vals = pred.get("values", [])
        return row.get(col) in vals
    if op not in _CMP_OPS:
        raise PredicateError(f"不支持的比较符：{op}")
    return _CMP_OPS[op](row.get(col), pred.get("value"))


_SQL_OP = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def predicate_to_sql(pred: dict | None) -> tuple[str | None, list]:
    """行过滤谓词 → SQL WHERE 片段（verify 口径收窄用），语义与 eval_predicate 对齐。

    返回 (sql 或 None, 绑定参数列表)；None 表示无过滤（恒真）。
    NULL 语义对齐 Python 端静态求值：
    - eq None → IS NULL；ne v → (col != v OR col IS NULL)（Python None != v 为 True）；
    - in 含 None → 补 OR col IS NULL；空 IN → 1 = 0；
    - gt/ge/lt/le 对 NULL 行两端均为假，SQL 三值逻辑天然一致。
    结构不合法/含 LIKE 等非静态操作 → PredicateError。
    """
    if not pred:
        return None, []
    _forbidden(pred)
    if "and" in pred:
        parts: list[str] = []
        args: list = []
        for p in pred["and"]:
            s, a = predicate_to_sql(p)
            if s:
                parts.append(f"({s})")
                args.extend(a)
        return (" AND ".join(parts) if parts else None), args
    if "or" in pred:
        parts, args = [], []
        for p in pred["or"]:
            s, a = predicate_to_sql(p)
            if s:
                parts.append(f"({s})")
                args.extend(a)
        if not parts:
            return "1 = 0", []          # 空分支：any([])=False
        return " OR ".join(parts), args
    if "not" in pred:
        s, a = predicate_to_sql(pred["not"])
        return (f"NOT ({s})" if s else None), a
    col = pred["col"]
    op = str(pred.get("op", "eq")).lower()
    c = f"`{col}`"
    if op == "in":
        vals = pred.get("values", [])
        if not vals:
            return "1 = 0", []
        ph = ", ".join(["%s"] * len(vals))
        if any(v is None for v in vals):
            return f"({c} IN ({ph}) OR {c} IS NULL)", list(vals)
        return f"{c} IN ({ph})", list(vals)
    if op not in _SQL_OP:
        raise PredicateError(f"不支持的比较符：{op}")
    v = pred.get("value")
    if op == "eq":
        return (f"{c} IS NULL", []) if v is None else (f"{c} = %s", [v])
    if op == "ne":
        return (f"{c} IS NOT NULL", []) if v is None else (f"({c} != %s OR {c} IS NULL)", [v])
    return f"{c} {_SQL_OP[op]} %s", [v]


# ============================================================
# 操作模型：一个 Op = 一条幂等语句（replace / delete）
# ============================================================

@dataclass
class Op:
    kind: str                       # replace | delete
    table: str                      # 源表名（合并键的一部分）
    target_table: str               # 目标表名（含改名/前后缀）
    pk_cols: list[str]              # 主键列（delete 的 WHERE / 合并键）
    cols: list[str] = field(default_factory=list)      # replace 的映射列
    values: tuple | None = None                        # replace 行镜像值
    where_vals: tuple | None = None                    # delete 主键值
    pk_key: tuple | None = None                        # 合并键（主键值元组）
    src_ts: int = 0                # 事件源库时间戳（延迟计算）
    origin: str = "insert"         # 来源事件类型：insert/update/delete（统计口径）
    log_file: str | None = None    # 事件位点（file+pos 语义）
    log_pos: int | None = None
    gtid_snapshot: str = ""        # 事件时已执行 GTID 集合快照（不可变字符串）

    def replace_sql(self) -> tuple[str, tuple]:
        collist = ", ".join(f"`{c}`" for c in self.cols)
        tpl = ", ".join(["%s"] * len(self.cols))
        return (f"REPLACE INTO `{self.target_table}` ({collist}) VALUES ({tpl})", self.values)

    def delete_sql(self) -> tuple[str, tuple]:
        where = " AND ".join(f"`{c}`=%s" for c in self.pk_cols)
        return (f"DELETE FROM `{self.target_table}` WHERE {where}", self.where_vals)


def merge_ops(ops: list[Op]) -> list[Op]:
    """批内同主键合并保留最终态（T3.4）：按 (表, 主键值) 取最后一条操作。

    主键变更 UPDATE 已在转换期拆为 delete(旧键)+replace(新键) 两条独立键位的 Op，
    天然参与「键位最后态」合并不误合并（验收项）。执行顺序：先全部 DELETE 再 REPLACE，
    规避批内主键互换等唯一键冲突场景。
    """
    final: dict[tuple[str, tuple], Op] = {}
    for op in ops:
        final[(op.table, op.pk_key)] = op
    deletes = [op for op in final.values() if op.kind == "delete"]
    replaces = [op for op in final.values() if op.kind == "replace"]
    return deletes + replaces


# ============================================================
# 事件 → Op 转换器
# ============================================================

_TABLE_RE = re.compile(
    r"(?:TABLE|DATABASE|SCHEMA)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"`?(?:[\w$]+`?\.`?)?([\w$]+)`?", re.IGNORECASE)


def parse_affected_tables(ddl_text: str) -> list[str]:
    """从 DDL 文本尽力提取受影响表名（T3.5 落库 affected_tables_json）。"""
    return list(dict.fromkeys(m.group(1) for m in _TABLE_RE.finditer(ddl_text or "")))


_KEYWORD2 = {"TABLE", "DATABASE", "SCHEMA", "VIEW", "INDEX", "TRIGGER", "EVENT",
             "PROCEDURE", "FUNCTION", "USER"}


def ddl_kind(ddl_text: str) -> str:
    """「同类 DDL」记忆键（E12）：动作（含对象类型）+ 首个受影响表，如 'ALTER TABLE users'。"""
    words = (ddl_text or "").strip().split()
    if not words:
        return "UNKNOWN *"
    action = words[0].upper()
    if len(words) > 1 and words[1].upper() in _KEYWORD2:
        action = f"{action} {words[1].upper()}"
    tables = parse_affected_tables(ddl_text)
    return f"{action} {tables[0] if tables else '*'}"


class EventTransformer:
    """按任务映射配置把 ROW 事件转换为 Op 列表。

    mapping 结构（与全量引擎 MappingResolver 共用同一份 mapping_json）：
      scope/tables/rename/prefix/suffix/source_database/target_database（表范围与改名）
      column_filter: {源表名: [排除列, ...]}            —— 列过滤（界面 M4，接口本期就绪）
      row_filters:   {源表名: 谓词 dict}                —— 行过滤（E3）
    """

    def __init__(self, mapping: dict, source_schema: str | None, target_schema: str | None,
                 rename: dict | None = None):
        m = mapping or {}
        self.scope = m.get("scope", "all")
        self.selected = set(m.get("tables") or [])
        self.column_filter: dict[str, list[str]] = m.get("column_filter") or {}
        self.row_filters: dict[str, dict] = m.get("row_filters") or {}
        self.source_schema = m.get("source_database") or source_schema
        self.target_schema = m.get("target_database") or target_schema
        self._rename = m.get("rename") or {}
        self._prefix = m.get("prefix") or ""
        self._suffix = m.get("suffix") or ""
        if rename:
            self._rename.update(rename)
        # 观测计数：范围外丢弃、行过滤丢弃
        self.out_of_scope_dropped = 0
        self.filter_dropped = 0

    def in_scope(self, schema: str, table: str) -> bool:
        if self.source_schema and schema and schema != self.source_schema:
            return False
        if self.scope == "selected" and table not in self.selected:
            return False
        return True

    def target_name(self, src_table: str) -> str:
        name = self._rename.get(src_table, src_table)
        return f"{self._prefix}{name}{self._suffix}"

    def _mapped_cols(self, table: str, row: dict, pk_cols: list[str]) -> tuple[list[str], list]:
        """列过滤对齐：剔除排除列（主键列永不剔除），保持事件列序。"""
        excluded = set(self.column_filter.get(table) or [])
        cols, vals = [], []
        for c, v in row.items():
            if c in excluded and c not in pk_cols:
                continue
            cols.append(c)
            vals.append(v)
        return cols, tuple(vals)

    def _pk_of(self, pk_cols: list[str], row: dict) -> tuple:
        return tuple(row.get(c) for c in pk_cols)

    # ---- 各事件类型 ----
    def insert(self, schema: str, table: str, rows: list[dict], pk_cols: list[str],
               src_ts: int, log_file: str | None, log_pos: int | None,
               gtid_snapshot: tuple) -> list[Op]:
        if not self.in_scope(schema, table):
            self.out_of_scope_dropped += len(rows)
            return []
        out: list[Op] = []
        tgt = self.target_name(table)
        pred = self.row_filters.get(table)
        for row in rows:
            if not eval_predicate(pred, row):     # E3：INSERT 不满足谓词 = 不在同步范围
                self.filter_dropped += 1
                continue
            cols, vals = self._mapped_cols(table, row, pk_cols)
            out.append(Op("replace", table, tgt, pk_cols, cols=cols, values=vals,
                          pk_key=self._pk_of(pk_cols, row), src_ts=src_ts,
                          log_file=log_file, log_pos=log_pos, gtid_snapshot=gtid_snapshot,
                          origin="insert"))
        return out

    def update(self, schema: str, table: str, rows: list[dict], pk_cols: list[str],
               src_ts: int, log_file: str | None, log_pos: int | None,
               gtid_snapshot: tuple) -> list[Op]:
        if not self.in_scope(schema, table):
            self.out_of_scope_dropped += len(rows)
            return []
        out: list[Op] = []
        tgt = self.target_name(table)
        pred = self.row_filters.get(table)
        for row in rows:
            before, after = row["before_values"], row["after_values"]
            key_before, key_after = self._pk_of(pk_cols, before), self._pk_of(pk_cols, after)
            if key_before != key_after:
                # ---- E2 主键值变更：同批 DELETE 旧行 + REPLACE 新行（同一目标事务提交） ----
                out.append(Op("delete", table, tgt, pk_cols, where_vals=key_before,
                              pk_key=key_before, src_ts=src_ts, log_file=log_file,
                              log_pos=log_pos, gtid_snapshot=gtid_snapshot, origin="update"))
                cols, vals = self._mapped_cols(table, after, pk_cols)
                out.append(Op("replace", table, tgt, pk_cols, cols=cols, values=vals,
                              pk_key=key_after, src_ts=src_ts, log_file=log_file,
                              log_pos=log_pos, gtid_snapshot=gtid_snapshot, origin="update"))
                continue
            in_b = eval_predicate(pred, before)
            in_a = eval_predicate(pred, after)
            if in_b and not in_a:
                # ---- E3 退出范围：转按 before 主键 DELETE，防越界行污染目标库 ----
                out.append(Op("delete", table, tgt, pk_cols, where_vals=key_before,
                              pk_key=key_before, src_ts=src_ts, log_file=log_file,
                              log_pos=log_pos, gtid_snapshot=gtid_snapshot, origin="update"))
            elif in_a:
                # in_b&in_a 正常 REPLACE；!in_b&in_a 进入范围 → REPLACE 完整 after 镜像
                cols, vals = self._mapped_cols(table, after, pk_cols)
                out.append(Op("replace", table, tgt, pk_cols, cols=cols, values=vals,
                              pk_key=key_after, src_ts=src_ts, log_file=log_file,
                              log_pos=log_pos, gtid_snapshot=gtid_snapshot, origin="update"))
            else:
                self.filter_dropped += 1     # 前后均不在范围：丢弃计数
        return out

    def delete(self, schema: str, table: str, rows: list[dict], pk_cols: list[str],
               src_ts: int, log_file: str | None, log_pos: int | None,
               gtid_snapshot: tuple) -> list[Op]:
        if not self.in_scope(schema, table):
            self.out_of_scope_dropped += len(rows)
            return []
        tgt = self.target_name(table)
        out: list[Op] = []
        for row in rows:
            key = self._pk_of(pk_cols, row)
            out.append(Op("delete", table, tgt, pk_cols, where_vals=key, pk_key=key,
                          src_ts=src_ts, log_file=log_file, log_pos=log_pos,
                          gtid_snapshot=gtid_snapshot, origin="delete"))
        return out

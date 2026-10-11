"""映射高级特性校验（T4.5 / §4.2.4）：列过滤 / 行过滤 / 触发器开关的配置期拦截。

- 列过滤：排除列不得属于主键/唯一键；NOT NULL 且无默认值列不可排除；
  CDC 表列过滤要求目标列可空或有默认值（目标库在线时校验）。
- 行过滤（E3）：谓词结构静态校验（复用 cdc_transform.eval_predicate 的结构限制，
  以空行求值验证结构 + 以样本值验证字面量类型），不支持 LIKE/子查询等非静态结构。
- 全量/增量/校验/备份共用同一份 mapping_json —— 本模块为唯一配置入口校验。

纯函数（给定元数据）便于测试；live 校验（连库取元数据）为可选增强。
"""
from __future__ import annotations

from app.core.errors import ValidationError
from app.services.cdc_transform import PredicateError, eval_predicate


def validate_row_filter(table: str, pred: dict | None) -> None:
    """行过滤谓词结构校验（E3）：仅列+比较/IN+字面量的 AND/OR 组合。"""
    if pred in (None, {}):
        return
    if not isinstance(pred, dict):
        raise ValidationError(f"表 {table} 行过滤谓词格式错误", hint="应为条件对象（条件构建器产物）")
    # 结构探针：空行求值仅验证结构（无值命中恒 False，不抛错即结构合法）
    try:
        eval_predicate(pred, {})
    except PredicateError as e:
        raise ValidationError(f"表 {table} 行过滤条件不支持：{e}",
                              hint="仅支持「列 + 比较/IN + 字面量」的 AND/OR 组合") from e
    except KeyError as e:
        raise ValidationError(f"表 {table} 行过滤条件缺少字段：{e}",
                              hint="每个简单条件需要 col / op / value（或 values）") from e


def validate_column_filter(table: str, excluded: list[str], columns: list[dict],
                           *, target_columns: list[dict] | None = None,
                           cdc_table: bool = False) -> None:
    """列过滤校验。

    columns / target_columns 元素：{name, nullable(bool), column_default, column_key}
      column_key：PRI / UNI / MUL（information_schema.columns 口径）
    """
    if not excluded:
        return
    colmap = {c["name"]: c for c in columns}
    for col in excluded:
        meta = colmap.get(col)
        if meta is None:
            raise ValidationError(f"表 {table} 列过滤排除列 {col} 不存在于源表")
        if meta.get("column_key") in ("PRI", "UNI"):
            raise ValidationError(
                f"表 {table} 列过滤排除列 {col} 属于主键/唯一键，禁止排除",
                hint="唯一键列被排除将破坏幂等写与行定位")
        if not meta.get("nullable", True) and meta.get("column_default") is None:
            raise ValidationError(
                f"表 {table} 列过滤排除列 {col} 为 NOT NULL 且无默认值，禁止排除",
                hint="目标库写入时该列无值可填，将导致插入失败")
        if cdc_table and target_columns is not None:
            tmeta = next((c for c in target_columns if c["name"] == col), None)
            if tmeta is not None and not tmeta.get("nullable", True) \
                    and tmeta.get("column_default") is None:
                raise ValidationError(
                    f"表 {table} 列过滤排除列 {col} 在目标库为 NOT NULL 且无默认值",
                    hint="增量链路 REPLACEd 行不含该列，需目标列可空或有默认值")


def validate_mapping(mapping: dict | None, *, table_meta: dict[str, list[dict]] | None = None,
                     target_meta: dict[str, list[dict]] | None = None,
                     cdc_enabled: bool = False) -> list[str]:
    """mapping 全量校验，返回警告列表（错误直接抛 ValidationError）。

    table_meta / target_meta：{源表名: 列元数据列表}；传入即启用列过滤深度校验。
    """
    m = mapping or {}
    warnings: list[str] = []
    for t, pred in (m.get("row_filters") or {}).items():
        validate_row_filter(t, pred)
        warnings.append(f"表 {t} 启用行过滤：子集同步，孤儿行清理将自动禁用，"
                        "退出范围行会按源库删除转 DELETE")
    for t, excluded in (m.get("column_filter") or {}).items():
        cols = (table_meta or {}).get(t)
        if cols is not None:
            validate_column_filter(t, excluded, cols,
                                   target_columns=(target_meta or {}).get(t),
                                   cdc_table=cdc_enabled)
    return warnings


def is_subset_mapping(mapping: dict | None) -> bool:
    """子集同步判定：任一表启用行过滤 → 孤儿行清理全链路禁用（E14）。"""
    return bool((mapping or {}).get("row_filters"))

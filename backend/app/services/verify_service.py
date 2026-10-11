"""校验服务（T4.2 / §4.5）：行数校验 / 分块 checksum / 行级 diff / 以源库覆盖修复。

- 行数校验：逐表精确 count（information_schema 估算仅作大表排序筛选，不作结论）。
- checksum 校验：主键范围分块（默认 10 万行/块）两端 CRC32 聚合比对；不一致块下钻
  逐行 diff（表/主键/差异列/两端值；敏感列脱敏后存 detail_json）。
- 会话统一（硬性项）：两端连接统一字符集（按源库连接配置）与时区（取源库 time_zone），
  规避校验误报。
- 修复：「以源库覆盖修复选中差异」——差异行按源库当前值 REPLACE 写入目标库（复用幂等写
  语义）；target_extra（目标有源无）行删除，行过滤子集表禁用此类修复。
- 全量/重刷成功后自动行数校验（trigger=auto）；不一致率超阈值 → 告警。
"""
from __future__ import annotations

import logging
import threading
import zlib
from datetime import date, datetime

import pymysql

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.progress import ProgressTracker
from app.core.ws_broadcaster import emit_from_thread
from app.models.m2_repository import RunRepository, TaskRepository
from app.models.m4_repository import VerifyResultRepository
from app.services.full_engine import MappingResolver, get_primary_key, list_source_tables
from app.services.mysql_client import MySQLTarget, fetch_all, fetch_one, open_connection

log = logging.getLogger("dsync.verify")

SENSITIVE_PAT = ("password", "passwd", "secret", "token", "credential")
DEFAULT_CHUNK_ROWS = 100000


def mask_if_sensitive(col: str, value) -> object:
    if value is None:
        return None
    low = col.lower()
    if any(p in low for p in SENSITIVE_PAT):
        return "******"
    return value


def stable_repr(v) -> str:
    """跨类型稳定序列化（None/bytes/datetime/decimal 差异不构成误报口径）。"""
    if v is None:
        return "\x00"
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ")
    return str(v)


def row_crc(values: tuple) -> int:
    return zlib.crc32("|".join(stable_repr(v) for v in values).encode("utf-8"))


def diff_rows(src_rows: dict, tgt_rows: dict, compare_cols: list[str],
              max_diffs: int) -> tuple[list[dict], int]:
    """按主键逐行比对，返回（diff 列表, 总差异数）。diff：pk/差异列/两端值。"""
    diffs: list[dict] = []
    total = 0
    for pk in sorted(set(src_rows) | set(tgt_rows), key=str):
        s, t = src_rows.get(pk), tgt_rows.get(pk)
        if s is None:
            total += 1
            if len(diffs) < max_diffs:
                diffs.append({"pk": list(pk), "type": "target_extra",
                              "target": {c: mask_if_sensitive(c, t.get(c)) for c in compare_cols}})
            continue
        if t is None:
            total += 1
            if len(diffs) < max_diffs:
                diffs.append({"pk": list(pk), "type": "target_missing",
                              "source": {c: mask_if_sensitive(c, s.get(c)) for c in compare_cols}})
            continue
        changed = {c: {"source": mask_if_sensitive(c, s[c]), "target": mask_if_sensitive(c, t[c])}
                   for c in compare_cols if stable_repr(s.get(c)) != stable_repr(t.get(c))}
        if changed:
            total += 1
            if len(diffs) < max_diffs:
                diffs.append({"pk": list(pk), "type": "mismatch", "columns": changed})
    return diffs, total


class VerifyService:
    def __init__(self, *, tasks: TaskRepository, runs: RunRepository,
                 verify_repo: VerifyResultRepository, settings, tracker: ProgressTracker,
                 get_source: callable, get_target: callable,
                 alert_repo=None, broadcaster=None):
        self.tasks = tasks
        self.runs = runs
        self.verify_repo = verify_repo
        self.settings = settings
        self.tracker = tracker
        self._get_source = get_source
        self._get_target = get_target
        self.alert_repo = alert_repo
        self.broadcaster = broadcaster
        self._threads: dict[int, threading.Thread] = {}

    def _p(self, key: str, default):
        v = self.settings.get(key)
        return default if v is None else v

    # ---------- 发起 ----------
    def start(self, task_id: int, *, method: str = "checksum", trigger: str = "manual",
              tables: list[str] | None = None) -> int:
        if method not in ("row_count", "checksum"):
            raise ValidationError("method 仅支持 row_count/checksum")
        task = self.tasks.get(task_id)
        if not task:
            raise NotFoundError(f"任务 {task_id} 不存在")
        if task["state"] in ("initializing", "full_resyncing", "backing_up"):
            raise ConflictError("任务全量运行中，校验结果不具参考性",
                                hint="请等待全量结束后再发起校验")
        vid = self.verify_repo.create(task_id, method=method, trigger=trigger)
        t = threading.Thread(target=self._run,
                             args=(dict(task), vid, method, tables),
                             daemon=True, name=f"verify-{vid}")
        self._threads[vid] = t
        t.start()
        return vid

    def auto_row_count_after_resync(self, task_id: int, run_id: int) -> int | None:
        """全量/重刷成功后自动行数校验（§4.5，trigger=auto）。"""
        task = self.tasks.get(task_id)
        if not task:
            return None
        vid = self.verify_repo.create(task_id, method="row_count", trigger="auto",
                                      run_id=run_id)
        t = threading.Thread(target=self._run, args=(task, vid, "row_count", None),
                             daemon=True, name=f"verify-auto-{vid}")
        self._threads[vid] = t
        t.start()
        return vid

    # ---------- 会话统一（硬性项） ----------
    def _open_unified(self, target: MySQLTarget, tz: str | None) -> pymysql.connections.Connection:
        """两端校验会话显式统一字符集与时区（按源库连接配置），规避误报。"""
        conn = open_connection(target)
        with conn.cursor() as cur:
            if tz:
                cur.execute("SET time_zone=%s", (tz,))
            cur.execute("SET sql_mode=ANSI")
        return conn

    # ---------- 主体 ----------
    def _run(self, task: dict, vid: int, method: str, tables: list[str] | None) -> None:
        detail: dict = {"tables": {}, "warnings": []}
        summary = {"tables_checked": 0, "mismatch_tables": 0, "mismatch_rows": 0}
        try:
            src: MySQLTarget = self._get_source(self.tasks.get(task["id"]))
            tgt: MySQLTarget = self._get_target(self.tasks.get(task["id"]))
            resolver = MappingResolver(task["mapping"], src.default_schema, tgt.default_schema)
            src_conn = open_connection(src)
            with src_conn.cursor() as cur:
                cur.execute("SELECT @@session.time_zone AS tz")
                tz = cur.fetchone()["tz"]
            # 会话统一：目标端复用源库字符集与时区
            tgt_conn = self._open_unified(tgt, tz)
            try:
                all_tables = [t["name"] for t in
                              list_source_tables(src_conn, resolver.source_schema)]
                use_tables = tables or resolver.resolve(all_tables)
                unknown = [t for t in use_tables if t not in all_tables]
                if unknown:
                    raise ValidationError(f"校验表在源库不存在：{', '.join(unknown)}")
                # 大表优先（估算仅作排序筛选，不作结论）
                metas = {t["name"]: t for t in list_source_tables(src_conn, resolver.source_schema)}
                use_tables = sorted(use_tables, key=lambda n: -metas.get(n, {}).get("rows_est", 0))
                mapping = task["mapping"] or {}
                chunk_rows = int(self._p("checksum_chunk_rows", DEFAULT_CHUNK_ROWS))
                excluded_all = mapping.get("column_filter") or {}
                for table in use_tables:
                    if method == "row_count":
                        tr = self._verify_row_count(src_conn, tgt_conn, resolver, table)
                    else:
                        tr = self._verify_checksum(src_conn, tgt_conn, resolver, table,
                                                   chunk_rows,
                                                   set(excluded_all.get(table) or []),
                                                   no_pk_degraded=self._is_degraded(mapping, table))
                    detail["tables"][table] = tr
                    summary["tables_checked"] += 1
                    if tr["status"] != "match":
                        summary["mismatch_tables"] += 1
                        summary["mismatch_rows"] += tr.get("diff_count", 0)
                        if tr.get("warning"):
                            detail["warnings"].append(f"{table}: {tr['warning']}")
                self.verify_repo.finish(
                    vid, state="success" if summary["mismatch_tables"] == 0 else "mismatch",
                    summary=summary, detail=detail)
                # 不一致率超阈值 → 告警（阈值默认 0.1%）
                total_rows = sum((detail["tables"][t].get("src_count") or 0) for t in use_tables)
                ratio = (summary["mismatch_rows"] / total_rows) if total_rows else 0.0
                if summary["mismatch_rows"] > 0 and ratio >= float(self._p("verify_mismatch_alert_ratio", 0.001)):
                    self._fire_mismatch(task["id"], summary, ratio)
                if self.broadcaster:
                    emit_from_thread(self.broadcaster, "verify.finished",
                                     {"verify_id": vid, "task_id": task["id"],
                                      "method": method, **summary}, task["id"])
            finally:
                src_conn.close()
                tgt_conn.close()
        except Exception as e:  # noqa: BLE001 —— 校验线程异常兜底落库（M2 规约）
            log.exception("校验 vid=%s failed", vid)
            summary["error"] = str(e)
            self.verify_repo.finish(vid, state="failed", summary=summary, detail=detail)

    @staticmethod
    def _is_degraded(mapping: dict, table: str) -> bool:
        return ((mapping.get("pk_mode") or {}).get(table) == "degraded")

    def _compare_cols(self, src_conn, schema: str, table: str, pk: list[str],
                      excluded: set[str]) -> list[str]:
        rows = fetch_all(src_conn,
                         "SELECT column_name FROM information_schema.columns "
                         "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                         (schema, table))
        cols = [r.get("column_name") or r.get("COLUMN_NAME") for r in rows]
        return [c for c in cols if c not in excluded or c in pk]

    @staticmethod
    def _lexi_gt(cols: list[str], vals: tuple) -> tuple[str, list]:
        """字典序「元组 > vals」的 OR 展开（不含 WHERE）。

        (c1,...,cn) > (v1,...,vn) ⇔ (c1>v1) OR (c1=v1 AND c2>v2) OR ...
        修复前直接写行构造 (c1,...,cn) > (v1,...,vn)——MySQL 5.7 不走索引
        范围访问（8.0.16+ 才支持），展开后范围优化器可合并为同一复合索引
        的多段 range，5.7/8.0 均可走索引。单列退化为标量比较。
        """
        n = len(cols)
        conds: list[str] = []
        args: list = []
        for k in range(n):
            parts = [f"`{cols[i]}` = %s" for i in range(k)]
            parts.append(f"`{cols[k]}` > %s")
            args.extend(vals[i] for i in range(k))   # 等值前缀同样是绑定参数
            args.append(vals[k])
            conds.append("(" + " AND ".join(parts) + ")")
        return " OR ".join(conds), args

    @staticmethod
    def _lexi_le(cols: list[str], vals: tuple) -> tuple[str, list]:
        """字典序「元组 <= vals」的 OR 展开（不含 WHERE）。末列取 <=，其余取 <。"""
        n = len(cols)
        conds: list[str] = []
        args: list = []
        for k in range(n - 1):
            parts = [f"`{cols[i]}` = %s" for i in range(k)]
            parts.append(f"`{cols[k]}` < %s")
            args.extend(vals[i] for i in range(k))
            args.append(vals[k])
            conds.append("(" + " AND ".join(parts) + ")")
        parts = [f"`{cols[i]}` = %s" for i in range(n - 1)]
        parts.append(f"`{cols[n - 1]}` <= %s")
        args.extend(vals[i] for i in range(n - 1))
        args.append(vals[n - 1])
        conds.append("(" + " AND ".join(parts) + ")")
        return " OR ".join(conds), args

    # ---- 行数校验 ----
    def _verify_row_count(self, src_conn, tgt_conn, resolver: MappingResolver,
                          table: str) -> dict:
        s = int(fetch_one(src_conn, f"SELECT COUNT(*) AS n FROM `{resolver.source_schema}`.`{table}`")["n"])
        t = int(fetch_one(tgt_conn, f"SELECT COUNT(*) AS n FROM `{resolver.target_schema}`.`{resolver.target_name(table)}`")["n"])
        return {"status": "match" if s == t else "mismatch", "src_count": s, "tgt_count": t,
                "diff_count": abs(s - t) if s != t else 0}

    # ---- checksum 校验 ----
    def _verify_checksum(self, src_conn, tgt_conn, resolver: MappingResolver, table: str,
                         chunk_rows: int, excluded: set[str], *,
                         no_pk_degraded: bool = False) -> dict:
        tgt_table = resolver.target_name(table)
        pk = get_primary_key(src_conn, resolver.source_schema, table)
        cols = self._compare_cols(src_conn, resolver.source_schema, table, pk, excluded)
        collist = ", ".join(f"`{c}`" for c in cols)
        result: dict = {"status": "match", "chunks": 0, "mismatch_chunks": 0,
                        "diff_count": 0, "diffs": [], "src_count": 0, "tgt_count": 0}

        # ---- 无主键降级比对（T4.5）：整表 ORDER BY 全列聚合 CRC（无法定位行级，且物理删除不可捕获） ----
        if not pk or no_pk_degraded:
            if not pk:
                result["warning"] = ("无主键表降级比对：整表聚合 CRC（无法行级定位；"
                                     "物理删除无法捕获，建议补主键走 CDC）")
            order = ", ".join(f"`{c}`" for c in cols)
            s_crc = self._agg_crc(src_conn, f"SELECT {collist} FROM `{resolver.source_schema}`.`{table}` ORDER BY {order}")
            t_crc = self._agg_crc(tgt_conn, f"SELECT {collist} FROM `{resolver.target_schema}`.`{tgt_table}` ORDER BY {order}")
            result["src_crc"] = s_crc
            result["tgt_crc"] = t_crc
            s_cnt = int(fetch_one(src_conn, f"SELECT COUNT(*) AS n FROM `{resolver.source_schema}`.`{table}`")["n"])
            t_cnt = int(fetch_one(tgt_conn, f"SELECT COUNT(*) AS n FROM `{resolver.target_schema}`.`{tgt_table}`")["n"])
            result["src_count"], result["tgt_count"] = s_cnt, t_cnt
            if s_crc != t_crc or s_cnt != t_cnt:
                result.update(status="mismatch", diff_count=max(s_cnt, t_cnt))
            return result

        # 分块两端比对（P2-31 修复 v2）：全主键字典序 keyset + 半开区间
        # v1 修复：按首列 `pkc > %s` 推进会跳过复合主键同首列值的行（漏检），
        #   目标端双闭区间使边界行跨块重复计数（误报）→ 改元组 keyset + 半开区间。
        # v2 加固：行构造比较 (c1,c2)>(x,y) 在 MySQL 5.7 不走索引范围访问
        #   （8.0.16+ 才支持）→ 大表分块退化为每块全表扫描；展开为 OR 词典序
        #   形式后范围优化器可合并为同一复合索引的多段 range，双版本均走索引。
        pkc_list = ", ".join(f"`{c}`" for c in pk)

        last_vals: tuple | None = None
        while True:
            pred, args = (self._lexi_gt(pk, last_vals) if last_vals is not None else ("", []))
            where = f" WHERE {pred}" if pred else ""
            sql = (f"SELECT {collist} FROM `{resolver.source_schema}`.`{table}`{where} "
                   f"ORDER BY {pkc_list} LIMIT {int(chunk_rows)}")
            s_rows = fetch_all(src_conn, sql, tuple(args))
            if not s_rows:
                break
            boundary_vals = tuple(s_rows[-1][c] for c in pk)
            # 目标端同口径半开区间：(上一块边界, 本块边界]
            where_parts: list[str] = []
            t_args: list = []
            if last_vals is not None:
                p, a = self._lexi_gt(pk, last_vals)
                where_parts.append(f"({p})")
                t_args.extend(a)
            p, a = self._lexi_le(pk, boundary_vals)
            where_parts.append(f"({p})")
            t_args.extend(a)
            t_where = " WHERE " + " AND ".join(where_parts)
            t_sql = (f"SELECT {collist} FROM `{resolver.target_schema}`.`{tgt_table}`{t_where} "
                     f"ORDER BY {pkc_list}")
            t_rows = fetch_all(tgt_conn, t_sql, tuple(t_args))
            last_vals = boundary_vals
            result["chunks"] += 1
            result["src_count"] += len(s_rows)
            result["tgt_count"] += len(t_rows)
            s_crc = zlib.crc32(b"".join(row_crc(tuple(r.values())).to_bytes(4, "big") for r in s_rows))
            t_crc = zlib.crc32(b"".join(row_crc(tuple(r.values())).to_bytes(4, "big") for r in t_rows))
            if s_crc != t_crc or len(s_rows) != len(t_rows):
                result["mismatch_chunks"] += 1
                s_by_pk = {tuple(r[c] for c in pk): r for r in s_rows}
                t_by_pk = {tuple(r[c] for c in pk): r for r in t_rows}
                diffs, total = diff_rows(s_by_pk, t_by_pk, cols, max_diffs=100)
                result["diff_count"] += total
                result["diffs"].extend(diffs[: max(0, 100 - len(result["diffs"]))])
        # 尾块：源库遍历完后，目标库残留 > 末块边界的行 = 目标多余（孤儿）
        pred_lo, lo_args = (self._lexi_gt(pk, last_vals) if last_vals is not None else ("", []))
        tail_where = f" WHERE {pred_lo}" if pred_lo else ""
        tail_sql = (f"SELECT {collist} FROM `{resolver.target_schema}`.`{tgt_table}`"
                    f"{tail_where} ORDER BY {pkc_list}")
        t_tail = fetch_all(tgt_conn, tail_sql, tuple(lo_args))
        if t_tail:
            result["mismatch_chunks"] += 1
            result["tgt_count"] += len(t_tail)
            t_by_pk = {tuple(r[c] for c in pk): r for r in t_tail}
            diffs, total = diff_rows({}, t_by_pk, cols, max_diffs=100)
            result["diff_count"] += total
            result["diffs"].extend(diffs[: max(0, 100 - len(result["diffs"]))])
        if result["mismatch_chunks"]:
            result["status"] = "mismatch"
        return result

    @staticmethod
    def _agg_crc(conn, sql: str) -> str:
        """无主键整表聚合 CRC（SSCursor 流式，防大表载入内存）。"""
        agg = 0
        with conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(10000)
                if not rows:
                    break
                for r in rows:
                    agg = zlib.crc32(row_crc(tuple(r)).to_bytes(4, "big"), agg)
        return f"{agg:08x}"

    def _fire_mismatch(self, task_id: int, summary: dict, ratio: float) -> None:
        if self.alert_repo:
            try:
                self.alert_repo.fire(
                    f"verify_mismatch:{task_id}",
                    f"校验不一致超标：{summary['mismatch_rows']} 行差异"
                    f"（{summary['mismatch_tables']} 表，不一致率 {ratio:.2%}），请查看校验报告",
                    level="error", task_id=task_id)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 修复（以源库覆盖修复选中差异） ----------
    def repair(self, vid: int, *, tables: list[str] | None = None,
               pks: list[list] | None = None) -> dict:
        """默认不自动修复——显式 API 触发。返回 {replaced, deleted, skipped}。"""
        vr = self.verify_repo.get(vid, with_detail=True)
        if not vr:
            raise NotFoundError(f"校验报告 {vid} 不存在")
        if vr["state"] not in ("mismatch", "success"):
            raise ConflictError(f"校验报告状态为 {vr['state']}，无可修复差异")
        task = self.tasks.get(vr["task_id"])
        if not task:
            raise NotFoundError("关联任务不存在")
        detail = vr.get("detail") or {}
        rows_by_table = detail.get("tables") or {}
        src: MySQLTarget = self._get_source(task)
        tgt: MySQLTarget = self._get_target(task)
        resolver = MappingResolver(task["mapping"], src.default_schema, tgt.default_schema)
        row_filters = task["mapping"].get("row_filters") or {}
        src_conn = open_connection(src)
        tgt_conn = open_connection(tgt)
        # 数据源未设 default_schema 时连接无默认库，未限定库名的 DELETE/REPLACE 报 1046
        src_conn.select_db(resolver.source_schema)
        tgt_conn.select_db(resolver.target_schema)
        replaced = deleted = skipped = 0
        try:
            for table, tr in rows_by_table.items():
                if tables and table not in tables:
                    continue
                diffs = tr.get("diffs") or []
                if not diffs:
                    continue
                pk = get_primary_key(src_conn, resolver.source_schema, table)
                if not pk:
                    skipped += len(diffs)
                    continue
                tgt_table = resolver.target_name(table)
                pkc = pk[0] if len(pk) == 1 else None
                cols = self._compare_cols(src_conn, resolver.source_schema, table, pk, set())
                collist = ", ".join(f"`{c}`" for c in cols)
                for d in diffs:
                    if pks and list(d.get("pk") or []) not in [list(p) for p in pks]:
                        continue
                    if d["type"] == "target_extra":
                        # 子集同步表禁用孤儿行类修复（目标多余行可能属于过滤范围外语义）
                        if table in row_filters:
                            skipped += 1
                            continue
                        if pkc is None:
                            skipped += 1
                            continue
                        with tgt_conn.cursor() as cur:
                            cur.execute(f"DELETE FROM `{tgt_table}` WHERE `{pkc}`=%s",
                                        (d["pk"][0],))
                        tgt_conn.commit()
                        deleted += 1
                    elif d["type"] == "target_missing":
                        # 行过滤子集表（E3/E14）：target_missing 很可能只是「被谓词排除的行」
                        # （verify 的行数/checksum 口径此前未按 row_filters 收窄），
                        # 禁止回写，否则 repair 会把被过滤行 REPLACE 进目标库，破坏子集语义
                        if table in row_filters:
                            skipped += 1
                            continue
                        row = fetch_one(src_conn,
                                        f"SELECT {collist} FROM `{resolver.source_schema}`.`{table}` "
                                        f"WHERE `{pkc}`=%s", (d["pk"][0],)) if pkc else None
                        if row is None:
                            skipped += 1
                            continue
                        self._replace_row(tgt_conn, tgt_table, cols, row)
                        replaced += 1
                    else:  # mismatch：以源库当前值覆盖
                        if pkc is None:
                            skipped += 1
                            continue
                        row = fetch_one(src_conn,
                                        f"SELECT {collist} FROM `{resolver.source_schema}`.`{table}` "
                                        f"WHERE `{pkc}`=%s", (d["pk"][0],))
                        if row is None:
                            # 源库已不存在（校验后又被删）→ 按源库现状删除目标行
                            with tgt_conn.cursor() as cur:
                                cur.execute(f"DELETE FROM `{tgt_table}` WHERE `{pkc}`=%s",
                                            (d["pk"][0],))
                            tgt_conn.commit()
                            deleted += 1
                            continue
                        self._replace_row(tgt_conn, tgt_table, cols, row)
                        replaced += 1
        finally:
            src_conn.close()
            tgt_conn.close()
        return {"verify_id": vid, "replaced": replaced, "deleted": deleted, "skipped": skipped}

    @staticmethod
    def _replace_row(tgt_conn, tgt_table: str, cols: list[str], row: dict) -> None:
        collist = ", ".join(f"`{c}`" for c in cols)
        tpl = ", ".join(["%s"] * len(cols))
        with tgt_conn.cursor() as cur:
            cur.execute(f"REPLACE INTO `{tgt_table}` ({collist}) VALUES ({tpl})",
                        tuple(row[c] for c in cols))
        tgt_conn.commit()

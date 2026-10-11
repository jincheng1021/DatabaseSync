"""环境预检服务（T1.6 / §4.1 / E1）。

逐项返回 {key, level: pass|warn|fail, message, hint, affected(权限不足将影响的功能)}。
结果持久化 datasource_precheck_result（E9）。
"""
from __future__ import annotations

import logging

import pymysql

from app.services.mysql_client import MySQLTarget, fetch_all, fetch_one, fetch_variables, open_connection

log = logging.getLogger("dsync.precheck")


def _item(key: str, level: str, message: str, hint: str | None = None, affected: list[str] | None = None) -> dict:
    return {"key": key, "level": level, "message": message, "hint": hint, "affected": affected or []}


def _has_priv(conn, priv: str) -> bool:
    """SHOW GRANTS 文本匹配判断权限（粗粒度；ALL PRIVILEGES 视为全含）。"""
    rows = fetch_all(conn, "SHOW GRANTS FOR CURRENT_USER()")
    text = " ".join(str(v) for r in rows for v in r.values()).upper()
    return "ALL PRIVILEGES" in text or priv.upper() in text


def run_precheck(t: MySQLTarget, role: str) -> tuple[str, list[dict]]:
    """role: source | target | both（both=源+目标，M1 数据源不区分角色，按双角色全量检查）。"""
    items: list[dict] = []

    # ============ 通用项 ============
    try:
        conn = open_connection(t)
    except pymysql.err.OperationalError as e:
        code, msg = e.args[0], e.args[1] if len(e.args) > 1 else ""
        if code in (1045,):
            return "fail", [_item("auth", "fail", "认证失败：用户名或密码错误", "检查账号密码；8.0 请确认账号主机段授权")]
        return "fail", [
            _item("tcp", "fail", f"无法连接 {t.host}:{t.port} —— {msg}", "检查网络可达性、端口、防火墙"),
            _item("auth", "fail", "连接未建立，认证项未检测"),
        ]

    try:
        vars_ = fetch_variables(conn)
        version = vars_.get("version", "")
        major = int(version.split(".")[0]) if version else 0
        is_80 = major >= 8

        if major not in (5, 8):
            items.append(_item("version", "warn", f"版本 {version} 非标（预期 5.7/8.0）", "仅对 5.7/8.0 提供完整兼容保障"))
        else:
            items.append(_item("version", "pass", f"MySQL {version}"))

        # 认证通过即记录
        items.append(_item("auth", "pass", f"认证通过（账号 {t.username}@{t.host}）"))

        sql_mode = vars_.get("sql_mode", "")
        items.append(_item("sql_mode", "pass", f"sql_mode = {sql_mode or '(空)'}",
                           hint="与对端差异过大时预检会提示"))
        charset_server = vars_.get("character_set_server", "")
        charset_db = vars_.get("character_set_database", "")
        conn_cs = (t.charset or "").strip().lower()
        mismatch = bool(conn_cs and charset_server and conn_cs != charset_server.strip().lower())
        cs_level = "warn" if (charset_server not in ("utf8mb4", "utf8") or mismatch) else "pass"
        cs_hint: str | None = None
        if charset_server and charset_server not in ("utf8mb4", "utf8"):
            cs_hint = "服务器默认字符集建议 utf8mb4；与源库字符集不一致可能导致数据漂移"
        elif mismatch:
            cs_hint = (f"数据源配置的连接字符集（{conn_cs}）与服务器默认字符集（{charset_server}）不一致，"
                       "连接会话以配置值为准；请确认与库/表实际字符集匹配，否则可能引入转码")
        items.append(_item("charset", cs_level,
                           f"连接字符集 = {conn_cs or '(未配置)'} · character_set_server = {charset_server or '(未取到)'}"
                           f" · character_set_database = {charset_db or '(未取到)'}",
                           hint=cs_hint))
        packet = int(vars_.get("max_allowed_packet", "0") or 0)
        items.append(_item("max_allowed_packet", "pass" if packet >= 64 * 1024 * 1024 else "warn",
                           f"max_allowed_packet = {packet // 1024 // 1024}MB",
                           hint="低于 64MB 时大批量写入可能受影响"))

        # ============ 源库项 ============
        if role in ("source", "both"):
            log_bin = vars_.get("log_bin", "OFF")
            items.append(_item("log_bin", "pass" if log_bin == "ON" else "fail",
                               f"log_bin = {log_bin}", "my.cnf 配置 log_bin=ON 后重启",
                               affected=["增量同步（CDC）"] if log_bin != "ON" else []))
            fmt = vars_.get("binlog_format", "")
            items.append(_item("binlog_format", "pass" if fmt == "ROW" else "fail",
                               f"binlog_format = {fmt or '(未设置)'}", "SET GLOBAL binlog_format=ROW",
                               affected=["增量同步"] if fmt != "ROW" else []))
            row_image = vars_.get("binlog_row_image", "")
            items.append(_item("binlog_row_image", "pass" if row_image == "FULL" else "fail",
                               f"binlog_row_image = {row_image or '(未设置)'}",
                               "SET GLOBAL binlog_row_image=FULL（REPLACE 幂等写依赖完整镜像）",
                               affected=["增量同步"] if row_image != "FULL" else []))
            gtid = vars_.get("gtid_mode", "OFF")
            items.append(_item("gtid_mode", "pass" if gtid == "ON" else "warn",
                               f"gtid_mode = {gtid}",
                               "建议开启 GTID（自动位点定位更可靠）；未开启时使用 file+position"))
            server_id = vars_.get("server_id", "0")
            items.append(_item("server_id", "pass" if server_id != "0" else "warn",
                               f"server_id = {server_id}", "server_id=0 时工具将以独立 id 订阅，避免冲突"))

            # E15：多唯一索引表提示（REPLACE 先删后插语义可能因任一唯一键冲突删除多行）
            try:
                uni_rows = fetch_all(conn,
                    "SELECT TABLE_SCHEMA, TABLE_NAME, COUNT(DISTINCT INDEX_NAME) AS uni_cnt "
                    "FROM information_schema.STATISTICS "
                    "WHERE NON_UNIQUE=0 AND INDEX_NAME <> 'PRIMARY' "
                    "AND TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema') "
                    "GROUP BY TABLE_SCHEMA, TABLE_NAME HAVING uni_cnt >= 2 ORDER BY uni_cnt DESC LIMIT 10")
                if uni_rows:
                    names = ", ".join(f"{r['TABLE_SCHEMA']}.{r['TABLE_NAME']}({r['uni_cnt']})"
                                      for r in uni_rows[:5])
                    items.append(_item("multi_unique_index", "warn",
                                       f"检测到多唯一索引表（{names} 等 {len(uni_rows)} 张）",
                                       "REPLACE 幂等写为「先删后插」语义，任一唯一键冲突可能删除多行、"
                                       "删除范围可能超出源库变更语义（E15 已知限制）；"
                                       "目标库只读副本定位下风险可控，校验服务可兜底发现差异",
                                       affected=["增量同步（涉及上述表时）"]))
                else:
                    items.append(_item("multi_unique_index", "pass", "无多唯一索引表"))
            except Exception:
                pass

            # 权限清单（E1：RELOAD 为 FTWRL 取一致位点必需）
            for priv, desc, affected_funcs in [
                ("SELECT", "SELECT", ["全量拷贝", "校验"]),
                ("RELOAD", "RELOAD（FTWRL 取一致位点必需）", ["新库初始化", "全量重刷取位点", "备份导出"]),
                ("REPLICATION SLAVE", "REPLICATION SLAVE", ["增量同步（CDC）"]),
                ("REPLICATION CLIENT", "REPLICATION CLIENT", ["增量同步（CDC）"]),
                ("SHOW VIEW", "SHOW VIEW", ["结构迁移（视图）"]),
                ("PROCESS", "PROCESS", ["锁等待诊断（E7 阻塞摘要）"]),
            ]:
                ok = _has_priv(conn, priv)
                items.append(_item(f"priv_{priv.replace(' ', '_').lower()}",
                                   "pass" if ok else "fail", f"权限 {desc}",
                                   f"GRANT {priv} ON *.* TO 同步账号" if not ok else None,
                                   affected=[] if ok else affected_funcs))

            # 存储过程/函数定义读取权限
            if is_80:
                has_routine = _has_priv(conn, "SHOW_ROUTINE")
                items.append(_item("priv_show_routine", "pass" if has_routine else "warn",
                                   "SHOW_ROUTINE 权限（读取存储过程/函数定义）",
                                   "GRANT SHOW_ROUTINE ON *.* TO 同步账号" if not has_routine else None,
                                   affected=[] if has_routine else ["存储过程/函数迁移"]))
            else:
                ok = _has_priv(conn, "SELECT")  # mysql.proc SELECT 以权限探测执行
                try:
                    fetch_one(conn, "SELECT COUNT(*) AS n FROM mysql.proc LIMIT 1")
                    proc_ok = True
                except Exception:
                    proc_ok = False
                items.append(_item("priv_mysql_proc", "pass" if proc_ok else "warn",
                                   "mysql.proc 读取权限（5.7 存储过程/函数定义）",
                                   "GRANT SELECT ON mysql.proc TO 同步账号" if not proc_ok else None,
                                   affected=[] if proc_ok else ["存储过程/函数迁移"]))

            # 事件定义权限
            try:
                fetch_one(conn, "SELECT COUNT(*) AS n FROM information_schema.EVENTS LIMIT 1")
                evt_ok = True
            except Exception:
                evt_ok = False
            items.append(_item("priv_event", "pass" if evt_ok else "warn",
                               "事件定义读取权限（EVENT）",
                               "GRANT EVENT ON 相关库.* TO 同步账号" if not evt_ok else None,
                               affected=[] if evt_ok else ["定时事件迁移"]))

            # binlog 保留时长
            keep = vars_.get("binlog_expire_logs_seconds") or vars_.get("expire_logs_days")
            try:
                keep_hours = float(keep)
                if keep < 1000:  # expire_logs_days 单位为天
                    keep_hours *= 24
                items.append(_item("binlog_retention",
                                   "pass" if keep_hours >= 24 else "warn",
                                   f"binlog 保留约 {keep_hours:.0f} 小时",
                                   "低于 24 小时：停机超过保留期会导致位点失效、需重新初始化" if keep_hours < 24 else None))
            except (TypeError, ValueError):
                pass

            # 8.0 认证插件检查（caching_sha2 + 非 SSL → 失败）
            if is_80:
                row = fetch_one(conn, "SELECT plugin FROM mysql.user WHERE user=%s AND host='%%' LIMIT 1", (t.username,))
                if not row:
                    rows = fetch_all(conn, "SELECT plugin, host FROM mysql.user WHERE user=%s", (t.username,))
                    row = rows[0] if rows else None
                plugin = (row or {}).get("plugin", "")
                # P2 修复：8.0 已移除 have_ssl 系统变量（恒取默认 YES，判定失真），
                # 改用 SHOW STATUS LIKE 'Ssl_cipher' 实测当前连接是否真在走 SSL
                ssl_row = fetch_one(conn, "SHOW STATUS LIKE 'Ssl_cipher'")
                ssl_in_use = bool(ssl_row and ssl_row.get("Value")) and t.ssl_mode != "disabled"
                if plugin == "caching_sha2_password" and not ssl_in_use:
                    items.append(_item("auth_plugin", "fail",
                                       f"账号认证插件为 caching_sha2_password 且未启用 SSL",
                                       "启用 SSL，或改用 mysql_native_password 专用同步账号：ALTER USER ... IDENTIFIED WITH mysql_native_password BY ...",
                                       affected=["增量同步（binlog dump 将失败）"]))
                else:
                    items.append(_item("auth_plugin", "pass", f"认证插件 {plugin or '默认'}（SSL={'启用' if ssl_in_use else '未启用'}）"))

        # ============ 目标库项 ============
        if role in ("target", "both"):
            for priv, desc in [
                ("INSERT", "INSERT"), ("UPDATE", "UPDATE"), ("DELETE", "DELETE"),
                ("CREATE", "CREATE"), ("ALTER", "ALTER"), ("DROP", "DROP"), ("INDEX", "INDEX"),
            ]:
                ok = _has_priv(conn, priv)
                items.append(_item(f"tgt_priv_{priv.lower()}", "pass" if ok else "fail",
                                   f"目标库权限 {desc}",
                                   f"GRANT {priv} ON 目标库.* TO 同步账号" if not ok else None,
                                   affected=[] if ok else ["数据写入/结构迁移"]))

            # 外键级联检测
            try:
                fk_rows = fetch_all(conn,
                    "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE DELETE_RULE IN ('CASCADE','SET NULL') OR UPDATE_RULE IN ('CASCADE','SET NULL') LIMIT 20")
                if fk_rows:
                    names = ", ".join(f"{r['TABLE_NAME']}.{r['CONSTRAINT_NAME']}" for r in fk_rows[:5])
                    items.append(_item("fk_cascade", "warn",
                                       f"检测到级联外键（{names} 等 {len(fk_rows)} 项）",
                                       "同步期间将关闭外键检查；级联规则不会在目标库生效，父子表数据由源库 binlog 全量镜像，请确认语义"))
                else:
                    items.append(_item("fk_cascade", "pass", "无级联外键"))
            except Exception:
                pass

        # 磁盘余量（目标库视角提示）
        try:
            row = fetch_one(conn, "SELECT ROUND(SUM(data_length+index_length)/1024/1024/1024,2) AS gb "
                                  "FROM information_schema.TABLES WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema')")
            gb = float((row or {}).get("gb") or 0)
            items.append(_item("disk_hint", "warn" if gb > 40 else "pass",
                               f"当前数据体量约 {gb:.2f} GB（不含系统库）",
                               "请确认服务器磁盘余量充足（建议 ≥ 数据体量 × 2）" if gb > 40 else None))
        except Exception:
            pass

    finally:
        try:
            conn.close()
        except Exception:
            pass

    overall = "pass"
    if any(i["level"] == "fail" for i in items):
        overall = "fail"
    elif any(i["level"] == "warn" for i in items):
        overall = "warn"
    return overall, items


def list_tables(t: MySQLTarget, schema: str | None = None) -> list[dict]:
    """库/表/列元数据（供后续映射配置界面，T1.5）。"""
    conn = open_connection(t)
    try:
        schemas = fetch_all(conn,
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN ('mysql','sys','information_schema','performance_schema')")
        names = [schema] if schema else [r["SCHEMA_NAME"] for r in schemas]
        result: list[dict] = []
        for s in names:
            tables = fetch_all(conn,
                "SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'", (s,))
            for tb in tables:
                cols = fetch_all(conn,
                    "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, IS_NULLABLE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (s, tb["TABLE_NAME"]))
                result.append({"schema": s, "table": tb["TABLE_NAME"],
                               "est_rows": tb["TABLE_ROWS"], "comment": tb["TABLE_COMMENT"],
                               "columns": cols})
        return result
    finally:
        conn.close()

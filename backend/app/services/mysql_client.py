"""MySQL 连接封装（T1.5/T1.6 基础）：同步 PyMySQL，供预检与表元数据使用。

凭据一律运行时解密，不落日志。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pymysql


@dataclass
class MySQLTarget:
    host: str
    port: int
    username: str
    password: str | None
    ssl_mode: str = "disabled"
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    default_schema: str | None = None
    read_timeout: int = 30   # 引擎链路经 _tuned() 调优（GLOBAL_DEFAULTS.db_read_timeout_seconds）
    write_timeout: int = 30


def open_connection(t: MySQLTarget, database: str | None = None) -> pymysql.connections.Connection:
    # M1 支持关闭/仅加密两种模式；CA/双向证书在凭据齐备后由设置页导入（M2 细化）
    return pymysql.connect(
        host=t.host, port=t.port, user=t.username, password=t.password or "",
        database=database or t.default_schema or None,
        charset=t.charset, connect_timeout=t.connect_timeout,
        read_timeout=t.read_timeout, write_timeout=t.write_timeout,
        cursorclass=pymysql.cursors.DictCursor,
        ssl_disabled=(t.ssl_mode == "disabled"),
    )


def open_connection_with_retry(t: MySQLTarget, database: str | None = None,
                               *, attempts: int = 3, backoff: float = 1.0) -> pymysql.connections.Connection:
    """建连重试：OperationalError（含握手读包超时 2013——pymysql connect 的握手
    用 read_timeout 而非 connect_timeout，低性能库 30s 内未吐握手包即断）退避重试 1s/2s。"""
    for i in range(max(1, attempts)):
        try:
            return open_connection(t, database)
        except pymysql.err.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(backoff * (2 ** i))
    raise RuntimeError("unreachable")  # pragma: no cover


def fetch_variables(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SHOW VARIABLES")
        return {r["Variable_name"]: str(r["Value"]) for r in cur.fetchall()}


def fetch_one(conn, sql: str, args: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def fetch_all(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def fetch_all_lc(conn, sql: str, args: tuple = ()) -> list[dict]:
    """fetch_all + 结果列名统一小写。

    information_schema 列名大小写随服务端而异：MySQL 按查询书写原样返回，
    MariaDB / 部分变体恒返回大写（如 TABLE_NAME）。统一小写后按小写键取值。
    """
    return [{str(k).lower(): v for k, v in r.items()} for r in fetch_all(conn, sql, args)]


def db_exists(conn, schema: str) -> bool:
    """目标连接上判断库是否存在（information_schema.SCHEMATA 单查）。"""
    if not schema:
        return False
    rows = fetch_all(conn, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                           "WHERE SCHEMA_NAME=%s", (schema,))
    return bool(rows)


def probe_databases(target: MySQLTarget, schemas: list[str]) -> dict[str, dict]:
    """批量探测目标实例上各库的存在性 + 基表数量（批量创建/调度路由共用）。

    低性能库友好：整个探测仅开 1 条连接、每 64 个库 2 条聚合 SQL，
    不逐库建连。返回 {schema: {"exists": bool, "tables": int}}，查不到 = 不存在。
    """
    out: dict[str, dict] = {s: {"exists": False, "tables": 0} for s in schemas}
    uniq = sorted({s for s in schemas if s})
    if not uniq:
        return out
    conn = open_connection(target)
    try:
        for i in range(0, len(uniq), 64):
            chunk = uniq[i:i + 64]
            ph = ",".join(["%s"] * len(chunk))
            for r in fetch_all_lc(conn,
                    f"SELECT SCHEMA_NAME AS n FROM information_schema.SCHEMATA "
                    f"WHERE SCHEMA_NAME IN ({ph})", tuple(chunk)):
                name = r.get("n")
                if name in out:
                    out[name]["exists"] = True
            for r in fetch_all_lc(conn,
                    f"SELECT table_schema AS s, COUNT(*) AS c FROM information_schema.tables "
                    f"WHERE table_type='BASE TABLE' AND table_schema IN ({ph}) "
                    f"GROUP BY table_schema", tuple(chunk)):
                name = r.get("s")
                if name in out:
                    out[name]["tables"] = int(r.get("c") or 0)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out

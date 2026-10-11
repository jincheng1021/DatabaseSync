"""E6 结构指纹 + T2.4 两级限流器 + T2.9 备份格式切分测试。"""
from __future__ import annotations

import time

from app.core.limiter import TokenBucket, TwoLevelLimiter
from app.services.backup_service import BackupService, split_sql_statements
from app.services.structure_fingerprint import clean_definer, fingerprint, normalize_ddl

DDL_A = """CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=1024 DEFAULT CHARSET=utf8mb4"""
DDL_A_DIFFERENT_AUTOINC = DDL_A.replace("AUTO_INCREMENT=1024", "AUTO_INCREMENT=99999")
DDL_A_ALTERED = DDL_A.replace("varchar(64)", "varchar(128)")


# ---------------- E6 结构指纹 ----------------
def test_fingerprint_ignores_auto_increment():
    assert fingerprint(DDL_A) == fingerprint(DDL_A_DIFFERENT_AUTOINC)


def test_fingerprint_detects_alter():
    assert fingerprint(DDL_A) != fingerprint(DDL_A_ALTERED)


def test_normalize_strips_definer():
    ddl = "CREATE DEFINER=`root`@`localhost` PROCEDURE `p1`() BEGIN END"
    assert "DEFINER" not in normalize_ddl(ddl)
    assert "DEFINER" not in clean_definer(ddl)


def test_fingerprint_stable():
    assert fingerprint(DDL_A) == fingerprint(" ".join(DDL_A.split()))


# ---------------- 两级限流器（E16） ----------------
def test_token_bucket_zero_rate_unlimited():
    b = TokenBucket(0)
    assert b.acquire(10**9) == 0.0


def test_token_bucket_rate():
    b = TokenBucket(10)
    assert b.acquire(5) == 0.0              # 初始容量直接放行
    wait = b.acquire(10)
    assert wait > 0                          # 超出容量需等待
    assert 0 < wait <= 1.5


def test_two_level_takes_max_wait():
    lim = TwoLevelLimiter(global_rate=0)
    lim.configure_task(1, 10)
    lim._task_buckets[1].tokens = 0
    w = lim.acquire(1, 20)
    assert w > 0


def test_two_level_global_gate():
    lim = TwoLevelLimiter(global_rate=100)
    lim.configure_task(1, 0)                 # 任务不限
    assert lim.acquire(1, 500) > 0           # 全局总闸仍限


def test_limiter_acquire_blocking_throttles_and_returns():
    """P2-5 回归：阻塞式取令牌真正节流；批大于桶容量分片取，不死循环。"""
    lim = TwoLevelLimiter(global_rate=1000)
    lim.configure_task(1, 0)                 # 任务不限，仅全局总闸（容量 1000）
    t0 = time.monotonic()
    lim.acquire_blocking(1, 2500)            # 3 片，约 1.5s 节流后返回
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed <= 4.0


def test_limiter_acquire_blocking_unlimited_returns_immediately():
    lim = TwoLevelLimiter(global_rate=0)
    lim.configure_task(1, 0)
    t0 = time.monotonic()
    lim.acquire_blocking(1, 10**9)
    assert time.monotonic() - t0 < 0.5


# ---------------- 备份语句切分（恢复回放依赖） ----------------
def test_split_simple():
    assert split_sql_statements("SET NAMES utf8mb4;\nDROP TABLE `a`;") == [
        "SET NAMES utf8mb4", "DROP TABLE `a`"]


def test_split_with_quotes_and_escapes():
    sql = "INSERT INTO `t` VALUES ('a;b''c', \"x\\\"y\");"
    parts = split_sql_statements(sql)
    assert len(parts) == 1
    assert "'a;b''c'" in parts[0]


def test_split_multiline_insert():
    sql = "INSERT INTO `t` (`a`) VALUES\n(1),\n(2);\n\nINSERT INTO `t` VALUES (3);"
    parts = split_sql_statements(sql)
    assert len(parts) == 2
    assert "(2)" in parts[0]


def test_split_comments():
    sql = "-- header\nSET NAMES utf8;\n# note\nCREATE TABLE `a` (id int);"
    parts = split_sql_statements(sql)
    assert parts == ["SET NAMES utf8", "CREATE TABLE `a` (id int)"]


def test_split_dangling_statement_kept():
    parts = split_sql_statements("INSERT INTO `t` VALUES (1);\nINSERT INTO `t` VALUES (2)")
    assert len(parts) == 2
    assert parts[1].startswith("INSERT")


def test_insert_sql_escaping():
    rows = [(1, "O'Neil", None, b"\x00abc")]
    sql = BackupService._insert_sql("t", ["id", "name", "note", "blob"], rows)
    assert "'O\\'Neil'" in sql
    assert "NULL" in sql
    assert sql.strip().endswith(";")
    # 可被我们的切分器还原为单条语句
    assert len(split_sql_statements(sql)) == 1


def test_insert_sql_binary_hex_literal():
    """B3 回归：二进制一律 X'...' 十六进制字面量——尾反斜杠/内含引号不再产生转义歧义。"""
    rows = [(1, b"abc\\", 2), (2, b"it's\xff\x00", 3)]
    sql = BackupService._insert_sql("t", ["id", "blob", "n"], rows)
    assert "X'" + b"abc\\".hex() + "'" in sql
    assert "X'" + b"it's\xff\x00".hex() + "'" in sql
    assert "_binary'" not in sql          # 旧格式已废弃
    # 同批两行合为一条 INSERT；十六进制内的 27(') 等字节不干扰语句边界解析
    assert len(split_sql_statements(sql)) == 1


def test_insert_sql_text_special_bytes():
    """B3 回归：文本 NUL(\\0) 与 Ctrl+Z(\\Z) 转义，避免回放截断。"""
    rows = [(1, "a\0b\x1ac")]
    sql = BackupService._insert_sql("t", ["id", "s"], rows)
    assert "a\\0b\\Zc" in sql
    assert len(split_sql_statements(sql)) == 1


# ---------------- P2-3 复合主键断点 ----------------
def test_pk_checkpoint_json_roundtrip():
    """P2-3 回归：主键 checkpoint 值 JSON 安全往返（datetime/bytes/Decimal 不崩不失真）。"""
    import json as _json
    from datetime import datetime as dt
    from decimal import Decimal

    from app.services.full_engine import FullEngine

    vals = [dt(2026, 1, 2, 3, 4, 5), b"\xff\x01", Decimal("3.14"), 7, "abc"]
    safe = [FullEngine._pk_to_json(v) for v in vals]
    roundtrip = _json.loads(_json.dumps(safe))          # 模拟落库→读回
    restored = [FullEngine._pk_from_json(v) for v in roundtrip]
    assert restored[0] == "2026-01-02 03:04:05"         # datetime → ISO 串（MySQL 隐式转换比较）
    assert restored[1] == b"\xff\x01"                   # bytes → hex 标记还原
    assert restored[2] == "3.14"
    assert restored[3:] == [7, "abc"]


def test_restore_expected_rows_empty_table_regression():
    """P0 回归：「成功(校验不一致)」误报。

    restore 收尾校验此前写法 `int(meta.get("rows") or -1)` 把合法空表 rows=0
    短路成 -1 → 备份含任意空表时恢复必然误报 mismatch。
    """
    from app.services.restore_service import expected_rows

    assert expected_rows({"rows": 0}) == 0          # 空表：0 是合法基准，必须参与校验
    assert expected_rows({"rows": 128}) == 128
    assert expected_rows({}) == -1                  # 未记录 → 不参与校验
    assert expected_rows({"rows": None}) == -1


def test_restore_stmt_table_parse():
    """回放明细跟踪的语句→表名解析（INSERT/CREATE/DROP/TRUNCATE/ALTER/SET）。"""
    from app.services.restore_service import stmt_table

    assert stmt_table("INSERT INTO `b_lw_user` (`id`) VALUES\n(1)") == "b_lw_user"
    assert stmt_table("INSERT IGNORE INTO t1 (a) VALUES (1)") == "t1"
    # 局限（可接受）：含空格的引号表名只解析出首段（本工具产物表名来自 SHOW TABLES，
    # 正常命名不含空格）
    assert stmt_table("REPLACE INTO `t 2` (a) VALUES (1)") == "t"
    assert stmt_table("CREATE TABLE `t3` (id INT)") == "t3"
    assert stmt_table("CREATE TABLE IF NOT EXISTS t4 (id INT)") == "t4"
    assert stmt_table("DROP TABLE IF EXISTS `t5`") == "t5"
    assert stmt_table("TRUNCATE `t6`") == "t6"
    assert stmt_table("ALTER TABLE `t7` ADD COLUMN c INT") == "t7"
    assert stmt_table("SET NAMES utf8mb4") is None
    assert stmt_table("SET SESSION sql_mode = ''") is None
    assert stmt_table("LOCK TABLES `t` WRITE") is None

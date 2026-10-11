"""SQLite 元数据仓库（T1.2）：WAL 模式 + 版本化迁移。

仓库层隔离：业务层一律经由 models/repository.py 访问，禁止直连 SQL。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH

_write_lock = threading.Lock()  # WAL 允许并发读，写串行化（§16：写集中、防锁库）


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class Database:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH

    @contextmanager
    def conn(self):
        c = connect(self.db_path)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @contextmanager
    def write_conn(self):
        with _write_lock:
            with self.conn() as c:
                yield c

    def init_schema(self) -> int:
        """执行版本化迁移，返回当前 schema 版本。"""
        applied = 0
        with self.write_conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))")
            row = c.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = row["v"] or 0
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                for sql in statements:
                    c.execute(sql)
                c.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                applied += 1
        return applied


# 版本化迁移：M1 建 4 张核心表（T1.2），其余表按里程碑在后续迁移号补齐。
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """CREATE TABLE datasource (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            db_type TEXT NOT NULL DEFAULT 'mysql',
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 3306,
            username TEXT NOT NULL,
            password_enc TEXT,
            ssl_mode TEXT NOT NULL DEFAULT 'disabled',
            ssl_ca_enc TEXT,
            ssl_cert_enc TEXT,
            ssl_key_enc TEXT,
            default_schema TEXT,
            charset TEXT NOT NULL DEFAULT 'utf8mb4',
            connect_timeout INTEGER NOT NULL DEFAULT 10,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE TABLE datasource_precheck_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datasource_id INTEGER NOT NULL UNIQUE REFERENCES datasource(id) ON DELETE CASCADE,
            checked_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            overall TEXT NOT NULL,
            items_json TEXT NOT NULL
        )""",
        """CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            ip TEXT,
            action TEXT NOT NULL,
            object_type TEXT,
            object_id TEXT,
            before_json TEXT,
            after_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE INDEX idx_audit_created ON audit_log(created_at)""",
        """CREATE TABLE app_setting (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )        """,
    ]),
    # ---- M2（T2.1/T2.9/T2.10）：任务、运行记录、位点、备份、DDL 待决策（§8） ----
    (2, [
        """CREATE TABLE sync_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_id INTEGER NOT NULL REFERENCES datasource(id),
            target_id INTEGER NOT NULL REFERENCES datasource(id),
            mode TEXT NOT NULL,               -- backup_only / init_plus_cdc / scheduled_full
            mapping_json TEXT NOT NULL DEFAULT '{}',
            migrate_triggers INTEGER NOT NULL DEFAULT 0,
            ddl_policy TEXT NOT NULL DEFAULT 'intercept',
            ddl_rules_json TEXT NOT NULL DEFAULT '{}',
            rate_limit_rows INTEGER NOT NULL DEFAULT 0,
            batch_size INTEGER NOT NULL DEFAULT 5000,
            batch_interval_ms INTEGER NOT NULL DEFAULT 0,
            queue_capacity INTEGER NOT NULL DEFAULT 50000,
            lag_alert_sec INTEGER NOT NULL DEFAULT 60,
            safety_net_backup INTEGER NOT NULL DEFAULT 1,
            orphan_cleanup INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE TABLE run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES sync_task(id) ON DELETE SET NULL,
            backup_id INTEGER,
            run_type TEXT NOT NULL,           -- init/full_resync/backup/restore/...
            trigger_type TEXT NOT NULL DEFAULT 'manual',  -- §8 trigger（SQLite 关键字改名列）
            state TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ended_at TEXT,
            rows_inserted INTEGER NOT NULL DEFAULT 0,
            rows_updated INTEGER NOT NULL DEFAULT 0,
            rows_deleted INTEGER NOT NULL DEFAULT 0,
            tables_total INTEGER NOT NULL DEFAULT 0,
            tables_done INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            log_path TEXT,
            report_json TEXT
        )""",
        """CREATE INDEX idx_run_task ON run_history(task_id, started_at)""",
        """CREATE TABLE run_table_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES run_history(id) ON DELETE CASCADE,
            table_name TEXT NOT NULL,
            rows_total INTEGER,
            rows_done INTEGER NOT NULL DEFAULT 0,
            slice_checkpoint_json TEXT,
            state TEXT NOT NULL DEFAULT 'pending',
            error TEXT
        )""",
        """CREATE INDEX idx_run_table ON run_table_detail(run_id)""",
        """CREATE TABLE binlog_position (
            task_id INTEGER PRIMARY KEY REFERENCES sync_task(id) ON DELETE CASCADE,
            gtid_executed TEXT,
            binlog_file TEXT,
            binlog_pos INTEGER,
            server_id_used INTEGER,
            last_commit_at TEXT,
            last_batch_stats_json TEXT
        )""",
        """CREATE TABLE backup_file (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            datasource_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL,
            scope_json TEXT NOT NULL DEFAULT '{}',
            rows_json TEXT,
            created_by TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            retention_tag TEXT NOT NULL DEFAULT 'manual'
        )""",
        """CREATE INDEX idx_backup_created ON backup_file(created_at)""",
        """CREATE TABLE ddl_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES sync_task(id) ON DELETE CASCADE,
            ddl_text TEXT NOT NULL,
            source_position TEXT,
            affected_tables_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            remember_choice INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_key TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'warning',
            task_id INTEGER,
            message TEXT NOT NULL,
            channel_results_json TEXT,
            fired_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            resolved_at TEXT
        )""",
        """CREATE INDEX idx_alert_key ON alert_log(alert_key, fired_at)""",
    ]),
    # ---- M3（T3.6/T3.8）：延迟历史曲线、重刷缓存队列磁盘溢写登记 ----
    (3, [
        """CREATE TABLE lag_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            lag_seconds REAL NOT NULL
        )""",
        """CREATE INDEX idx_lag_task_ts ON lag_history(task_id, ts)""",
        """CREATE TABLE resync_cache_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            spill_path TEXT,
            spilled_ops INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'caching',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        "ALTER TABLE sync_task ADD COLUMN return_state TEXT",
    ]),
    # ---- M4（T4.1/T4.2）：调度计划、校验结果 ----
    (4, [
        """CREATE TABLE schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES sync_task(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            action TEXT NOT NULL,             -- cdc_start/cdc_stop/full_resync/verify/backup_export/orphan_cleanup
            cron TEXT NOT NULL,               -- 五段 cron：分 时 日 月 周
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            enabled INTEGER NOT NULL DEFAULT 1,
            action_params_json TEXT NOT NULL DEFAULT '{}',
            overlap_policy TEXT NOT NULL DEFAULT 'skip',   -- skip（跳过并记录）/ queue（排队等待）
            misfire_policy TEXT NOT NULL DEFAULT 'skip',   -- skip（跳过）/ run_once（补跑一次）
            next_fire_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""",
        """CREATE INDEX idx_schedule_task ON schedule(task_id)""",
        """CREATE TABLE verify_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES sync_task(id) ON DELETE CASCADE,
            run_id INTEGER,
            method TEXT NOT NULL,             -- row_count / checksum
            state TEXT NOT NULL DEFAULT 'running',   -- running/success/failed
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            tables_total INTEGER NOT NULL DEFAULT 0,
            tables_checked INTEGER NOT NULL DEFAULT 0,
            mismatch_tables INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT,
            detail_json TEXT,                 -- 行级 diff（敏感列可配脱敏）
            started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ended_at TEXT
        )""",
        """CREATE INDEX idx_verify_task ON verify_result(task_id, started_at)""",
    ]),
]

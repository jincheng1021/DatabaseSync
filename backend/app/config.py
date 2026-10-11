"""应用配置：运行数据卷路径可配（T1.1）。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根 = backend/
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"

DATA_DIR = Path(os.environ.get("DSYNC_DATA_DIR", BASE_DIR / "data"))
KEYS_DIR = DATA_DIR / "keys"          # E4：主密钥独立子目录，与 SQLite 物理隔离
MASTER_KEY_PATH = KEYS_DIR / "master.key"
DB_PATH = DATA_DIR / "metadata.db"
LOG_DIR = Path(os.environ.get("DSYNC_LOG_DIR", BASE_DIR / "logs"))
BACKUP_DIR = Path(os.environ.get("DSYNC_BACKUP_DIR", BASE_DIR / "backups"))
FRONTEND_DIST_DIR = Path(os.environ.get("DSYNC_FRONTEND_DIST", BASE_DIR.parent / "frontend" / "dist"))

# ---- 会话与安全（§12） ----
SESSION_TIMEOUT_SECONDS = int(os.environ.get("DSYNC_SESSION_TIMEOUT", 2 * 60 * 60))  # 2 小时
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
BOOTSTRAP_ADMIN_USERNAME = "admin"

# ---- 运行日志（§4.7） ----
LOG_RETENTION_DAYS = int(os.environ.get("DSYNC_LOG_RETENTION_DAYS", 30))

# ---- 监听地址（仅裸跑 python main.py 生效；容器由 uvicorn CLI 参数决定） ----
HOST = os.environ.get("DSYNC_HOST", "0.0.0.0")
PORT = int(os.environ.get("DSYNC_PORT", 8080))

# ---- 全局默认参数（§9，落 app_setting，任务级可覆盖） ----
GLOBAL_DEFAULTS: dict[str, object] = {
    "full_table_concurrency": 4,          # 全量表级并发数 1~16
    "full_batch_size": 5000,              # 全量批大小（行/批）
    "cdc_commit_batch_rows": 1000,        # 增量提交批（条）
    "cdc_commit_interval_ms": 1000,       # 增量提交批（毫秒）
    "task_rate_limit_rows": 0,            # 任务级限流 0=不限
    "global_rate_limit_rows": 20000,      # 全局总闸（E16）
    "queue_capacity": 50000,              # 内存队列容量（事件）
    "big_tx_threshold_rows": 100000,      # 大事务阈值
    "lag_alert_seconds": 60,              # 延迟告警阈值
    "reconnect_backoff_max_seconds": 60,  # 重连退避上限
    "checksum_chunk_rows": 100000,        # checksum 分块
    "ftwrl_lock_wait_timeout": 10,        # FTWRL 锁等待超时（E7）
    "binlog_retention_warn_hours": 24,    # binlog 保留警告线
    "alert_silence_minutes": 30,          # 告警静默期
    "verify_mismatch_alert_ratio": 0.001, # 校验不一致率告警阈值（0.1%）
    "log_retention_days": 30,             # 日志保留
    "backup_min_free_bytes": 1073741824,  # 备份磁盘余量阈值（默认 1GB，低于拒绝）
    "backup_retention_per_tag": 20,       # 每 tag 保留最近 N 份
    "cdc_server_id_base": 30000,          # CDC 伪装从库 server_id 基数（server_id=base+task_id）
    "batch_init_parallel": 2,             # 批量初始化并发上限（低性能源库建议 1）
    "db_read_timeout_seconds": 120,       # 连接读超时（含 pymysql 握手读包；低性能库单批查询/commit 可超 30s）
    "db_write_timeout_seconds": 120,      # 连接写超时
}

# M1 允许的设置键（设置页只读展示）
SETTING_KEYS = set(GLOBAL_DEFAULTS.keys()) | {"alert_webhook_url", "alert_smtp_json", "master_key_fingerprint"}

for _d in (DATA_DIR, KEYS_DIR, LOG_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

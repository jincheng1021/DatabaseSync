"""删库重建回归（部署前验证）：SQLite 元数据库删除后，init_schema 自动重建全部表 + bootstrap 窗口语义。

背景：lifespan 启动钩子 db.init_schema() 按版本化迁移（v1~v4）建表，
删除 metadata.db(+wal/shm) 后重启应完整恢复 schema 与默认参数；
管理员账号随库清空，需重新一次性 bootstrap（存在抢先初始化安全窗口）。
"""
from __future__ import annotations

import pytest

from app.core.errors import ConflictError
from app.core.sessions import AuthService, LoginGuard, SessionStore
from app.db.database import Database
from app.models.repository import DatasourceRepository, SettingRepository

# 与 MIGRATIONS v1~v4 建表一一对应（16 张）
EXPECTED_TABLES = {
    "schema_migrations",
    # v1
    "datasource", "datasource_precheck_result", "audit_log", "app_setting",
    # v2
    "sync_task", "run_history", "run_table_detail", "binlog_position",
    "backup_file", "ddl_pending", "alert_log",
    # v3
    "lag_history", "resync_cache_state",
    # v4
    "schedule", "verify_result",
}


def _table_names(db: Database) -> set[str]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r["name"] for r in rows}


def test_drop_db_auto_reinit(tmp_path):
    db_path = tmp_path / "meta.db"
    db = Database(db_path)
    applied_first = db.init_schema()
    assert applied_first == 4
    assert _table_names(db) == EXPECTED_TABLES

    # 模拟使用过：写入一条数据源
    ds_repo = DatasourceRepository(db)
    ds_repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"})

    # 删库（连同 WAL/SHM），模拟误删 metadata.db
    for suffix in ("", "-wal", "-shm"):
        p = tmp_path / f"meta.db{suffix}"
        p.unlink(missing_ok=True)

    # 重启等价：新建 Database 实例再次 init_schema，应自动完整重建
    db2 = Database(db_path)
    applied = db2.init_schema()
    assert applied == 4, "删库后首次 init_schema 应重放 v1~v4 全部迁移"
    assert _table_names(db2) == EXPECTED_TABLES

    with db2.conn() as c:
        ver = c.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        n_ds = c.execute("SELECT COUNT(*) AS n FROM datasource").fetchone()
    assert ver["v"] == 4
    assert n_ds["n"] == 0, "业务数据随库删除，不自动恢复"

    # 默认参数恢复链路：ensure_defaults 可在新库上正常落库
    settings = SettingRepository(db2)
    settings.ensure_defaults()
    assert settings.get("global_rate_limit_rows") == 20000


def test_drop_db_bootstrap_window(tmp_path):
    db_path = tmp_path / "meta.db"
    Database(db_path).init_schema()

    # 删库重建
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"meta.db{suffix}").unlink(missing_ok=True)
    db2 = Database(db_path)
    db2.init_schema()

    settings = SettingRepository(db2)
    assert settings.get("admin_password_hash") is None

    auth = AuthService(settings, SessionStore(), LoginGuard())
    assert auth.is_initialized() is False

    # 重建后可重新一次性 bootstrap；重复调用被拒（抢先初始化窗口语义不变）
    auth.bootstrap("admin", "reinit-pass-1")
    assert auth.is_initialized() is True
    with pytest.raises(ConflictError):
        auth.bootstrap("admin", "reinit-pass-2")


def test_fresh_boot_create_app_on_empty_db(tmp_path, monkeypatch):
    """P0 回归（真实启动路径）：空库上 create_app() 必须自举 schema。

    真实启动 = 模块导入即执行 create_app()（早于 lifespan 的 init_schema），
    而装配期已有 DB 查询（batch_init_parallel）。修复前：空库/删库重启直接
    sqlite3.OperationalError: no such table: app_setting。
    """
    import app.core.security as sec
    import app.db.database as dbmod
    import app.main as main_mod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "meta.db")
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(sec.MasterKeyManager.__init__, "__defaults__",
                        (tmp_path / "keys" / "master.key",))

    app = main_mod.create_app()  # 修复前此处抛 no such table: app_setting
    assert _table_names(app.state.db) == EXPECTED_TABLES
    with app.state.db.conn() as c:
        ver = c.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    assert ver["v"] == 4
    # 装配期设置查询可用（batch_init_parallel 读取链路）
    assert app.state.settings.get("batch_init_parallel") is None

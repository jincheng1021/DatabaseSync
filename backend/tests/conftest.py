"""M1 测试公共夹具：临时数据库 + 内存服务装配。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import CredentialCipher, MasterKeyManager  # noqa: E402
from app.db.database import Database  # noqa: E402
from app.models.repository import AuditRepository, DatasourceRepository, SettingRepository  # noqa: E402


@pytest.fixture()
def temp_db(tmp_path):
    db = Database(tmp_path / "meta.db")
    db.init_schema()
    return db


@pytest.fixture()
def key_mgr(tmp_path):
    return MasterKeyManager(tmp_path / "keys" / "master.key")


@pytest.fixture()
def cipher(key_mgr):
    return CredentialCipher(key_mgr)


@pytest.fixture()
def repos(temp_db):
    return {
        "ds": DatasourceRepository(temp_db),
        "audit": AuditRepository(temp_db),
        "settings": SettingRepository(temp_db),
    }

# ==== M2 夹具（T2.1/T2.4/T2.9 测试基建） ====

from app.core.limiter import TwoLevelLimiter  # noqa: E402
from app.core.orchestrator import Orchestrator  # noqa: E402
from app.core.progress import ProgressTracker  # noqa: E402
from app.models.m2_repository import (BackupRepository, DdlPendingRepository,  # noqa: E402
                                      PositionRepository, RunRepository, TaskRepository)
from app.models.repository import AlertRepository  # noqa: E402


@pytest.fixture()
def m2_repos(temp_db):
    return {
        "tasks": TaskRepository(temp_db),
        "runs": RunRepository(temp_db),
        "positions": PositionRepository(temp_db),
        "backups": BackupRepository(temp_db),
        "ddl": DdlPendingRepository(temp_db),
        "alerts": AlertRepository(temp_db),
        "db": temp_db,
    }


@pytest.fixture()
def seed_ds(m2_repos):
    """FK 前置：两个数据源。"""
    db = m2_repos["db"]
    from app.models.repository import DatasourceRepository
    repo = DatasourceRepository(db)
    return [repo.create({"name": "src", "host": "127.0.0.1", "port": 3306, "username": "u"}),
            repo.create({"name": "dst", "host": "127.0.0.1", "port": 3307, "username": "u"})]


@pytest.fixture()
def orch(m2_repos):
    return Orchestrator(m2_repos["tasks"], broadcaster=None, run_repo=m2_repos["runs"])


@pytest.fixture()
def tracker():
    return ProgressTracker()


@pytest.fixture()
def limiter():
    return TwoLevelLimiter(global_rate=1000)


@pytest.fixture()
def a_task(m2_repos, orch, seed_ds):
    """已就绪的 init_plus_cdc 任务骨架（不触发引擎）。"""
    tasks = m2_repos["tasks"]
    tid = tasks.create({
        "name": "t1", "source_id": seed_ds[0], "target_id": seed_ds[1],
        "mode": "init_plus_cdc", "mapping": {"scope": "all"}, "state": "draft",
    })
    return tasks.get(tid)

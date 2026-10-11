"""T2.1 状态机测试：非法路径拒绝 + E5 保存即就绪 + DDL 待决策回迁。"""
from __future__ import annotations

import pytest

from app.core.errors import ConflictError
from app.core.orchestrator import (BACKING_UP, DRAFT, DDL_PENDING, ERROR, FULL_RESYNCING, IDLE,
                                   INITIALIZING)


def test_illegal_transition_rejected(orch, a_task):
    with pytest.raises(ConflictError):
        orch.transition(a_task["id"], BACKING_UP)          # init_plus_cdc 不允许直接进备份中
    with pytest.raises(ConflictError):
        orch.transition(a_task["id"], IDLE)                # 草稿不允许直接空闲


def test_init_path(orch, a_task):
    tid = a_task["id"]
    orch.transition(tid, INITIALIZING, reason="发起初始化")
    assert orch.tasks.get(tid)["state"] == INITIALIZING
    orch.transition(tid, IDLE, reason="初始化成功")
    assert orch.tasks.get(tid)["state"] == IDLE


def test_init_fail_to_error_and_recover(orch, a_task):
    tid = a_task["id"]
    orch.transition(tid, INITIALIZING)
    orch.transition(tid, ERROR, reason="初始化失败")
    orch.transition(tid, DRAFT, reason="人工复位")
    assert orch.tasks.get(tid)["state"] == DRAFT


def test_e5_save_as_ready(orch, m2_repos, seed_ds):
    """E5：scheduled_full 保存即就绪，首次触发按目标库空/非空路由。"""
    tasks = m2_repos["tasks"]
    tid = tasks.create({"name": "sf1", "source_id": seed_ds[0], "target_id": seed_ds[1],
                        "mode": "scheduled_full", "state": "draft"})
    orch.transition(tid, IDLE, reason="保存即就绪（E5）")
    assert tasks.get(tid)["state"] == IDLE
    target = orch.route_first_trigger(tid, target_db_is_empty=True)
    assert target == INITIALIZING
    # 非空库路由到重刷
    tid2 = tasks.create({"name": "sf2", "source_id": seed_ds[0], "target_id": seed_ds[1],
                         "mode": "scheduled_full", "state": "draft"})
    orch.transition(tid2, IDLE)
    assert orch.route_first_trigger(tid2, target_db_is_empty=False) == FULL_RESYNCING


def test_backup_only_lightweight_path(orch, m2_repos, seed_ds):
    tasks = m2_repos["tasks"]
    tid = tasks.create({"name": "b1", "source_id": seed_ds[0], "target_id": seed_ds[0],
                        "mode": "backup_only", "state": "draft"})
    orch.transition(tid, IDLE)
    orch.transition(tid, BACKING_UP)
    orch.transition(tid, IDLE)
    with pytest.raises(ConflictError):
        orch.transition(tid, INITIALIZING)   # backup_only 不允许进入初始化


def test_ddl_pending_enter_exit(orch, a_task):
    tid = a_task["id"]
    orch.transition(tid, INITIALIZING)
    ret = orch.enter_ddl_pending(tid, reason="结构指纹不一致")
    assert ret == INITIALIZING
    assert orch.tasks.get(tid)["state"] == DDL_PENDING
    orch.exit_ddl_pending(tid, reason="决策完成")
    assert orch.tasks.get(tid)["state"] == INITIALIZING


def test_transition_logged_to_run_history(orch, a_task, m2_repos):
    """§10/T5.1：每次状态迁移落 run_history（run_type=state_change），非法迁移不留痕。"""
    tid = a_task["id"]
    orch.transition(tid, INITIALIZING, reason="发起初始化")
    orch.transition(tid, ERROR, reason="初始化失败")
    runs, total = m2_repos["runs"].list(task_id=tid, run_type="state_change")
    assert total == 2
    assert [r["report"]["to"] for r in runs] == [ERROR, INITIALIZING]  # id DESC
    assert runs[0]["report"]["reason"] == "初始化失败"
    # 非法迁移被拒且不落历史
    from app.core.errors import ConflictError
    import pytest
    with pytest.raises(ConflictError):
        orch.transition(tid, BACKING_UP)
    _, total2 = m2_repos["runs"].list(task_id=tid, run_type="state_change")
    assert total2 == 2

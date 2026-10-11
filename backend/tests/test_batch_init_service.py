"""M6 批量初始化排队器测试：入队过滤、并发上限、失败不阻塞、status 快照。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import ConflictError
from app.services.batch_init_service import BatchInitService


class FakeEngine:
    """记录 start_init 调用与并发峰值；可注入失败行为。"""

    def __init__(self, conflict_ids=(), error_ids=(), hold=0.05):
        self.calls = []
        self.conflict_ids = set(conflict_ids)
        self.error_ids = set(error_ids)
        self.hold = hold
        self._active = 0
        self._lock = threading.Lock()
        self.peak = 0

    def start_init(self, tid, *, confirm_rebuild=False, confirm_name=None, trigger="manual"):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            self.calls.append(tid)
            if tid in self.conflict_ids:
                raise ConflictError("任务正在运行中")
            if tid in self.error_ids:
                raise RuntimeError("boom")
            time.sleep(self.hold)
        finally:
            with self._lock:
                self._active -= 1


class FakeAudit:
    def __init__(self):
        self.records = []

    def record(self, actor, action, **kw):
        self.records.append((actor, action, kw.get("object_id")))


def _mk_draft_tasks(tasks_repo, ds_ids, n, mode="init_plus_cdc", state="draft", prefix="t"):
    ids = []
    for i in range(n):
        ids.append(tasks_repo.create({
            "name": f"{prefix}-{mode}-{state}-{i}", "source_id": ds_ids[0],
            "target_id": ds_ids[1], "mode": mode, "mapping": {"scope": "all"},
            "state": state}))
    return ids


def test_enqueue_filters_non_draft(m2_repos, seed_ds):
    svc = BatchInitService(FakeEngine(), m2_repos["tasks"])
    draft = _mk_draft_tasks(m2_repos["tasks"], seed_ds, 2)
    idle = _mk_draft_tasks(m2_repos["tasks"], seed_ds, 1, state="idle")
    scheduled = _mk_draft_tasks(m2_repos["tasks"], seed_ds, 1, mode="scheduled_full",
                                state="idle")
    accepted = svc.enqueue(draft + idle + scheduled)
    assert accepted == draft
    assert svc.status()["queued"] == draft


def test_enqueue_missing_task_skipped(m2_repos, seed_ds):
    svc = BatchInitService(FakeEngine(), m2_repos["tasks"])
    assert svc.enqueue([99999]) == []


def test_parallel_cap_and_all_started(m2_repos, seed_ds):
    tasks = m2_repos["tasks"]
    ids = _mk_draft_tasks(tasks, seed_ds, 6)
    engine = FakeEngine(hold=0.08)
    svc = BatchInitService(engine, tasks)
    enqueued = svc.enqueue(ids, max_parallel=2)
    assert enqueued == ids
    deadline = time.time() + 10
    while len(engine.calls) < 6 and time.time() < deadline:
        time.sleep(0.02)
    assert len(engine.calls) == 6          # 全部发起，失败不阻塞
    assert engine.peak <= 2                # 并发峰值不超上限
    # 队列消化完毕
    deadline = time.time() + 5
    while svc.status()["running"] > 0 and time.time() < deadline:
        time.sleep(0.02)
    assert svc.status()["queued"] == []


def test_conflict_and_error_do_not_block_rest(m2_repos, seed_ds):
    tasks = m2_repos["tasks"]
    ids = _mk_draft_tasks(tasks, seed_ds, 4)
    engine = FakeEngine(conflict_ids={ids[0]}, error_ids={ids[2]}, hold=0.01)
    audit = FakeAudit()
    svc = BatchInitService(engine, tasks, audit=audit)
    svc.enqueue(ids, max_parallel=2)
    deadline = time.time() + 10
    while len(engine.calls) < 4 and time.time() < deadline:
        time.sleep(0.02)
    assert sorted(engine.calls) == sorted(ids)     # 失败项也被尝试过，后续未阻塞
    actions = {(r[1], r[2]) for r in audit.records}
    assert ("task.batch_init.skip", ids[0]) in actions
    assert ("task.batch_init.fail", ids[2]) in actions


def test_default_parallel_clamped(m2_repos, seed_ds):
    svc = BatchInitService(FakeEngine(), m2_repos["tasks"], default_parallel=99)
    assert svc.status()["default_parallel"] == 8
    svc2 = BatchInitService(FakeEngine(), m2_repos["tasks"], default_parallel=0)
    assert svc2.status()["default_parallel"] == 1

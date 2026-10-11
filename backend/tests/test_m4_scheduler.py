"""M4 调度服务测试（T4.1）：六类动作分发、overlap 并发策略、错过补偿、失败落历史+告警。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.errors import ConflictError, NotFoundError
from app.core.progress import ProgressTracker
from app.core.orchestrator import Orchestrator
from app.models.m2_repository import RunRepository, TaskRepository
from app.models.m4_repository import ScheduleRepository
from app.models.repository import AlertRepository, DatasourceRepository, SettingRepository
from app.services.scheduler_service import SchedulerService


class FakeCdc:
    def __init__(self):
        self.calls = []
        self._running = set()

    def is_running(self, tid):
        return tid in self._running

    def start(self, tid, trigger="manual"):
        self.calls.append(("start", tid, trigger))
        self._running.add(tid)
        return 777

    def stop(self, tid, timeout=30.0):
        self.calls.append(("stop", tid))
        self._running.discard(tid)


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.db_state = "nonempty"   # M6：target_db_state 探测结果可注入

    def target_db_state(self, tid):
        self.calls.append(("db_state", tid))
        return self.db_state

    def start_full_resync(self, tid, *, tables=None, safety_net=True,
                          orphan_cleanup=True, trigger="manual"):
        self.calls.append(("resync", tid, tables, trigger))
        return 888

    def start_init(self, tid, *, confirm_rebuild=False, confirm_name=None, trigger="manual"):
        self.calls.append(("init", tid, confirm_rebuild, trigger))
        return 887


class FakeOrphan:
    def __init__(self):
        self.calls = []

    def start(self, tid, *, trigger="manual"):
        self.calls.append(("orphan", tid, trigger))
        return 889


class FakeVerify:
    def __init__(self):
        self.calls = []

    def start(self, tid, *, method="checksum", trigger="manual", tables=None):
        self.calls.append(("verify", tid, method, trigger))
        return 890


class FakeAlert:
    def __init__(self):
        self.fired = []

    def fire(self, key, message, **kw):
        self.fired.append((key, message))
        return True


@pytest.fixture()
def svc(temp_db):
    tasks = TaskRepository(temp_db)
    runs = RunRepository(temp_db)
    schedules = ScheduleRepository(temp_db)
    settings = SettingRepository(temp_db)
    ds = DatasourceRepository(temp_db)
    src = ds.create({"name": "s", "host": "h", "port": 3306, "username": "u"})
    dst = ds.create({"name": "d", "host": "h", "port": 3307, "username": "u"})
    tid = tasks.create({"name": "t", "source_id": src, "target_id": dst,
                        "mode": "init_plus_cdc", "mapping": {"scope": "all"},
                        "state": "idle"})
    orch = Orchestrator(tasks, broadcaster=None)
    fake_cdc = FakeCdc()
    fake_alert = FakeAlert()
    s = SchedulerService(
        schedules=schedules, tasks=tasks, runs=runs, settings=settings, orch=orch,
        engine=FakeEngine(), cdc=fake_cdc, backup_service=None,
        orphan_service=FakeOrphan(), verify_service=FakeVerify(),
        alert_service=fake_alert)
    s.fake_cdc = fake_cdc
    s.fake_alert = fake_alert
    s.tid = tid
    return s


def _mk_schedule(svc, action="cdc_start", **kw):
    sid = svc.schedules.create({"task_id": svc.tid, "name": "plan", "action": action,
                                "cron": "0 2 * * *", "enabled": True, **kw})
    return sid


def test_dispatch_cdc_start_cron_trigger(svc):
    sid = _mk_schedule(svc)
    s = svc.schedules.get(sid)
    run_id = svc.runs.start(task_id=svc.tid, run_type=f"schedule_{s['action']}", trigger="cron")
    svc._execute(s, run_id)
    assert ("start", svc.tid, "cron") in svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["run_type"] == "schedule_cdc_start"
    assert runs[0]["state"] == "success"


def test_dispatch_cdc_start_already_running_records_success(svc):
    sid = _mk_schedule(svc)
    s = svc.schedules.get(sid)
    svc.fake_cdc._running.add(svc.tid)
    run_id = svc.runs.start(task_id=svc.tid, run_type="schedule_cdc_start", trigger="cron")
    svc._execute(s, run_id)
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "success"


def test_dispatch_full_resync_with_table_set(svc):
    sid = _mk_schedule(svc, action="full_resync", action_params={"tables": ["a", "b"]})
    s = svc.schedules.get(sid)
    run_id = svc.runs.start(task_id=svc.tid, run_type="schedule_full_resync", trigger="cron")
    svc._execute(s, run_id)
    assert ("resync", svc.tid, ["a", "b"], "cron") in svc.engine.calls


# ---- M6：full_resync 首触发按目标库状态路由 ----

def _dispatch_full_resync(svc):
    sid = _mk_schedule(svc, action="full_resync")
    s = svc.schedules.get(sid)
    run_id = svc.runs.start(task_id=svc.tid, run_type="schedule_full_resync", trigger="cron")
    svc._execute(s, run_id)
    return run_id


def test_dispatch_full_resync_missing_db_routes_to_init(svc):
    svc.engine.db_state = "missing"
    run_id = _dispatch_full_resync(svc)
    assert ("init", svc.tid, False, "cron") in svc.engine.calls
    assert not any(c[0] == "resync" for c in svc.engine.calls)
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "success"


def test_dispatch_full_resync_empty_db_routes_to_init(svc):
    svc.engine.db_state = "empty"
    _dispatch_full_resync(svc)
    assert ("init", svc.tid, False, "cron") in svc.engine.calls


def test_dispatch_full_resync_nonempty_db_goes_resync(svc):
    svc.engine.db_state = "nonempty"
    _dispatch_full_resync(svc)
    assert ("resync", svc.tid, None, "cron") in svc.engine.calls
    assert not any(c[0] == "init" for c in svc.engine.calls)


def test_dispatch_full_resync_probe_failure_falls_back_to_resync(svc, monkeypatch):
    def boom(tid):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(svc.engine, "target_db_state", boom)
    _dispatch_full_resync(svc)
    assert ("resync", svc.tid, None, "cron") in svc.engine.calls


def test_dispatch_orphan_and_verify_actions(svc):
    for action, marker in (("orphan_cleanup", "orphan"), ("verify", "verify")):
        sid = _mk_schedule(svc, action=action)
        s = svc.schedules.get(sid)
        run_id = svc.runs.start(task_id=svc.tid, run_type=f"schedule_{action}", trigger="cron")
        svc._execute(s, run_id)
    assert any(c[0] == "orphan" and c[2] == "cron" for c in svc.orphan_service.calls)
    assert any(c[0] == "verify" and c[3] == "cron" for c in svc.verify_service.calls)


def test_execute_failure_records_failed_and_alerts(svc, monkeypatch):
    sid = _mk_schedule(svc, action="full_resync")
    s = svc.schedules.get(sid)

    def boom(*a, **kw):
        raise ConflictError("任务正在运行中")
    monkeypatch.setattr(svc.engine, "start_full_resync", boom)
    run_id = svc.runs.start(task_id=svc.tid, run_type="schedule_full_resync", trigger="cron")
    svc._execute(s, run_id)
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "failed"
    assert "任务正在运行中" in runs[0]["error_summary"]
    assert svc.fake_alert.fired and "schedule_action_failed" in svc.fake_alert.fired[0][0]


def test_overlap_skip_records(svc):
    sid = _mk_schedule(svc)
    key = (svc.tid, "cdc_start")
    svc._running.add(key)                      # 模拟前次未完成
    svc._run_schedule(sid)
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "skipped"
    assert "跳过" in runs[0]["error_summary"]
    assert not svc.fake_cdc.calls              # 未真正执行


def test_overlap_queue_executes_after_drain(svc):
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"overlap_policy": "queue"})
    key = (svc.tid, "cdc_start")
    svc._running.add(key)
    svc._run_schedule(sid)                     # 入队
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "skipped"       # 排队先落一条「等待」记录
    assert "排队" in runs[0]["error_summary"]
    svc._running.discard(key)
    svc._drain_queue(key)
    assert ("start", svc.tid, "cron") in svc.fake_cdc.calls


def test_misfire_run_once_compensates(svc):
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"misfire_policy": "run_once",
                               "next_fire_at": "2020-01-01T00:00:00"})
    svc._misfire_check()
    assert ("start", svc.tid, "misfire_run") in svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert any(r["trigger"] == "misfire_run" for r in runs)


def test_misfire_skip_default(svc):
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"misfire_policy": "skip",
                               "next_fire_at": "2020-01-01T00:00:00"})
    svc._misfire_check()
    assert not svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert any(r["state"] == "skipped" for r in runs)
    # next_fire_at 已被重算到未来
    nfa = svc.schedules.get(sid)["next_fire_at"]
    assert nfa and nfa[:4] >= "2024"


def test_dispatch_rejects_unknown_action(svc):
    with pytest.raises(Exception):
        svc._dispatch_action(svc.tid, "hack_the_planet", {})


def test_misfire_tz_aware_next_fire_at_no_crash(svc):
    """P0-1 回归：_register 落库的 next_fire_at 是带时区 ISO 串，
    重启补偿检查不得因 aware/naive 比较抛 TypeError。"""
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"next_fire_at": "2020-01-01T00:00:00+08:00"})  # aware 且已过期
    svc._misfire_check()   # 修复前：TypeError 穿透；修复后：按 skip 记录
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert any(r["state"] == "skipped" for r in runs)


def test_misfire_tz_aware_future_no_compensate(svc):
    """aware 且未过期的 next_fire_at 不触发补偿。"""
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"next_fire_at": "2099-01-01T00:00:00+08:00"})
    svc._misfire_check()
    assert not svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert not runs


def test_reload_all_with_aware_next_fire_at_recovers(svc):
    """真实崩溃路径：启用计划 + aware next_fire_at，reload_all（重启恢复）必须可完成。"""
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"next_fire_at": "2020-01-01T00:00:00+08:00"})
    n = svc.reload_all()   # 修复前：TypeError → 应用启动失败
    assert n == 1
    assert svc.schedules.get(sid)["next_fire_at"][:4] >= "2024"  # 已重算未来


def test_run_schedule_survives_runs_start_failure(svc, monkeypatch):
    """B9 回归：runs.start 抛异常（如 SQLite 瞬时 readonly）后，
    key 不得滞留 _running，下一次触发必须照常执行。"""
    sid = _mk_schedule(svc)
    calls = {"n": 0}
    real_start = svc.runs.start

    def flaky_start(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk I/O error (readonly 抖动)")
        return real_start(*a, **kw)

    monkeypatch.setattr(svc.runs, "start", flaky_start)
    svc._run_schedule(sid)                     # 第一次：登记 run 失败
    key = (svc.tid, "cdc_start")
    assert key not in svc._running             # 修复前：永久滞留
    svc._run_schedule(sid)                     # 第二次：应照常执行
    assert ("start", svc.tid, "cron") in svc.fake_cdc.calls


def test_scheduler_pool_sized_for_blocking_actions(svc):
    """B9：默认执行器线程池必须大于 APScheduler 默认的 10，
    保证 backup_export 长导出占线时其余计划仍可触发。"""
    svc.start()
    try:
        pool = svc._scheduler._executors["default"]
        # APScheduler 3.11+ 会把配置的 concurrent.futures 池规范化为自有包装类
        # （仅暴露 ._pool）；直接配置时则是 concurrent.futures 池本身（._max_workers）
        inner = getattr(pool, "_pool", pool)
        assert getattr(inner, "_max_workers", 0) >= 20
    finally:
        svc.shutdown()


# ---- 手动立即触发（trigger=manual） ----

def test_trigger_now_runs_with_manual_trigger(svc):
    """trigger_now：调度器未启动时同步执行，全链路 trigger=manual。"""
    sid = _mk_schedule(svc)
    result = svc.trigger_now(sid)
    assert result["status"] == "accepted"
    assert result["task_id"] == svc.tid and result["action"] == "cdc_start"
    assert ("start", svc.tid, "manual") in svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert any(r["trigger"] == "manual" and r["state"] == "success" for r in runs)


def test_trigger_now_respects_overlap_skip(svc):
    sid = _mk_schedule(svc)
    svc._running.add((svc.tid, "cdc_start"))   # 模拟前次未完成
    svc.trigger_now(sid)
    assert not svc.fake_cdc.calls
    runs, _ = svc.runs.list(task_id=svc.tid)
    assert runs[0]["state"] == "skipped"
    assert runs[0]["trigger"] == "manual"


def test_trigger_now_respects_overlap_queue(svc):
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"overlap_policy": "queue"})
    key = (svc.tid, "cdc_start")
    svc._running.add(key)
    svc.trigger_now(sid)                       # 入队（记录 manual 来源）
    svc._running.discard(key)
    svc._drain_queue(key)
    assert ("start", svc.tid, "manual") in svc.fake_cdc.calls


def test_trigger_now_not_found(svc):
    with pytest.raises(NotFoundError):
        svc.trigger_now(99999)


def test_queue_limit_exceeded_records_skipped(svc, monkeypatch):
    """overlap=queue 排队达到上限后，后续触发按 skipped 落历史且不入队。"""
    sids = [_mk_schedule(svc, name=f"p{i}", overlap_policy="queue") for i in range(3)]
    monkeypatch.setattr(svc, "_queue_limit", lambda: 1)
    key = (svc.tid, "cdc_start")
    svc._running.add(key)
    for sid in sids:
        svc.trigger_now(sid)
    with svc._queued_lock:
        assert len(svc._queues.get(key, [])) == 1        # 只有第 1 条入队
    runs, _ = svc.runs.list(task_id=svc.tid)
    skipped = [r for r in runs if r["state"] == "skipped"]
    assert any("上限" in (r["error_summary"] or "") for r in skipped)


def test_remove_task_jobs_clears_jobs_and_queue(svc):
    """remove_task_jobs：移除 APScheduler job + 清空排队队列（任务删除路径）。"""
    sid = _mk_schedule(svc, overlap_policy="queue")
    svc.start()
    try:
        s = svc.schedules.get(sid)
        svc._register(s)
        assert svc._scheduler.get_job(f"schedule-{sid}") is not None
        key = (svc.tid, "cdc_start")
        with svc._queued_lock:
            svc._queues[key] = [(sid, "cron")]
        svc.remove_task_jobs(svc.tid)
        assert svc._scheduler.get_job(f"schedule-{sid}") is None
        with svc._queued_lock:
            assert key not in svc._queues
    finally:
        svc.shutdown()


def test_remove_task_jobs_tolerates_empty_list(svc):
    """CASCADE 已删光 schedule 行（列表为空）时仍不报错，队列照清。"""
    key = (svc.tid, "cdc_start")
    with svc._queued_lock:
        svc._queues[key] = [(123, "manual")]
    svc.remove_task_jobs(svc.tid)   # 无 schedule 行，不应抛异常
    with svc._queued_lock:
        assert key not in svc._queues


def test_misfire_async_when_started(svc, monkeypatch):
    """调度器已启动时，misfire 补跑走异步 job：next_fire_at 立即重算，
    补跑在调度线程池内完成（短轮询等待），不阻塞调用方。"""
    sid = _mk_schedule(svc)
    svc.schedules.update(sid, {"misfire_policy": "run_once"})
    # 延迟补跑写库：主线程的 _register 落库先行完成，规避 Windows 下两线程
    # 高频开闭 WAL 连接的瞬时 readonly 抖动（见工程备忘），同时保住真异步验证
    real_misfire = svc._execute_misfire

    def slow_misfire(s, run_id):
        time.sleep(0.3)
        real_misfire(s, run_id)
    monkeypatch.setattr(svc, "_execute_misfire", slow_misfire)
    svc.start()
    try:
        svc.schedules.update(sid, {"next_fire_at": "2020-01-01T00:00:00+08:00"})
        t0 = time.time()
        svc._misfire_check()
        # next_fire_at 已被立即重算到未来（不等补跑完成）→ 未被补跑阻塞
        nfa = svc.schedules.get(sid)["next_fire_at"]
        assert nfa and nfa[:4] >= "2024"
        # 返回时补跑尚未执行（job 还在延迟期）→ 确定性证明未阻塞（计时断言在满载下不稳）
        assert not any(c[0] == "start" for c in svc.fake_cdc.calls)
        assert time.time() - t0 < 5             # 兜底：远小于等待补跑完成所需时间
        # 异步补跑最终完成（trigger=misfire_run）
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(c[0] == "start" and c[2] == "misfire_run" for c in svc.fake_cdc.calls):
                break
            time.sleep(0.05)
        assert any(c[0] == "start" and c[2] == "misfire_run" for c in svc.fake_cdc.calls)
    finally:
        svc.shutdown()
        time.sleep(0.5)   # 等 wait=False 的线程池收尾，避免污染后续测试的 tmp 目录


# ---- M6 修复回归：backup_export 调度动作必须传 ds_row（NoneType 崩溃根因） ----

class StubBackupSvc:
    def __init__(self):
        self.kwargs = None

    def export(self, **kw):
        self.kwargs = kw
        return [1]


def test_dispatch_backup_export_passes_ds_row(svc):
    svc.backup_service = StubBackupSvc()
    sid = _mk_schedule(svc, action="backup_export")
    s = svc.schedules.get(sid)
    run_id = svc.runs.start(task_id=svc.tid, run_type="schedule_backup_export", trigger="cron")
    svc._execute(s, run_id)
    kw = svc.backup_service.kwargs
    assert kw is not None
    task = svc.tasks.get(svc.tid)
    assert kw["ds_row"]["source_id"] == task["source_id"]
    assert kw["ds_row"]["target_id"] == task["target_id"]

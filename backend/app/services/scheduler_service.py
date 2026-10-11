"""调度服务（T4.1 / §4.4）：APScheduler + 六类动作 + 时区 + 并发/错过策略。

- schedule 表为权威配置（应用重启后重建全部计划 → 自动恢复，无需外部 jobstore 持久化）。
- CronTrigger 五段 cron（分 时 日 月 周）+ 独立时区（E11，默认 Asia/Shanghai）。
- 六类动作统一走动作分发器 → 各引擎服务，run_history 落 run_type=schedule_*、trigger=cron。
- 并发控制：同任务同动作同时刻单实例；前次未完成本次【跳过并记录】（默认）或【排队等待】。
- 错过补偿：停机跨过触发点，重启后【跳过】（默认）或【补跑一次】；全部落执行历史。
"""
from __future__ import annotations

import logging
import threading
import time
from apscheduler.executors.pool import ThreadPoolExecutor  # APScheduler 自有实现（concurrent.futures 版不被接受）
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.errors import AppError, NotFoundError, ValidationError
from app.models.m2_repository import RunRepository, TaskRepository
from app.models.m4_repository import SCHEDULE_ACTIONS, ScheduleRepository
from app.models.repository import AuditRepository

log = logging.getLogger("dsync.scheduler")

DEFAULT_TZ = "Asia/Shanghai"

QUEUE_LIMIT_DEFAULT = 100   # overlap=queue 单键等待上限（settings: schedule_queue_limit 可覆盖）


def validate_cron(cron: str) -> None:
    parts = (cron or "").split()
    if len(parts) != 5:
        raise ValidationError("cron 须为五段表达式：分 时 日 月 周",
                              hint="示例：'0 2 * * *' = 每天 02:00")
    try:
        CronTrigger.from_crontab(cron)
    except ValueError as e:
        raise ValidationError(f"cron 表达式不合法：{e}") from e


def next_fire_times(cron: str, timezone: str, count: int = 5,
                    now: datetime | None = None) -> list[str]:
    """未来 N 次触发预览（按指定时区计算，E11 验收项）。"""
    validate_cron(cron)
    trigger = CronTrigger.from_crontab(cron, timezone=timezone)
    now = now or datetime.now(trigger.timezone)
    out: list[str] = []
    nxt = trigger.get_next_fire_time(None, now)
    while nxt is not None and len(out) < count:
        out.append(nxt.isoformat())
        nxt = trigger.get_next_fire_time(nxt, nxt)
    return out


class SchedulerService:
    def __init__(self, *, schedules: ScheduleRepository, tasks: TaskRepository,
                 runs: RunRepository, settings, orch, engine, cdc,
                 backup_service, orphan_service, verify_service, alert_service=None,
                 audit: AuditRepository | None = None, broadcaster=None):
        self.schedules = schedules
        self.tasks = tasks
        self.runs = runs
        self.settings = settings
        self.orch = orch
        self.engine = engine
        self.cdc = cdc
        self.backup_service = backup_service
        self.orphan_service = orphan_service
        self.verify_service = verify_service
        self.alert_service = alert_service
        self.audit = audit
        self.broadcaster = broadcaster
        self._scheduler: BackgroundScheduler | None = None
        self._lock = threading.Lock()
        self._running: set[tuple[int, str]] = set()          # (task_id, action) 单实例
        self._queues: dict[tuple[int, str], list[tuple[int, str]]] = {}  # overlap=queue 等待队列：[(sid, trigger)]
        self._queued_lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        # B9：默认池仅 10 线程——backup_export 在调度线程内同步执行（FTWRL+gzip），
        # 大库导出长期占线会拖垮其余计划的触发，显式扩容兜底。
        self._scheduler = BackgroundScheduler(
            timezone=DEFAULT_TZ,
            executors={"default": ThreadPoolExecutor(max_workers=20)})
        self._scheduler.start()
        self.reload_all()

    def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def reload_all(self) -> int:
        """应用重启后按 schedule 表重建全部计划（自动恢复）。返回注册数。"""
        n = 0
        for s in self.schedules.list(enabled_only=True):
            try:
                self._register(s)
                n += 1
            except Exception:  # noqa: BLE001 —— 单条配置损坏不阻断其余计划
                log.exception("调度计划注册失败 sid=%s", s["id"])
        self._misfire_check()
        return n

    def _register(self, s: dict) -> None:
        trigger = CronTrigger.from_crontab(s["cron"], timezone=s["timezone"] or DEFAULT_TZ)
        job_id = f"schedule-{s['id']}"
        if self._scheduler is not None:
            if self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
            self._scheduler.add_job(
                self._run_schedule, trigger, id=job_id,
                args=[s["id"]], name=f"{s['name']}({s['action']})",
                misfire_grace_time=None,      # 错过判定交给自实现补偿（misfire_policy）
                max_instances=1, coalesce=True)
        # 登记下次触发时间（供停机补偿判定与前端展示）
        nxt = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
        self.schedules.update(s["id"], {"next_fire_at": nxt.isoformat() if nxt else None})

    def _misfire_check(self) -> None:
        """停机跨过触发点补偿：next_fire_at 已过 → skip（默认）/ run_once（补跑一次）。

        注意：_register 落库的是带时区 ISO 串（aware），比较时必须按其时区取 now，
        否则 aware vs naive 抛 TypeError 且穿透 reload_all → start → lifespan（P0-1）。
        """
        for s in self.schedules.list(enabled_only=True):
            nfa = s.get("next_fire_at")
            if not nfa:
                continue
            try:
                nfa_dt = datetime.fromisoformat(nfa)
                # 按目标自身的时区语义取当前时间，保证比较两侧类型一致
                now = datetime.now(nfa_dt.tzinfo) if nfa_dt.tzinfo else datetime.now()
                missed = nfa_dt <= now
            except (ValueError, TypeError):
                continue
            if not missed:
                continue
            if s.get("misfire_policy") == "run_once":
                run_id = self._record_run(s, "misfire_run")
                self._record_dispatch(s, run_id)
                if self._scheduler is not None:
                    # 生产路径：补跑投递为一次性 job（调度线程池执行），多个补跑
                    # 与 backup_export 等长动作不再阻塞启动（lifespan）路径
                    self._scheduler.add_job(
                        self._execute_misfire, DateTrigger(run_date=datetime.now()),
                        id=f"misfire-{s['id']}-{time.time_ns()}",
                        args=[s, run_id], misfire_grace_time=None)
                else:
                    self._execute(s, run_id, trigger="misfire_run")   # 测试/未启动环境：同步补跑（保持历史时序确定）
                log.info("错过补偿：补跑一次 schedule=%s action=%s", s["name"], s["action"])
            else:
                self._record_skip(s, reason="停机跨过触发点，按 misfire=skip 跳过")
            self._register(s)   # 重算 next_fire_at

    # ---------- CRUD 联动 ----------
    def reload_schedule(self, sid: int) -> None:
        s = self.schedules.get(sid)
        if not s:
            raise NotFoundError(f"调度计划 {sid} 不存在")
        if self._scheduler is None:
            return
        if s["enabled"]:
            validate_cron(s["cron"])
            self._register(s)
        else:
            job_id = f"schedule-{sid}"
            if self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)

    def preview(self, cron: str, timezone: str = DEFAULT_TZ, count: int = 5) -> dict:
        try:
            return {"cron": cron, "timezone": timezone, "next": next_fire_times(cron, timezone, count)}
        except ValidationError as e:
            raise AppError(e.message, code=e.code, hint=e.hint, status_code=422) from e

    def remove_schedule(self, sid: int) -> None:
        """删除单条计划时移除对应 APScheduler job（供 API 层复用，勿直接摸 _scheduler）。"""
        if self._scheduler is not None:
            job_id = f"schedule-{sid}"
            if self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)

    def remove_task_jobs(self, task_id: int) -> None:
        """任务删除前调用：移除该任务全部计划的 APScheduler job 并清空排队队列。

        注意：DB 级 CASCADE 可能已删光 schedule 行（列表为空），此时仍需清队列；
        _running 中残留项由 _run_once 的 finally 自清（run_history.task_id 为
        ON DELETE SET NULL，finish 不会崩）。
        """
        for s in self.schedules.list(task_id=task_id):
            self.remove_schedule(s["id"])
        with self._queued_lock:
            for key in [k for k in self._queues if k[0] == task_id]:
                self._queues.pop(key, None)

    def trigger_now(self, sid: int) -> dict:
        """手动立即触发一次（trigger=manual）。停用计划也允许（显式用户意图）。

        overlap 语义不在此预判：一律投递，由 _run_once 在锁内权威判定
        （skip → 落 skipped 历史；queue → 入队，受上限约束）。
        """
        s = self.schedules.get(sid)
        if not s:
            raise NotFoundError(f"调度计划 {sid} 不存在")
        if self._scheduler is not None:
            # 生产路径：一次性立即 job（调度线程池执行），API 不被长动作阻塞
            self._scheduler.add_job(
                self._run_manual, DateTrigger(run_date=datetime.now()),
                id=f"manual-{sid}-{time.time_ns()}", misfire_grace_time=None)
        else:
            self._run_schedule(sid, trigger="manual")   # 测试/未启动环境：同步执行
        return {"status": "accepted", "schedule_id": sid,
                "task_id": s["task_id"], "action": s["action"]}

    def _run_manual(self, sid: int) -> None:
        self._run_schedule(sid, trigger="manual")

    def _execute_misfire(self, s: dict, run_id: int) -> None:
        """misfire 异步补跑 job 入口（生产路径一次性 job 调用）。"""
        self._execute(s, run_id, trigger="misfire_run")

    # ---------- 触发与分发 ----------
    def _queue_limit(self) -> int:
        """overlap=queue 单键等待上限（settings.schedule_queue_limit 可覆盖，None 回退默认）。"""
        try:
            v = self.settings.get("schedule_queue_limit")
            return int(v) if v else QUEUE_LIMIT_DEFAULT
        except Exception:  # noqa: BLE001 —— settings 不可用时回退默认，不影响触发
            return QUEUE_LIMIT_DEFAULT

    def _run_schedule(self, sid: int, trigger: str = "cron") -> None:
        """APScheduler 触发入口（线程池线程）。"""
        key = self._run_once(sid, trigger=trigger)
        if key is not None:
            self._drain_queue(key)

    def _run_once(self, sid: int, trigger: str = "cron") -> tuple[int, str] | None:
        """单次执行（不含排队出队）；返回任务 key 供出队循环，跳过/无效返回 None。"""
        s = self.schedules.get(sid)
        if not s or not s["enabled"]:
            return None
        key = (s["task_id"], s["action"])
        with self._lock:
            if key in self._running:
                if s["overlap_policy"] == "queue":
                    with self._queued_lock:
                        limit = self._queue_limit()
                        q = self._queues.setdefault(key, [])
                        if len(q) < limit:
                            q.append((sid, trigger))
                            queued = True
                        else:
                            queued = False
                    if not queued:
                        self._record_skip(s, reason=f"前次未完成且排队已达上限 {limit}，本次按 skipped 处理",
                                          trigger=trigger)
                        return None
                    self._record_skip(s, reason="前次未完成，按 overlap=queue 排队等待", trigger=trigger)
                    return None
                self._record_skip(s, reason="前次未完成，按 overlap=skip 跳过并记录", trigger=trigger)
                return None
            self._running.add(key)
        # B9：登记 run / 执行链全程保护——任一步抛异常（如 SQLite 瞬时 readonly），
        # key 绝不能滞留 _running，否则该 (task_id, action) 此后全部被误判「前次未完成」跳过。
        try:
            try:
                run_id = self._record_run(s, trigger)
                self._record_dispatch(s, run_id)
                self._execute(s, run_id, trigger=trigger)
            except Exception:  # noqa: BLE001 —— M2 教训：入口线程异常就地消化，不逃逸 APScheduler 工作线程
                log.exception("调度执行链异常 sid=%s key=%s", s["id"], key)
        finally:
            with self._lock:
                self._running.discard(key)
        return key

    def _drain_queue(self, key: tuple[int, str]) -> None:
        """overlap=queue 出队执行（P2 修复：迭代化，修复前递归调用 _run_schedule
        在长队列时深递归）。队列项为 (sid, trigger)，按各自触发来源执行。"""
        while True:
            with self._queued_lock:
                q = self._queues.get(key) or []
                item = q.pop(0) if q else None
                if not q:
                    self._queues.pop(key, None)
            if item is None:
                return
            sid, trigger = item
            log.info("overlap=queue 出队执行 sid=%s key=%s trigger=%s", sid, key, trigger)
            self._run_once(sid, trigger=trigger)

    def _record_run(self, s: dict, trigger: str) -> int:
        return self.runs.start(task_id=s["task_id"], run_type=f"schedule_{s['action']}",
                               trigger=trigger)

    def _record_dispatch(self, s: dict, run_id: int) -> None:
        log.info("调度触发：%s action=%s run=%s tz=%s", s["name"], s["action"], run_id, s["timezone"])

    def _record_skip(self, s: dict, *, reason: str, trigger: str = "cron") -> None:
        run_id = self.runs.start(task_id=s["task_id"], run_type=f"schedule_{s['action']}",
                                 trigger=trigger)
        self.runs.finish(run_id, state="skipped", error=reason,
                         report={"schedule_id": s["id"], "schedule_name": s["name"],
                                 "action": s["action"], "skipped": True, "reason": reason})
        log.info("调度跳过：%s action=%s（%s）", s["name"], s["action"], reason)

    def _execute(self, s: dict, run_id: int, trigger: str = "cron") -> None:
        """动作执行统一走任务编排器（引擎线程语义），异常→failed 落历史+告警。"""
        task_id, action, params = s["task_id"], s["action"], s["action_params"] or {}
        try:
            self._dispatch_action(task_id, action, params, trigger=trigger)
            self.runs.finish(run_id, state="success",
                             report={"schedule_id": s["id"], "action": action})
        except AppError as e:
            self.runs.finish(run_id, state="failed", error=e.message,
                             report={"schedule_id": s["id"], "action": action})
            self._alert(task_id, f"schedule_action_failed:{task_id}:{action}",
                        f"调度动作 {s['name']}（{action}）执行失败：{e.message}")
        except Exception as e:  # noqa: BLE001 —— 引擎线程兜底（M2 踩坑：异常不逃逸线程）
            log.exception("调度动作执行失败 sid=%s run=%s", s["id"], run_id)
            self.runs.finish(run_id, state="failed", error=str(e),
                             report={"schedule_id": s["id"], "action": action})
            self._alert(task_id, f"schedule_action_failed:{task_id}:{action}",
                        f"调度动作 {s['name']}（{action}）执行失败：{e}")

    def _alert(self, task_id: int, key: str, message: str) -> None:
        if self.alert_service is not None:
            try:
                self.alert_service.fire(key, message, level="error", task_id=task_id)
            except Exception:  # noqa: BLE001
                log.exception("调度告警发送失败")

    def _dispatch_action(self, task_id: int, action: str, params: dict,
                         trigger: str = "cron") -> object:
        if action not in SCHEDULE_ACTIONS:
            raise ValidationError(f"不支持的调度动作：{action}")
        task = self.tasks.get(task_id)
        if not task:
            raise NotFoundError(f"任务 {task_id} 不存在")
        if action == "cdc_start":
            if self.cdc.is_running(task_id):
                return {"already_running": True}
            if not self.orch:
                raise AppError("编排器未装配")
            # B6：先启动成功再迁状态，失败（无位点等）不留「cdc_running 僵尸态」
            try:
                run_id = self.cdc.start(task_id, trigger=trigger)
            except KeyError as e:
                raise AppError(f"增量启动失败：{e}",
                               hint="请确认任务已完成初始化（取得 P0 位点）后重试")
            self.orch.transition(task_id, "cdc_running", reason="定时触发：启动增量")
            return run_id
        if action == "cdc_stop":
            if not self.cdc.is_running(task_id):
                return {"already_stopped": True}
            self.cdc.stop(task_id, timeout=30.0)
            self.orch.transition(task_id, "idle", reason="定时触发：停止增量")
            return {"stopped": True}
        if action == "full_resync":
            # M6：首触发按目标库状态路由（E5 语义落地）——目标库不存在/为空时
            # 先走初始化（自动建库+结构迁移+拷贝），避免 resync 对缺失库直接报错
            try:
                db_state = self.engine.target_db_state(task_id)
            except Exception:  # noqa: BLE001 —— 探测失败（连不上等）按原语义走 resync 报错
                log.warning("目标库状态探测失败 task=%s，按全量重刷执行", task_id, exc_info=True)
                db_state = "nonempty"
            if db_state in ("missing", "empty"):
                return self.engine.start_init(task_id, confirm_rebuild=False, trigger=trigger)
            return self.engine.start_full_resync(
                task_id, tables=params.get("tables"),
                safety_net=bool(params.get("safety_net", True)),
                orphan_cleanup=bool(params.get("orphan_cleanup", True)),
                trigger=trigger)
        if action == "verify":
            return self.verify_service.start(
                task_id, method=params.get("method", "checksum"), trigger=trigger,
                tables=params.get("tables"))
        if action == "backup_export":
            return self.backup_service.export(
                datasource_id=int(task["target_id"]),
                schema=(task["mapping"] or {}).get("target_database") or "",
                tables=params.get("tables"), structure_only=False,
                created_by="schedule", retention_tag="schedule",
                task_id=task_id,
                ds_row=task)   # 任务行含 source_id/target_id，_resolve_source 据此解密定位
        if action == "orphan_cleanup":
            return self.orphan_service.start(task_id, trigger=trigger)
        raise ValidationError(f"不支持的调度动作：{action}")

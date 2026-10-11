"""任务编排器（T2.1 / §10）：状态迁移唯一驱动入口，禁止模块私改。

- 校验三类任务状态机的合法迁移路径，非法路径拒绝（验收项）。
- 每次迁移更新 sync_task.state 并广播 task.state_changed（§5.3）。
- E5：scheduled_full 保存即就绪（draft → idle），首次触发时按目标库空/非空自动路由。
"""
from __future__ import annotations

import threading

from app.core.errors import ConflictError, NotFoundError
from app.models.m2_repository import TaskRepository

# ---- 状态常量（§10） ----
DRAFT = "draft"
IDLE = "idle"
INITIALIZING = "initializing"
FULL_RESYNCING = "full_resyncing"
BACKING_UP = "backing_up"
CDC_RUNNING = "cdc_running"
PAUSED = "paused"
DDL_PENDING = "ddl_pending"
ERROR = "error"
NEEDS_REINIT = "needs_reinit"

MODE_LABELS = {
    "init_plus_cdc": "初始化+持续增量",
    "scheduled_full": "定时全量",
    "backup_only": "仅备份",
}

# 合法迁移表：(mode, from_state) -> {to_state: 事件说明}
# ddl_pending 的回迁态在运行时记录于 _return_state。
_TRANSITIONS: dict[tuple[str, str], dict[str, str]] = {
    # init_plus_cdc：草稿 → 初始化中 → 空闲（仅全量成功）/ 增量运行中（M3）/ 异常 / 草稿（取消）
    ("init_plus_cdc", DRAFT): {INITIALIZING: "发起初始化", FULL_RESYNCING: "DDL 决策后续做受影响表（E6）",
                               ERROR: "校验失败"},
    ("init_plus_cdc", INITIALIZING): {IDLE: "初始化成功（仅全量）", CDC_RUNNING: "初始化成功（含增量）",
                                      DRAFT: "取消初始化", ERROR: "初始化失败",
                                      DDL_PENDING: "源库 DDL 待决策"},
    ("init_plus_cdc", DDL_PENDING): {ERROR: "DDL 待决策期间失败", IDLE: "人工复位"},  # 回迁态由 decide 动态给出（持久化于 return_state）
    ("init_plus_cdc", CDC_RUNNING): {PAUSED: "暂停", IDLE: "手动停止", FULL_RESYNCING: "全量重刷触发",
                                     DDL_PENDING: "源库 DDL 待决策", NEEDS_REINIT: "位点失效",
                                     ERROR: "不可恢复错误"},
    ("init_plus_cdc", PAUSED): {CDC_RUNNING: "恢复", IDLE: "手动停止", NEEDS_REINIT: "位点失效",
                                DDL_PENDING: "源库 DDL 待决策", ERROR: "不可恢复错误"},
    ("init_plus_cdc", FULL_RESYNCING): {CDC_RUNNING: "重刷完成", IDLE: "重刷完成",
                                        DDL_PENDING: "源库 DDL 待决策", ERROR: "重刷失败",
                                        NEEDS_REINIT: "位点失效"},
    ("init_plus_cdc", ERROR): {DRAFT: "人工复位", IDLE: "人工复位", CDC_RUNNING: "人工恢复"},
    ("init_plus_cdc", NEEDS_REINIT): {INITIALIZING: "重新初始化", FULL_RESYNCING: "全量重刷"},
    ("init_plus_cdc", IDLE): {INITIALIZING: "重新初始化", FULL_RESYNCING: "全量重刷",
                              CDC_RUNNING: "启动增量"},
    # scheduled_full（E5）：保存即就绪；首次触发按目标库空/非空自动路由
    ("scheduled_full", DRAFT): {IDLE: "保存即就绪（E5）"},
    ("scheduled_full", IDLE): {INITIALIZING: "触发（目标库为空→按初始化）",
                               FULL_RESYNCING: "触发（目标库非空→按重刷）"},
    ("scheduled_full", INITIALIZING): {IDLE: "初始化成功", ERROR: "初始化失败",
                                       DDL_PENDING: "源库 DDL 待决策"},
    ("scheduled_full", FULL_RESYNCING): {IDLE: "重刷成功", ERROR: "重刷失败",
                                         DDL_PENDING: "源库 DDL 待决策"},
    ("scheduled_full", DDL_PENDING): {ERROR: "DDL 待决策期间失败", IDLE: "人工复位"},
    ("scheduled_full", ERROR): {IDLE: "人工恢复"},
    # backup_only：轻量路径 空闲 ⇄ 备份中
    ("backup_only", DRAFT): {IDLE: "保存即就绪"},
    ("backup_only", IDLE): {BACKING_UP: "发起备份"},
    ("backup_only", BACKING_UP): {IDLE: "备份完成", ERROR: "备份失败"},
    ("backup_only", ERROR): {IDLE: "人工恢复"},
}


class Orchestrator:
    """状态迁移唯一入口。线程安全（引擎线程与 API 同时驱动）。"""

    def __init__(self, task_repo: TaskRepository, broadcaster=None, run_repo=None):
        self.tasks = task_repo
        self.broadcaster = broadcaster
        self.runs = run_repo  # §10：迁移审计落 run_history（可空＝测试环境）
        self._lock = threading.Lock()
        self._return_state: dict[int, str] = {}  # DDL 待决策前的运行态

    def _log_transition(self, task_id: int, from_state: str, to_state: str,
                        reason: str) -> None:
        """迁移审计旁路容错：历史落库失败不影响主迁移。"""
        if not self.runs:
            return
        try:
            self.runs.log_state_change(task_id=task_id, from_state=from_state,
                                       to_state=to_state, reason=reason)
        except Exception:
            pass

    # ---- 校验 ----
    def assert_can(self, mode: str, from_state: str, to_state: str) -> None:
        allowed = _TRANSITIONS.get((mode, from_state), {})
        if to_state not in allowed:
            raise ConflictError(
                f"非法状态迁移：{from_state} → {to_state}",
                hint=f"当前任务模式 {MODE_LABELS.get(mode, mode)} 下，"
                     f"「{from_state}」允许迁移至：{'、'.join(allowed) or '（无）'}")

    # ---- 迁移（唯一驱动入口） ----
    def transition(self, task_id: int, to_state: str, *, reason: str = "") -> dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise NotFoundError(f"任务 {task_id} 不存在")
            self.assert_can(task["mode"], task["state"], to_state)
            from_state = task["state"]
            self.tasks.update(task_id, {"state": to_state})
        self._log_transition(task_id, from_state, to_state, reason)
        task = self.tasks.get(task_id)
        self._emit_state_changed(task, from_state=from_state, to_state=to_state, reason=reason)
        return task

    def transition_from_current(self, task_id: int, to_state: str, *, reason: str = "") -> dict:
        """对 DDL 待决策回迁等动态起点：from=当前态做迁移校验后落库。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise NotFoundError(f"任务 {task_id} 不存在")
            from_state = task["state"]
            self.assert_can(task["mode"], from_state, to_state)
            self.tasks.update(task_id, {"state": to_state})
        self._log_transition(task_id, from_state, to_state, reason)
        self._emit_state_changed(task, from_state, to_state, reason)
        return task

    # ---- DDL 待决策：进入（记忆回迁态）/ 退出 ----
    def enter_ddl_pending(self, task_id: int, reason: str = "结构指纹中止/DDL 拦截") -> str | None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise NotFoundError(f"任务 {task_id} 不存在")
            if task["state"] == DDL_PENDING:
                return self._return_state.get(task_id) or task.get("return_state")
            self.assert_can(task["mode"], task["state"], DDL_PENDING)
            return_state = task["state"]
            self._return_state[task_id] = return_state
            # M3：回迁态持久化（应用重启后决策仍能回到正确运行态）
            self.tasks.update(task_id, {"state": DDL_PENDING, "return_state": return_state or ""})
        self._log_transition(task_id, task["state"], DDL_PENDING, reason)
        self._emit_state_changed(task, task["state"], DDL_PENDING, reason)
        return return_state

    def exit_ddl_pending(self, task_id: int, reason: str = "DDL 决策完成") -> dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise NotFoundError(f"任务 {task_id} 不存在")
            return_state = (self._return_state.pop(task_id, None)
                            or task.get("return_state") or IDLE)
            self.tasks.update(task_id, {"state": return_state, "return_state": ""})
        self._log_transition(task_id, DDL_PENDING, return_state, reason)
        self._emit_state_changed(task, DDL_PENDING, return_state, reason)
        return task

    # ---- 首次触发路由（E5）----
    def route_first_trigger(self, task_id: int, target_db_is_empty: bool) -> str:
        """scheduled_full/idle 触发时按目标库空/非空路由到 initializing / full_resyncing。"""
        target = INITIALIZING if target_db_is_empty else FULL_RESYNCING
        self.transition(task_id, target,
                        reason="目标库为空 → 按初始化执行" if target_db_is_empty
                               else "目标库非空 → 按全量重刷执行")
        return target

    # ---- 事件广播（线程安全：引擎线程经主事件循环投递） ----
    def _emit_state_changed(self, task: dict, from_state: str, to_state: str, reason: str) -> None:
        if not self.broadcaster:
            return
        import asyncio
        payload = {"task_id": task["id"], "name": task["name"], "mode": task["mode"],
                   "from": from_state, "to": to_state, "reason": reason}
        loop = getattr(self.broadcaster, "main_loop", None)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self.broadcaster.broadcast("task.state_changed", payload, task_id=str(task["id"])))

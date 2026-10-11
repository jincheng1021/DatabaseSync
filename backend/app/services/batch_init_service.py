"""批量初始化排队器（M6）：多库批量创建后按并发上限发起初始化。

- 内存队列 + 惰性 worker 池；并发上限默认 2（GLOBAL_DEFAULTS.batch_init_parallel），
  低性能源库建议 max_parallel=1。
- 单任务发起失败不阻塞后续；start_init 自带 ConflictError 防护，
  与用户手动发起竞态时安全跳过。
- 崩溃恢复语义（拍板可接受）：队列纯内存，重启丢失；任务仍处 draft，
  列表页可见，可手动初始化或重调 POST /api/tasks/batch-init。
"""
from __future__ import annotations

import logging
import queue
import threading

from app.core.errors import ConflictError

log = logging.getLogger("dsync.batch_init")

_WORKER_IDLE_TIMEOUT = 5.0  # 秒；空闲超时且队列已空 → worker 退出（惰性缩容）


class BatchInitService:
    def __init__(self, engine, tasks, audit=None, default_parallel: int = 2):
        self.engine = engine
        self.tasks = tasks
        self.audit = audit
        self.default_parallel = max(1, min(8, int(default_parallel)))
        self._q: queue.Queue = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._running = 0
        self._queued_ids: list[int] = []

    # ---------- 入队 ----------
    def enqueue(self, task_ids: list[int], max_parallel: int | None = None) -> list[int]:
        """逐个校验后入队（非 init_plus_cdc / 非 draft 的任务静默过滤）。

        返回实际入队的 task_id 列表；未入队 = 已在运行/态不符/不存在。
        """
        parallel = max(1, min(8, int(max_parallel or self.default_parallel)))
        accepted: list[int] = []
        for tid in task_ids:
            tid = int(tid)
            task = self.tasks.get(tid)
            if not task or task["mode"] != "init_plus_cdc" or task["state"] != "draft":
                continue
            self._q.put(tid)
            with self._lock:
                self._queued_ids.append(tid)
            accepted.append(tid)
        if accepted:
            self._ensure_workers(parallel)
        return accepted

    def _ensure_workers(self, parallel: int) -> None:
        with self._lock:
            need = parallel - len(self._workers)
            for _ in range(max(0, need)):
                t = threading.Thread(target=self._worker, daemon=True,
                                     name="batch-init-worker")
                self._workers.append(t)
                t.start()

    # ---------- 消费 ----------
    def _worker(self) -> None:
        me = threading.current_thread()
        while True:
            try:
                tid = self._q.get(timeout=_WORKER_IDLE_TIMEOUT)
            except queue.Empty:
                with self._lock:
                    if self._q.unfinished_tasks == 0:
                        try:
                            self._workers.remove(me)
                        except ValueError:  # noqa: PERF203 —— 竞态下可能已被移除
                            pass
                        return
                continue
            with self._lock:
                if tid in self._queued_ids:
                    self._queued_ids.remove(tid)
                self._running += 1
            try:
                self._start_one(tid)
            finally:
                with self._lock:
                    self._running -= 1
                self._q.task_done()

    def _start_one(self, tid: int) -> None:
        try:
            self.engine.start_init(tid, confirm_rebuild=False, trigger="batch_init")
            log.info("批量初始化已发起 task=%s", tid)
            if self.audit:
                self.audit.record("system", "task.batch_init", object_type="task",
                                  object_id=tid, after={"trigger": "batch_init"})
        except ConflictError as e:
            # 已在运行/库非空等：跳过不阻塞后续（手动并发发起的竞态在此吸收）
            log.info("批量初始化跳过 task=%s：%s", tid, e.message)
            if self.audit:
                self.audit.record("system", "task.batch_init.skip", object_type="task",
                                  object_id=tid, after={"reason": e.message})
        except Exception as e:  # noqa: BLE001 —— 引擎已置 error 态；继续处理队列后续
            log.exception("批量初始化发起失败 task=%s", tid)
            if self.audit:
                self.audit.record("system", "task.batch_init.fail", object_type="task",
                                  object_id=tid, after={"error": str(e)})

    # ---------- 状态 / 停机 ----------
    def status(self) -> dict:
        with self._lock:
            return {"queued": list(self._queued_ids), "running": self._running,
                    "default_parallel": self.default_parallel}

    def shutdown(self) -> None:
        """daemon 线程随进程退出；清空队列丢弃未发起项（任务留 draft 可重试）。"""
        while True:
            try:
                self._q.get_nowait()
                self._q.task_done()
            except queue.Empty:
                break
        with self._lock:
            self._queued_ids.clear()

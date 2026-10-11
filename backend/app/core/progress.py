"""运行进度跟踪器（T2.5）：任务级 + 表级双层模型，WS 每秒推送 task.progress。

引擎线程写内存模型，主循环异步任务每秒广播（§4.2.1 进度模型）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TableProgress:
    name: str
    state: str = "pending"          # pending/copying/done/failed/ddl_blocked/skipped
    rows_total: int | None = None
    rows_done: int = 0
    error: str | None = None


@dataclass
class RunProgress:
    run_id: int
    task_id: int | None
    run_type: str                    # init/full_resync/backup/restore
    phase: str = "prepare"           # prepare/position/structure/copy/finalize/restore/done
    tables: dict[str, TableProgress] = field(default_factory=dict)
    rows_total: int = 0
    rows_done: int = 0
    started_at: float = field(default_factory=time.monotonic)
    paused: bool = False
    cancel_requested: bool = False
    error: str | None = None
    _count_lock: threading.Lock = field(default_factory=threading.Lock,
                                        repr=False, compare=False)

    def add_rows(self, n: int) -> None:
        """多拷贝线程并发累加（P2-6：裸 += 非原子，会丢计数导致进度/速率失真）。"""
        with self._count_lock:
            self.rows_done += n

    def snapshot(self) -> dict:
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        speed = self.rows_done / elapsed
        eta = (self.rows_total - self.rows_done) / speed if speed > 0 and self.rows_total > self.rows_done else None
        # rows_total 为估算值（rows_est），实际拷贝可能超估算 → 展示层钳制不超 100%
        _done = min(self.rows_done, self.rows_total) if self.rows_total > 0 else self.rows_done
        return {
            "run_id": self.run_id, "task_id": self.task_id, "run_type": self.run_type,
            "phase": self.phase, "paused": self.paused,
            "rows_total": self.rows_total, "rows_done": _done,
            "speed_rows_per_sec": round(speed, 1),
            "eta_seconds": round(eta) if eta is not None else None,
            "tables_total": len(self.tables),
            "tables_done": sum(1 for t in self.tables.values() if t.state == "done"),
            "tables": [{"name": t.name, "state": t.state, "rows_total": t.rows_total,
                        "rows_done": t.rows_done, "error": t.error}
                       for t in self.tables.values()],
            "error": self.error,
        }


class ProgressTracker:
    """线程安全的活跃 run 进度注册表。"""

    def __init__(self):
        self._runs: dict[int, RunProgress] = {}
        self._lock = threading.Lock()

    def start(self, run_id: int, task_id: int | None, run_type: str) -> RunProgress:
        rp = RunProgress(run_id=run_id, task_id=task_id, run_type=run_type)
        with self._lock:
            self._runs[run_id] = rp
        return rp

    def get(self, run_id: int) -> RunProgress | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_by_task(self, task_id: int) -> RunProgress | None:
        with self._lock:
            for rp in self._runs.values():
                if rp.task_id == task_id:
                    return rp
        return None

    def active(self) -> list[RunProgress]:
        with self._lock:
            return list(self._runs.values())

    def remove(self, run_id: int) -> RunProgress | None:
        with self._lock:
            return self._runs.pop(run_id, None)

    def request_pause(self, run_id: int) -> bool:
        rp = self.get(run_id)
        if rp:
            rp.paused = True
            return True
        return False

    def request_resume(self, run_id: int) -> bool:
        rp = self.get(run_id)
        if rp:
            rp.paused = False
            return True
        return False

    def request_cancel(self, run_id: int) -> bool:
        rp = self.get(run_id)
        if rp:
            rp.cancel_requested = True
            return True
        return False

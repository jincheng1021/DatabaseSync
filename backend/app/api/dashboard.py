"""仪表盘聚合 API（§6.2）：统计卡数据单次取全。

GET /api/dashboard/summary 返回：
- task_counts：任务总数/运行中/异常/暂停/待重建（运行态集合与 tasks.py 一致）
- today_events：今日（本地自然日）累计应用行数（run_history rows_* + CDC counters 兜底）
- unresolved_alerts：未解除告警数
- generated_at：聚合时间（本地时区）

缓存：5 秒 TTL 内存缓存（挂 app.state，测试间互不影响）；
WS 侧实时性由 task.state_changed / alert.fired 等事件驱动，不受缓存影响。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_actor

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# 与 tasks.py「任务运行中禁止修改」的运行态集合保持一致
RUNNING_STATES = {"initializing", "full_resyncing", "backing_up", "cdc_running"}

_CACHE_TTL = 5.0


@router.get("/summary")
def dashboard_summary(request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    now = time.monotonic()
    cached = getattr(s, "_dash_summary_cache", None)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    tasks = s.task_repo.list()
    states = [t["state"] for t in tasks]
    data = {
        "task_counts": {
            "total": len(tasks),
            "running": sum(1 for st in states if st in RUNNING_STATES),
            "error": states.count("error"),
            "paused": states.count("paused"),
            "needs_reinit": states.count("needs_reinit"),
        },
        "today_events": s.run_repo.today_events(),
        "unresolved_alerts": s.alert_repo.count_unresolved(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    s._dash_summary_cache = (now, data)  # noqa: SLF001 —— state 级缓存，测试隔离友好
    return data

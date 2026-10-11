"""监控 API（T3.6 / §5.1）：延迟 1 分钟粒度历史曲线。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_actor

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/lag")
def lag_history(request: Request, task_id: int,
                range_minutes: int = Query(default=1440, ge=1, le=43200),
                actor: dict = Depends(get_current_actor)):
    """增量延迟历史：[{ts, lag_seconds}]，1 分钟粒度（实时增量经 WS task.lag）。"""
    s = request.app.state
    return {"task_id": task_id,
            "live": s.cdc.is_running(task_id),
            "current_lag": s.cdc.get_lag(task_id),
            "points": s.cdc.lag_history(task_id, range_minutes)}


@router.get("/lag-batch")
def lag_history_batch(request: Request, task_ids: str,
                      range_minutes: int = Query(default=360, ge=1, le=43200),
                      actor: dict = Depends(get_current_actor)):
    """多任务延迟历史批量（仪表盘聚合曲线）：task_ids 逗号分隔，最多取 8 个。"""
    s = request.app.state
    ids: list[int] = []
    for part in task_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    series = [{"task_id": tid,
               "live": s.cdc.is_running(tid),
               "current_lag": s.cdc.get_lag(tid),
               "points": s.cdc.lag_history(tid, range_minutes)}
              for tid in ids[:8]]
    return {"series": series, "requested": len(ids), "returned": len(series)}

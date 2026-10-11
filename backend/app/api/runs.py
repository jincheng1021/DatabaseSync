"""执行历史 API（T2.1 / §5.1）：run 分页与下钻、run 级暂停/恢复/取消。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_actor
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(request: Request, actor: dict = Depends(get_current_actor),
              task_id: int | None = None, run_type: str | None = None,
              status: str | None = None, page: int = Query(default=1, ge=1),
              page_size: int = Query(default=20, ge=1, le=100)):
    s = request.app.state
    items, total = s.run_repo.list(task_id=task_id, run_type=run_type, status=status,
                                   page=page, page_size=page_size)
    return {"runs": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{run_id}")
def get_run(run_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    run = s.run_repo.get(run_id)
    if not run:
        raise NotFoundError(f"run {run_id} 不存在")
    rp = s.tracker.get(run_id)
    run["live_progress"] = rp.snapshot() if rp else None
    # P2：下钻查询收进仓库层（修复前 API 层直连 SQL，违反仓库层约定）
    run["table_details"] = s.run_repo.table_details(run_id)
    return run


@router.post("/{run_id}/pause")
def pause_run(run_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    if not s.tracker.get(run_id):
        raise NotFoundError("该 run 不在运行中")
    s.tracker.request_pause(run_id)
    # P2：补审计（修复前 pause/resume 无审计，cancel 有——写操作覆盖不完整）
    s.audit.record(actor["username"], "run.pause", ip=actor["ip"], object_type="run",
                   object_id=run_id)
    return {"run_id": run_id, "paused": True}


@router.post("/{run_id}/resume")
def resume_run(run_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    if not s.tracker.get(run_id):
        raise NotFoundError("该 run 不在运行中")
    s.tracker.request_resume(run_id)
    s.audit.record(actor["username"], "run.resume", ip=actor["ip"], object_type="run",
                   object_id=run_id)
    return {"run_id": run_id, "paused": False}


@router.post("/{run_id}/cancel")
def cancel_run(run_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    if not s.tracker.get(run_id):
        raise NotFoundError("该 run 不在运行中")
    s.tracker.request_cancel(run_id)
    s.audit.record(actor["username"], "run.cancel", ip=actor["ip"], object_type="run",
                   object_id=run_id)
    return {"run_id": run_id, "cancel_requested": True}

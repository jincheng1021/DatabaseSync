"""调度 API（T4.1 / §5.1）：schedule CRUD + 未来触发预览 + 立即触发。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_actor
from app.core.errors import NotFoundError, ValidationError
from app.models.m4_repository import SCHEDULE_ACTIONS
from app.services.scheduler_service import DEFAULT_TZ, validate_cron

router = APIRouter(prefix="/api", tags=["schedules"])


class ScheduleCreateReq(BaseModel):
    name: str = Field(max_length=128)
    action: str
    cron: str
    timezone: str = DEFAULT_TZ
    enabled: bool = True
    action_params: dict = Field(default_factory=dict)
    overlap_policy: str = "skip"   # skip / queue
    misfire_policy: str = "skip"   # skip / run_once


class ScheduleUpdateReq(BaseModel):
    name: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    action_params: dict | None = None
    overlap_policy: str | None = None
    misfire_policy: str | None = None


def _svc(request: Request):
    return request.app.state


def _validate(body: ScheduleCreateReq | ScheduleUpdateReq):
    action = getattr(body, "action", None)
    if action is not None and action not in SCHEDULE_ACTIONS:
        raise ValidationError(f"action 仅支持 {'/'.join(SCHEDULE_ACTIONS)}")
    if body.overlap_policy not in (None, "skip", "queue"):
        raise ValidationError("overlap_policy 仅支持 skip/queue")
    if body.misfire_policy not in (None, "skip", "run_once"):
        raise ValidationError("misfire_policy 仅支持 skip/run_once")


@router.get("/tasks/{task_id}/schedules")
def list_schedules(task_id: int, request: Request,
                   actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    if not s.task_repo.get(task_id):
        raise NotFoundError(f"任务 {task_id} 不存在")
    return {"schedules": s.schedule_repo.list(task_id)}


@router.post("/tasks/{task_id}/schedules", status_code=201)
def create_schedule(task_id: int, body: ScheduleCreateReq, request: Request,
                    actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    if not s.task_repo.get(task_id):
        raise NotFoundError(f"任务 {task_id} 不存在")
    _validate(body)
    validate_cron(body.cron)
    sid = s.schedule_repo.create({**body.model_dump(), "task_id": task_id})
    s.scheduler.reload_schedule(sid)
    s.audit.record(actor["username"], "schedule.create", ip=actor["ip"],
                   object_type="schedule", object_id=sid,
                   after={"task_id": task_id, "action": body.action, "cron": body.cron})
    return s.schedule_repo.get(sid)


@router.get("/schedules/preview")
def preview(cron: str, timezone: str = DEFAULT_TZ, count: int = 5,
            request: Request = None, actor: dict = Depends(get_current_actor)):
    """未来 N 次触发预览（按时区计算，E11）。"""
    s = _svc(request)
    return s.scheduler.preview(cron, timezone, min(max(count, 1), 20))


@router.put("/schedules/{sid}")
def update_schedule(sid: int, body: ScheduleUpdateReq, request: Request,
                    actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    old = s.schedule_repo.get(sid)
    if not old:
        raise NotFoundError(f"调度计划 {sid} 不存在")
    _validate(body)
    if body.cron is not None:
        validate_cron(body.cron)
    s.schedule_repo.update(sid, body.model_dump(exclude_none=True))
    s.scheduler.reload_schedule(sid)
    s.audit.record(actor["username"], "schedule.update", ip=actor["ip"],
                   object_type="schedule", object_id=sid,
                   before={"cron": old["cron"], "enabled": old["enabled"]},
                   after=body.model_dump(exclude_none=True))
    return s.schedule_repo.get(sid)


@router.post("/schedules/{sid}/trigger", status_code=202)
def trigger_schedule(sid: int, request: Request,
                     actor: dict = Depends(get_current_actor)):
    """立即触发一次调度计划（trigger=manual）。

    停用计划也允许手动触发（显式用户意图）；overlap 语义由调度服务在锁内
    权威判定（skip→落 skipped 历史 / queue→入队受上限约束），本端点恒返 202
    accepted，最终结果以执行历史为准。
    """
    s = _svc(request)
    sch = s.schedule_repo.get(sid)
    if not sch:
        raise NotFoundError(f"调度计划 {sid} 不存在")
    result = s.scheduler.trigger_now(sid)
    s.audit.record(actor["username"], "schedule.trigger", ip=actor["ip"],
                   object_type="schedule", object_id=sid,
                   after={"task_id": sch["task_id"], "action": sch["action"]})
    return result


@router.delete("/schedules/{sid}", status_code=204)
def delete_schedule(sid: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    old = s.schedule_repo.get(sid)
    if not old:
        raise NotFoundError(f"调度计划 {sid} 不存在")
    s.schedule_repo.delete(sid)
    s.scheduler.remove_schedule(sid)
    s.audit.record(actor["username"], "schedule.delete", ip=actor["ip"],
                   object_type="schedule", object_id=sid, before={"name": old["name"]})

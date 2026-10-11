"""校验 API（T4.2 / §5.1）：发起校验、报告列表/详情、以源库覆盖修复选中差异。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_actor

router = APIRouter(prefix="/api", tags=["verify"])


class VerifyStartReq(BaseModel):
    method: str = "checksum"        # row_count / checksum
    tables: list[str] | None = None


class RepairReq(BaseModel):
    tables: list[str] | None = None
    pks: list[list] | None = Field(default=None,
                                   description="仅修复指定主键（默认全部差异行）")


@router.post("/tasks/{task_id}/verify", status_code=201)
def start_verify(task_id: int, body: VerifyStartReq, request: Request,
                 actor: dict = Depends(get_current_actor)):
    vid = request.app.state.verify_service.start(
        task_id, method=body.method, trigger="manual", tables=body.tables)
    request.app.state.audit.record(actor["username"], "task.verify", ip=actor["ip"],
                                   object_type="task", object_id=task_id,
                                   after={"verify_id": vid, "method": body.method})
    return {"verify_id": vid}


@router.get("/verify-results")
def list_verify_results(request: Request, task_id: int | None = None,
                        page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1, le=100),
                        actor: dict = Depends(get_current_actor)):
    items, total = request.app.state.verify_repo.list(task_id, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/verify-results/{vid}")
def get_verify_result(vid: int, request: Request, detail: bool = False,
                      actor: dict = Depends(get_current_actor)):
    vr = request.app.state.verify_repo.get(vid, with_detail=detail)
    if not vr:
        from app.core.errors import NotFoundError
        raise NotFoundError(f"校验报告 {vid} 不存在")
    return vr


@router.post("/verify-results/{vid}/repair")
def repair(vid: int, body: RepairReq, request: Request,
           actor: dict = Depends(get_current_actor)):
    """以源库覆盖修复选中差异（默认不自动修复）。"""
    result = request.app.state.verify_service.repair(vid, tables=body.tables, pks=body.pks)
    request.app.state.audit.record(actor["username"], "verify.repair", ip=actor["ip"],
                                   object_type="verify_result", object_id=vid, after=result)
    return result

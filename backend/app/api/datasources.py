"""数据源路由（T1.5/T1.6）：CRUD / 克隆 / 预检 / 表元数据。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_actor
from app.schemas.api import DatasourceReq, DatasourceUpdateReq, PrecheckReq
from app.services.datasource_service import DatasourceService

router = APIRouter(prefix="/api/datasources", tags=["datasources"])


def _svc(request: Request) -> DatasourceService:
    return request.app.state.ds_service


@router.get("")
def list_datasources(request: Request, actor: dict = Depends(get_current_actor)):
    return _svc(request).list()


@router.post("", status_code=201)
def create_datasource(request: Request, body: DatasourceReq,
                      actor: dict = Depends(get_current_actor)):
    return _svc(request).create(body.model_dump(), actor["username"], actor["ip"])


@router.get("/{ds_id}")
def get_datasource(ds_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    return _svc(request).get(ds_id)


@router.put("/{ds_id}")
def update_datasource(ds_id: int, request: Request, body: DatasourceUpdateReq,
                      actor: dict = Depends(get_current_actor)):
    return _svc(request).update(ds_id, body.model_dump(exclude_unset=True),
                                actor["username"], actor["ip"])


@router.delete("/{ds_id}")
def delete_datasource(ds_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    _svc(request).delete(ds_id, actor["username"], actor["ip"])
    return {"ok": True}


@router.post("/{ds_id}/clone", status_code=201)
def clone_datasource(ds_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    return _svc(request).clone(ds_id, actor["username"], actor["ip"])


@router.post("/{ds_id}/precheck")
def precheck_datasource(ds_id: int, request: Request, body: PrecheckReq,
                        actor: dict = Depends(get_current_actor)):
    return _svc(request).precheck(ds_id, body.role, actor["username"], actor["ip"])


@router.get("/{ds_id}/tables")
def datasource_tables(ds_id: int, request: Request, schema: str | None = None,
                      actor: dict = Depends(get_current_actor)):
    return {"tables": _svc(request).tables(ds_id, schema)}


@router.get("/{ds_id}/schemas")
def datasource_schemas(ds_id: int, request: Request,
                       actor: dict = Depends(get_current_actor)):
    """实例上的业务库清单（排除系统库），供任务向导选择源库。"""
    return {"schemas": _svc(request).schemas(ds_id)}


@router.get("/{ds_id}/table-meta")
def datasource_table_meta(ds_id: int, request: Request, schema: str | None = None,
                          actor: dict = Depends(get_current_actor)):
    """T4.6 表映射编辑器元数据：表 + 列（键/可空/默认值）+ 主键（无主键表红标）。"""
    return _svc(request).table_meta(ds_id, schema)

"""备份 API（T2.9/T2.10 / §5.1）：列表/导出/下载/删除/恢复预检/恢复。"""
from __future__ import annotations

import gzip
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_actor
from app.core.errors import AppError, NotFoundError, ValidationError

router = APIRouter(prefix="/api/backups", tags=["backups"])


class BackupCreateReq(BaseModel):
    datasource_id: int
    schema_name: str = Field(alias="schema")
    tables: list[str] | None = None
    structure_only: bool = False

    model_config = {"populate_by_name": True}


class RestoreReq(BaseModel):
    target_datasource_id: int
    target_db: str = Field(max_length=64)
    safety_net: bool = True
    auto_pause_related: bool = True
    drop_extra_tables: bool = True


@router.get("")
def list_backups(request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    return {"backups": s.backup_repo.list()}


@router.post("", status_code=201)
def create_backup(body: BackupCreateReq, request: Request,
                  actor: dict = Depends(get_current_actor)):
    s = request.app.state
    ds = s.ds_service.repo.get(body.datasource_id)
    if not ds:
        raise NotFoundError("数据源不存在")
    if not body.schema_name:
        raise ValidationError("未指定备份库（schema）")
    ids = s.backup_service.export(
        datasource_id=body.datasource_id, schema=body.schema_name, tables=body.tables,
        structure_only=body.structure_only, created_by="manual",
        ds_row=s.ds_service.repo.get(body.datasource_id))
    s.audit.record(actor["username"], "backup.create", ip=actor["ip"], object_type="backup",
                   object_id=ids[0] if ids else None,
                   after={"schema": body.schema_name, "tables": body.tables})
    return {"backup_ids": ids, "backup": s.backup_repo.get(ids[0]) if ids else None}


@router.get("/{backup_id}")
def get_backup(backup_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    row = s.backup_repo.get(backup_id)
    if not row:
        raise NotFoundError(f"备份 {backup_id} 不存在")
    return row


@router.get("/{backup_id}/download")
def download_backup(backup_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    row = s.backup_repo.get(backup_id)
    if not row or not Path(row["file_path"]).exists():
        raise NotFoundError("备份文件不存在或已被清理")
    return FileResponse(row["file_path"], media_type="application/gzip",
                        filename=row["file_name"])


@router.get("/{backup_id}/file-header")
def backup_header(backup_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    """文件详情：文件头环境声明与回放注意事项（页面 8）。"""
    s = request.app.state
    row = s.backup_repo.get(backup_id)
    if not row or not Path(row["file_path"]).exists():
        raise NotFoundError("备份文件不存在或已被清理")
    try:
        with gzip.open(row["file_path"], "rt", encoding="utf-8", errors="replace") as f:
            head = []
            for line in f:
                head.append(line.rstrip("\n"))
                if len(head) >= 30 or line.strip().startswith("DROP TABLE"):
                    break
    except OSError as e:
        raise AppError(f"读取备份文件头失败：{e}")
    notes = [
        "产物为覆盖式幂等语义（DROP TABLE IF EXISTS + CREATE + INSERT），回放将删除并重建同名表。",
        "文件头已固定字符集 / sql_mode / 时区，直接用 mysql 客户端回放即可保持语义一致。",
        "恢复到目标库前请确认磁盘余量（解压体积 × 3）与目标版本不低于备份源版本。",
    ]
    return {"file_name": row["file_name"], "sha256": row["sha256"],
            "header": "\n".join(head), "replay_notes": notes}


@router.delete("/{backup_id}", status_code=204)
def delete_backup(backup_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = request.app.state
    s.backup_service.delete(backup_id)
    s.audit.record(actor["username"], "backup.delete", ip=actor["ip"], object_type="backup",
                   object_id=backup_id)


@router.get("/{backup_id}/restore-precheck")
def restore_precheck(backup_id: int, request: Request, target_datasource_id: int, target_db: str,
                     actor: dict = Depends(get_current_actor)):
    s = request.app.state
    result = s.restore_service.precheck(backup_id, target_datasource_id, target_db)
    s.audit.record(actor["username"], "backup.restore_precheck", ip=actor["ip"],
                   object_type="backup", object_id=backup_id,
                   after={"target_datasource_id": target_datasource_id, "target_db": target_db})
    return result


@router.post("/{backup_id}/restore")
def restore_backup(backup_id: int, body: RestoreReq, request: Request,
                   actor: dict = Depends(get_current_actor)):
    s = request.app.state
    run_id = s.restore_service.start_restore(
        backup_id, target_ds_id=body.target_datasource_id, target_db=body.target_db,
        safety_net=body.safety_net, auto_pause_related=body.auto_pause_related,
        drop_extra_tables=body.drop_extra_tables)
    s.audit.record(actor["username"], "backup.restore", ip=actor["ip"], object_type="backup",
                   object_id=backup_id, after={"run_id": run_id, "target_db": body.target_db,
                                               "safety_net": body.safety_net})
    return {"run_id": run_id}

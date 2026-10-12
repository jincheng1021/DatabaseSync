"""任务 API（T2.1/T2.7 + M3 增量启停 / §5.1）：CRUD + 初始化/重刷/进度/位点/DDL 决策/增量生命周期。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_actor
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.orchestrator import CDC_RUNNING, MODE_LABELS, PAUSED
from app.schemas.api import DDLDecisionReq
from app.services.cdc_transform import ddl_kind
from app.services.mapping_validator import is_subset_mapping, validate_mapping
from app.services.mysql_client import fetch_all_lc, open_connection, probe_databases

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _live_schema_columns(ds_service, ds_row: dict | None,
                         schema: str | None) -> dict[str, list[dict]] | None:
    """连库取整库列元数据（P0-4 修复：使列过滤深度校验真正生效）。

    单条聚合查询按表分组；连接失败返回 None（不阻塞任务保存，
    运行时开拷前结构预检 full_engine._precheck_target_structure 兜底拦截）。
    返回 {表名: [{name, nullable, column_default, column_key}, ...]}。
    """
    if not ds_row:
        return None
    try:
        conn = open_connection(ds_service._to_target(ds_row))
        try:
            rows = fetch_all_lc(conn,
                                "SELECT table_name AS tn, column_name AS cn, "
                                "is_nullable AS nul, column_default AS dflt, column_key AS ck "
                                "FROM information_schema.columns WHERE table_schema=%s",
                                (schema,))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 —— 元数据不可得时降级为警告
        return None
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["tn"], []).append({
            "name": r["cn"], "nullable": str(r["nul"]).upper() == "YES",
            "column_default": r["dflt"], "column_key": r["ck"]})
    return out


def _mapping_filter_meta(ds_service, mapping: dict | None,
                         source_ds: dict | None, target_ds: dict | None):
    """P0-4：mapping 含列过滤时取两端列元数据（不涉及列过滤时返回 (None, None, False)。"""
    if not (mapping or {}).get("column_filter"):
        return None, None, False
    src_meta = _live_schema_columns(
        ds_service, source_ds,
        (mapping or {}).get("source_database") or (source_ds or {}).get("default_schema"))
    tgt_meta = _live_schema_columns(
        ds_service, target_ds,
        (mapping or {}).get("target_database") or (target_ds or {}).get("default_schema"))
    return src_meta, tgt_meta, True


class TaskCreateReq(BaseModel):
    name: str = Field(max_length=128)
    source_id: int
    target_id: int
    mode: str  # backup_only / init_plus_cdc / scheduled_full
    mapping: dict = Field(default_factory=dict)
    migrate_triggers: bool = False
    rate_limit_rows: int = Field(default=0, ge=0)
    batch_size: int = Field(default=5000, ge=100, le=100000)
    batch_interval_ms: int = Field(default=0, ge=0, le=10000)
    safety_net_backup: bool = True
    orphan_cleanup: bool = True


class TaskUpdateReq(BaseModel):
    name: str | None = None
    mapping: dict | None = None
    migrate_triggers: bool | None = None
    rate_limit_rows: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=100, le=100000)
    batch_interval_ms: int | None = None
    safety_net_backup: bool | None = None
    orphan_cleanup: bool | None = None


class InitReq(BaseModel):
    confirm_rebuild: bool = False
    confirm_name: str | None = None


class FullResyncReq(BaseModel):
    tables: list[str] | None = None
    safety_net: bool = True
    orphan_cleanup: bool = True


class RunActionReq(BaseModel):
    run_id: int | None = None


class OrphanCleanupReq(BaseModel):
    dry_run: bool = False   # True = 只算差集不删除（预览落 run report）


# ---- M6：多库批量创建 ----

class BatchPair(BaseModel):
    source_database: str = Field(min_length=1, max_length=64)
    target_database: str | None = Field(default=None, max_length=64)  # 缺省 = 与源库同名


class TaskBatchCreateReq(BaseModel):
    name: str = Field(max_length=128)                     # 基础名，实际任务名 = {name}-{目标库}
    source_id: int
    target_id: int
    mode: str                                             # 复用 VALID_MODES
    db_pairs: list[BatchPair] = Field(min_length=1, max_length=64)
    mapping_base: dict = Field(default_factory=dict)      # 仅 scope/tables/rename/prefix/suffix，库级键逐对注入
    auto_start_init: bool = False                         # init_plus_cdc 且目标库 missing/empty 时自动排队初始化
    max_parallel: int = Field(default=2, ge=1, le=8)      # 批量初始化并发上限（低性能库建议 1）
    batch_size: int = Field(default=5000, ge=100, le=100000)
    rate_limit_rows: int = Field(default=0, ge=0)
    batch_interval_ms: int = Field(default=0, ge=0, le=10000)
    safety_net_backup: bool = True
    orphan_cleanup: bool = True
    migrate_triggers: bool = False


class BatchInitReq(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=64)
    max_parallel: int = Field(default=2, ge=1, le=8)


class ProbeTargetsReq(BaseModel):
    target_id: int
    target_databases: list[str] = Field(min_length=1, max_length=64)


def _target_state(info: dict) -> str:
    if not info.get("exists"):
        return "missing"
    return "empty" if int(info.get("tables") or 0) == 0 else "nonempty"


def _unique_task_name(base: str, existing: set[str]) -> str:
    """sync_task.name UNIQUE：冲突时追加 -2/-3… 序号（同步截断到 128 字符）。"""
    name = base[:128]
    if name not in existing:
        return name
    i = 2
    while f"{name}-{i}" in existing:
        i += 1
    return f"{name}-{i}"


def _svc(request: Request):
    return request.app.state


VALID_MODES = {"backup_only", "init_plus_cdc", "scheduled_full"}


@router.get("")
def list_tasks(request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    tasks = s.task_repo.list()
    out = []
    for t in tasks:
        t = dict(t)
        rp = s.tracker.get_by_task(t["id"])
        t["progress"] = rp.snapshot() if rp else None
        t["mode_label"] = MODE_LABELS.get(t["mode"], t["mode"])
        if s.cdc.is_running(t["id"]):
            t["lag_seconds"] = s.cdc.get_lag(t["id"])
            t["cdc_stats"] = s.cdc.get_stats(t["id"])
        out.append(t)
    return {"tasks": out}


@router.post("", status_code=201)
def create_task(body: TaskCreateReq, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    if body.mode not in VALID_MODES:
        raise ValidationError(f"mode 仅支持 {'/'.join(sorted(VALID_MODES))}")
    if not s.ds_service.repo.get(body.source_id) or not s.ds_service.repo.get(body.target_id):
        raise NotFoundError("源/目标数据源不存在")
    if body.source_id == body.target_id:
        raise ValidationError("源与目标不能为同一数据源")
    # P2 修复：创建路径同样走映射校验（修复前仅更新路径校验，非法谓词/主键列排除可入库）
    if body.mapping:
        # P0-4 修复：列过滤深度校验需连库列元数据（修复前三处调用均未传 table_meta，
        # 「排除列不得属于主键/唯一键」规则从未真正执行）
        table_meta, target_meta, _ = _mapping_filter_meta(
            s.ds_service, body.mapping,
            s.ds_service.repo.get(body.source_id), s.ds_service.repo.get(body.target_id))
        validate_mapping(body.mapping, table_meta=table_meta, target_meta=target_meta,
                         cdc_enabled=body.mode == "init_plus_cdc")
    # E5：scheduled_full / backup_only 保存即就绪（创建时直接落库，无需再迁移）
    state = "draft" if body.mode == "init_plus_cdc" else "idle"
    task_id = s.task_repo.create({**body.model_dump(), "state": state})
    s.audit.record(actor["username"], "task.create", ip=actor["ip"], object_type="task",
                   object_id=task_id, after={"name": body.name, "mode": body.mode})
    return s.task_repo.get(task_id)


# ---- M6：多库批量创建（每对源库/目标库生成一个独立任务，复用单库对引擎全链路） ----

@router.post("/batch/probe-targets")
def probe_batch_targets(body: ProbeTargetsReq, request: Request,
                        actor: dict = Depends(get_current_actor)):
    """批量探测目标库状态（单连接 2 条聚合 SQL，低性能库友好）。

    向导选择源库后调用：展示「不存在(将新建) / 存在·空 / 存在·非空」。
    """
    s = _svc(request)
    ds = s.ds_service.repo.get(body.target_id)
    if not ds:
        raise NotFoundError("目标数据源不存在")
    names = list(dict.fromkeys(n.strip() for n in body.target_databases if n and n.strip()))
    if not names:
        raise ValidationError("target_databases 不能为空")
    probe = probe_databases(s.ds_service._to_target(ds), names)
    return {"items": [{"target_database": n, **probe[n], "state": _target_state(probe[n])}
                      for n in names]}


@router.post("/batch", status_code=201)
def batch_create_tasks(body: TaskBatchCreateReq, request: Request,
                       actor: dict = Depends(get_current_actor)):
    """多库批量创建：部分成功语义，单对失败不回滚其他对。

    - 目标库缺省 = 与源库同名；任务名 = {name}-{目标库}（UNIQUE 冲突加序号）。
    - init_plus_cdc 且目标库已存在非空 → 拒绝该对（防误清库，走单任务清空重建流程）。
    - auto_start_init：创建成功且目标库 missing/empty 的任务自动入批量初始化队列。
    """
    s = _svc(request)
    if body.mode not in VALID_MODES:
        raise ValidationError(f"mode 仅支持 {'/'.join(sorted(VALID_MODES))}")
    src_ds = s.ds_service.repo.get(body.source_id)
    tgt_ds = s.ds_service.repo.get(body.target_id)
    if not src_ds or not tgt_ds:
        raise NotFoundError("源/目标数据源不存在")
    if body.source_id == body.target_id:
        raise ValidationError("源与目标不能为同一数据源")

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    seen_tgts: set[str] = set()
    for p in body.db_pairs:
        src_db = (p.source_database or "").strip()
        tgt_db = (p.target_database or p.source_database or "").strip()
        if not src_db or not tgt_db:
            raise ValidationError("库对中的源库/目标库不能为空")
        key = (src_db, tgt_db)
        if key in seen:
            raise ValidationError(f"库对重复：{src_db} → {tgt_db}")
        if tgt_db in seen_tgts:
            raise ValidationError(f"目标库重复：{tgt_db}（多个源库不能写入同一目标库，"
                                  f"表名会冲突）")
        seen.add(key)
        seen_tgts.add(tgt_db)
        pairs.append(key)

    # 服务端权威探测（创建与向导展示之间库状态可能漂移，以此时为准）
    probe = probe_databases(s.ds_service._to_target(tgt_ds), [t for _, t in pairs])
    existing_names = {t["name"] for t in s.task_repo.list()}
    created: list[dict] = []
    failed: list[dict] = []
    for src_db, tgt_db in pairs:
        st = _target_state(probe.get(tgt_db) or {})
        if body.mode == "init_plus_cdc" and st == "nonempty":
            failed.append({"source_database": src_db, "target_database": tgt_db,
                           "reason": f"目标库 {tgt_db} 已存在且非空（防误清库拒绝，"
                                     f"如需覆盖请用单任务「清空重建」流程）"})
            continue
        mapping = {**(body.mapping_base or {}), "source_database": src_db, "target_database": tgt_db}
        try:
            # P0-4 修复：列过滤深度校验传连库列元数据（元数据不可得时 None 降级跳过）
            table_meta, target_meta, _ = _mapping_filter_meta(
                s.ds_service, mapping, src_ds, tgt_ds)
            validate_mapping(mapping, table_meta=table_meta, target_meta=target_meta,
                             cdc_enabled=body.mode == "init_plus_cdc")
        except ValidationError as e:
            failed.append({"source_database": src_db, "target_database": tgt_db,
                           "reason": e.message})
            continue
        state = "draft" if body.mode == "init_plus_cdc" else "idle"
        name = _unique_task_name(f"{body.name}-{tgt_db}", existing_names)
        existing_names.add(name)
        tid = s.task_repo.create({
            "name": name, "source_id": body.source_id, "target_id": body.target_id,
            "mode": body.mode, "mapping": mapping,
            "migrate_triggers": body.migrate_triggers,
            "rate_limit_rows": body.rate_limit_rows, "batch_size": body.batch_size,
            "batch_interval_ms": body.batch_interval_ms,
            "safety_net_backup": body.safety_net_backup, "orphan_cleanup": body.orphan_cleanup,
            "state": state})
        s.audit.record(actor["username"], "task.batch_create", ip=actor["ip"], object_type="task",
                       object_id=tid, after={"name": name, "source_database": src_db,
                                             "target_database": tgt_db, "mode": body.mode})
        created.append({"task_id": tid, "name": name, "source_database": src_db,
                        "target_database": tgt_db, "target_state": st})

    # 自动初始化：仅 missing/empty（nonempty 已在上方拒绝），由队列限并发串行发起
    init_enqueued: list[int] = []
    init_skipped: list[int] = []
    if body.auto_start_init and body.mode == "init_plus_cdc":
        candidates = [c["task_id"] for c in created if c["target_state"] in ("missing", "empty")]
        if candidates:
            init_enqueued = s.batch_init.enqueue(candidates, max_parallel=body.max_parallel)
            init_skipped = [i for i in candidates if i not in init_enqueued]
    if created or failed:
        s.audit.record(actor["username"], "task.batch_create.summary", ip=actor["ip"],
                       object_type="task",
                       after={"created": len(created), "failed": len(failed),
                              "init_enqueued": len(init_enqueued), "mode": body.mode})
    return {"created": created, "failed": failed,
            "init_enqueued": init_enqueued, "init_skipped": init_skipped}


@router.post("/batch-init")
def batch_init_tasks(body: BatchInitReq, request: Request,
                     actor: dict = Depends(get_current_actor)):
    """批量初始化（重试入口）：仅接受 draft 态 init_plus_cdc 任务，队列限并发串行发起。"""
    s = _svc(request)
    enqueued = s.batch_init.enqueue(body.task_ids, max_parallel=body.max_parallel)
    s.audit.record(actor["username"], "task.batch_init", ip=actor["ip"], object_type="task",
                   after={"task_ids": enqueued, "max_parallel": body.max_parallel})
    return {"enqueued": enqueued, "skipped": [i for i in body.task_ids if i not in enqueued]}


@router.get("/batch-init/status")
def batch_init_status(request: Request, actor: dict = Depends(get_current_actor)):
    return request.app.state.batch_init.status()


@router.get("/{task_id}")
def get_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    rp = s.tracker.get_by_task(task_id)
    t["progress"] = rp.snapshot() if rp else None
    t["mode_label"] = MODE_LABELS.get(t["mode"], t["mode"])
    t["position"] = s.position_repo.get(task_id)
    if s.cdc.is_running(task_id):
        t["cdc_stats"] = s.cdc.get_stats(task_id)
        t["lag_seconds"] = s.cdc.get_lag(task_id)
    t["ddl_pending"] = s.ddl_repo.list_pending(task_id)
    return t


# 映射可修改的任务态（T4.5：映射变更需任务暂停后修改——运行中一律禁止）
MAPPING_EDITABLE_STATES = {"draft", "idle", "paused", "error", "needs_reinit"}


@router.put("/{task_id}")
def update_task(task_id: int, body: TaskUpdateReq, request: Request,
                actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    old = s.task_repo.get(task_id)
    if not old:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if old["state"] in ("initializing", "full_resyncing", "backing_up", "cdc_running"):
        raise AppError("任务运行中禁止修改", hint="暂停或等待运行结束后再修改映射与策略")
    patch = body.model_dump(exclude_none=True)
    if "mapping" in patch:
        # T4.5：映射变更前置——任务须暂停/空闲；保存时全量预检（列过滤/行过滤口径校验）
        if old["state"] not in MAPPING_EDITABLE_STATES:
            raise ConflictError("任务当前状态不允许修改映射", hint="请先暂停任务")
        mapping = dict(old.get("mapping") or {})
        mapping.update(patch["mapping"] or {})
        # P0-4 修复：列过滤深度校验需连库列元数据（元数据不可得时降级为警告不阻塞保存）
        table_meta, target_meta, need_meta = _mapping_filter_meta(
            s.ds_service, mapping,
            s.ds_service.repo.get(old["source_id"]), s.ds_service.repo.get(old["target_id"]))
        warnings = validate_mapping(mapping, table_meta=table_meta, target_meta=target_meta,
                                    cdc_enabled=old["mode"] == "init_plus_cdc")
        if need_meta and table_meta is None:
            warnings.append("列过滤深度校验未执行（无法连接数据源取列元数据）；"
                            "运行时开拷前结构预检仍会拦截唯一键/缺列风险")
        patch["mapping"] = mapping
        s.task_repo.update(task_id, patch)
        s.audit.record(actor["username"], "task.update", ip=actor["ip"], object_type="task",
                       object_id=task_id, before={"name": old["name"]}, after={"mapping": True})
        out = s.task_repo.get(task_id)
        out["mapping_warnings"] = warnings
        out["subset_sync"] = is_subset_mapping(mapping)
        return out
    s.task_repo.update(task_id, patch)
    s.audit.record(actor["username"], "task.update", ip=actor["ip"], object_type="task",
                   object_id=task_id, before={"name": old["name"]}, after=patch)
    return s.task_repo.get(task_id)


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    old = s.task_repo.get(task_id)
    if not old:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if old["state"] in ("initializing", "full_resyncing", "backing_up", "cdc_running"):
        raise AppError("任务运行中禁止删除", hint="先取消运行再删除")
    # 先取调度计划 sid 列表（DB 级 CASCADE 会随任务删除 schedule 行），删后清理内存 job/队列
    sched_sids = [r["id"] for r in s.schedule_repo.list(task_id)]
    s.task_repo.delete(task_id)
    if sched_sids:
        s.scheduler.remove_task_jobs(task_id)
    s.audit.record(actor["username"], "task.delete", ip=actor["ip"], object_type="task",
                   object_id=task_id, before={"name": old["name"]})


@router.post("/{task_id}/init")
def init_task(task_id: int, body: InitReq, request: Request,
              actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    run_id = s.engine.start_init(task_id, confirm_rebuild=body.confirm_rebuild,
                                 confirm_name=body.confirm_name)
    s.audit.record(actor["username"], "task.init", ip=actor["ip"], object_type="task",
                   object_id=task_id, after={"run_id": run_id,
                                             "confirm_rebuild": body.confirm_rebuild})
    return {"run_id": run_id}


@router.post("/{task_id}/full-resync")
def full_resync(task_id: int, body: FullResyncReq, request: Request,
                actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    run_id = s.engine.start_full_resync(task_id, tables=body.tables,
                                        safety_net=body.safety_net,
                                        orphan_cleanup=body.orphan_cleanup)
    s.audit.record(actor["username"], "task.full_resync", ip=actor["ip"], object_type="task",
                   object_id=task_id, after={"run_id": run_id, "tables": body.tables})
    return {"run_id": run_id}


@router.get("/{task_id}/progress")
def task_progress(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    rp = s.tracker.get_by_task(task_id)
    return {"progress": rp.snapshot() if rp else None}


@router.get("/{task_id}/position")
def task_position(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    """T2.6：P0 位点可查。"""
    s = _svc(request)
    pos = s.position_repo.get(task_id)
    if not pos:
        raise NotFoundError("尚未取得一致位点（P0）", hint="先完成一次初始化")
    return pos


@router.post("/{task_id}/pause")
def pause_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if s.cdc.is_running(task_id) and t["state"] == CDC_RUNNING:
        # M3：增量暂停（reader 停拉，队列排空提交后位点落盘）
        s.cdc.pause(task_id)
        s.orch.transition(task_id, PAUSED, reason="手动暂停增量")
        s.audit.record(actor["username"], "task.pause", ip=actor["ip"], object_type="task",
                       object_id=task_id)
        return {"task_id": task_id, "paused": True}
    rp = s.tracker.get_by_task(task_id)
    if not rp:
        raise NotFoundError("任务当前无运行中的 run")
    s.tracker.request_pause(rp.run_id)
    return {"run_id": rp.run_id, "paused": True}


@router.post("/{task_id}/resume")
def resume_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if t["state"] == PAUSED:
        # M3：增量恢复（应用重启后引擎不在运行则重新拉起，位点续传）
        if not s.cdc.is_running(task_id):
            # B6：先拉起成功再迁状态，失败不留「cdc_running 僵尸态」
            try:
                s.cdc.start(task_id, trigger="manual")
            except Exception as e:  # noqa: BLE001
                raise AppError(f"增量引擎拉起失败：{e}",
                               hint="请检查位点与数据源配置后重试，任务保持暂停态")
            s.orch.transition(task_id, CDC_RUNNING, reason="恢复增量")
        else:
            s.cdc.resume(task_id)
            s.orch.transition(task_id, CDC_RUNNING, reason="恢复增量")
        s.audit.record(actor["username"], "task.resume", ip=actor["ip"], object_type="task",
                       object_id=task_id)
        return {"task_id": task_id, "paused": False}
    rp = s.tracker.get_by_task(task_id)
    if not rp:
        raise NotFoundError("任务当前无运行中的 run")
    s.tracker.request_resume(rp.run_id)
    return {"run_id": rp.run_id, "paused": False}


@router.post("/{task_id}/start")
def start_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    """T3.9：启动增量（空闲启动 / 异常人工恢复；需已有一致位点）。"""
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if t["mode"] != "init_plus_cdc":
        raise ValidationError("仅「初始化+持续增量」任务支持增量启停")
    if not s.position_repo.get(task_id):
        raise AppError("尚未取得一致位点（P0）", hint="请先完成一次初始化，再启动增量")
    if s.cdc.is_running(task_id):
        raise ConflictError("增量已在运行中")
    # B6：先启动成功再迁状态，失败不留「cdc_running 僵尸态」
    s.orch.assert_can(t["mode"], t["state"], CDC_RUNNING)
    run_id = s.cdc.start(task_id, trigger="manual")
    s.orch.transition(task_id, CDC_RUNNING,
                      reason="人工恢复增量" if t["state"] == "error" else "启动增量")
    s.audit.record(actor["username"], "task.start", ip=actor["ip"], object_type="task",
                   object_id=task_id, after={"run_id": run_id})
    return {"run_id": run_id, "started": True}


@router.post("/{task_id}/stop")
def stop_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    """T3.9：停止增量（排空队列、位点最终落盘后转空闲）。"""
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    if t["state"] not in (CDC_RUNNING, PAUSED):
        raise ConflictError("任务不在增量运行/暂停态", hint="仅增量运行中或已暂停的任务可停止")
    if not s.cdc.is_running(task_id):
        s.orch.transition(task_id, "idle", reason="停止增量（引擎未运行）")
        return {"task_id": task_id, "stopped": True}
    s.cdc.stop(task_id, timeout=30.0)
    s.orch.transition(task_id, "idle", reason="手动停止增量")
    s.audit.record(actor["username"], "task.stop", ip=actor["ip"], object_type="task",
                   object_id=task_id)
    return {"task_id": task_id, "stopped": True}


@router.post("/{task_id}/recover")
def recover_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    """人工恢复：error → idle（状态机所有模式的 error 均可迁 idle）。

    定时全量/仅备份：恢复为空闲后等待下次调度或重新发起；
    增量任务：恢复为空闲后可重新初始化或全量重刷（有位点时用「启动增量」直接续传）。
    """
    s = _svc(request)
    t = s.task_repo.get(task_id)
    if not t:
        raise NotFoundError(f"任务 {task_id} 不存在")
    s.orch.assert_can(t["mode"], t["state"], "idle")
    s.orch.transition(task_id, "idle", reason="人工恢复（故障已处理）")
    s.audit.record(actor["username"], "task.recover", ip=actor["ip"], object_type="task",
                   object_id=task_id, before={"state": t["state"]})
    return {"task_id": task_id, "state": "idle", "recovered": True}


@router.post("/{task_id}/orphan-cleanup", status_code=201)
def orphan_cleanup(task_id: int, request: Request, actor: dict = Depends(get_current_actor),
                   body: OrphanCleanupReq | None = None):
    """T4.4：手动孤儿行清理（行过滤子集表自动跳过；任务开关关闭时拒绝）。

    body 可省略（向后兼容）；dry_run=true 只算差集与保险丝判定，不删除，
    预览结果（orphan_count/sample_deleted）落 run report，在执行历史查看。
    """
    s = _svc(request)
    dry_run = bool(body and body.dry_run)
    run_id = s.orphan_service.start(task_id, trigger="manual", dry_run=dry_run)
    s.audit.record(actor["username"], "task.orphan_cleanup", ip=actor["ip"],
                   object_type="task", object_id=task_id,
                   after={"run_id": run_id, "dry_run": dry_run})
    return {"run_id": run_id, "dry_run": dry_run}


@router.get("/{task_id}/ddl-pending")
def ddl_pending_list(task_id: int, request: Request,
                     actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    return {"items": s.ddl_repo.list_pending(task_id)}


@router.post("/{task_id}/ddl-decision")
def ddl_decision(task_id: int, body: DDLDecisionReq, request: Request,
                 actor: dict = Depends(get_current_actor)):
    """DDL 决策（E6/E12）：执行/跳过 + 记住选择（ddl_rules_json 持久化，重启后生效）。

    依回迁态分流：CDC 运行态 → 目标库执行/跳过后恢复消费；
    全量初始化/重刷态 → 受影响表从结构阶段重做（M2 路径）。
    """
    s = _svc(request)
    old = s.task_repo.get(task_id)
    if not old:
        raise NotFoundError(f"任务 {task_id} 不存在")
    pendings = s.ddl_repo.list_pending(task_id)
    if not pendings:
        raise NotFoundError("该任务无待决策 DDL")
    if body.decision not in ("execute", "skip"):
        raise ValidationError("decision 仅支持 execute/skip")
    return_state = (s.orch._return_state.get(task_id) or old.get("return_state") or "").strip()
    affected: list[str] = []
    for p in pendings:
        affected.extend(p.get("affected_tables") or [])
        s.ddl_repo.decide(p["id"], body.decision, body.remember)
        s.audit.record(actor["username"], f"task.ddl_decision.{body.decision}",
                       ip=actor["ip"], object_type="task", object_id=task_id,
                       after={"ddl_id": p["id"], "remember": body.remember,
                              "kind": ddl_kind(p["ddl_text"])})
    if body.remember:
        # E12：同类 DDL 记住选择 → 任务级规则持久化（重启后仍生效）
        rules = dict(old.get("ddl_rules") or {})
        remember = dict(rules.get("remember") or {})
        for p in pendings:
            remember[ddl_kind(p["ddl_text"])] = body.decision
        rules["remember"] = remember
        s.task_repo.update(task_id, {"ddl_rules": rules})
    s.orch.exit_ddl_pending(task_id, reason=f"DDL 决策：{body.decision}")
    if return_state in (CDC_RUNNING, PAUSED):
        # CDC 路径：执行 → 应用到目标库；随后解除消费阻塞
        applied = 0
        if body.decision == "execute":
            for p in pendings:
                s.cdc.apply_ddl(task_id, p["ddl_text"])
                applied += 1
        s.cdc.resume_after_ddl(task_id)
        return {"decision": body.decision, "path": "cdc", "applied": applied,
                "remember": body.remember}
    run_id = s.engine.start_table_redo(task_id, sorted(set(affected)))
    return {"decision": body.decision, "path": "full_redo",
            "redo_run_id": run_id, "redo_tables": sorted(set(affected))}


@router.post("/{task_id}/cancel")
def cancel_task(task_id: int, request: Request, actor: dict = Depends(get_current_actor)):
    s = _svc(request)
    rp = s.tracker.get_by_task(task_id)
    if not rp:
        raise NotFoundError("任务当前无运行中的 run")
    s.tracker.request_cancel(rp.run_id)
    s.audit.record(actor["username"], "task.cancel", ip=actor["ip"], object_type="task",
                   object_id=task_id, after={"run_id": rp.run_id})
    return {"run_id": rp.run_id, "cancel_requested": True}

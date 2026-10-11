"""应用入口（M4 装配）：Uvicorn 单进程，路由挂载 + 静态前端托管 + 启动钩子。

M4：调度服务（APScheduler + 六类动作 + 时区/并发/错过策略）、校验服务
（行数/checksum/行级 diff/修复）、告警服务（Webhook/SMTP/降噪/WS）、孤儿行清理动作。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_routes
from app.api import backups as backup_routes
from app.api import dashboard as dashboard_routes
from app.api import datasources as ds_routes
from app.api import metrics as metrics_routes
from app.api import runs as run_routes
from app.api import schedules as schedule_routes
from app.api import settings as settings_routes
from app.api import tasks as task_routes
from app.api import verify as verify_routes
from app.api import ws as ws_routes
from app.config import DATA_DIR, FRONTEND_DIST_DIR, LOG_DIR
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.limiter import TwoLevelLimiter
from app.core.logging_setup import setup_logging
from app.core.orchestrator import Orchestrator
from app.core.progress import ProgressTracker
from app.core.security import MasterKeyManager
from app.core.sessions import AuthService, LoginGuard, SessionStore
from app.core.ws_broadcaster import WSBroadcaster
from app.db.database import Database
from app.models.m2_repository import (BackupRepository, CdcRunRepository,
                                      DdlPendingRepository, PositionRepository,
                                      RunRepository, TaskRepository)
from app.models.m4_repository import ScheduleRepository, VerifyResultRepository
from app.models.repository import (AlertRepository, AuditRepository, DatasourceRepository,
                                   SettingRepository)
from app.services.alert_service import AlertService
from app.services.backup_service import BackupService
from app.services.batch_init_service import BatchInitService
from app.services.cdc_engine import CdcEngine
from app.services.datasource_service import DatasourceService
from app.services.full_engine import FullEngine
from app.services.orphan_service import OrphanService
from app.services.restore_service import RestoreService
from app.services.scheduler_service import SchedulerService
from app.services.verify_service import VerifyService

log = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动钩子：初始化仓库 / 主密钥 / 默认参数
    db: Database = app.state.db
    db.init_schema()
    app.state.settings.ensure_defaults()
    app.state.limiter.set_global_rate(int(app.state.settings.get("global_rate_limit_rows") or 20000))
    fp = app.state.key_manager.fingerprint()
    log.info("启动完成：主密钥指纹 %s…，schema 就绪", fp[:8])
    # M2：WS 进度每秒推送循环（task.progress，§4.2.1 进度模型）
    app.state.progress_task = asyncio.create_task(_progress_loop(app))
    # M3：增量监控循环（task.lag / task.stats / 延迟告警与解除 / 1 分钟历史采样）
    app.state.cdc_monitor_task = asyncio.create_task(_cdc_monitor_loop(app))
    # M3 启动自检（T3.7）：崩溃前处于增量运行态的任务自动续跑
    recovered = app.state.cdc.recover_on_startup()
    if recovered:
        log.info("启动自检：%s 个增量任务已恢复续跑 %s", len(recovered), recovered)
        app.state.alert_repo.fire("app_recovered", f"应用重启，增量任务自动续跑：{recovered}",
                                  level="info")
    # M4：调度服务启动（schedule 表权威配置，重启后计划自动恢复 + 错过补偿）
    app.state.scheduler.start()
    n_schedules = len(app.state.schedule_repo.list(enabled_only=True))
    if n_schedules:
        log.info("调度服务就绪：%s 个启用计划已注册", n_schedules)
    yield
    app.state.scheduler.shutdown()
    app.state.batch_init.shutdown()
    app.state.progress_task.cancel()
    app.state.cdc_monitor_task.cancel()
    log.info("应用关闭")


async def _progress_loop(app: FastAPI):
    """每秒广播活跃 run 的 task.progress（引擎线程写内存模型）。"""
    broadcaster: WSBroadcaster = app.state.broadcaster
    tracker: ProgressTracker = app.state.tracker
    while True:
        try:
            await asyncio.sleep(1.0)
            for rp in tracker.active():
                payload = rp.snapshot()
                await broadcaster.broadcast("task.progress", payload,
                                            task_id=str(rp.task_id) if rp.task_id else None)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("进度推送循环异常")


async def _cdc_monitor_loop(app: FastAPI):
    """每秒：task.lag / task.stats 推送 + 延迟阈值告警与 50% 回落解除（T3.6）。"""
    cdc = app.state.cdc
    broadcaster: WSBroadcaster = app.state.broadcaster
    last_minute: dict[int, int] = {}
    while True:
        try:
            await asyncio.sleep(1.0)
            payloads = cdc.monitor_tick()   # 线程安全的快照 + WS emit（线程侧投递）
            for p in payloads:
                tid = p["task_id"]
                minute = int(time.time() * 1000) // 60000
                if last_minute.get(tid) != minute and p.get("lag_seconds") is not None:
                    last_minute[tid] = minute
                    cdc.record_minute_sample(tid, minute * 60000, p["lag_seconds"])
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("增量监控循环异常")
        _ = broadcaster


def create_app() -> FastAPI:
    app = FastAPI(title="数据库同步工具", version="0.6.1-M6", lifespan=lifespan)

    # ---- state 装配 ----
    db = Database()
    app.state.db = db
    # 全新/被删空的元数据库：装配期即执行幂等迁移（P0 修复）。
    # 此前 schema 仅在 lifespan 初始化，而装配本身（如 batch_init_parallel 读取
    # app_setting）已发生 DB 查询 → 空库首次启动/删库重启直接崩溃。
    db.init_schema()
    app.state.settings = SettingRepository(db)
    app.state.audit = AuditRepository(db)
    app.state.schedule_repo = ScheduleRepository(db)
    app.state.verify_repo = VerifyResultRepository(db)
    app.state.key_manager = MasterKeyManager()
    app.state.sessions = SessionStore()
    app.state.guard = LoginGuard()
    app.state.auth = AuthService(app.state.settings, app.state.sessions, app.state.guard)
    app.state.broadcaster = WSBroadcaster()
    app.state.tracker = ProgressTracker()
    app.state.limiter = TwoLevelLimiter(global_rate=20000)  # 全局总闸默认值；lifespan 按设置校准

    from app.core.security import CredentialCipher
    app.state.cipher = CredentialCipher(app.state.key_manager)

    ds_repo = DatasourceRepository(db)
    app.state.ds_service = DatasourceService(ds_repo, app.state.cipher, app.state.audit)

    def _resolve_source(x: dict):
        """入参可为任务行（含 source_id/target_id）或数据源行，统一解密出 MySQLTarget。"""
        if "source_id" in x and "target_id" in x:
            x = ds_repo.get(x["source_id"]) or {}
        return app.state.ds_service._to_target(x)

    def _resolve_target(x: dict):
        if "source_id" in x and "target_id" in x:
            x = ds_repo.get(x["target_id"]) or {}
        return app.state.ds_service._to_target(x)

    # ---- M2 核心装配 ----
    app.state.task_repo = TaskRepository(db)
    app.state.run_repo = RunRepository(db)
    app.state.position_repo = PositionRepository(db)
    app.state.backup_repo = BackupRepository(db)
    app.state.ddl_repo = DdlPendingRepository(db)
    app.state.lag_repo = CdcRunRepository(db)
    app.state.orch = Orchestrator(app.state.task_repo, app.state.broadcaster,
                                  run_repo=app.state.run_repo)
    # ---- M4 告警服务：存储（alert_log）+ 通道触达 + WS 推送（对外接口兼容 AlertRepository） ----
    app.state.alert_repo = AlertService(AlertRepository(db), app.state.settings,
                                        app.state.broadcaster, cipher=app.state.cipher)
    app.state.backup_service = BackupService(
        backups=app.state.backup_repo, runs=app.state.run_repo,
        settings=app.state.settings, tracker=app.state.tracker,
        get_source=_resolve_source, alert_repo=app.state.alert_repo,
        broadcaster=app.state.broadcaster)
    # ---- M3 增量引擎（接口抽象：CdcEngine 实现 IncrementalEngine） ----
    app.state.cdc = CdcEngine(
        tasks=app.state.task_repo, runs=app.state.run_repo,
        positions=app.state.position_repo, settings=app.state.settings,
        orchestrator=app.state.orch, limiter=app.state.limiter,
        get_source=_resolve_source, get_target=_resolve_target,
        ddl_repo=app.state.ddl_repo, alert_repo=app.state.alert_repo,
        audit=app.state.audit, broadcaster=app.state.broadcaster,
        lag_repo=app.state.lag_repo, cache_dir=DATA_DIR / "cdc_cache")
    app.state.engine = FullEngine(
        tasks=app.state.task_repo, runs=app.state.run_repo,
        positions=app.state.position_repo, settings=app.state.settings,
        orchestrator=app.state.orch, tracker=app.state.tracker,
        limiter=app.state.limiter, get_source=_resolve_source, get_target=_resolve_target,
        backup_service=app.state.backup_service, broadcaster=app.state.broadcaster,
        cdc_engine=app.state.cdc, alert_repo=app.state.alert_repo)
    app.state.restore_service = RestoreService(
        backups=app.state.backup_repo, runs=app.state.run_repo, tasks=app.state.task_repo,
        settings=app.state.settings, tracker=app.state.tracker, limiter=app.state.limiter,
        get_target=_resolve_target, backup_service=app.state.backup_service,
        audit=app.state.audit, alert_repo=app.state.alert_repo,
        broadcaster=app.state.broadcaster,
        orch=app.state.orch, cdc=app.state.cdc)
    # ---- M4：校验服务 / 孤儿行清理 / 调度服务 ----
    app.state.verify_service = VerifyService(
        tasks=app.state.task_repo, runs=app.state.run_repo,
        verify_repo=app.state.verify_repo, settings=app.state.settings,
        tracker=app.state.tracker, get_source=_resolve_source, get_target=_resolve_target,
        alert_repo=app.state.alert_repo, broadcaster=app.state.broadcaster)
    app.state.orphan_service = OrphanService(
        tasks=app.state.task_repo, runs=app.state.run_repo, tracker=app.state.tracker,
        get_source=_resolve_source, get_target=_resolve_target,
        alert_repo=app.state.alert_repo, broadcaster=app.state.broadcaster)
    app.state.scheduler = SchedulerService(
        schedules=app.state.schedule_repo, tasks=app.state.task_repo,
        runs=app.state.run_repo, settings=app.state.settings, orch=app.state.orch,
        engine=app.state.engine, cdc=app.state.cdc,
        backup_service=app.state.backup_service, orphan_service=app.state.orphan_service,
        verify_service=app.state.verify_service, alert_service=app.state.alert_repo,
        audit=app.state.audit, broadcaster=app.state.broadcaster)
    app.state.engine.verify_service = app.state.verify_service
    # ---- M6：批量初始化排队器（默认并发读全局设置，低性能源库可调 1） ----
    app.state.batch_init = BatchInitService(
        engine=app.state.engine, tasks=app.state.task_repo, audit=app.state.audit,
        default_parallel=int(app.state.settings.get("batch_init_parallel") or 2))

    # ---- WS 快照提供器（重连补发，§5.3） ----
    def _snapshot() -> dict:
        import time as _t
        tasks = [{"id": t["id"], "name": t["name"], "mode": t["mode"], "state": t["state"]}
                 for t in app.state.task_repo.list()]
        runs = [{"run_id": rp.run_id, "task_id": rp.task_id, **rp.snapshot()}
                for rp in app.state.tracker.active()]
        return {"tasks": tasks, "runs": runs, "alerts": app.state.alert_repo.list_recent(10),
                "server_time": int(_t.time() * 1000)}

    app.state.broadcaster.snapshot_provider = _snapshot

    # ---- 统一错误结构（§5.1） ----
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "body"
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_FAILED",
            "message": f"参数「{field}」校验失败：{first.get('msg', '')}",
            "hint": "请按接口文档检查字段类型与取值范围"}})

    # ---- 路由 ----
    app.include_router(auth_routes.router)
    app.include_router(ds_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(task_routes.router)
    app.include_router(backup_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(run_routes.router)
    app.include_router(schedule_routes.router)
    app.include_router(verify_routes.router)
    app.include_router(ws_routes.router)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": app.version}

    # ---- 静态前端托管（frontend 构建产物输出至 frontend/dist） ----
    if FRONTEND_DIST_DIR.exists():
        if (FRONTEND_DIST_DIR / "assets").exists():
            app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            # 路径穿越防护（B1）：解码后的 ../ 不得逃出 dist 目录
            dist_root = FRONTEND_DIST_DIR.resolve()
            file = (FRONTEND_DIST_DIR / full_path).resolve()
            if (full_path and file.is_file() and file.is_relative_to(dist_root)):
                return FileResponse(file)
            return FileResponse(dist_root / "index.html")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    from app.config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)

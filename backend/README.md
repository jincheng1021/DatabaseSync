# 数据库同步工具 · 后端（M1 基础框架）

FastAPI 单体后端：元数据仓库（SQLite WAL）、凭据加密（AES-256-GCM）、认证会话、数据源管理与环境预检、审计、WebSocket 通道。

## 启动

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> ⚠️ **主密钥备份**：`data/keys/master.key` 为凭据加密主密钥（AES-256-GCM）。
> 该文件丢失将导致已保存的数据库密码无法解密 —— 请务必与 `data/` 一并备份。

## 目录

- `app/api/` 路由层（auth / datasources / settings / ws）
- `app/services/` 服务层（datasource / precheck / mysql_client）
- `app/core/` 编排基础（errors / security / sessions / ws_broadcaster / audit / logging）
- `app/db/` SQLite WAL + 版本化迁移（migration 1：datasource、datasource_precheck_result、audit_log、app_setting）
- `tests/` M1 测试集（31 例：加密往返 / 会话锁定 / 预检分支 / 集成全链路 / WS 鉴权）

## M1 交付判定对应

| 判定项 | 落点 |
| --- | --- |
| 双库预检全绿（含 RELOAD / SHOW_ROUTINE / EVENT） | `services/precheck_service.py`（E1 权限清单 + 8.0/5.7 分支） |
| 密码落库密文 | `core/security.py` + `models/repository.py`（password_enc） |
| WS 通且未授权被拒 | `api/ws.py`（握手会话校验，4401 + 审计，E10） |
| 未登录 API 全 401 / bootstrap 一次性 / 锁定 | `api/deps.py` + `core/sessions.py` |

## 运行数据卷

- `data/metadata.db`（SQLite WAL）+ `data/keys/master.key`（权限 600，目录隔离，E4）
- `logs/` 按天滚动，保留 30 天
- `backups/`（M2 启用）

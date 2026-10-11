"""数据源服务（T1.5 / §4.1）：生命周期 + 凭据加密出库 + 预检编排。"""
from __future__ import annotations

import logging

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.security import CredentialCipher
from app.models.repository import AuditRepository, DatasourceRepository
from app.services.mysql_client import MySQLTarget
from app.services.precheck_service import list_tables, run_precheck

log = logging.getLogger("dsync.datasource")

MASK = "******"

# 请求体允许的字段
EDITABLE = ("name", "host", "port", "username", "password", "ssl_mode", "default_schema",
            "charset", "connect_timeout", "note", "ssl_ca", "ssl_cert", "ssl_key")


class DatasourceService:
    def __init__(self, repo: DatasourceRepository, cipher: CredentialCipher, audit: AuditRepository):
        self.repo = repo
        self.cipher = cipher
        self.audit = audit

    # ---- 出参脱敏：密码/证书只写不读（UI 永不回显明文，仅掩码） ----
    @staticmethod
    def _mask(d: dict) -> dict:
        d = dict(d)
        # P2 修复：按密文是否实际存在置位（修复前无条件 True，未设密码也报「已配置」）
        d["has_password"] = bool(d.get("password_enc"))
        for col in ("password_enc", "ssl_ca_enc", "ssl_cert_enc", "ssl_key_enc"):
            d.pop(col, None)
        return d

    def create(self, body: dict, actor: str, ip: str | None) -> dict:
        self._validate(body)
        if self.repo.get_by_name(body["name"]):
            raise ConflictError(f"数据源名称「{body['name']}」已存在", code="NAME_DUP")
        enc = {"password_enc": self.cipher.encrypt(body["password"]) if body.get("password") else None}
        for k in ("ssl_ca", "ssl_cert", "ssl_key"):
            if body.get(k):
                enc[f"{k}_enc"] = self.cipher.encrypt(body[k])
        ds_id = self.repo.create({**body, **enc})
        self.audit.record(actor, "datasource.create", ip=ip, object_type="datasource",
                          object_id=ds_id, after=self._public(body))
        return self._mask(self.repo.get(ds_id))

    def update(self, ds_id: int, body: dict, actor: str, ip: str | None) -> dict:
        old = self.repo.get(ds_id)
        if not old:
            raise NotFoundError("数据源不存在")
        before_public = self._mask(old)
        patch: dict = {k: v for k, v in body.items() if k in EDITABLE and v is not None}
        if "name" in patch:
            dup = self.repo.get_by_name(patch["name"])
            if dup and dup["id"] != ds_id:
                raise ConflictError(f"数据源名称「{patch['name']}」已存在", code="NAME_DUP")
        # 编辑留空（None/空串）表示密码不变更（§4.1）
        if body.get("password") is None or body.get("password") == "":
            patch.pop("password", None)
        else:
            patch["password_enc"] = self.cipher.encrypt(body["password"])
        for k in ("ssl_ca", "ssl_cert", "ssl_key"):
            v = patch.pop(k, None)
            if v:
                patch[f"{k}_enc"] = self.cipher.encrypt(v)
        patch.pop("password", None)
        self.repo.update(ds_id, patch)
        self.audit.record(actor, "datasource.update", ip=ip, object_type="datasource",
                          object_id=ds_id, before=before_public, after=self._public(patch))
        return self._mask(self.repo.get(ds_id))

    def delete(self, ds_id: int, actor: str, ip: str | None) -> None:
        old = self.repo.get(ds_id)
        if not old:
            raise NotFoundError("数据源不存在")
        refs = self.repo.count_tasks_referencing(ds_id)
        if refs > 0:
            raise ConflictError(f"该数据源被 {refs} 个同步任务引用，禁止删除", code="REFERENCED",
                                hint="先删除引用它的同步任务")
        self.repo.delete(ds_id)
        self.audit.record(actor, "datasource.delete", ip=ip, object_type="datasource",
                          object_id=ds_id, before=self._mask(old))

    def clone(self, ds_id: int, actor: str, ip: str | None) -> dict:
        """克隆（E14）：凭据独立重新加密存储（新 nonce）。"""
        old = self.repo.get(ds_id)
        if not old:
            raise NotFoundError("数据源不存在")
        base = f"{old['name']}-副本"
        name, n = base, 2
        while self.repo.get_by_name(name):
            name = f"{base}{n}"
            n += 1
        new_id = self.repo.create({
            "name": name, "host": old["host"], "port": old["port"], "username": old["username"],
            "password_enc": self._reencrypt(old.get("password_enc")),
            "ssl_mode": old["ssl_mode"], "ssl_ca_enc": self._reencrypt(old.get("ssl_ca_enc")),
            "ssl_cert_enc": self._reencrypt(old.get("ssl_cert_enc")),
            "ssl_key_enc": self._reencrypt(old.get("ssl_key_enc")),
            "default_schema": old["default_schema"], "charset": old["charset"],
            "connect_timeout": old["connect_timeout"], "note": old["note"],
        })
        self.audit.record(actor, "datasource.clone", ip=ip, object_type="datasource",
                          object_id=new_id, after={"from_id": ds_id, "name": name})
        return self._mask(self.repo.get(new_id))

    def _reencrypt(self, token: str | None) -> str | None:
        """解出明文重新加密（生成新 nonce，保证凭据独立存储）。"""
        plain = self.cipher.try_decrypt(token)
        return self.cipher.encrypt(plain) if plain is not None else None

    def list(self) -> list[dict]:
        out = []
        for d in self.repo.list():
            d = self._mask(d)
            pre = self.repo.get_precheck(d["id"])
            d["precheck"] = {"overall": pre["overall"], "checked_at": pre["checked_at"]} if pre else None
            d["referenced_tasks"] = self.repo.count_tasks_referencing(d["id"])
            out.append(d)
        return out

    def get(self, ds_id: int) -> dict:
        d = self.repo.get(ds_id)
        if not d:
            raise NotFoundError("数据源不存在")
        d = self._mask(d)
        d["precheck"] = self.repo.get_precheck(ds_id)
        d["referenced_tasks"] = self.repo.count_tasks_referencing(ds_id)
        return d

    # ---- 预检 ----
    def precheck(self, ds_id: int, role: str, actor: str, ip: str | None) -> dict:
        d = self.repo.get(ds_id)
        if not d:
            raise NotFoundError("数据源不存在")
        target = self._to_target(d)
        overall, items = run_precheck(target, role)
        self.repo.save_precheck(ds_id, overall, items)
        self.audit.record(actor, "datasource.precheck", ip=ip, object_type="datasource",
                          object_id=ds_id, after={"overall": overall})
        return {"datasource_id": ds_id, "overall": overall, "items": items}

    def tables(self, ds_id: int, schema: str | None):
        d = self.repo.get(ds_id)
        if not d:
            raise NotFoundError("数据源不存在")
        return list_tables(self._to_target(d), schema)

    def schemas(self, ds_id: int) -> list[str]:
        """列出该实例上的业务库（排除系统库），供任务向导选择源库/目标库。"""
        from app.services.mysql_client import fetch_all_lc, open_connection
        d = self.repo.get(ds_id)
        if not d:
            raise NotFoundError("数据源不存在")
        conn = open_connection(self._to_target(d), None)
        try:
            rows = fetch_all_lc(conn,
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME NOT IN ('mysql','sys','information_schema','performance_schema') "
                "ORDER BY SCHEMA_NAME")
            return [r["schema_name"] for r in rows]
        finally:
            conn.close()

    def table_meta(self, ds_id: int, schema: str | None) -> dict:
        """T4.6 表映射编辑器元数据：表清单 + 每表列（可空/默认值/键）+ 主键。

        供前端：无主键表红标、列过滤候选列、行过滤条件构建器候选列。
        """
        from app.services.mysql_client import fetch_all_lc, open_connection
        d = self.repo.get(ds_id)
        if not d:
            raise NotFoundError("数据源不存在")
        t = self._to_target(d)
        conn = open_connection(t, schema)
        try:
            if not schema:
                schema = t.default_schema
            if not schema:
                # 数据源未设置 default_schema：回退到第一个业务库，避免 table_schema=NULL 恒空
                rows = fetch_all_lc(conn,
                    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME NOT IN ('mysql','sys','information_schema','performance_schema') "
                    "ORDER BY SCHEMA_NAME LIMIT 1")
                schema = rows[0]["schema_name"] if rows else None
            if not schema:
                return {"schema": None, "tables": []}
            # fetch_all_lc：information_schema 列名大小写随服务端而异（MariaDB 恒大写），统一小写取值
            tables = fetch_all_lc(
                conn,
                "SELECT table_name, table_type, table_rows FROM information_schema.tables "
                "WHERE table_schema=%s ORDER BY table_name", (schema,))
            columns = fetch_all_lc(
                conn,
                "SELECT table_name, column_name, is_nullable, column_default, column_key, data_type "
                "FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name, ordinal_position",
                (schema,))
            by_table: dict[str, list[dict]] = {}
            for c in columns:
                by_table.setdefault(c["table_name"], []).append({
                    "name": c["column_name"],
                    "nullable": c["is_nullable"] == "YES",
                    "column_default": c["column_default"],
                    "column_key": c["column_key"],
                    "data_type": c["data_type"],
                })
            out = []
            for tb in tables:
                name = tb["table_name"]
                cols = by_table.get(name, [])
                pk = [c["name"] for c in cols if c["column_key"] == "PRI"]
                out.append({"name": name, "type": tb.get("table_type"),
                            "rows_est": int(tb.get("table_rows") or 0),
                            "primary_key": pk, "has_pk": bool(pk),
                            "columns": cols})
            return {"schema": schema, "tables": out}
        finally:
            conn.close()

    # ---- helpers ----
    def _to_target(self, d: dict) -> MySQLTarget:
        enc = d.get("password_enc")
        if enc:
            plain = self.cipher.try_decrypt(enc)
            if plain is None:
                # P2 修复：解密失败显式报错（修复前静默回退空密码 → 连接报「认证失败」误导排障）
                raise AppError(
                    f"数据源「{d.get('name')}」凭据解密失败（主密钥不匹配或密文损坏）",
                    hint="请确认 data/keys/master.key 为加密该凭据时所用密钥；必要时重新编辑并保存密码")
            password = plain
        else:
            password = ""
        return MySQLTarget(
            host=d["host"], port=int(d["port"]), username=d["username"],
            password=password,
            ssl_mode=d.get("ssl_mode", "disabled"), charset=d.get("charset", "utf8mb4"),
            connect_timeout=int(d.get("connect_timeout") or 10),
            default_schema=d.get("default_schema"),
        )

    @staticmethod
    def _validate(body: dict) -> None:
        for f in ("name", "host", "username"):
            if not str(body.get(f) or "").strip():
                raise ValidationError(f"缺少必填字段：{f}")
        try:
            port = int(body.get("port") or 3306)
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            raise ValidationError("端口必须是 1~65535 的整数")
        if body.get("ssl_mode") not in (None, "disabled", "encrypt", "verify_ca", "full"):
            raise ValidationError("ssl_mode 仅支持 disabled/encrypt/verify_ca/full")

    @staticmethod
    def _public(body: dict) -> dict:
        return {k: v for k, v in body.items() if "password" not in k and "ssl_ca" not in k and "ssl_cert" not in k and "ssl_key" not in k}

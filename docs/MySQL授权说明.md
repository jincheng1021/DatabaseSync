# MySQL 授权说明（E1）

> 依据：设计方案 §4.1 预检权限清单 / §11 E1。工具启动前请按下文为同步账号授权；预检页会逐项核对并给出缺失项。

## 1. 源库账号（最小权限）

```sql
-- ① 基础读取与结构
GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO 'dsync'@'%';

-- ② RELOAD：FTWRL 取一致位点 P0 必需（新库初始化 / 全量重刷 / 备份导出）
GRANT RELOAD ON *.* TO 'dsync'@'%';

-- ③ 增量订阅
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'dsync'@'%';

-- ④ 存储过程/函数定义读取（按版本二选一）
-- MySQL 8.0：
GRANT SHOW_ROUTINE ON *.* TO 'dsync'@'%';
-- MySQL 5.7（读 mysql.proc）：
GRANT SELECT ON mysql.proc TO 'dsync'@'%';

-- ⑤ 定时事件定义读取（涉及 EVENT 迁移时）
GRANT EVENT ON *.* TO 'dsync'@'%';

FLUSH PRIVILEGES;
```

权限 → 功能影响对照（缺失时预检逐项标红/标黄）：

| 权限 | 影响功能 |
| --- | --- |
| SELECT | 全量拷贝、校验 |
| RELOAD | 一致位点 P0（FTWRL）；缺失则初始化/重刷/备份失败 |
| REPLICATION SLAVE / CLIENT | 增量同步（CDC） |
| SHOW VIEW | 视图结构迁移 |
| PROCESS | E7 锁等待诊断（FTWRL 超时时的 PROCESSLIST 摘要） |
| SHOW_ROUTINE（8.0）/ mysql.proc SELECT（5.7） | 存储过程/函数迁移 |
| EVENT | 定时事件迁移 |

最小化替代：若同步范围明确限定库，`SHOW_ROUTINE`/`EVENT` 可改为库级
`GRANT SHOW_ROUTINE, EVENT ON db1.* TO ...`（8.0 支持）。

## 2. 目标库账号

```sql
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX
  ON 目标库.* TO 'dsync'@'%';
FLUSH PRIVILEGES;
```

说明：

- DDL 决策为「执行」时需要对目标库的 CREATE/ALTER/DROP/INDEX；
- 校验修复（REPLACE + 删除目标多余行）需要 INSERT/UPDATE/DELETE；
- 恢复（DROP TABLE IF EXISTS + 重建）需要 DROP/CREATE；
- **孤儿行清理（2026-10-12 修复后）**：在目标库会话内创建临时表做集合差比对，
  需要 `CREATE TEMPORARY TABLES` 权限（库级即可）。缺失时报错信息含权限名；
  临时表随会话结束自动消亡，不落 binlog、不占长期空间：

  ```sql
  GRANT CREATE TEMPORARY TABLES ON 目标库.* TO 'dsync'@'%';
  ```

## 3. 认证插件：caching_sha2_password（MySQL 8.0）

预检规则：账号插件为 `caching_sha2_password` **且未启用 SSL** → 预检失败（binlog dump 认证将失败）。

二选一处理：

```sql
-- 方案 A：专用同步账号改用原生插件（推荐，配合 SSL 亦可）
CREATE USER 'dsync'@'%' IDENTIFIED WITH mysql_native_password BY '强密码';
-- MySQL 8.4+ 注意：mysql_native_password 默认禁用，需在 my.cnf 加
--   mysql_native_password=ON（8.4 兼容适配登记为 §18 O3）

-- 方案 B：保留 caching_sha2_password，启用 SSL 连接
-- 工具侧数据源配置 ssl_mode=REQUIRED（或 VERIFY_*）
```

核查当前插件：

```sql
SELECT user, host, plugin FROM mysql.user WHERE user='dsync';
```

## 4. 其他源库参数要求（预检同步核对）

| 参数 | 要求 | 原因 |
| --- | --- | --- |
| log_bin | ON | CDC 前提 |
| binlog_format | ROW | 行级事件解析 |
| binlog_row_image | FULL | REPLACE 幂等写依赖完整行镜像 |
| gtid_mode | ON（建议） | 自动位点定位；否则 file+pos |
| binlog 保留 | ≥ 24 小时 | 停机超保留期 → 位点失效需重新初始化 |
| server_id | 非 0 | 工具以独立 id 订阅，冲突时自动避让 |

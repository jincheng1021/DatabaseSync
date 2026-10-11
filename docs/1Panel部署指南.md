# 1Panel 部署指南（数据库同步工具）

> 适用版本：0.6.0-M6 及以上 ｜ 配套文件：`deploy/docker-compose.yml`、`deploy/Dockerfile`、`.dockerignore`
> 本指南替代《部署手册》§2.3 的 Caddy HTTPS 方案——1Panel 环境统一使用 **1Panel 网站反向代理 + 证书** 承担 HTTPS。
> 非常驻 Docker 技能要求：只需会使用 1Panel 的「文件 / 编排 / 网站 / 计划任务」四个界面功能。

## 0. 1Panel 适配改了什么

| 项 | 原方案 | 1Panel 适配后 | 原因 |
| --- | --- | --- | --- |
| HTTPS 反代 | Caddy 容器（https profile） | **移除**，改用 1Panel「网站 → 反向代理 + 证书」 | 1Panel 自带 OpenResty 与证书申请/续期，重复部署徒增排障面 |
| Docker 网络 | 默认网络（随机名） | 显式命名 `dsync-net` | OpenResty 加入后可 `proxy_pass http://dsync:8080` 直连容器，8080 无需对公网开放 |
| 时区 | 容器默认 UTC | 镜像装 tzdata + `TZ=Asia/Shanghai` | 调度器 cron 按本地时间触发，UTC 会让计划任务整体偏移 8 小时 |
| 容器日志 | 未限制 | json-file 10MB × 3 轮转 | 防止 stdout 日志撑爆磁盘（1Panel 服务器常年在线） |
| 构建源 | npmjs / PyPI 官方源 | 默认 npmmirror / 清华源（.env 可覆盖） | 国内服务器构建提速数倍 |
| 构建上下文 | 无 .dockerignore | 新增项目根 `.dockerignore` | 排除 node_modules（258MB+）、本地 db、文档，构建上下文从数百 MB 降到几 MB |

---

## 1. 服务器要求

| 项 | 要求 |
| --- | --- |
| 1Panel | v1.10+（已内置 Docker / Docker Compose v2 / OpenResty） |
| 内存 | ≥ 2 GB |
| 磁盘 | 数据体量 × 2（备份产物）+ 日志余量 |
| 网络 | 可达源/目标 MySQL（3306）；浏览器可达 1Panel 面板 |

---

## 2. 需要上传的文件（清单）

在服务器上创建项目目录 `/opt/dsync`，按以下结构上传（**绿色对勾为必须上传**）：

```
/opt/dsync/
├── .dockerignore              ✅ 项目根目录下（注意是隐藏文件，勿漏传）
├── backend/
│   ├── app/                   ✅ 整个 app 目录（含全部子目录与 .py 文件）
│   └── requirements.txt       ✅
├── frontend/
│   ├── package.json           ✅
│   ├── package-lock.json      ✅
│   ├── index.html             ✅
│   ├── vite.config.ts         ✅
│   ├── tsconfig.json          ✅
│   └── src/                   ✅ 整个 src 目录
│   （node_modules/ ❌ 不上传——容器内自动安装，258MB 传了也白传）
│   （dist/ ❌ 不上传——容器内重新构建）
└── deploy/
    ├── docker-compose.yml     ✅
    ├── Dockerfile             ✅
    ├── upgrade.sh             ✅（后续升级用）
    └── Caddyfile              ⬜ 可不传（仅非 1Panel 环境留档）
    └── systemd/               ⬜ 可不传（裸机方案用，容器部署无关）
```

**一句话版**：把本地项目里的 `backend/`（只要 `app/` 和 `requirements.txt`）、`frontend/`（只要源码和 package 清单，**不要 node_modules 和 dist**）、`deploy/`（只要 `docker-compose.yml`、`Dockerfile`、`upgrade.sh`）、根目录 `.dockerignore` 传到 `/opt/dsync/` 即可。

> ⚠️ `.dockerignore` 是以点开头的隐藏文件，FTP/SFTP 工具默认可能不显示，务必确认已上传到 `/opt/dsync/.dockerignore`——缺了它首次构建会打包 258MB 的 node_modules。

---

## 3. 上传方式

**方式 A（推荐）：1Panel 文件管理**
1. 1Panel → 主机 → 文件 → 进入 `/opt` → 创建目录 `dsync`；
2. 本地先把要传的内容打成一个 zip（不含 node_modules），上传 zip 后右键「解压」到 `/opt/dsync`，比逐文件上传快得多；
3. 解压后按 §2 清单核对结构（本地打 zip 时目录层级要与清单一致）。

**方式 B：SFTP 工具（XFTP / WinSCP 等）** 直接按 §2 结构上传。

**方式 C：Git** 若代码在 Git 仓库，服务器上 `git clone` 到 `/opt/dsync` 亦可（.dockerignore 会自动排除无关文件，但仓库里若含 node_modules 需先清理）。

---

## 4. 首次启动前的准备（必须执行）

数据卷目录由运行账户（uid=999）写入，**首次启动前**必须建好目录并授权，否则容器启动即报权限错误。用 1Panel「主机 → 终端」或 SSH 执行：

```bash
cd /opt/dsync/deploy
mkdir -p data backups logs
chown -R 999:999 data backups logs
```

（可选）同目录创建 `.env` 调整端口绑定——不创建则用默认值 `0.0.0.0:8080`：

```bash
cat > /opt/dsync/deploy/.env <<'EOF'
DSYNC_BIND=0.0.0.0
DSYNC_PORT=8080
# NPM_REGISTRY=https://registry.npmjs.org
# PIP_INDEX_URL=https://pypi.org/simple
EOF
```

---

## 5. 创建编排（1Panel 界面操作）

1. 1Panel → **容器 → 编排 → 创建编排**；
2. 名称：`dsync`；
3. **来源选「路径」**（⚠️ 不要选「模板/编辑内容」——粘贴方式会把 compose 保存到 1Panel 内部目录，`context: ..` 相对路径会失效）；
4. 路径选择：`/opt/dsync/deploy`；
5. 等待状态变为「已启动」。首次会自动构建镜像（拉取 node/python 基础镜像 + npm/pip 安装，约 3~10 分钟，取决于服务器带宽），可在 1Panel「容器 → 镜像」看到 `dsync:latest`。

> 编排创建失败排查：1Panel → 容器 → 编排 → 点进 `dsync` 查看日志；常见原因是 `.dockerignore` 未上传（构建极慢）或第 4 节目录未授权（容器秒退）。

---

## 6. 首次初始化与冒烟验证

在 1Panel 终端按序执行：

```bash
# 1. 容器健康（STATUS 应为 Up (healthy)）
docker ps --filter name=dsync

# 2. 服务健康（核验版本号）
curl http://127.0.0.1:8080/api/health        # {"ok":true,"version":"0.6.0-M6"}

# 3. 首次 bootstrap（应 200；重复调用应 409 BOOTSTRAP_DONE）
curl -X POST http://127.0.0.1:8080/api/auth/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<强密码≥8位>"}'

# 4. 前端托管（应返回 200）
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
```

⚠️ **bootstrap 安全窗口**：初始化接口一次性，删库/首次启动后任何能访问 :8080 的人都可以抢先建管理员——完成冒烟后请立刻做完 §7 反代并限制 8080 对外访问，或先关掉 8080 放行规则。

随后浏览器打开 `http://服务器IP:8080` → 登录 → 录入源/目标数据源并跑预检 → 建任务（详见《用户手册》）。

---

## 7. 网站反向代理 + HTTPS（1Panel 承担）

### 7.1 创建反向代理网站

1. 1Panel → **网站 → 创建网站 → 反向代理**；
2. 主域名：填你的域名（如 `sync.example.com`），或无域名时用 `服务器IP` + 任意未占端口（如 `sync.local:80`）；
3. 代理地址（二选一）：

| 方案 | 代理地址 | 前提 |
| --- | --- | --- |
| **A（推荐，最简单）** | `http://服务器内网IP:8080`（如 `http://192.168.1.10:8080`） | 无，8080 只要在内网可达即可 |
| B（更安全，容器直连） | `http://dsync:8080` | OpenResty 容器加入 `dsync-net` 网络（见 7.3） |

4. 创建后进入网站设置确认代理生效。

### 7.2 必改：反向代理开启 WebSocket 支持

系统使用 WebSocket 推送告警与运行事件，**默认反代配置会掐断 WS**。1Panel → 网站 → 该站点 → 反向代理 → 编辑配置文件，在 `location /` 块中确认包含：

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

（1Panel 反代模板一般自带 Upgrade 头，重点核对 `proxy_read_timeout`——长连接默认 60s 断开会导致前端频繁重连。）

### 7.3（方案 B）OpenResty 加入 dsync-net

1Panel → 容器 → 容器 → 找到 `1Panel-openresty` 容器 → 若为编排创建，在其 compose 里给 openresty 服务追加：

```yaml
    networks:
      - 1panel-network
      - dsync-net

networks:
  1panel-network:
    external: true
    name: 1panel-network
  dsync-net:
    external: true
    name: dsync-net
```

然后重建 openresty 容器。之后站点代理地址填 `http://dsync:8080`，同时把 `.env` 中 `DSYNC_BIND` 改为 `127.0.0.1` 重建编排，8080 即完全不对外暴露。

### 7.4 HTTPS 证书

1. 1Panel → **网站 → 证书 → 申请证书**（Let's Encrypt，需域名已解析到本机）或上传已有证书；
2. 网站 → 该站点 → HTTPS → 选择证书，开启「强制 HTTPS」。

完成后浏览器 `https://sync.example.com` 即为正式入口。

---

## 8. 防火墙与安全

1. 1Panel → **主机 → 防火墙**：
   - 放行：`80`、`443`、1Panel 面板端口（SSH 端口按需）；
   - **不放行 `8080`**（走反代即可；确需直连调试时临时放行、用完删除）；
2. 云服务器安全组同步以上规则；
3. 数据源凭据加密主密钥位于 `data/keys/master.key`，权限已由镜像固定为运行账户；**务必另行备份**（见 §10）。

---

## 9. 升级与回滚

### 升级

```bash
# 1. 上传新代码到 /opt/dsync（覆盖 backend/app、frontend/src 等，方式同 §3）
# 2. 用 deploy/upgrade.sh（推荐，自动备份 SQLite 与位点）：
cd /opt/dsync/deploy
bash upgrade.sh <新版本号>
# 或手动：
docker compose -f /opt/dsync/deploy/docker-compose.yml build
docker compose -f /opt/dsync/deploy/docker-compose.yml up -d
```

升级后核验：`curl http://127.0.0.1:8080/api/health` 版本号已变、任务详情页增量位点未丢失。

### 回滚

```bash
docker tag dsync:<旧版本> dsync:latest
docker compose -f /opt/dsync/deploy/docker-compose.yml up -d
```

如需回档数据：先 `docker compose down`，把 `upgrade-backups/<时间戳>/` 内的 `metadata.db*` 覆盖回 `/opt/dsync/deploy/data/`，再启动。

> 也可在 1Panel → 容器 → 编排 → `dsync` →「重建」触发重建，但**必须先完成代码上传**；upgrade.sh 的自动备份步骤不可省。

---

## 10. 备份策略（1Panel 计划任务）

- **必须备份**：`/opt/dsync/deploy/data/metadata.db`（SQLite WAL，建议冷备）+ **`/opt/dsync/deploy/data/keys/master.key`（独立存放！）**。二者缺一恢复即不完整：主密钥丢失 = 全部数据源凭据密文不可解密。
- 1Panel → **计划任务 → 创建任务**：
  - 任务 1：Shell 脚本，每日凌晨执行——
    ```bash
    TS=$(date +%F_%H%M)
    mkdir -p /opt/dsync/backups/meta
    sqlite3 /opt/dsync/deploy/data/metadata.db ".backup /opt/dsync/backups/meta/metadata_${TS}.db"
    cp /opt/dsync/deploy/data/keys/master.key /opt/dsync/backups/meta/master.key_${TS}
    find /opt/dsync/backups/meta -mtime +30 -delete
    ```
  - 任务 2：1Panel「备份数据库/目录」类型，对 `/opt/dsync/backups` 整目录定期备份到云端/对象存储（注意 master.key 属高敏文件，云端存储需加密可信）。

---

## 11. 常见问题（FAQ）

| 现象 | 原因与处理 |
| --- | --- |
| 编排一直「启动中」或容器 unhealthy | 首次构建未完成（耐心等）或端口被占：`netstat -tlnp \| grep 8080`；1Panel 自身服务若占 8080，在 `.env` 改 `DSYNC_PORT=18080` 后重建编排，并同步修改反代地址 |
| 容器启动秒退，日志 Permission denied | 第 4 节 `chown -R 999:999 data backups logs` 未执行 |
| 调度计划没按设定时间触发 | 时区问题，确认镜像 TZ 生效：`docker exec dsync date` 应为 CST（+0800） |
| 反代后告警/运行日志不实时推送 | 反代未开 WebSocket（§7.2）或 `proxy_read_timeout` 过短 |
| 反代 502 | 代理地址写错（方案 B 需 OpenResty 已加入 dsync-net）；容器 unhealthy 时也会 502 |
| 前端能开但登录后接口报错 | bootstrap 未做或做了两次；用 §6 步骤 3 核对（重复返回 409 为正常） |
| 构建极慢/超时 | `.dockerignore` 未上传到 `/opt/dsync/`（上下文塞进了 node_modules）；或镜像源被墙，在 `.env` 覆盖 `NPM_REGISTRY`/`PIP_INDEX_URL` |
| 误删 metadata.db | 全自动重建库，但管理员需重新 bootstrap、业务配置全部重录——先读《部署手册》§6，核心是**平时做好 §10 备份** |

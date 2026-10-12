# ============================================================
# 数据库同步工具 · 多阶段构建（§12 / T5.5，1Panel 适配版）
#   Stage 1: Node 构建前端（Vite 产物 dist/）
#   Stage 2: Python 后端镜像，静态托管前端产物（单端口 :8080）
# 构建：docker build -f deploy/Dockerfile -t dsync:VERSION .
# 说明：
#   - 默认走国内镜像源（npm npmmirror / pip 清华），海外构建机
#     可用 build-arg 覆盖（见 docker-compose.yml build.args）
#   - 安装 tzdata 并固定 TZ=Asia/Shanghai：调度器 cron 依赖本地
#     时间，容器默认 UTC 会导致计划任务偏移 8 小时
# ============================================================

# ---------- Stage 1：前端构建 ----------
FROM node:22-alpine AS frontend-builder
WORKDIR /build
# 依赖层缓存：先拷贝清单再装依赖
COPY frontend/package.json frontend/package-lock.json ./
# 构建机可用 NPM_REGISTRY 换源（默认 npmmirror，海外可改 https://registry.npmjs.org）
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "${NPM_REGISTRY}" && npm ci
COPY frontend/ ./
# 直接调用 vite 构建（不含 vue-tsc，规避其脆弱性；类型检查由本地测试门禁承担）
RUN node node_modules/vite/bin/vite.js build

# ---------- Stage 2：运行镜像 ----------
# Python 3.12：与本地测试 venv（3.13）及 requirements 锁定版本兼容；APScheduler==3.11.3 是包版本，与 Python 无关
FROM python:3.12-slim AS runtime

# pip 换源（默认清华源，海外构建机可覆盖为官方源）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 安全基线：非 root 运行；固定 uid/gid=999（与部署手册 chown 999:999 指引对齐）
# 注意：/home/dsync 是【容器内】运行账户的家目录（仅 useradd 记录用，应用不读写它），
#       与宿主机部署目录 /opt/dsync 无关——容器内不存在 /opt/dsync，勿混淆
RUN groupadd -r -g 999 dsync && useradd -r -u 999 -g dsync -m -d /home/dsync dsync

# 时区数据：调度器 cron 依赖容器本地时间，缺 tzdata 时 TZ 不生效
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r /app/backend/requirements.txt

COPY backend/app /app/backend/app
COPY --from=frontend-builder /build/dist /app/frontend/dist

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DSYNC_DATA_DIR=/app/data \
    DSYNC_LOG_DIR=/app/logs \
    DSYNC_BACKUP_DIR=/app/backups \
    DSYNC_FRONTEND_DIST=/app/frontend/dist

# 运行数据三卷：元数据（含主密钥 keys/）、备份产物、运行日志
VOLUME ["/app/data", "/app/backups", "/app/logs"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4); \
sys.exit(0 if r.status==200 else 1)"

USER dsync

CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "/app/backend", \
     "--host", "0.0.0.0", "--port", "8080"]

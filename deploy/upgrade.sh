#!/usr/bin/env bash
# ============================================================
# 数据库同步工具 · 升级脚本（§12 / T5.5）
# 升级原则：
#   1. 升级前自动备份 SQLite（metadata.db + WAL/SHM）与主密钥指纹记录
#   2. 位点数据（binlog_position 表）随 SQLite 一起备份，升级后向后兼容校验
#   3. 镜像版本化：dsync:<version>，回滚 = 改回镜像 tag 重启
# 用法：./upgrade.sh <new-version>   示例：./upgrade.sh 1.0.0
# ============================================================
set -euo pipefail

VERSION="${1:?用法: ./upgrade.sh <new-version>}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="./upgrade-backups/${STAMP}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

echo "==> [1/5] 停止服务（保留容器，便于回滚）"
docker compose -f "${COMPOSE_FILE}" stop dsync

echo "==> [2/5] 备份 SQLite 与运行数据 → ${BACKUP_ROOT}"
mkdir -p "${BACKUP_ROOT}"
for f in metadata.db metadata.db-wal metadata.db-shm; do
  [ -f "./data/${f}" ] && cp "./data/${f}" "${BACKUP_ROOT}/"
done
# 主密钥只记录指纹（私钥本身不复制到备份目录，避免密文与密钥同处存放——E4 隔离原则）
if [ -f "./data/keys/master.key" ]; then
  sha256sum ./data/keys/master.key | awk '{print $1}' > "${BACKUP_ROOT}/master_key.sha256"
  echo "    主密钥指纹已记录（master_key.sha256）；密钥本体请按运维制度另行保管"
fi

echo "==> [3/5] 记录当前位点数据（升级后向后兼容校验依据）"
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps dsync \
  python - <<'PYEOF' > "${BACKUP_ROOT}/positions_before_upgrade.txt" 2>/dev/null || \
  echo "（容器内导出失败，可升级后人工比对 binlog_position 表）" > "${BACKUP_ROOT}/positions_before_upgrade.txt"
import sqlite3, os
db = sqlite3.connect(os.environ.get("DSYNC_DATA_DIR", "/app/data") + "/metadata.db")
for row in db.execute("SELECT task_id, gtid_set, binlog_file, binlog_pos FROM binlog_position"):
    print(*row, sep=" | ")
PYEOF

echo "==> [4/5] 拉取/构建新版本镜像 dsync:${VERSION}"
docker compose -f "${COMPOSE_FILE}" build --pull dsync
docker tag "$(docker compose -f "${COMPOSE_FILE}" images dsync -q)" "dsync:${VERSION}"

echo "==> [5/5] 启动新版本"
docker compose -f "${COMPOSE_FILE}" up -d dsync

echo ""
echo "升级完成。请人工核验："
echo "  1) curl http://127.0.0.1:8080/api/health 返回新版本号"
echo "  2) 前端任务详情页核对增量任务位点未丢失（对照 ${BACKUP_ROOT}/positions_before_upgrade.txt）"
echo "  3) 异常回滚：docker tag dsync:<old> dsync:latest && docker compose -f ${COMPOSE_FILE} up -d dsync"
echo "     （如需回档数据：将 ${BACKUP_ROOT}/metadata.db* 覆盖回 ./data/）"

"""M2 仓库层测试：任务 CRUD / 运行记录与断点 / 备份元数据与保留 / 位点。"""
from __future__ import annotations

from app.models.m2_repository import BackupRepository, RunRepository, TaskRepository


def test_task_crud_roundtrip(m2_repos, seed_ds):
    tasks: TaskRepository = m2_repos["tasks"]
    tid = tasks.create({"name": "t", "source_id": seed_ds[0], "target_id": seed_ds[1], "mode": "init_plus_cdc",
                        "mapping": {"scope": "all", "rename": {"a": "b"}}})
    t = tasks.get(tid)
    assert t["mapping"]["rename"] == {"a": "b"}
    assert t["safety_net_backup"] is True and t["orphan_cleanup"] is True
    tasks.update(tid, {"mapping": {"scope": "selected", "tables": ["x"]}, "rate_limit_rows": 100})
    t = tasks.get(tid)
    assert t["mapping"]["tables"] == ["x"] and t["rate_limit_rows"] == 100
    assert tasks.list_by_target(seed_ds[1])[0]["id"] == tid
    assert tasks.delete(tid)


def test_run_repo_tables_and_checkpoint(m2_repos, seed_ds, a_task):
    runs: RunRepository = m2_repos["runs"]
    rid = runs.start(task_id=a_task["id"], run_type="init", trigger="manual",
                     tables=["a", "b", "c"])
    runs.upsert_table(rid, "a", state="copying", rows_total=100)
    runs.upsert_table(rid, "a", rows_done=60, checkpoint={"last_pk": 60})
    assert runs.get_table_checkpoint(rid, "a") == {"last_pk": 60}
    runs.upsert_table(rid, "a", state="done", rows_done=100)
    run = runs.get(rid)
    assert run["state"] == "running" and run["trigger"] == "manual"
    runs.finish(rid, state="success", report={"ok": True})
    run = runs.get(rid)
    assert run["state"] == "success" and run["report"]["ok"] is True
    assert runs.count_tables(rid, "done") == 1
    items, total = runs.list(task_id=a_task["id"], run_type="init")
    assert total == 1 and items[0]["id"] == rid


def test_backup_repo_retention(m2_repos, tmp_path):
    backups: BackupRepository = m2_repos["backups"]
    ids = []
    for i in range(5):
        ids.append(backups.create({
            "datasource_id": 1, "file_name": f"b{i}.sql.gz",
            "file_path": str(tmp_path / f"b{i}.sql.gz"),
            "size_bytes": 10, "sha256": f"hash{i}", "rows_baseline": {"t": {"rows": i}},
            "retention_tag": "manual"}))
    assert backups.oldest_ids_by_tag("manual", 3) == sorted(ids[:2])
    row = backups.get(ids[0])
    assert row["rows_baseline"]["t"]["rows"] == 0
    assert backups.delete(ids[0])


def test_backup_restoring_mutex_flag(m2_repos):
    backups: BackupRepository = m2_repos["backups"]
    runs: RunRepository = m2_repos["runs"]
    bid = backups.create({"datasource_id": 1, "file_name": "b", "file_path": "x",
                          "size_bytes": 1, "sha256": "h"})
    assert backups.is_restoring(bid) is False
    rid = runs.start(task_id=None, run_type="restore", backup_id=bid)
    assert backups.is_restoring(bid) is True
    runs.finish(rid, state="failed")
    assert backups.is_restoring(bid) is False


def test_run_finish_progress_backfill(m2_repos):
    """执行历史进度回填：终态写入 rows_inserted/tables_done/tables_total（此前恒 0/0）。"""
    runs: RunRepository = m2_repos["runs"]
    rid = runs.start(task_id=None, run_type="init", trigger="manual", tables=["a", "b"])
    runs.finish(rid, state="success",
                progress={"rows_inserted": 12345, "tables_total": 2, "tables_done": 2})
    run = runs.get(rid)
    assert run["rows_inserted"] == 12345
    assert run["tables_total"] == 2 and run["tables_done"] == 2
    # 不传 progress 时不动进度列
    rid2 = runs.start(task_id=None, run_type="restore", trigger="manual")
    runs.finish(rid2, state="failed", error="x")
    run2 = runs.get(rid2)
    assert run2["rows_inserted"] == 0 and run2["tables_total"] == 0


def test_restore_table_detail_lifecycle(m2_repos):
    """恢复 run 表级明细闭环（修复前：预插 pending 后全程不更新 → 详情恒「等待/0 行」）。"""
    runs: RunRepository = m2_repos["runs"]
    rid = runs.start(task_id=None, run_type="restore", trigger="manual",
                     backup_id=6, tables=["t1", "t2", "t3"])
    # 回放中：表切换落 copying + 基准行数，INSERT 批落已回放行数（upsert 自带补插兜底）
    runs.upsert_table(rid, "t1", state="copying", rows_total=100)
    runs.upsert_table(rid, "t1", rows_done=60)
    # 收尾校验：逐表回填 done/mismatch + 实际行数
    runs.upsert_table(rid, "t1", state="done", rows_done=100, rows_total=100)
    runs.upsert_table(rid, "t2", state="mismatch", rows_done=7, rows_total=9)
    runs.finish(rid, state="success_with_mismatch",
                progress={"rows_inserted": 107, "tables_total": 3,
                          "tables_done": runs.count_tables(rid, "done")})
    run = runs.get(rid)
    assert run["tables_total"] == 3 and run["tables_done"] == 1   # mismatch 不计入 done
    assert run["rows_inserted"] == 107
    with runs.db.conn() as c:
        rows = c.execute("SELECT table_name, state FROM run_table_detail WHERE run_id=? ORDER BY id",
                         (rid,)).fetchall()
    assert {r["table_name"]: r["state"] for r in rows} == {
        "t1": "done", "t2": "mismatch", "t3": "pending"}


def test_position_p0_roundtrip(m2_repos, a_task):
    pos_repo = m2_repos["positions"]
    tid = a_task["id"]
    pos_repo.save_p0(tid, {"gtid_executed": "uuid:1-5", "binlog_file": "binlog.000003",
                         "binlog_pos": 8899, "server_id_used": 101, "extra": {"method": "ftwrl"}})
    p = pos_repo.get(tid)
    assert p["gtid_executed"] == "uuid:1-5" and p["binlog_pos"] == 8899
    assert p["extra"]["method"] == "ftwrl"
    pos_repo.save_p0(tid, {"gtid_executed": "uuid:1-9", "binlog_file": "binlog.000004",
                         "binlog_pos": 100, "server_id_used": 101})
    assert pos_repo.get(tid)["gtid_executed"] == "uuid:1-9"

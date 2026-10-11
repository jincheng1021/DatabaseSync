"""增量引擎接口抽象（T3.1 / §2.1 演进预留 / §4.3）。

上层（API / 编排器 / 全量引擎）只依赖本接口：
mysql-replication 适配器（MysqlBinlogAdapter）为首版实现，
未来替换 Debezium Server 适配器时上层零改动。

binlog 消费循环运行于独立线程，与服务主线程解耦（§2.1）。
"""
from __future__ import annotations

import abc


class IncrementalEngine(abc.ABC):
    """增量引擎接口：生命周期 + 观测 + 回调注入点。"""

    @abc.abstractmethod
    def start(self, task_id: int, *, trigger: str = "manual") -> int:
        """启动增量消费（从持久化位点续传），返回 cdc_session run id。"""

    @abc.abstractmethod
    def pause(self, task_id: int) -> None:
        """暂停：停止拉取 binlog，队列内事件排空提交后位点落盘。"""

    @abc.abstractmethod
    def resume(self, task_id: int) -> None:
        """恢复消费。"""

    @abc.abstractmethod
    def stop(self, task_id: int, *, timeout: float = 30.0) -> None:
        """停止：排空队列后释放连接，位点最终落盘。"""

    @abc.abstractmethod
    def is_running(self, task_id: int) -> bool:
        """是否处于消费线程活跃状态。"""

    @abc.abstractmethod
    def get_lag(self, task_id: int) -> float | None:
        """最近提交批的延迟秒数（最后事件源库时间戳 vs 本机时间）。"""

    @abc.abstractmethod
    def get_position(self, task_id: int) -> dict | None:
        """当前位点快照（GTID 集合 / file+pos）。"""

    @abc.abstractmethod
    def get_stats(self, task_id: int) -> dict:
        """窗口期增/改/删计数与队列水位。"""

    @abc.abstractmethod
    def lag_history(self, task_id: int, minutes: int = 1440) -> list[dict]:
        """1 分钟粒度延迟历史曲线 [{ts, lag_seconds}]。"""

    # ---- 重刷-增量串行编排（T3.8）：全量引擎回调 ----
    @abc.abstractmethod
    def begin_resync(self, task_id: int, tables: list[str]) -> None:
        """全量重刷开始：涉及表增量事件转入缓存队列，不涉及表直通。"""

    @abc.abstractmethod
    def end_resync(self, task_id: int) -> None:
        """全量重刷完成：触发缓存按 binlog 顺序重放，清空后恢复直通。"""

    # ---- DDL 拦截衔接（T3.5） ----
    @abc.abstractmethod
    def resume_after_ddl(self, task_id: int) -> None:
        """DDL 决策完成后解除消费阻塞。"""

    @abc.abstractmethod
    def recover_on_startup(self) -> list[int]:
        """启动自检（T3.7）：崩溃前处于增量运行态的任务自动续跑，返回已恢复任务 id。"""

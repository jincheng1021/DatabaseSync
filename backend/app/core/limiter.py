"""两级限流器（T2.4 / §9 / E16）：任务级令牌桶 + 全局总闸。

避免单一全局桶下多任务互相饿死：任务桶控单任务节奏，总闸控全任务合计吞吐。
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """线程安全令牌桶。rate=0 表示不限流（acquire 恒通过）。"""

    def __init__(self, rate: float):
        self.rate = float(rate)
        self.capacity = max(rate, 1.0) if rate > 0 else 0
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self.rate = float(rate)
            self.capacity = max(rate, 1.0) if rate > 0 else 0
            self.tokens = self.capacity

    def acquire(self, n: float = 1.0) -> float:
        """取 n 个令牌；返回需等待秒数（0=立即可用）。

        令牌不足时保留现有累积不清零（P2-5 配套）：阻塞式调用方按返回秒数
        分段 sleep 重试，令牌随时间持续累积，总等待收敛于 n/rate；
        原实现每轮清零会丢弃部分积累，拉长等待且与分段睡眠配合失真。
        """
        if self.rate <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return 0.0
            return (n - self.tokens) / self.rate


class TwoLevelLimiter:
    """任务级 + 全局两级。engine 每写一批前调用 acquire(rows)。"""

    def __init__(self, global_rate: float = 20000):
        self._global = TokenBucket(global_rate)
        self._task_buckets: dict[int, TokenBucket] = {}
        self._lock = threading.Lock()

    def configure_task(self, task_id: int, rate_rows: int) -> None:
        with self._lock:
            if task_id in self._task_buckets:
                self._task_buckets[task_id].set_rate(rate_rows)
            else:
                self._task_buckets[task_id] = TokenBucket(rate_rows)

    def remove_task(self, task_id: int) -> None:
        with self._lock:
            self._task_buckets.pop(task_id, None)

    def set_global_rate(self, rate_rows: int) -> None:
        self._global.set_rate(rate_rows)

    def acquire(self, task_id: int, rows: float) -> float:
        """返回两级限流下该批需等待的最大秒数（非阻塞，供需要自行调度的调用方）。"""
        wait = 0.0
        with self._lock:
            bucket = self._task_buckets.get(task_id)
        if bucket:
            wait = max(wait, bucket.acquire(rows))
        wait = max(wait, self._global.acquire(rows))
        return wait

    def _chunk_capacity(self, task_id: int) -> float:
        """两级桶中生效的最小容量（0=不限流，无需分片）。"""
        with self._lock:
            bucket = self._task_buckets.get(task_id)
        caps = []
        if bucket and bucket.rate > 0:
            caps.append(bucket.capacity)
        if self._global.rate > 0:
            caps.append(self._global.capacity)
        return min(caps) if caps else 0.0

    def acquire_blocking(self, task_id: int, rows: float) -> None:
        """阻塞式取令牌（P2-5 修复）：循环等待直到两级限流放行。

        修复前调用方 sleep(min(wait,2)) 后照常执行，批大于桶容量时限流
        退化为「最多延迟 2 秒」。批大于桶容量时按容量分片逐片取，避免
        n>capacity 永远无法积累的死循环。
        """
        remaining = float(rows)
        cap = self._chunk_capacity(task_id)
        while remaining > 1e-9:
            chunk = min(remaining, cap) if cap > 0 else remaining
            wait = self.acquire(task_id, chunk)
            if wait > 0:
                time.sleep(min(wait, 2.0))
                continue
            remaining -= chunk

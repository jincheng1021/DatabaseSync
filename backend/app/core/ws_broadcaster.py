"""WS 广播器（T1.8 / §5.2-5.3）：订阅管理与 JSON 信封广播。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("dsync.ws")


@dataclass
class Subscriber:
    ws: object                      # WebSocket 实例
    username: str
    scope: set[str] = field(default_factory=set)  # 空 = 全部任务；否则任务 id 字符串集合


class WSBroadcaster:
    """广播接口：业务侧只调用 broadcast(type, payload) / send_to(ws, type, payload)。
    M1 注入心跳与快照骨架；M2 起任务状态/进度经此推送真实数据。"""

    SEND_TIMEOUT = 5.0   # P2-26：单客户端 send 超时（秒），防慢客户端阻塞整体广播

    def __init__(self):
        self._subs: dict[object, Subscriber] = {}
        self._lock = asyncio.Lock()
        self.main_loop: asyncio.AbstractEventLoop | None = None  # main 装配时注入
        self.snapshot_provider = None  # callable -> dict（main 注入真实快照）

    async def register(self, ws, username: str) -> Subscriber:
        sub = Subscriber(ws=ws, username=username)
        async with self._lock:
            self._subs[ws] = sub
        return sub

    async def unregister(self, ws) -> None:
        async with self._lock:
            self._subs.pop(ws, None)

    @staticmethod
    def envelope(event_type: str, payload: dict) -> dict:
        return {"type": event_type, "timestamp": int(time.time() * 1000), "payload": payload}

    async def send_to(self, ws, event_type: str, payload: dict) -> None:
        try:
            await asyncio.wait_for(
                ws.send_text(json.dumps(self.envelope(event_type, payload), ensure_ascii=False)),
                timeout=self.SEND_TIMEOUT)
        except Exception:
            pass

    async def broadcast(self, event_type: str, payload: dict, *, task_id: str | None = None) -> None:
        """按订阅范围过滤：scope 为空 = 全部；否则需包含 task_id。

        P2-26：逐客户端 send 带 wait_for 超时——修复前慢客户端（TCP 半开/假死）
        的 await send_text 可能无限挂起，阻塞同一事件循环上的全部广播；
        超时/断链即摘除订阅。
        """
        async with self._lock:
            subs = list(self._subs.values())
        msg = json.dumps(self.envelope(event_type, payload), ensure_ascii=False)
        for sub in subs:
            if task_id and sub.scope and str(task_id) not in sub.scope:
                continue
            try:
                await asyncio.wait_for(sub.ws.send_text(msg), timeout=self.SEND_TIMEOUT)
            except Exception:
                await self.unregister(sub.ws)

    async def snapshot(self, sub: Subscriber) -> dict:
        """重连后状态快照补发（§5.3：断线重连后服务端补发最近任务状态快照）。"""
        if self.snapshot_provider:
            try:
                return self.snapshot_provider()
            except Exception:  # noqa: BLE001
                log.exception("快照生成失败")
        return {"tasks": [], "alerts": [], "server_time": int(time.time() * 1000)}


def emit_from_thread(broadcaster: WSBroadcaster, event_type: str, payload: dict,
                     task_id: str | int | None = None) -> None:
    """引擎线程安全投递：经主事件循环调度广播。"""
    loop = getattr(broadcaster, "main_loop", None)
    if loop and loop.is_running():
        loop.call_soon_threadsafe(
            asyncio.ensure_future,
            broadcaster.broadcast(event_type, payload, task_id=str(task_id) if task_id else None))

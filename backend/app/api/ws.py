"""WS /ws/events（T1.8 / §5.2 E10）：握手会话校验，未授权断开并记审计。"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    # 握手阶段校验会话（E10）：未授权 → 4401 关闭 + 审计。
    # websocket 端点无法注入 Request，经 scope 取 app 状态。
    app = ws.scope["app"]
    token = ws.cookies.get("dsync_session")
    user = app.state.auth.sessions.validate(token)
    await ws.accept()
    if not user:
        app.state.audit.record("anonymous", "ws.unauthorized_connect",
                               ip=ws.client.host if ws.client else None,
                               object_type="websocket")
        await ws.close(code=4401)
        return

    broadcaster = app.state.broadcaster
    sub = await broadcaster.register(ws, user["username"])
    await broadcaster.send_to(ws, "ws.connected", {"username": user["username"]})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                await broadcaster.send_to(ws, "ws.error", {"message": "无效 JSON"})
                continue
            action = msg.get("action")
            if action == "subscribe":
                tasks = msg.get("tasks") or []          # [] 或缺省 = 全部任务
                sub.scope = {str(t) for t in tasks}
                snap = await broadcaster.snapshot(sub)
                await broadcaster.send_to(ws, "ws.snapshot", snap)
            elif action == "ping":
                await broadcaster.send_to(ws, "ws.pong", {})
            elif action == "unsubscribe":
                sub.scope = set()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unregister(ws)

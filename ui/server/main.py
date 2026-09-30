"""FastAPI websocket bridge for the AERUPT React UI.

Run:  py run_ui.py            (serves http://127.0.0.1:8000)
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aerupt import __version__
from aerupt.config import load_env
from ui.server.session import AgentSession

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(os.path.dirname(HERE), "client", "dist")
INDEX = os.path.join(DIST, "index.html")

app = FastAPI(title="AERUPT — Interruptible Real-Time Agent")


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "built": os.path.exists(INDEX),
            "agent": "AERUPT", "version": __version__}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    outbox: asyncio.Queue = asyncio.Queue()
    session = AgentSession(outbox=outbox)
    await session.start()

    async def writer() -> None:
        while True:
            msg = await outbox.get()
            await websocket.send_json(msg)

    writer_task = asyncio.create_task(writer())
    try:
        while True:
            raw = await websocket.receive_json()
            typ = raw.get("type")
            if typ == "transcript":
                text = str(raw.get("text", ""))
                if bool(raw.get("end_of_turn")):
                    await session.user_text(text)
                else:
                    await session.partial(text)
            elif typ == "interrupt":
                await session.interrupt(str(raw.get("reason", "user_override")))
            elif typ == "reset":
                await session.stop()
                session = AgentSession(outbox=outbox)
                await session.start()
    except WebSocketDisconnect:
        pass
    finally:
        await session.stop()
        writer_task.cancel()


if os.path.exists(DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST, "assets")),
              name="assets")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(INDEX)
else:
    @app.get("/")
    async def root() -> Dict[str, Any]:
        return {"error": "client not built",
                "hint": "cd ui/client && npm install && npm run build"}
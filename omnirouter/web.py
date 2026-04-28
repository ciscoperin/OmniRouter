"""FastAPI web UI for OmniRouter.

Serves the single-page UI at ``/`` and exposes:
    GET  /api/status        — point-in-time status snapshot
    GET  /api/logs          — full ring buffer (used on first load)
    POST /api/logs/clear    — clear the on-screen log
    POST /api/listener/stop — stop the DICOM listener
    POST /api/listener/start— (re)start the DICOM listener
    WS   /ws/logs           — live log stream
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    CACHE_DIR,
    DESTINATION,
    LISTEN_DISPLAY_HOST,
    LISTEN_PORT,
    LOCAL_AET,
)
from .log_bus import bus
from .router import router as dicom_router

log = logging.getLogger("omnirouter.web")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.attach_loop(asyncio.get_running_loop())
    dicom_router.start()
    try:
        yield
    finally:
        dicom_router.stop()


app = FastAPI(title="OmniRouter", lifespan=lifespan)


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "version": "1.0.0",
            "listening_address": LISTEN_DISPLAY_HOST,
            "listening_port": LISTEN_PORT,
            "local_aet": LOCAL_AET,
            "cache_dir": CACHE_DIR.name,
            "cache_path": str(CACHE_DIR),
            "destination": {
                "host": DESTINATION.host,
                "port": DESTINATION.port,
                "aet": DESTINATION.aet,
                "use_tls": DESTINATION.use_tls,
                "verify_peer": DESTINATION.verify_peer,
                "client_cert_configured": bool(DESTINATION.client_cert),
                "ca_configured": bool(DESTINATION.ca_file),
            },
            "router": dicom_router.status(),
        }
    )


@app.get("/api/logs")
async def get_logs() -> JSONResponse:
    return JSONResponse({"entries": bus.snapshot()})


@app.post("/api/logs/clear")
async def clear_logs() -> JSONResponse:
    bus.clear()
    log.info("Log cleared by operator")
    return JSONResponse({"ok": True})


@app.post("/api/listener/stop")
async def stop_listener() -> JSONResponse:
    dicom_router.stop()
    return JSONResponse({"ok": True, "running": False})


@app.post("/api/listener/start")
async def start_listener() -> JSONResponse:
    dicom_router.start()
    return JSONResponse({"ok": True, "running": True})


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    await ws.accept()
    queue = bus.subscribe()
    try:
        # Send the existing buffer so a fresh client sees historical lines.
        await ws.send_json({"type": "snapshot", "entries": bus.snapshot()})
        while True:
            entry = await queue.get()
            await ws.send_json({"type": "entry", "entry": entry})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error")
    finally:
        bus.unsubscribe(queue)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

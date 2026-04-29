"""FastAPI web UI for OmniRouter.

Serves the single-page UI at ``/`` and exposes:
    GET  /api/status        — point-in-time status snapshot
    GET  /api/destination   — current destination settings (no secrets)
    PUT  /api/destination   — update destination (per-mode payload)
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
from typing import Annotated, Literal, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import (
    CACHE_DIR,
    LISTEN_DISPLAY_HOST,
    LISTEN_PORT,
    LOCAL_AET,
    Destination,
    destination_store,
    get_destination,
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


def _destination_payload() -> dict:
    """Public-safe destination dict for the UI. Never includes the bearer token."""
    d: Destination = get_destination()
    return {
        "mode": d.mode,
        # DIMSE
        "host": d.host,
        "port": d.port,
        "aet": d.aet,
        "verify_peer": d.verify_peer,
        "client_cert_configured": bool(d.client_cert),
        "ca_configured": bool(d.ca_file),
        # DICOMweb
        "base_url": d.base_url,
        "verify_tls": d.verify_tls,
        "delivery_mode": d.delivery_mode,
        "bearer_configured": bool(d.bearer_token),
        # Legacy alias for any existing client code.
        "use_tls": d.use_tls,
    }


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "version": "1.0.2",
            "listening_address": LISTEN_DISPLAY_HOST,
            "listening_port": LISTEN_PORT,
            "local_aet": LOCAL_AET,
            "cache_dir": CACHE_DIR.name,
            "cache_path": str(CACHE_DIR),
            "destination": _destination_payload(),
            "router": dicom_router.status(),
        }
    )


@app.get("/api/destination")
async def get_destination_endpoint() -> JSONResponse:
    return JSONResponse(_destination_payload())


# ---------------------------------------------------------------------------
# Pydantic discriminated union for the destination PUT payload.
#
# Each variant carries its own ``mode`` literal. FastAPI/Pydantic uses that
# field to pick the right model and reject inconsistent combinations
# (e.g. supplying base_url for DIMSE mode).
# ---------------------------------------------------------------------------
class _DimseDestination(BaseModel):
    # Reject DICOMweb-only fields (base_url, bearer_token, verify_tls,
    # delivery_mode) when the caller selects a DIMSE mode.
    model_config = {"extra": "forbid"}

    mode: Literal["dicom", "dicom_tls"]
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    aet: str = Field(..., min_length=1, max_length=16)


class _DicomWebDestination(BaseModel):
    # Reject DIMSE-only fields (host, port, aet) when the caller selects
    # the DICOMweb mode — keeps the wire contract crisp and surfaces
    # client bugs immediately as 422s.
    model_config = {"extra": "forbid"}

    mode: Literal["dicomweb"]
    base_url: str = Field(..., min_length=8, max_length=2048)
    # ``None`` means "keep existing token unchanged".
    bearer_token: str | None = Field(default=None, max_length=4096)
    verify_tls: bool = True
    delivery_mode: Literal["sync", "async"] = "sync"


DestinationPayload = Annotated[
    Union[_DimseDestination, _DicomWebDestination],
    Field(discriminator="mode"),
]


@app.put("/api/destination")
async def update_destination(payload: DestinationPayload) -> JSONResponse:  # type: ignore[valid-type]
    try:
        if isinstance(payload, _DimseDestination):
            destination_store.update(
                mode=payload.mode,
                host=payload.host,
                port=payload.port,
                aet=payload.aet,
            )
        else:
            assert isinstance(payload, _DicomWebDestination)
            destination_store.update(
                mode=payload.mode,
                base_url=payload.base_url,
                bearer_token=payload.bearer_token,  # None == keep existing
                verify_tls=payload.verify_tls,
                delivery_mode=payload.delivery_mode,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(_destination_payload())


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

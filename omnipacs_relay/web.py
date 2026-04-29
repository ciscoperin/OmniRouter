"""FastAPI app for OmniPACS Relay.

Two route groups:

* **STOW-RS data plane** — ``POST /studies`` and
  ``POST /studies/{StudyInstanceUID}`` per DICOM PS3.18. Bearer-token
  authenticated, content-typed ``multipart/related; type="application/dicom"``.
* **Admin control plane** — JSON API + WebSocket log stream consumed by
  the dashboard at ``/``.

Sync vs async semantics:
  * Sync (``X-OmniPACS-Delivery: sync``, default per local target):
    after spooling each instance the handler waits for the background
    worker to forward it before returning. Response body is the standard
    PS3.18 STOW-RS payload (``00081199`` / ``00081198`` sequences).
  * Async (``X-OmniPACS-Delivery: async``): the handler spools every
    instance, fsyncs, then returns ``202 {"accepted": N}`` immediately
    while the worker runs on its own schedule.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path as PathParam,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydicom import dcmread

from .config import (
    PUBLIC_DISPLAY_HOST,
    RELAY_AET,
    SPOOL_DIR,
    WEB_PORT,
    LocalTarget,
    VALID_DELIVERY_MODES,
    get_local_target,
    local_target_store,
)
from .forwarder import forwarder
from .log_bus import bus
from .multipart import MultipartError, parse_dicom_multipart
from .spool import spool
from .tokens import LABEL_MAX_LEN, token_store

log = logging.getLogger("omnipacs_relay.web")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.attach_loop(asyncio.get_running_loop())
    forwarder.start()
    log.info(
        "OmniPACS Relay listening on %s:%d, AET=%s, spool=%s",
        PUBLIC_DISPLAY_HOST, WEB_PORT, RELAY_AET, SPOOL_DIR,
    )
    try:
        yield
    finally:
        forwarder.stop()


app = FastAPI(title="OmniPACS Relay", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
WWW_AUTHENTICATE = 'Bearer realm="omnipacs-relay"'


def _bearer_auth(authorization: str | None) -> str:
    """Validate the Authorization header. Returns the matching token's label."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": WWW_AUTHENTICATE},
        )
    presented = authorization.split(" ", 1)[1].strip()
    rec = token_store.validate(presented)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is not recognised",
            headers={"WWW-Authenticate": WWW_AUTHENTICATE},
        )
    return rec.label


def _require_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    return _bearer_auth(authorization)


# ---------------------------------------------------------------------------
# STOW-RS data plane
# ---------------------------------------------------------------------------
DELIVERY_HEADER = "X-OmniPACS-Delivery"
SYNC_PER_INSTANCE_TIMEOUT_S = 60.0


def _resolve_delivery_mode(header_value: str | None) -> str:
    if header_value:
        v = header_value.strip().lower()
        if v in VALID_DELIVERY_MODES:
            return v
    return get_local_target().default_delivery_mode


def _stow_response_body(
    successes: list[dict], failures: list[dict]
) -> dict:
    """Build the PS3.18 STOW-RS DICOM JSON response.

    successes / failures are pre-built sub-items containing 00081150
    (ReferencedSOPClassUID), 00081155 (ReferencedSOPInstanceUID), and —
    for failures — 00081197 (FailureReason) per PS3.18.
    """
    out: dict = {}
    if successes:
        out["00081199"] = {"vr": "SQ", "Value": successes}
    if failures:
        out["00081198"] = {"vr": "SQ", "Value": failures}
    return out


def _ref_sop_item(sop_class: str, sop_instance: str) -> dict:
    return {
        "00081150": {"vr": "UI", "Value": [sop_class]},
        "00081155": {"vr": "UI", "Value": [sop_instance]},
    }


def _failure_item(sop_class: str, sop_instance: str, reason_code: int) -> dict:
    item = _ref_sop_item(sop_class or "", sop_instance or "")
    item["00081197"] = {"vr": "US", "Value": [reason_code]}
    return item


# Failure-reason codes per PS3.4 STOW-RS table CC.2.3-1.
FR_PROCESSING_FAILURE = 0x0110     # generic
FR_DUPLICATE_SOP = 0xA770          # not really used here
FR_NOT_AUTHORIZED = 0xA700         # used only at handler-level
FR_INVALID = 0xC000                # invalid attribute value


async def _handle_stow(
    request: Request,
    constrained_study_uid: str | None,
    auth_label: str,
) -> JSONResponse:
    body = await request.body()
    delivery_mode = _resolve_delivery_mode(request.headers.get(DELIVERY_HEADER))
    content_type = request.headers.get("content-type")

    try:
        parts = parse_dicom_multipart(body, content_type)
    except MultipartError as exc:
        log.warning(
            "STOW from token=%s rejected: %s (Content-Type=%r, body=%dB)",
            auth_label, exc, content_type, len(body),
        )
        raise HTTPException(status_code=400, detail=str(exc))

    log.info(
        "STOW from token=%s: %d instance(s), delivery=%s, body=%.1fKB",
        auth_label, len(parts), delivery_mode, len(body) / 1024,
    )

    successes: list[dict] = []
    failures: list[dict] = []
    waiters: list[tuple[str, "object"]] = []  # (sop_uid, _SopWaiter)
    spool_paths: list[Path] = []

    for raw in parts:
        # Parse the DICOM blob once so we know the UIDs.
        try:
            ds = dcmread(io.BytesIO(raw))
            sop_class = str(getattr(ds, "SOPClassUID", "") or "")
            sop_instance = str(getattr(ds, "SOPInstanceUID", "") or "")
            study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        except Exception as exc:
            log.warning("STOW part failed to parse as DICOM: %s", exc)
            failures.append(_failure_item("", "", FR_INVALID))
            continue

        if not sop_class or not sop_instance or not study_uid:
            failures.append(_failure_item(sop_class, sop_instance, FR_INVALID))
            continue

        # If the URL constrained the StudyInstanceUID, every instance
        # MUST match — anything else is rejected per PS3.18 CC.
        if constrained_study_uid and study_uid != constrained_study_uid:
            log.warning(
                "STOW part study mismatch: url=%s dataset=%s",
                constrained_study_uid, study_uid,
            )
            failures.append(_failure_item(sop_class, sop_instance, FR_INVALID))
            continue

        # In sync mode register a waiter BEFORE spooling so we can't
        # miss a fast worker that fulfils between write and register.
        waiter = None
        if delivery_mode == "sync":
            waiter = forwarder.register_waiter(sop_instance)

        try:
            path = spool.write_instance(study_uid, sop_instance, raw)
            spool_paths.append(path)
        except Exception as exc:
            log.exception("Spool write failed for sop=%s", sop_instance)
            if waiter is not None:
                forwarder.discard_waiter(sop_instance)
            failures.append(
                _failure_item(sop_class, sop_instance, FR_PROCESSING_FAILURE)
            )
            continue

        # Tentatively count as success — for sync mode we'll downgrade
        # after the wait below.
        successes.append(_ref_sop_item(sop_class, sop_instance))
        if waiter is not None:
            waiters.append((study_uid, sop_instance, waiter, sop_class))  # type: ignore[arg-type]

    # Wake the worker right now so it doesn't wait for its idle interval.
    forwarder.kick()

    # Async mode: return 202 immediately after fsync — done.
    if delivery_mode == "async":
        return JSONResponse(
            {"accepted": len(successes), "rejected": len(failures)},
            status_code=202,
        )

    # Sync mode: wait for each spooled instance to be forwarded (or
    # quarantined) before responding. Per-instance timeout caps total wait.
    successes_after: list[dict] = []
    for study_uid, sop_instance, waiter, sop_class in waiters:
        ok, reason = await asyncio.get_running_loop().run_in_executor(
            None, waiter.wait, SYNC_PER_INSTANCE_TIMEOUT_S
        )
        if ok:
            successes_after.append(_ref_sop_item(sop_class, sop_instance))
        else:
            log.warning(
                "Sync STOW: instance sop=%s did not forward: %s",
                sop_instance, reason,
            )
            failures.append(
                _failure_item(sop_class, sop_instance, FR_PROCESSING_FAILURE)
            )
            # Whether the wait timed out or the worker reported failure,
            # the caller (OmniRouter) now owns the retry. Drop the spool
            # entry + waiter so a later worker pass can't double-deliver
            # alongside the caller's retry. Idempotent — the fail-fast
            # path inside the worker already dropped the file via
            # spool.discard_pending; on timeout the file is still here.
            forwarder.abandon_sync(study_uid, sop_instance)

    # Replace the tentative successes with the post-wait ones.
    successes = successes_after

    body_out = _stow_response_body(successes, failures)
    # The wire contract (mirrored on the OmniRouter STOW client) treats
    # any well-formed sync request as HTTP 200 + a PS3.18 response body
    # — successes go in 00081199, failures in 00081198. The OmniRouter
    # forwarder inspects the body to decide which instances to retry,
    # so we never short-circuit to a 4xx for partial- or all-failure.
    return JSONResponse(content=body_out, status_code=200,
                        media_type="application/dicom+json")


@app.post("/studies")
async def stow_root(
    request: Request,
    auth_label: Annotated[str, Depends(_require_bearer)],
) -> JSONResponse:
    return await _handle_stow(request, None, auth_label)


@app.post("/studies/{study_uid}")
async def stow_for_study(
    request: Request,
    study_uid: Annotated[str, PathParam(min_length=1, max_length=200)],
    auth_label: Annotated[str, Depends(_require_bearer)],
) -> JSONResponse:
    return await _handle_stow(request, study_uid, auth_label)


# ---------------------------------------------------------------------------
# Admin control plane
# ---------------------------------------------------------------------------
def _local_target_payload() -> dict:
    t: LocalTarget = get_local_target()
    return {
        "host": t.host,
        "port": t.port,
        "aet": t.aet,
        "default_delivery_mode": t.default_delivery_mode,
    }


_PROCESS_STARTED_TS = time.time()


@app.get("/api/status")
async def admin_status() -> JSONResponse:
    return JSONResponse(
        {
            "version": "1.0.0",
            "service": "OmniPACS Relay",
            "public_host": PUBLIC_DISPLAY_HOST,
            "public_port": WEB_PORT,
            "relay_aet": RELAY_AET,
            "spool_path": str(SPOOL_DIR),
            "local_target": _local_target_payload(),
            "forwarder_running": forwarder.is_running(),
            "token_count": token_store.count(),
            "spool": spool.stats(),
            "started_ts": _PROCESS_STARTED_TS,
            "uptime_seconds": time.time() - _PROCESS_STARTED_TS,
        }
    )


@app.get("/api/local-target")
async def admin_get_local_target() -> JSONResponse:
    return JSONResponse(_local_target_payload())


class _LocalTargetUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    aet: str = Field(..., min_length=1, max_length=16)
    default_delivery_mode: str = Field("sync", pattern="^(sync|async)$")


@app.put("/api/local-target")
async def admin_put_local_target(payload: _LocalTargetUpdate) -> JSONResponse:
    try:
        local_target_store.update(
            host=payload.host,
            port=payload.port,
            aet=payload.aet,
            default_delivery_mode=payload.default_delivery_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info(
        "Local target updated → %s (default delivery: %s)",
        get_local_target().describe(), payload.default_delivery_mode,
    )
    return JSONResponse(_local_target_payload())


@app.post("/api/forwarder/start")
async def admin_start_forwarder() -> JSONResponse:
    forwarder.start()
    return JSONResponse({"running": True})


@app.post("/api/forwarder/stop")
async def admin_stop_forwarder() -> JSONResponse:
    forwarder.stop()
    return JSONResponse({"running": False})


# ---- Token management ------------------------------------------------------
@app.get("/api/tokens")
async def admin_list_tokens() -> JSONResponse:
    return JSONResponse({"tokens": token_store.list_public()})


class _IssueToken(BaseModel):
    model_config = {"extra": "forbid"}
    label: Optional[str] = Field(default=None, max_length=LABEL_MAX_LEN)


@app.post("/api/tokens")
async def admin_issue_token(payload: _IssueToken) -> JSONResponse:
    try:
        raw, rec = token_store.issue(label=payload.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("Operator issued bearer token (label=%s)", rec.label)
    # The raw token is returned exactly once on creation.
    return JSONResponse({"token": raw, "record": rec.public_view()}, status_code=201)


@app.delete("/api/tokens/{label}")
async def admin_revoke_token(label: str) -> JSONResponse:
    if not token_store.revoke(label):
        raise HTTPException(status_code=404, detail=f"label {label!r} not found")
    return JSONResponse({"ok": True})


# ---- Quarantine management ------------------------------------------------
@app.get("/api/quarantine")
async def admin_list_quarantine() -> JSONResponse:
    return JSONResponse({"items": spool.list_quarantine()})


@app.post("/api/quarantine/{study_uid}/{sop_uid}/requeue")
async def admin_requeue_one(study_uid: str, sop_uid: str) -> JSONResponse:
    if not spool.requeue_quarantine(study_uid, sop_uid):
        raise HTTPException(status_code=404, detail="not in quarantine")
    forwarder.kick()
    return JSONResponse({"ok": True})


@app.post("/api/quarantine/requeue-all")
async def admin_requeue_all() -> JSONResponse:
    n = spool.requeue_all_quarantine()
    if n:
        forwarder.kick()
    return JSONResponse({"requeued": n})


# ---- Logs ------------------------------------------------------------------
@app.get("/api/logs")
async def admin_get_logs() -> JSONResponse:
    return JSONResponse({"entries": bus.snapshot()})


@app.post("/api/logs/clear")
async def admin_clear_logs() -> JSONResponse:
    bus.clear()
    log.info("Log cleared by operator")
    return JSONResponse({"ok": True})


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


# ---- Health (unauthenticated, for load balancers) -------------------------
@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "OmniPACS Relay"})


# ---- Static dashboard -----------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

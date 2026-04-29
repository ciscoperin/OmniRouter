"""OmniPACS Relay configuration.

All paths and ports are env-driven so an ops engineer can drop the
service into systemd / Docker without code changes.

Local target (the LAN-side PACS / VNA we forward C-STORE to) is
runtime-editable from the dashboard and persisted to
``<spool>/local_target.json``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("omnipacs_relay.config")


def _local_ip() -> str:
    """Best-effort detection of the host's primary LAN/loopback IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Web service identity
# ---------------------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", "5001"))
PUBLIC_DISPLAY_HOST = _local_ip()

# ---------------------------------------------------------------------------
# Relay identity (the AET we present to the local PACS as the SCU)
# ---------------------------------------------------------------------------
RELAY_AET = os.environ.get("OMNI_RELAY_AET", "OMNIRELAY")

# ---------------------------------------------------------------------------
# On-disk spool. Layout:
#   <spool>/inbox/<study>/<sop>.dcm        — accepted, awaiting forward
#   <spool>/quarantine/<study>/<sop>.dcm   — gave up after MAX_RETRIES
#   <spool>/tokens.json                    — bearer tokens (chmod 600)
#   <spool>/local_target.json              — local PACS target (chmod 600)
# ---------------------------------------------------------------------------
SPOOL_DIR = Path(os.environ.get("OMNI_RELAY_SPOOL", "omnirelay_spool")).resolve()
INBOX_DIR = SPOOL_DIR / "inbox"
QUARANTINE_DIR = SPOOL_DIR / "quarantine"

# ---------------------------------------------------------------------------
# Optional inbound TLS (production). Replit dev preview already terminates
# TLS via its proxy, so leave these unset in dev. In production an operator
# can either set both env vars to the cert/key paths, or front the service
# with a reverse proxy (Caddy / nginx).
# ---------------------------------------------------------------------------
TLS_CERT_PATH = os.environ.get("OMNI_RELAY_TLS_CERT") or None
TLS_KEY_PATH = os.environ.get("OMNI_RELAY_TLS_KEY") or None


# ---------------------------------------------------------------------------
# LocalTarget — the LAN-side PACS we forward to.
# ---------------------------------------------------------------------------
DeliveryMode = Literal["sync", "async"]
VALID_DELIVERY_MODES: tuple[DeliveryMode, ...] = ("sync", "async")


@dataclass
class LocalTarget:
    host: str = "127.0.0.1"
    port: int = 11112
    aet: str = "LOCAL_PACS"
    # Default per-request delivery mode when the client doesn't send the
    # X-OmniPACS-Delivery header. Sync is the safer default.
    default_delivery_mode: DeliveryMode = "sync"

    def describe(self) -> str:
        return f"{self.aet}@{self.host}:{self.port}"


def _local_target_from_env() -> LocalTarget:
    delivery_env = os.environ.get(
        "OMNI_RELAY_DEFAULT_DELIVERY", "sync"
    ).strip().lower()
    delivery: DeliveryMode = (
        delivery_env if delivery_env in VALID_DELIVERY_MODES else "sync"  # type: ignore[assignment]
    )
    return LocalTarget(
        host=os.environ.get("OMNI_RELAY_TARGET_HOST", "127.0.0.1"),
        port=int(os.environ.get("OMNI_RELAY_TARGET_PORT", "11112")),
        aet=os.environ.get("OMNI_RELAY_TARGET_AET", "LOCAL_PACS"),
        default_delivery_mode=delivery,
    )


class LocalTargetStore:
    """Thread-safe local target with JSON persistence (chmod 600)."""

    PERSISTED_FIELDS = ("host", "port", "aet", "default_delivery_mode")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = SPOOL_DIR / "local_target.json"
        self._target = _local_target_from_env()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())
            updates = {k: data[k] for k in self.PERSISTED_FIELDS if k in data}
            if (
                "default_delivery_mode" in updates
                and updates["default_delivery_mode"] not in VALID_DELIVERY_MODES
            ):
                updates.pop("default_delivery_mode")
            self._target = replace(self._target, **updates)
            log.info("Loaded local target override from %s", self._path)
        except Exception:
            log.exception("Could not load local target; using defaults")

    def _save_to_disk(self) -> None:
        try:
            SPOOL_DIR.mkdir(parents=True, exist_ok=True)
            payload = {k: getattr(self._target, k) for k in self.PERSISTED_FIELDS}
            self._path.write_text(json.dumps(payload, indent=2))
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        except Exception:
            log.exception("Could not persist local target to %s", self._path)

    def get(self) -> LocalTarget:
        with self._lock:
            return replace(self._target)

    def update(self, **changes: Any) -> LocalTarget:
        with self._lock:
            current = replace(self._target)
            validated = self._validate(current, changes)
            self._target = replace(self._target, **validated)
            self._save_to_disk()
            return replace(self._target)

    @staticmethod
    def _validate(current: LocalTarget, changes: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "host" in changes:
            host = str(changes["host"]).strip()
            if not host:
                raise ValueError("Local target host cannot be empty")
            if len(host) > 255:
                raise ValueError("Local target host is too long")
            out["host"] = host
        if "port" in changes:
            try:
                port = int(changes["port"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Local target port must be an integer") from exc
            if not (1 <= port <= 65535):
                raise ValueError("Local target port must be 1..65535")
            out["port"] = port
        if "aet" in changes:
            aet = str(changes["aet"]).strip()
            if not aet:
                raise ValueError("Local target AE Title cannot be empty")
            if len(aet) > 16:
                raise ValueError("AE Title must be 16 characters or fewer")
            out["aet"] = aet
        if "default_delivery_mode" in changes:
            dm = str(changes["default_delivery_mode"]).strip().lower()
            if dm not in VALID_DELIVERY_MODES:
                raise ValueError(
                    f"default_delivery_mode must be one of "
                    f"{', '.join(VALID_DELIVERY_MODES)}"
                )
            out["default_delivery_mode"] = dm
        return out


local_target_store = LocalTargetStore()


def get_local_target() -> LocalTarget:
    return local_target_store.get()

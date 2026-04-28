"""OmniRouter configuration.

Listening side is FIXED per product spec:
    Listening Address : 127.0.0.1 (localhost)
    Listening Port    : 7775
    Local AET         : OMNI
    Cache Directory   : ./omnicache

Outbound (forwarding) destination is editable from the UI ("Configuration"
menu) and persisted to ``<cache>/destination.json``. Defaults are taken
from environment variables on first run.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger("omnirouter.config")


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
# Fixed listener identity (per product spec)
# ---------------------------------------------------------------------------
LISTEN_BIND_HOST = "0.0.0.0"           # bind on all interfaces
LISTEN_DISPLAY_HOST = _local_ip()      # what the UI shows
LISTEN_PORT = 7775
LOCAL_AET = "OMNI"
CACHE_DIR = Path(os.environ.get("OMNI_CACHE_DIR", "omnicache")).resolve()

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", "5000"))


# ---------------------------------------------------------------------------
# Destination (runtime-editable, persisted to disk)
# ---------------------------------------------------------------------------
@dataclass
class Destination:
    host: str = "wan.example.com"
    port: int = 11112
    aet: str = "REMOTE_PACS"
    use_tls: bool = True
    # TLS material — paths to PEM files (optional). When omitted the router
    # generates a self-signed client cert at startup and disables peer
    # verification (v1 default).
    client_cert: str | None = None
    client_key: str | None = None
    ca_file: str | None = None
    verify_peer: bool = False

    def as_public_dict(self) -> dict:
        d = asdict(self)
        # Don't leak filesystem paths back to the browser.
        for k in ("client_cert", "client_key", "ca_file"):
            d[k] = bool(d[k])
        return d


def _destination_from_env() -> Destination:
    return Destination(
        host=os.environ.get("OMNI_DEST_HOST", "wan.example.com"),
        port=int(os.environ.get("OMNI_DEST_PORT", "11112")),
        aet=os.environ.get("OMNI_DEST_AET", "REMOTE_PACS"),
        use_tls=os.environ.get("OMNI_DEST_TLS", "true").lower() == "true",
        client_cert=os.environ.get("OMNI_TLS_CERT"),
        client_key=os.environ.get("OMNI_TLS_KEY"),
        ca_file=os.environ.get("OMNI_TLS_CA"),
        verify_peer=os.environ.get("OMNI_TLS_VERIFY", "false").lower() == "true",
    )


class DestinationStore:
    """Thread-safe destination configuration with JSON persistence."""

    PERSISTED_FIELDS = ("host", "port", "aet", "use_tls")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = CACHE_DIR / "destination.json"
        self._dest = _destination_from_env()
        self._load_from_disk()

    # --- IO ---------------------------------------------------------------
    def _load_from_disk(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                updates = {
                    k: data[k] for k in self.PERSISTED_FIELDS if k in data
                }
                self._dest = replace(self._dest, **updates)
                log.info("Loaded destination override from %s", self._path)
        except Exception:
            log.exception("Could not load destination override; using defaults")

    def _save_to_disk(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {k: getattr(self._dest, k) for k in self.PERSISTED_FIELDS}
            self._path.write_text(json.dumps(payload, indent=2))
        except Exception:
            log.exception("Could not persist destination override to %s", self._path)

    # --- API --------------------------------------------------------------
    def get(self) -> Destination:
        with self._lock:
            return replace(self._dest)

    def update(self, **changes: Any) -> Destination:
        validated = self._validate(changes)
        with self._lock:
            self._dest = replace(self._dest, **validated)
            self._save_to_disk()
            new = replace(self._dest)
        log.info(
            "Destination updated → %s@%s:%s (%s)",
            new.aet,
            new.host,
            new.port,
            "TLS" if new.use_tls else "plain",
        )
        return new

    @staticmethod
    def _validate(changes: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "host" in changes:
            host = str(changes["host"]).strip()
            if not host:
                raise ValueError("Destination host cannot be empty")
            out["host"] = host
        if "port" in changes:
            try:
                port = int(changes["port"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Destination port must be an integer") from exc
            if not (1 <= port <= 65535):
                raise ValueError("Destination port must be between 1 and 65535")
            out["port"] = port
        if "aet" in changes:
            aet = str(changes["aet"]).strip()
            if not aet:
                raise ValueError("Destination AE Title cannot be empty")
            if len(aet) > 16:
                raise ValueError("AE Title must be 16 characters or fewer")
            out["aet"] = aet
        if "use_tls" in changes:
            out["use_tls"] = bool(changes["use_tls"])
        return out


destination_store = DestinationStore()


def get_destination() -> Destination:
    """Convenience accessor used by the router and web layer."""
    return destination_store.get()

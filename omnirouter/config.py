"""OmniRouter configuration.

Listening side is FIXED per product spec:
    Listening Address : 127.0.0.1 (localhost)
    Listening Port    : 7775
    Local AET         : OMNI
    Cache Directory   : ./omnicache

Outbound (forwarding) destination is editable from the UI ("Configuration"
menu) and persisted to ``<cache>/destination.json``.

Three egress modes are supported:
    - ``dicom``     : plain DIMSE C-STORE
    - ``dicom_tls`` : DIMSE C-STORE over TLS
    - ``dicomweb``  : DICOMweb STOW-RS (HTTPS multipart/related)
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
Mode = Literal["dicom", "dicom_tls", "dicomweb"]
DeliveryMode = Literal["sync", "async"]

VALID_MODES: tuple[Mode, ...] = ("dicom", "dicom_tls", "dicomweb")
VALID_DELIVERY_MODES: tuple[DeliveryMode, ...] = ("sync", "async")


@dataclass
class Destination:
    # Egress protocol selector.
    mode: Mode = "dicom_tls"

    # --- DIMSE fields (used when mode == "dicom" or "dicom_tls") ---------
    host: str = "wan.example.com"
    port: int = 11112
    aet: str = "REMOTE_PACS"

    # DIMSE-TLS material — paths to PEM files (optional). Configured via
    # OMNI_TLS_* env vars only; not exposed in the UI.
    client_cert: str | None = None
    client_key: str | None = None
    ca_file: str | None = None
    verify_peer: bool = False

    # --- DICOMweb fields (used when mode == "dicomweb") ------------------
    base_url: str = ""
    bearer_token: str = ""
    verify_tls: bool = True
    delivery_mode: DeliveryMode = "sync"

    @property
    def use_tls(self) -> bool:
        """Backward-compat shim: True iff DIMSE-TLS mode."""
        return self.mode == "dicom_tls"

    def as_public_dict(self) -> dict:
        d = asdict(self)
        # Don't leak filesystem paths or secrets back to the browser.
        for k in ("client_cert", "client_key", "ca_file"):
            d[k] = bool(d[k])
        d["bearer_configured"] = bool(d.pop("bearer_token", ""))
        return d


def _destination_from_env() -> Destination:
    """Initial defaults pulled from env vars."""
    # Env-driven mode: prefer explicit OMNI_DEST_MODE; fall back to the
    # legacy OMNI_DEST_TLS boolean for backward compatibility.
    env_mode = os.environ.get("OMNI_DEST_MODE", "").strip().lower()
    if env_mode in VALID_MODES:
        mode: Mode = env_mode  # type: ignore[assignment]
    else:
        use_tls = os.environ.get("OMNI_DEST_TLS", "true").lower() == "true"
        mode = "dicom_tls" if use_tls else "dicom"

    delivery_env = os.environ.get("OMNI_DELIVERY_MODE", "sync").strip().lower()
    delivery: DeliveryMode = (
        delivery_env if delivery_env in VALID_DELIVERY_MODES else "sync"  # type: ignore[assignment]
    )

    return Destination(
        mode=mode,
        host=os.environ.get("OMNI_DEST_HOST", "wan.example.com"),
        port=int(os.environ.get("OMNI_DEST_PORT", "11112")),
        aet=os.environ.get("OMNI_DEST_AET", "REMOTE_PACS"),
        client_cert=os.environ.get("OMNI_TLS_CERT"),
        client_key=os.environ.get("OMNI_TLS_KEY"),
        ca_file=os.environ.get("OMNI_TLS_CA"),
        verify_peer=os.environ.get("OMNI_TLS_VERIFY", "false").lower() == "true",
        base_url=os.environ.get("OMNI_DICOMWEB_URL", ""),
        bearer_token=os.environ.get("OMNI_DICOMWEB_TOKEN", ""),
        verify_tls=os.environ.get("OMNI_DICOMWEB_VERIFY", "true").lower() == "true",
        delivery_mode=delivery,
    )


class DestinationStore:
    """Thread-safe destination configuration with JSON persistence."""

    # Fields written to disk. ``mode`` is authoritative; ``use_tls`` is
    # accepted as a legacy alias on read for backward compatibility.
    PERSISTED_FIELDS = (
        "mode",
        "host",
        "port",
        "aet",
        "base_url",
        "bearer_token",
        "verify_tls",
        "delivery_mode",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = CACHE_DIR / "destination.json"
        self._dest = _destination_from_env()
        self._load_from_disk()

    # --- IO ---------------------------------------------------------------
    def _load_from_disk(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())

            # Legacy migration: old files only had host/port/aet/use_tls.
            if "mode" not in data and "use_tls" in data:
                legacy_use_tls = data.pop("use_tls")
                data["mode"] = "dicom_tls" if legacy_use_tls else "dicom"
                log.info(
                    "Migrating legacy destination.json (use_tls=%s → mode=%s)",
                    legacy_use_tls,
                    data["mode"],
                )

            updates = {k: data[k] for k in self.PERSISTED_FIELDS if k in data}
            if "mode" in updates and updates["mode"] not in VALID_MODES:
                log.warning("Ignoring unknown mode %r in destination.json", updates["mode"])
                updates.pop("mode")
            if (
                "delivery_mode" in updates
                and updates["delivery_mode"] not in VALID_DELIVERY_MODES
            ):
                updates.pop("delivery_mode")

            self._dest = replace(self._dest, **updates)
            log.info("Loaded destination override from %s", self._path)
        except Exception:
            log.exception("Could not load destination override; using defaults")

    def _save_to_disk(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {k: getattr(self._dest, k) for k in self.PERSISTED_FIELDS}
            self._path.write_text(json.dumps(payload, indent=2))
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass  # filesystem may not support chmod (e.g. SMB)
        except Exception:
            log.exception("Could not persist destination override to %s", self._path)

    # --- API --------------------------------------------------------------
    def get(self) -> Destination:
        with self._lock:
            return replace(self._dest)

    def update(self, **changes: Any) -> Destination:
        # ``bearer_token=None`` means "keep existing token unchanged".
        if "bearer_token" in changes and changes["bearer_token"] is None:
            changes.pop("bearer_token")

        with self._lock:
            current = replace(self._dest)
            validated = self._validate(current, changes)
            self._dest = replace(self._dest, **validated)
            self._save_to_disk()
            new = replace(self._dest)

        if new.mode == "dicomweb":
            log.info(
                "Destination updated → STOW-RS %s (%s)",
                new.base_url,
                new.delivery_mode,
            )
        else:
            log.info(
                "Destination updated → %s@%s:%s (%s)",
                new.aet,
                new.host,
                new.port,
                "TLS" if new.mode == "dicom_tls" else "plain",
            )
        return new

    @staticmethod
    def _validate(current: Destination, changes: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}

        # Determine the resulting mode first — validation downstream depends on it.
        new_mode: Mode = current.mode
        if "mode" in changes:
            mode_in = str(changes["mode"]).strip().lower()
            if mode_in not in VALID_MODES:
                raise ValueError(
                    f"mode must be one of {', '.join(VALID_MODES)}"
                )
            new_mode = mode_in  # type: ignore[assignment]
            out["mode"] = new_mode

        # --- DIMSE fields (always validated if provided; required for DIMSE modes) ---
        if "host" in changes:
            host = str(changes["host"]).strip()
            if not host and new_mode != "dicomweb":
                raise ValueError("Destination host cannot be empty")
            out["host"] = host
        elif new_mode != "dicomweb" and not current.host:
            raise ValueError("Destination host cannot be empty")

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
            if not aet and new_mode != "dicomweb":
                raise ValueError("Destination AE Title cannot be empty")
            if len(aet) > 16:
                raise ValueError("AE Title must be 16 characters or fewer")
            out["aet"] = aet
        elif new_mode != "dicomweb" and not current.aet:
            raise ValueError("Destination AE Title cannot be empty")

        # --- DICOMweb fields ---
        if "base_url" in changes:
            base_url = str(changes["base_url"]).strip().rstrip("/")
            if base_url:
                # HTTPS-only: this mode is literally "DICOM over HTTPS".
                # Plain http:// would expose the bearer token in cleartext.
                if not base_url.startswith("https://"):
                    raise ValueError(
                        "STOW-RS base URL must start with https:// — bearer "
                        "tokens cannot be sent over plain HTTP"
                    )
                if len(base_url) > 2048:
                    raise ValueError("STOW-RS base URL is too long")
            out["base_url"] = base_url

        if "bearer_token" in changes:
            token = str(changes["bearer_token"])
            if token and len(token) > 4096:
                raise ValueError("Bearer token is too long (>4096 chars)")
            out["bearer_token"] = token

        if "verify_tls" in changes:
            out["verify_tls"] = bool(changes["verify_tls"])

        if "delivery_mode" in changes:
            dm = str(changes["delivery_mode"]).strip().lower()
            if dm not in VALID_DELIVERY_MODES:
                raise ValueError(
                    f"delivery_mode must be one of {', '.join(VALID_DELIVERY_MODES)}"
                )
            out["delivery_mode"] = dm

        # --- Mode-specific required-field checks (after merging) ---
        if new_mode == "dicomweb":
            effective_url = out.get("base_url", current.base_url)
            if not effective_url:
                raise ValueError("STOW-RS base URL is required for DICOM over HTTPS mode")
            effective_token = out.get("bearer_token", current.bearer_token)
            if not effective_token:
                raise ValueError(
                    "Bearer token is required for DICOM over HTTPS mode"
                )

        # Legacy alias: some clients may still send ``use_tls`` — translate it.
        if "use_tls" in changes and "mode" not in out:
            out["mode"] = "dicom_tls" if bool(changes["use_tls"]) else "dicom"

        return out


destination_store = DestinationStore()


def get_destination() -> Destination:
    """Convenience accessor used by the router and web layer."""
    return destination_store.get()

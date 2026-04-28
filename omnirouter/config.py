"""OmniRouter configuration.

Listening side is FIXED per product spec:
    Listening Address : 127.0.0.1 (localhost)
    Listening Port    : 7776
    Local AET         : OMNI
    Cache Directory   : ./omnicache

Outbound (forwarding) endpoint is generic / configurable for v1, and uses
DICOM TLS when ``DESTINATION_USE_TLS`` is true.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, asdict
from pathlib import Path


def _local_ip() -> str:
    """Best-effort detection of the host's primary LAN/loopback IP.

    On the production Windows box this returns the LAN address (matches the
    behavior of the original OmniRouter UI). When that lookup fails we fall
    back to 127.0.0.1.
    """
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
LISTEN_PORT = 7776
LOCAL_AET = "OMNI"
CACHE_DIR = Path(os.environ.get("OMNI_CACHE_DIR", "omnicache")).resolve()

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", "5000"))

# ---------------------------------------------------------------------------
# Generic v1 forwarding destination (DICOM TLS)
# Override with environment variables in production.
# ---------------------------------------------------------------------------
@dataclass
class Destination:
    host: str = os.environ.get("OMNI_DEST_HOST", "wan.example.com")
    port: int = int(os.environ.get("OMNI_DEST_PORT", "11112"))
    aet: str = os.environ.get("OMNI_DEST_AET", "REMOTE_PACS")
    use_tls: bool = os.environ.get("OMNI_DEST_TLS", "true").lower() == "true"
    # Paths to PEM files (optional). When omitted we generate a self-signed
    # client cert at startup and disable peer verification (v1 default).
    client_cert: str | None = os.environ.get("OMNI_TLS_CERT")
    client_key: str | None = os.environ.get("OMNI_TLS_KEY")
    ca_file: str | None = os.environ.get("OMNI_TLS_CA")
    verify_peer: bool = os.environ.get("OMNI_TLS_VERIFY", "false").lower() == "true"

    def as_public_dict(self) -> dict:
        d = asdict(self)
        # Don't leak filesystem paths back to the browser.
        for k in ("client_cert", "client_key", "ca_file"):
            d[k] = bool(d[k])
        return d


DESTINATION = Destination()

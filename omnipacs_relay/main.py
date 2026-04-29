"""Entry point for OmniPACS Relay.

Boots logging and serves the FastAPI app on ``WEB_PORT`` over HTTPS.
TLS credential resolution:

  1. If OMNI_RELAY_TLS_CERT and OMNI_RELAY_TLS_KEY are both set, uvicorn
     binds HTTPS using those operator-supplied credentials.
  2. Otherwise the relay generates (and reuses across restarts) a
     self-signed certificate under ``<spool>/tls/`` and binds HTTPS with
     that. This is the development default and means STOW-RS is always
     served over TLS.
  3. Operators who deliberately want plain HTTP — typically because a
     reverse proxy in front (Caddy / nginx / a cloud LB) is terminating
     TLS for them — can set ``OMNI_RELAY_DISABLE_TLS=1``.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from .config import TLS_CERT_PATH, TLS_KEY_PATH, WEB_HOST, WEB_PORT
from .log_bus import configure_logging
from .tls_util import ensure_dev_self_signed

log = logging.getLogger("omnipacs_relay.main")


def _resolve_tls() -> tuple[str | None, str | None]:
    """Return (certfile, keyfile) or (None, None) for plain HTTP."""
    if os.environ.get("OMNI_RELAY_DISABLE_TLS", "").lower() in ("1", "true", "yes"):
        log.info("OMNI_RELAY_DISABLE_TLS set — serving plain HTTP "
                 "(expect a TLS-terminating reverse proxy in front).")
        return None, None
    if TLS_CERT_PATH and TLS_KEY_PATH:
        log.info("Using operator-supplied TLS credentials cert=%s key=%s",
                 TLS_CERT_PATH, TLS_KEY_PATH)
        return TLS_CERT_PATH, TLS_KEY_PATH
    cert, key = ensure_dev_self_signed()
    log.info("Serving HTTPS with self-signed cert at %s", cert)
    return cert, key


def main() -> None:
    configure_logging(level=logging.INFO)

    kwargs: dict = {
        "host": WEB_HOST,
        "port": WEB_PORT,
        "log_level": "info",
        "access_log": False,
    }

    cert, key = _resolve_tls()
    if cert and key:
        kwargs["ssl_certfile"] = cert
        kwargs["ssl_keyfile"] = key

    uvicorn.run("omnipacs_relay.web:app", **kwargs)


if __name__ == "__main__":
    main()

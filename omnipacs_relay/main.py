"""Entry point for OmniPACS Relay.

Boots logging and serves the FastAPI app on ``WEB_PORT``. If TLS cert
and key paths are set in the environment, uvicorn binds HTTPS directly;
otherwise the service binds plain HTTP and expects to be fronted by a
reverse proxy (Caddy / nginx) — Replit's dev preview already terminates
TLS via its built-in proxy.
"""

from __future__ import annotations

import logging

import uvicorn

from .config import TLS_CERT_PATH, TLS_KEY_PATH, WEB_HOST, WEB_PORT
from .log_bus import configure_logging


def main() -> None:
    configure_logging(level=logging.INFO)

    kwargs: dict = {
        "host": WEB_HOST,
        "port": WEB_PORT,
        "log_level": "info",
        "access_log": False,
    }
    if TLS_CERT_PATH and TLS_KEY_PATH:
        kwargs["ssl_certfile"] = TLS_CERT_PATH
        kwargs["ssl_keyfile"] = TLS_KEY_PATH

    uvicorn.run("omnipacs_relay.web:app", **kwargs)


if __name__ == "__main__":
    main()
